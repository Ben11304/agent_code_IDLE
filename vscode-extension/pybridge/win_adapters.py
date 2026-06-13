"""Windows ConPTY adapters — drop-in replacements for the main app's
`backend.adapters.claude_stream` / `grok_stream`, monkeypatched in by
`server.py` only when running on Windows.

Why this file exists (and lives OUTSIDE the main app):
  * The main app's adapters use a Unix PTY (`pty` + `termios`) to defeat Node
    block-buffering. Those modules do not exist on Windows, and a plain pipe
    block-buffers the whole response until the child exits (verified: a single
    delta arrives at the end — no streaming).
  * The fix on Windows is a real pseudo-console via ConPTY (pywinpty). ConPTY
    DOES stream incrementally (verified), but it VT-processes the stream:
    it injects ANSI escapes and line-wraps at the console width. We strip ANSI
    and run a very wide console (30000 cols) so JSON events survive intact.
  * npm installs `claude` as a `.cmd`/`.ps1` shim that CreateProcess cannot run
    with an arbitrary prompt arg safely (cmd.exe re-parses & | < > % "). So we
    resolve the shim to its real `node <cli.js>` invocation and exec node.

This keeps `app/` byte-for-byte unchanged; the Unix code path is never touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from typing import AsyncIterator

# CSI / OSC / lone-ESC / control chars that ConPTY interleaves with the JSON.
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC ... BEL/ST  (e.g. title)
    r"|\x1b[@-_][0-?]*[ -/]*[@-~]"            # CSI / other escape sequences
    r"|\x1b[@-_]"                             # lone two-char escapes
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"      # stray control chars
)

_JS_IN_SHIM_RE = re.compile(r'"%dp0%\\(.+?\.js)"')
_NODE_FALLBACKS = (
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
)
_CHILD_ENV = {"FORCE_COLOR": "0", "NO_COLOR": "1", "TERM": "dumb"}
_SENTINEL = object()
_PTY_COLS = 30000  # wide enough that stream-json lines rarely wrap


# --- shim -> node invocation resolution ------------------------------------

def _node_entry_from_cmd(cmd_shim: str) -> str | None:
    try:
        with open(cmd_shim, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _JS_IN_SHIM_RE.search(text)
    if not m:
        return None
    full = os.path.join(os.path.dirname(cmd_shim), m.group(1))
    return full if os.path.exists(full) else None


def _find_node(hint_dir: str) -> str | None:
    cand = os.path.join(hint_dir, "node.exe")
    if os.path.exists(cand):
        return cand
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    for path in _NODE_FALLBACKS:
        if os.path.exists(path):
            return path
    return None


def _resolve_program(name_or_path: str) -> list[str] | None:
    """Return the argv PREFIX that launches `name_or_path` directly.

    `.exe` -> [exe]; npm batch/ps1 shim -> [node, entry.js]; else None.
    """
    p = shutil.which(name_or_path)
    if not p:
        return None
    if p.lower().endswith(".exe"):
        return [p]
    shim_dir = os.path.dirname(p) or os.getcwd()
    base = os.path.splitext(os.path.basename(p))[0]
    cmd_shim = os.path.join(shim_dir, base + ".cmd")
    entry = _node_entry_from_cmd(cmd_shim)
    if entry:
        node = _find_node(shim_dir)
        if node:
            return [node, entry]
    return None


# --- ConPTY child ----------------------------------------------------------

class WinPtyChild:
    """Spawn `argv` under a ConPTY; expose stdout as an async line iterator.

    A background thread does the blocking `PtyProcess.read`, strips ANSI, splits
    on newlines, and hands complete lines to an asyncio.Queue on the loop thread.
    """

    def __init__(self, argv: list[str], cwd: str):
        self._argv = argv
        self._cwd = cwd
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._proc = None
        self._exitcode: int | None = None
        self._error: str | None = None

    async def start(self) -> bool:
        try:
            self._proc = await self._loop.run_in_executor(None, self._spawn)
        except Exception as exc:  # pywinpty missing / spawn failure
            self._error = f"ConPTY spawn failed: {exc}"
            return False
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def _spawn(self):
        from winpty import PtyProcess  # imported lazily so import errors surface here
        env = {**os.environ, **_CHILD_ENV}
        return PtyProcess.spawn(
            self._argv, cwd=self._cwd, env=env, dimensions=(50, _PTY_COLS)
        )

    def _read_loop(self):
        buf = ""
        try:
            while True:
                try:
                    data = self._proc.read(8192)
                except EOFError:
                    break
                except Exception:
                    break
                if not data:
                    if not self._proc.isalive():
                        break
                    continue
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._emit(line)
            if buf:
                self._emit(buf)
        finally:
            try:
                self._exitcode = self._proc.wait()
            except Exception:
                self._exitcode = None
            self._loop.call_soon_threadsafe(self._queue.put_nowait, _SENTINEL)

    def _emit(self, line: str):
        line = _ANSI.sub("", line).strip()
        if line:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line)

    async def lines(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            yield item

    @property
    def returncode(self):
        return self._exitcode

    async def aclose(self):
        if self._proc is not None:
            try:
                if self._proc.isalive():
                    self._proc.terminate(force=True)
            except Exception:
                pass


# --- argv builders (mirror the main app's flag construction) ----------------

def _claude_argv(model, effort, resume_session_id, system_prompt, message):
    prefix = _resolve_program("claude")
    if prefix is None:
        return None
    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-mode", "bypassPermissions",
            "--model", model]
    if effort:
        args += ["--effort", effort]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    args += [message]
    return prefix + args


def _grok_argv(model, effort, resume_session_id, best_of_n, check_loop, memory_mode,
               system_prompt, message):
    prefix = _resolve_program("grok")
    if prefix is None:
        return None
    args = ["--output-format", "streaming-json", "--no-alt-screen",
            "--permission-mode", "bypassPermissions", "--model", model]
    if effort:
        args += ["--effort", effort]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if best_of_n and best_of_n > 1:
        args += ["--best-of-n", str(int(best_of_n))]
    if check_loop:
        args += ["--check"]
    if memory_mode == "on":
        args += ["--experimental-memory"]
    elif memory_mode == "off":
        args += ["--no-memory"]
    if system_prompt:
        args += ["--system-prompt-override", system_prompt]
    args += ["-p", message]
    return prefix + args


# --- public adapters (same event contract as the Unix versions) -------------

async def claude_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "claude-sonnet-4-6",
    effort: str | None = None,
    resume_session_id: str | None = None,
) -> AsyncIterator[dict]:
    argv = _claude_argv(model, effort, resume_session_id, system_prompt, message)
    if argv is None:
        yield {"type": "error", "message": "claude CLI not found or node entry unresolved"}
        return

    child = WinPtyChild(argv, cwd)
    if not await child.start():
        yield {"type": "error", "message": child._error or "claude ConPTY start failed"}
        return

    assembled: list[str] = []
    claude_session_id: str | None = None

    try:
        async for line in child.lines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")
            if etype == "system" and evt.get("subtype") == "init":
                claude_session_id = evt.get("session_id")
                yield {"type": "meta", "data": {
                    "claude_session_id": claude_session_id,
                    "model": evt.get("model"),
                }}
            elif etype == "stream_event":
                ev = evt.get("event") or {}
                ev_type = ev.get("type")
                if ev_type == "content_block_start":
                    block = ev.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "thinking":
                        yield {"type": "status", "status": "thinking"}
                    elif btype == "text":
                        yield {"type": "status", "status": "responding"}
                elif ev_type == "content_block_delta":
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "thinking_delta":
                        chunk = delta.get("thinking") or ""
                        if chunk:
                            yield {"type": "thinking", "text": chunk}
                    elif dtype == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            assembled.append(text)
                            yield {"type": "delta", "text": text}
            elif etype == "assistant":
                msg = evt.get("message") or {}
                for block in msg.get("content", []):
                    if block.get("type") == "text" and not assembled:
                        text = block.get("text", "")
                        if text:
                            assembled.append(text)
                            yield {"type": "delta", "text": text}
            elif etype == "result":
                final = evt.get("result") or "".join(assembled)
                usage = evt.get("usage") or {}
                if usage:
                    yield {"type": "meta", "data": {"usage": usage}}
                yield {"type": "done", "text": final, "meta": {
                    "duration_ms": evt.get("duration_ms"),
                    "total_cost_usd": evt.get("total_cost_usd"),
                    "claude_session_id": claude_session_id,
                    "usage": usage,
                }}
                return
    finally:
        await child.aclose()

    if child.returncode and child.returncode != 0:
        yield {"type": "error", "message": f"claude exited {child.returncode}"}
        return
    yield {"type": "done", "text": "".join(assembled),
           "meta": {"claude_session_id": claude_session_id}}


async def grok_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "grok-build",
    effort: str | None = None,
    resume_session_id: str | None = None,
    best_of_n: int | None = None,
    check_loop: bool = False,
    memory_mode: str | None = None,
) -> AsyncIterator[dict]:
    argv = _grok_argv(model, effort, resume_session_id, best_of_n, check_loop,
                      memory_mode, system_prompt, message)
    if argv is None:
        yield {"type": "error", "message": "grok CLI not found or node entry unresolved"}
        return

    child = WinPtyChild(argv, cwd)
    if not await child.start():
        yield {"type": "error", "message": child._error or "grok ConPTY start failed"}
        return

    assembled: list[str] = []
    grok_session_id: str | None = None
    in_text = False

    try:
        async for line in child.lines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "thought":
                yield {"type": "thinking", "text": evt.get("data") or ""}
            elif etype == "text":
                if not in_text:
                    yield {"type": "status", "status": "responding"}
                    in_text = True
                t = evt.get("data") or ""
                if t:
                    assembled.append(t)
                    yield {"type": "delta", "text": t}
            elif etype == "end":
                grok_session_id = evt.get("sessionId")
                if grok_session_id:
                    yield {"type": "meta", "data": {"claude_session_id": grok_session_id}}
                yield {"type": "done", "text": "".join(assembled), "meta": {
                    "claude_session_id": grok_session_id,
                    "stop_reason": evt.get("stopReason"),
                }}
                return
    finally:
        await child.aclose()

    if child.returncode and child.returncode != 0:
        yield {"type": "error", "message": f"grok exited {child.returncode}"}
        return
    yield {"type": "done", "text": "".join(assembled),
           "meta": {"claude_session_id": grok_session_id}}
