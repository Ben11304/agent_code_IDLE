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
import random
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

    # The CLI's built-in subagent tool (`Agent`, formerly `Task`) MUST stay off.
    # Left enabled, an orchestrator spawns an in-process subagent instead of emitting
    # the <dispatch agent="..."> tag this control plane is built on. That subagent is
    # invisible to us: no dispatch_started/complete SSE (graph never lights up), no
    # dispatch_results ledger row (no provenance), and — worst — the REAL worker agent
    # never runs, so none of its AGENT.md (role, research-integrity rules, manifest
    # version discipline) applies to the work done in its name. Observed 2026-07-12:
    # aecbench/BOSS on claude-sonnet-5 reported "RESEARCHER manifest 0.30.0 synced"
    # across 7 turns while the real RESEARCHER session had been idle for 15 hours and
    # the ledger recorded zero dispatches. Disabling the tool removes the shortcut, so
    # the model has to use <dispatch> — which restores the core invariant: if the graph
    # does not light up, the dispatch did not happen.
    #
    # Placement matters: --disallowed-tools is VARIADIC, so it must never sit directly
    # before the positional prompt or it swallows the message as a tool name. Keep it
    # ahead of --model (always present), which terminates the variadic.
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--permission-mode", "bypassPermissions",
           "--disallowed-tools", "Agent", "Task",
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
    # tool_use blocks arrive split across three stream events:
    # content_block_start (name + empty input) → content_block_delta
    # (input_json_delta partial_json chunks) → content_block_stop (input done).
    # Track them per block index so we can yield one tool_use event with the
    # fully-assembled input (file_path / command / pattern ...) when the block
    # closes — the UI renders a "files / commands accessed" bubble from these.
    tool_inputs: dict[int, dict] = {}

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
                # The CLI declares its whole loaded surface here — tools, skills,
                # plugins, subagents, MCP servers. This is the ONLY accurate source:
                # it is what the CLI actually resolved for THIS cwd + settings, not a
                # guess parsed from config files. Forward it as-is.
                # NOTE: hooks (e.g. the rtk command-rewriter) are NOT in this payload —
                # the CLI does not report them, so they cannot be surfaced from init.
                mcp = [
                    {"name": s.get("name"), "status": s.get("status")}
                    for s in (evt.get("mcp_servers") or []) if isinstance(s, dict)
                ]
                plugins = [
                    {"name": p.get("name"), "source": p.get("source")}
                    for p in (evt.get("plugins") or []) if isinstance(p, dict)
                ]
                yield {
                    "type": "meta",
                    "data": {
                        "claude_session_id": claude_session_id,
                        "model": evt.get("model"),
                        "cwd": evt.get("cwd"),
                        "tools": evt.get("tools") or [],
                        "skills": evt.get("skills") or [],
                        "plugins": plugins,
                        "subagents": evt.get("agents") or [],
                        "mcp_servers": mcp,
                        "slash_commands": evt.get("slash_commands") or [],
                        "permission_mode": evt.get("permissionMode"),
                        "output_style": evt.get("output_style"),
                        "cli_version": evt.get("claude_code_version"),
                        "init": True,
                    },
                }
            elif etype == "stream_event":
                ev = evt.get("event") or {}
                ev_type = ev.get("type")
                idx = ev.get("index")
                if ev_type == "content_block_start":
                    block = ev.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "thinking":
                        yield {"type": "status", "status": "thinking"}
                    elif btype == "text":
                        yield {"type": "status", "status": "responding"}
                    elif btype == "tool_use" and idx is not None:
                        # Start accumulating the tool's input JSON (streams in
                        # via input_json_delta partial_json chunks).
                        tool_inputs[idx] = {"name": block.get("name") or "?", "json": ""}
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
                    elif dtype == "input_json_delta" and idx in tool_inputs:
                        tool_inputs[idx]["json"] += delta.get("partial_json") or ""
                elif ev_type == "content_block_stop" and idx in tool_inputs:
                    # Tool input is complete — parse it and surface one event so
                    # the UI can show which file/command the agent accessed.
                    info = tool_inputs.pop(idx)
                    parsed: dict = {}
                    try:
                        parsed = json.loads(info["json"] or "{}")
                    except json.JSONDecodeError:
                        parsed = {}
                    yield {"type": "tool_use", "tool": info["name"], "input": parsed}
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

# ---------------------------------------------------------------------------
# Overload retry for the API-key adapters (deepseek, glm).
#
# api.z.ai (and api.deepseek.com) return HTTP 529 "overloaded" (Z.ai code 1305)
# when the ACCOUNT'S CONCURRENCY LIMIT is exceeded — NOT a global outage. A
# single sequential request always succeeds; only concurrent bursts trip it
# (reproduced: 6 concurrent glm-5.2 requests → 3×529). The AEC system dispatches
# up to 5 agents at once, so this fires regularly under multi-agent load.
#
# The 529 is transient — a slot frees the moment another agent's request lands.
# So re-running the same `claude -p` turn after a short backoff almost always
# succeeds. This wrapper inspects the terminal event of each attempt; if it
# carries an overload signature, it backs off and retries the whole turn,
# preserving live streaming for the eventual success. Live deltas from a
# successful attempt are yielded as they arrive; only the terminal event is
# held back until we know it is not a retryable error.
# ---------------------------------------------------------------------------

# substrings (lower-cased) that mark a terminal event as a retryable overload
_OVERLOAD_PATTERNS = (
    "529", "1305", "overloaded", "temporarily overloaded",
    "429", "rate limit", "rate_limit", "too many requests",
    "service may be temporarily",
)
# backoff seconds before each retry (index 0 → before retry #1)
_OVERLOAD_BACKOFF = (5.0, 12.0, 25.0)


def _event_is_overload(ev: dict) -> bool:
    """True if a terminal event (agent_done / error) carries an overload
    signature in its text/message (529 / 429 / overloaded)."""
    if not isinstance(ev, dict):
        return False
    txt = ev.get("text") or ev.get("message") or ""
    if not txt:
        return False
    low = txt.lower()
    return any(p in low for p in _OVERLOAD_PATTERNS)


async def _claude_stream_with_overload_retry(
    *,
    message: str,
    system_prompt: str,
    cwd: str,
    model: str,
    effort: str | None,
    resume_session_id: str | None,
    extra_env: dict | None,
    label: str = "model",
) -> AsyncIterator[dict]:
    """Run claude_stream, retrying the whole turn when the terminal event is an
    overload error (529/429). Live events stream through; only the terminal
    agent_done/error is buffered per attempt so an overload can be swallowed and
    the turn re-attempted without the UI seeing a dead error bubble."""
    max_retries = len(_OVERLOAD_BACKOFF)
    agent_id = None
    for attempt in range(max_retries + 1):
        terminal = None
        async for ev in claude_stream(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=model,
            effort=effort,
            resume_session_id=resume_session_id,
            extra_env=extra_env,
        ):
            if agent_id is None and isinstance(ev, dict) and ev.get("agent"):
                agent_id = ev.get("agent")
            if isinstance(ev, dict) and ev.get("type") in ("agent_done", "error"):
                terminal = ev  # hold back until we know it's not overload
                continue
            yield ev
        if terminal is None:
            return  # stream ended without a terminal event — nothing to retry
        if not _event_is_overload(terminal) or attempt >= max_retries:
            yield terminal
            return
        # overload → backoff + retry
        delay = _OVERLOAD_BACKOFF[attempt] + random.uniform(0.0, 2.0)
        yield {"type": "thinking", "agent": agent_id,
               "text": f"⚠ {label} gateway overloaded (529) — retry {attempt + 1}/{max_retries} in {delay:.0f}s" }
        await asyncio.sleep(delay)


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
    async for ev in _claude_stream_with_overload_retry(
        message=message,
        system_prompt=system_prompt,
        cwd=cwd,
        model=model,
        effort=effort,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
        label="deepseek",
    ):
        yield ev


# ---------------------------------------------------------------------------
# GLM adapter (Zhipu) — identical strategy to DeepSeek: drive the SAME `claude -p`
# harness, pointed at GLM's NATIVE Anthropic-compatible endpoint. Z.ai / Zhipu
# officially integrate GLM with Claude Code (the CLI speaks the Anthropic Messages
# API directly), so a GLM node inherits the full agent harness for free — file
# tools, permission mode, --resume memory, thinking, --effort, dispatch parsing,
# PTY streaming — with NO translation proxy in between.
#
# Default endpoint is Z.ai (international). Inside China, set GLM_BASE_URL to
# https://open.bigmodel.cn/api/anthropic. Override is injected via extra_env so it
# applies ONLY to this subprocess — Claude nodes + the user's terminal `claude`
# keep using subscription OAuth, fully unaffected. Never set ANTHROPIC_BASE_URL
# globally. GLM is billed per-token (API), unlike the Claude subscription.
# ---------------------------------------------------------------------------

def _load_glm_env_file() -> dict:
    """Parse ~/.config/glm/env — the SAME config file the user's `glm` CLI
    wrapper (~/bin/glm) sources. By reading the token from here, the UI's GLM
    nodes reuse the ONE token already granted to the terminal, instead of a
    second key in VietHuy/.env. File-based (not env-var based), so it does NOT
    depend on the uvicorn worker process having GLM_API_KEY exported — which
    fixes the 'GLM_API_KEY not set' failure when the worker doesn't inherit
    run.sh's env. Same key/endpoint/model the user gets from `glm -p`."""
    path = os.path.expanduser("~/.config/glm/env")
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                if v:
                    out[k.strip()] = v
    except OSError:
        pass
    return out


async def glm_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "glm-4.6",
    effort: str | None = None,
    resume_session_id: str | None = None,
) -> AsyncIterator[dict]:
    # Token source = the user's ~/.config/glm/env (the `glm` wrapper config),
    # falling back to GLM_API_KEY env var if the file is absent. ONE token,
    # same as the terminal's `glm -p`.
    cfg = _load_glm_env_file()
    key = cfg.get("GLM_API_KEY") or os.environ.get("GLM_API_KEY")
    if not key:
        yield {"type": "error",
               "message": "GLM key not found. Put GLM_API_KEY in ~/.config/glm/env (the `glm` wrapper config) or export GLM_API_KEY."}
        return
    base_url = (cfg.get("GLM_BASE_URL") or os.environ.get("GLM_BASE_URL")
                or "https://api.z.ai/api/anthropic")
    extra_env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_AUTH_TOKEN": key,
        # per-node model selection (glm-4.6 / glm-5.2) wins over the wrapper's
        # default GLM_MODEL, so /adapter and project.yaml still control it.
        "ANTHROPIC_MODEL": model,
    }
    async for ev in _claude_stream_with_overload_retry(
        message=message,
        system_prompt=system_prompt,
        cwd=cwd,
        model=model,
        effort=effort,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
        label="glm",
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
    if model == "glm":
        return glm_stream
    raise AdapterError(f"unknown model adapter: {model}")
