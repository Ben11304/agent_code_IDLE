"""Adapters wrap subscription-backed CLIs (claude, aas) so the UI never needs an API key.

Each adapter is an async generator that yields events:
    {"type": "delta",  "text": "..."}      partial text
    {"type": "meta",   "data": {...}}      side info (claude_session_id, model, ...)
    {"type": "error",  "message": "..."}   adapter-level failure
    {"type": "done",   "text": "..."}      final assembled text
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import shutil
import termios
from typing import AsyncIterator


class AdapterError(Exception):
    pass


# ---------------------------------------------------------------------------
# Claude adapter — wraps `claude -p` (Claude Code CLI, subscription auth).
# ---------------------------------------------------------------------------

async def claude_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "claude-sonnet-4-6",
    effort: str | None = None,
    resume_session_id: str | None = None,
) -> AsyncIterator[dict]:
    if shutil.which("claude") is None:
        yield {"type": "error", "message": "claude CLI not found on PATH"}
        return

    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--permission-mode", "bypassPermissions",
           "--model", model]
    if effort:
        cmd += ["--effort", effort]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    cmd += [message]

    # PTY trick: Node CLIs (claude is Node) block-buffer stdout when piped.
    # Routing stdout through a pty makes claude think it's a terminal so it
    # line-buffers each JSON event, which is what we need for real streaming.
    master_fd, slave_fd = pty.openpty()
    # Raw mode on slave: no \n -> \r\n translation, no echo, no canonicalization.
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[1] &= ~termios.OPOST  # no output post-processing
        attrs[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=slave_fd,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1", "TERM": "dumb"},
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=2 ** 20)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(
        lambda: protocol, os.fdopen(master_fd, "rb", buffering=0)
    )

    assembled: list[str] = []
    claude_session_id: str | None = None

    try:
        async for raw_line in reader:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")
            if etype == "system" and evt.get("subtype") == "init":
                claude_session_id = evt.get("session_id")
                yield {
                    "type": "meta",
                    "data": {
                        "claude_session_id": claude_session_id,
                        "model": evt.get("model"),
                    },
                }
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
                yield {"type": "done", "text": final, "meta": {
                    "duration_ms": evt.get("duration_ms"),
                    "total_cost_usd": evt.get("total_cost_usd"),
                    "claude_session_id": claude_session_id,
                }}
                return
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:
            pass
        try:
            transport.close()
        except Exception:
            pass

    if proc.returncode and proc.returncode != 0:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
        yield {"type": "error", "message": f"claude exited {proc.returncode}: {stderr[:500]}"}
        return

    # If we reached EOF without a "result" event, emit assembled as done.
    yield {"type": "done", "text": "".join(assembled), "meta": {"claude_session_id": claude_session_id}}


# ---------------------------------------------------------------------------
# Grok adapter — wraps user's `aas` CLI which already uses Grok subscription.
# ---------------------------------------------------------------------------

async def grok_stream(
    message: str,
    system_prompt: str,
    cwd: str,
) -> AsyncIterator[dict]:
    if shutil.which("aas") is None:
        yield {"type": "error", "message": "aas CLI not found on PATH"}
        return

    prompt = message
    if system_prompt:
        prompt = f"[ROLE]\n{system_prompt.strip()}\n\n[REQUEST]\n{message}"

    cmd = ["aas", "ask", prompt]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ},
    )
    assert proc.stdout is not None

    chunks: list[str] = []
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            if not line:
                continue
            chunks.append(line)
            yield {"type": "delta", "text": line}
    finally:
        await proc.wait()

    if proc.returncode and proc.returncode != 0:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
        yield {"type": "error", "message": f"aas exited {proc.returncode}: {stderr[:500]}"}
        return

    yield {"type": "done", "text": "".join(chunks), "meta": {}}


# ---------------------------------------------------------------------------

def get_stream(model: str):
    if model == "claude":
        return claude_stream
    if model == "grok":
        return grok_stream
    raise AdapterError(f"unknown model adapter: {model}")
