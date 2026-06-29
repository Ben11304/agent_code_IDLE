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


# StreamReader line-buffer cap for the stream-json reader. claude/grok emit one
# JSON event per line; a single event can be large when the model writes a whole
# file in one tool block (e.g. a LaTeX manuscript or two big TikZ figures). The
# old 1 MiB cap made readline() drop the line AND raise ValueError ("Separator is
# not found, and chunk exceed the limit") — which crashed the whole turn. We raise
# the cap and, in the read loop, recover from an oversized line instead of raising.
_READER_LIMIT = 64 * 2 ** 20  # 64 MiB


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
    extra_env: dict | None = None,
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
        # extra_env (per-subprocess only — never mutate the global os.environ) is
        # how the DeepSeek adapter points THIS claude -p at a local Anthropic-
        # compatible proxy without touching the user's normal Claude CLI / OAuth.
        env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1", "TERM": "dumb",
             **(extra_env or {})},
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=_READER_LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(
        lambda: protocol, os.fdopen(master_fd, "rb", buffering=0)
    )

    assembled: list[str] = []
    claude_session_id: str | None = None

    try:
        while True:
            try:
                raw_line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # A single stream-json event exceeded the reader buffer (e.g. a
                # whole file written in one tool block). readline() has already
                # dropped the oversized line; skip it and keep streaming instead
                # of letting the exception crash the entire turn.
                yield {"type": "status", "status": "responding"}
                continue
            if not raw_line:
                break  # EOF
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
                # Real token usage for this turn. For a resumed session the bulk
                # of the prompt lands in cache_read_input_tokens, so the context
                # window occupancy is the SUM of all input buckets + output.
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
    model: str = "grok-build",
    effort: str | None = None,
    resume_session_id: str | None = None,
    best_of_n: int | None = None,
    check_loop: bool = False,
    memory_mode: str | None = None,
) -> AsyncIterator[dict]:
    grok_bin = shutil.which("grok")
    if not grok_bin:
        candidate = os.path.expanduser("~/.grok/bin/grok")
        if os.path.exists(candidate):
            grok_bin = candidate
        else:
            yield {"type": "error", "message": "grok CLI not found on PATH or in ~/.grok/bin"}
            return

    cmd = [
        grok_bin,
        "--output-format", "streaming-json",
        "--no-alt-screen",
        "--permission-mode", "bypassPermissions",
        "--model", model,
    ]
    if effort:
        cmd += ["--effort", effort]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if best_of_n and best_of_n > 1:
        cmd += ["--best-of-n", str(int(best_of_n))]
    if check_loop:
        cmd += ["--check"]
    if memory_mode == "on":
        cmd += ["--experimental-memory"]
    elif memory_mode == "off":
        cmd += ["--no-memory"]
    if system_prompt:
        cmd += ["--system-prompt-override", system_prompt]
    # `-p / --single` takes the prompt as its value; put it last so all flags parse cleanly.
    cmd += ["-p", message]

    # PTY trick: same reason as claude_stream — defeat Node/Rust CLI block buffering.
    master_fd, slave_fd = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[1] &= ~termios.OPOST
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
    reader = asyncio.StreamReader(limit=_READER_LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(
        lambda: protocol, os.fdopen(master_fd, "rb", buffering=0)
    )

    assembled: list[str] = []
    grok_session_id: str | None = None
    in_text = False

    try:
        while True:
            try:
                raw_line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # A single stream-json event exceeded the reader buffer (e.g. a
                # whole file written in one tool block). readline() has already
                # dropped the oversized line; skip it and keep streaming instead
                # of letting the exception crash the entire turn.
                yield {"type": "status", "status": "responding"}
                continue
            if not raw_line:
                break  # EOF
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
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
                # main.py persists session id via meta event using key claude_session_id
                # (shared db column for both adapters)
                if grok_session_id:
                    yield {"type": "meta", "data": {"claude_session_id": grok_session_id}}
                final = "".join(assembled)
                yield {"type": "done", "text": final, "meta": {
                    "claude_session_id": grok_session_id,
                    "stop_reason": evt.get("stopReason"),
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
        yield {"type": "error", "message": f"grok exited {proc.returncode}: {stderr[:500]}"}
        return

    yield {"type": "done", "text": "".join(assembled), "meta": {"claude_session_id": grok_session_id}}


# ---------------------------------------------------------------------------
# DeepSeek adapter — drives the SAME `claude -p` harness, pointed at DeepSeek's
# NATIVE Anthropic-compatible endpoint (https://api.deepseek.com/anthropic).
# DeepSeek V4 is officially integrated with Claude Code: it speaks the Anthropic
# Messages API directly, supports 1M context, thinking mode, function calling,
# and auto-sets reasoning effort to "max" for Claude-Code-style agent requests.
# So a DeepSeek node inherits the full agent harness for free — file tools,
# permission mode, --resume memory, thinking, --effort, dispatch parsing, PTY
# streaming — with NO translation proxy in between.
#
# The override is injected via extra_env so it applies ONLY to this subprocess.
# The user's normal `claude` CLI and the Claude nodes are untouched — they keep
# using subscription OAuth. Do NOT set ANTHROPIC_BASE_URL globally anywhere.
#
# DEEPSEEK_BASE_URL can override the endpoint (e.g. to route via a local proxy
# instead), but the default needs no extra process running.
# ---------------------------------------------------------------------------

async def deepseek_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "deepseek-v4-flash",
    effort: str | None = None,
    resume_session_id: str | None = None,
) -> AsyncIterator[dict]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        yield {"type": "error",
               "message": "DEEPSEEK_API_KEY not set in the environment"}
        return
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    extra_env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_AUTH_TOKEN": key,
    }
    async for ev in claude_stream(
        message=message,
        system_prompt=system_prompt,
        cwd=cwd,
        model=model,
        effort=effort,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
    ):
        yield ev


# ---------------------------------------------------------------------------

def get_stream(model: str):
    if model == "claude":
        return claude_stream
    if model == "grok":
        return grok_stream
    if model == "deepseek":
        return deepseek_stream
    raise AdapterError(f"unknown model adapter: {model}")
