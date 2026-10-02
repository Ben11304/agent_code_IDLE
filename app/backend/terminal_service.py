"""Persistent terminal sessions backed by a dedicated tmux server.

The browser and uvicorn only own short-lived tmux *clients*.  The shell and its
foreground process live in tmux, so closing a pane, disconnecting the browser,
or restarting AgentUI detaches the client without terminating the session.
Sessions have no time-based expiry; users remove them explicitly through the
DELETE endpoint.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import time
import uuid

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect


_TMUX_SOCKET = os.environ.get("AGENTUI_TMUX_SOCKET", "agentui")
_SESSION_RE = re.compile(r"^[0-9a-f]{12}$")
_MAX_ATTACHMENTS = 6
_ATTACH_IDLE_S = 1800
_connections: dict[str, dict] = {}


def _terminal_env() -> dict[str, str]:
    """Return a clean login-shell environment without AgentUI's own venv."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    env.pop("PS1", None)
    # A tmux client launched from inside another tmux must still attach to the
    # dedicated AgentUI socket rather than being rejected as a nested client.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    if venv:
        env["PATH"] = ":".join(
            part for part in env.get("PATH", "").split(":")
            if part and not part.startswith(venv)
        )
    env["TERM"] = "xterm-256color"
    return env


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["tmux", "-L", _TMUX_SOCKET, *args],
            env=_terminal_env(),
            text=True,
            capture_output=True,
            check=check,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("tmux is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tmux command timed out") from exc


def _valid_session_id(session_id: str) -> str:
    value = str(session_id or "").strip().lower()
    if not _SESSION_RE.fullmatch(value):
        raise ValueError("invalid terminal session id")
    return value


def _session_exists_sync(session_id: str) -> bool:
    session_id = _valid_session_id(session_id)
    return _tmux("has-session", "-t", session_id).returncode == 0


def _configure_session_sync(session_id: str, title: str | None = None) -> None:
    """Apply AgentUI terminal behavior to new and previously-created sessions."""
    session_id = _valid_session_id(session_id)
    options = [
        ("set-option", "status", "off"),
        # With tmux mouse mode off, xterm translates the wheel into Up/Down
        # keypresses while tmux owns the alternate screen.  Mouse mode makes
        # WheelUp enter copy-mode instead, so users can inspect scrollback.
        ("set-option", "mouse", "on"),
        ("set-window-option", "history-limit", "50000"),
        ("set-window-option", "window-size", "latest"),
    ]
    if title is not None:
        options.append(("set-option", "@agentui_title", title))
    for command, name, value in options:
        result = _tmux(command, "-t", session_id, name, value)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"unable to configure tmux option {name}"
            )


def _configure_server_sync() -> None:
    """Tune dedicated-server mouse wheel bindings for fine-grained scrolling."""
    for table in ("copy-mode", "copy-mode-vi"):
        for wheel, action in (
            ("WheelUpPane", "scroll-up"),
            ("WheelDownPane", "scroll-down"),
        ):
            result = _tmux(
                "bind-key", "-T", table, wheel,
                "send-keys", "-X", "-N", "1", action,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or f"unable to configure tmux {wheel}"
                )


def _create_session_sync(title: str | None = None) -> dict:
    session_id = uuid.uuid4().hex[:12]
    label = (title or f"terminal {session_id[:6]}").strip()[:80]
    home = os.path.expanduser("~")
    # history-limit is copied into a pane when that pane is created; setting it
    # afterward changes the session option but leaves the existing pane at the
    # old capacity. Run both global defaults and new-session in one tmux command
    # queue so even the dedicated server's very first pane gets full scrollback.
    created = _tmux(
        "set-option", "-g", "history-limit", "50000", ";",
        "set-option", "-g", "mouse", "on", ";",
        "new-session", "-d", "-s", session_id, "-c", home,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "unable to create tmux session")
    # These are session/window presentation settings, not lifetime limits.
    try:
        _configure_server_sync()
        _configure_session_sync(session_id, label)
    except Exception:
        _tmux("kill-session", "-t", session_id)
        raise
    return {"id": session_id, "title": label}


def _list_sessions_sync() -> list[dict]:
    separator = "\x1f"
    fmt = separator.join([
        "#{session_name}", "#{session_created}", "#{session_activity}",
        "#{session_attached}", "#{pane_current_command}", "#{pane_current_path}",
        "#{pane_pid}", "#{@agentui_title}",
    ])
    result = _tmux("list-sessions", "-F", fmt)
    if result.returncode != 0:
        # tmux returns 1 when its dedicated server has no sessions.
        return []
    sessions: list[dict] = []
    for line in result.stdout.splitlines():
        fields = line.split(separator)
        if len(fields) != 8 or not _SESSION_RE.fullmatch(fields[0]):
            continue
        session_id, created, activity, attached, command, cwd, pane_pid, title = fields
        try:
            created_at = int(created)
        except ValueError:
            created_at = 0
        try:
            activity_at = int(activity)
        except ValueError:
            activity_at = created_at
        try:
            attached_count = int(attached)
        except ValueError:
            attached_count = 0
        try:
            process_pid = int(pane_pid)
        except ValueError:
            process_pid = None
        sessions.append({
            "id": session_id,
            "title": title or f"terminal {session_id[:6]}",
            "command": command or "shell",
            "cwd": cwd or "",
            "process_pid": process_pid,
            "created_at": created_at,
            "activity_at": activity_at,
            "attached_count": attached_count,
            "attached": attached_count > 0,
        })
    sessions.sort(key=lambda item: (item["activity_at"], item["created_at"]), reverse=True)
    return sessions


def _configure_existing_sessions_sync() -> int:
    """Upgrade persistent sessions after an AgentUI backend restart."""
    sessions = _list_sessions_sync()
    if sessions:
        _configure_server_sync()
    configured = 0
    for session in sessions:
        try:
            _configure_session_sync(session["id"])
            configured += 1
        except RuntimeError:
            # One stale/broken session must not prevent the UI from starting.
            continue
    return configured


def _kill_session_sync(session_id: str) -> bool:
    session_id = _valid_session_id(session_id)
    result = _tmux("kill-session", "-t", session_id)
    if result.returncode == 0:
        return True
    if "can't find session" in result.stderr.lower() or "no server running" in result.stderr.lower():
        return False
    raise RuntimeError(result.stderr.strip() or "unable to kill tmux session")


def _detach_connection(connection_id: str) -> None:
    """Kill only the tmux attach client; the tmux session keeps running."""
    connection = _connections.pop(connection_id, None)
    if not connection:
        return
    try:
        os.kill(connection["pid"], signal.SIGHUP)
    except Exception:
        pass
    try:
        os.kill(connection["pid"], signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(connection["fd"])
    except Exception:
        pass
    try:
        os.waitpid(connection["pid"], os.WNOHANG)
    except Exception:
        pass


def register_terminal_routes(app: FastAPI) -> None:
    @app.get("/api/terminal/sessions")
    async def api_terminal_sessions():
        try:
            sessions = await asyncio.to_thread(_list_sessions_sync)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"sessions": sessions, "count": len(sessions), "persistent": True}

    @app.delete("/api/terminal/sessions/{session_id}")
    async def api_kill_terminal_session(session_id: str):
        try:
            normalized = _valid_session_id(session_id)
            killed = await asyncio.to_thread(_kill_session_sync, normalized)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        # Closing attached clients after tmux dies makes their WebSockets settle
        # immediately instead of waiting for the PTY reader to observe EOF.
        for connection_id, connection in list(_connections.items()):
            if connection.get("session_id") == normalized:
                _detach_connection(connection_id)
        if not killed:
            raise HTTPException(404, "terminal session not found")
        return {"ok": True, "id": normalized}

    @app.websocket("/api/terminal/ws")
    async def api_terminal_ws(ws: WebSocket):
        """Create or attach a persistent tmux session through a transient PTY client."""
        await ws.accept()
        if len(_connections) >= _MAX_ATTACHMENTS:
            await ws.send_text(json.dumps({
                "t": "error",
                "message": "terminal view limit reached — hide another pane first",
            }))
            await ws.close(code=1013)
            return

        requested = str(ws.query_params.get("session_id") or "").strip().lower()
        try:
            if requested:
                session_id = _valid_session_id(requested)
                exists = await asyncio.to_thread(_session_exists_sync, session_id)
                if not exists:
                    await ws.send_text(json.dumps({
                        "t": "error", "message": "terminal session no longer exists",
                    }))
                    await ws.close(code=1008)
                    return
                # Also upgrades sessions created by older AgentUI versions, in
                # particular enabling mouse-driven tmux copy-mode scrollback.
                await asyncio.to_thread(_configure_session_sync, session_id)
                session = next(
                    (item for item in await asyncio.to_thread(_list_sessions_sync)
                     if item["id"] == session_id),
                    {"id": session_id, "title": f"terminal {session_id[:6]}"},
                )
            else:
                session = await asyncio.to_thread(_create_session_sync)
                session_id = session["id"]
        except (ValueError, RuntimeError) as exc:
            await ws.send_text(json.dumps({"t": "error", "message": str(exc)}))
            await ws.close(code=1011)
            return

        await ws.send_text(json.dumps({
            "t": "meta", "session_id": session_id,
            "title": session.get("title") or f"terminal {session_id[:6]}",
            "persistent": True,
        }))

        connection_id = uuid.uuid4().hex[:12]
        child_env = _terminal_env()
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.clear()
            os.environ.update(child_env)
            os.chdir(os.path.expanduser("~"))
            try:
                os.execvp(
                    "tmux",
                    ["tmux", "-L", _TMUX_SOCKET, "attach-session", "-t", session_id],
                )
            except Exception:
                os._exit(127)

        _connections[connection_id] = {
            "pid": pid, "fd": fd, "session_id": session_id, "last_io": time.time(),
        }
        loop = asyncio.get_running_loop()

        async def pump_out():
            while True:
                try:
                    data = await loop.run_in_executor(None, os.read, fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                connection = _connections.get(connection_id)
                if connection:
                    connection["last_io"] = time.time()
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break
            try:
                await ws.close()
            except Exception:
                pass

        out_task = asyncio.create_task(pump_out())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    message = json.loads(raw)
                except Exception:
                    continue
                kind = message.get("t")
                if kind == "i":
                    try:
                        os.write(fd, (message.get("d") or "").encode("utf-8"))
                        connection = _connections.get(connection_id)
                        if connection:
                            connection["last_io"] = time.time()
                    except OSError:
                        break
                elif kind == "r":
                    try:
                        cols = max(2, int(message.get("cols") or 80))
                        rows = max(2, int(message.get("rows") or 24))
                        fcntl.ioctl(
                            fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0),
                        )
                    except Exception:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            out_task.cancel()
            _detach_connection(connection_id)

    async def attachment_reaper():
        # This only detaches abandoned WebSocket clients. It never kills tmux
        # sessions, which intentionally have no expiry.
        while True:
            await asyncio.sleep(120)
            now = time.time()
            for connection_id, connection in list(_connections.items()):
                if now - connection.get("last_io", now) > _ATTACH_IDLE_S:
                    _detach_connection(connection_id)

    @app.on_event("startup")
    async def start_terminal_attachment_reaper():
        try:
            await asyncio.to_thread(_configure_existing_sessions_sync)
        except RuntimeError:
            # Keep the rest of AgentUI available when tmux is not installed.
            pass
        asyncio.create_task(attachment_reaper())

    @app.on_event("shutdown")
    async def detach_all_terminal_clients():
        # Deliberately preserve the dedicated tmux server and every session.
        for connection_id in list(_connections):
            _detach_connection(connection_id)
