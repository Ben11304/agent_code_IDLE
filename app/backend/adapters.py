"""Adapters wrap subscription-backed runtimes so the UI never needs an API key.

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
import re
import shutil
import sys
import termios
from pathlib import Path
from typing import AsyncIterator

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    Sandbox,
    SkillInput,
    TextInput,
)
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    ErrorNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    McpToolCallThreadItem,
    ReasoningEffort,
    ReasoningSummaryTextDeltaNotification,
    SkillsExtraRootsSetResponse,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStartedNotification,
    TurnStatus,
    WebSearchThreadItem,
)

from .capabilities import (
    codex_config_overrides,
    enabled_inventory_skills,
    inherited_capability_config,
    plugin_extra_roots,
)


class AdapterError(Exception):
    pass


# StreamReader line-buffer cap for the stream-json reader. claude/grok emit one
# JSON event per line; a single event can be large when the model writes a whole
# file in one tool block (e.g. a LaTeX manuscript or two big TikZ figures). The
# old 1 MiB cap made readline() drop the line AND raise ValueError ("Separator is
# not found, and chunk exceed the limit") — which crashed the whole turn. We raise
# the cap and, in the read loop, recover from an oversized line instead of raising.
_READER_LIMIT = 64 * 2 ** 20  # 64 MiB

# AgentUI-owned, destination-bound Notion MCP. The stdio server has no external
# runtime dependencies, so it uses the same interpreter as the backend. Raw
# Notion MCP writes are disabled only for scoped AgentUI subprocesses; the
# user's normal terminal configuration remains untouched.
_NOTION_REPORT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "capability_inventory" / "mcp" / "agentui_notion_report"
    / "implementation" / "notion_report"
)
_NOTION_REPORT_PYTHON = Path(sys.executable)
_NOTION_REPORT_RUNNER = _NOTION_REPORT_ROOT / "run_mcp.py"
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _agentui_notion_scoped(extra_env: dict | None) -> bool:
    if os.environ.get("AGENTUI_EVALUATION_MODE", "").lower() in {"1", "true"}:
        return False
    env = extra_env or {}
    return bool(
        _NOTION_REPORT_RUNNER.is_file()
        and env.get("AGENTUI_PROJECT_SLUG")
        and env.get("AGENTUI_AGENT_ID")
    )


def _codex_notion_env_vars(extra_env: dict | None) -> list[str]:
    """Names Codex may forward to the destination-bound stdio MCP.

    Codex intentionally does not pass arbitrary parent variables to stdio MCP
    processes.  Its ``env_vars`` setting is an allow-list, so keep values out of
    CLI arguments and forward only the scoped identity/config plus the token
    variable name selected by the verified destination registry.
    """
    env = extra_env or {}
    names = [
        "AGENTUI_PROJECT_SLUG",
        "AGENTUI_AGENT_ID",
        "AGENTUI_PROJECT_NAME",
        "AGENTUI_PROJECT_ROOT_AGENT_ID",
        "NOTION_REPORT_CONFIG",
    ]
    token_env = str(env.get("AGENTUI_NOTION_TOKEN_ENV") or "").strip()
    if token_env and _ENV_NAME_RE.fullmatch(token_env):
        names.append(token_env)
    return names


def _notion_mcp_args(extra_env: dict | None) -> list[str]:
    """Build a secret-free, immutable scope for the MCP launcher."""
    env = extra_env or {}
    args = [str(_NOTION_REPORT_RUNNER)]
    project_slug = str(env.get("AGENTUI_PROJECT_SLUG") or "").strip()
    agent_id = str(env.get("AGENTUI_AGENT_ID") or "").strip()
    project_name = str(env.get("AGENTUI_PROJECT_NAME") or "").strip()
    project_root_agent = str(env.get("AGENTUI_PROJECT_ROOT_AGENT_ID") or "").strip()
    project_directory = str(env.get("AGENTUI_PROJECT_ROOT") or "").strip()
    report_schema_file = str(env.get("AGENTUI_REPORT_SCHEMA_FILE") or "").strip()
    config_path = str(env.get("NOTION_REPORT_CONFIG") or "").strip()
    if project_slug and agent_id and config_path:
        args.extend([
            "--project", project_slug,
            "--agent", agent_id,
            "--config", config_path,
        ])
        if project_name:
            args.extend(["--project-name", project_name])
        if project_root_agent:
            args.extend(["--project-root-agent", project_root_agent])
        if project_directory and report_schema_file:
            args.extend([
                "--project-directory", project_directory,
                "--report-schema-file", report_schema_file,
            ])
    return args


def _claude_notion_mcp_config(extra_env: dict | None = None) -> str:
    return json.dumps({
        "mcpServers": {
            "agentui_notion_report": {
                "type": "stdio",
                "command": str(_NOTION_REPORT_PYTHON),
                "args": _notion_mcp_args(extra_env),
            }
        }
    })


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
    disallowed_tools = ["Agent", "Task"]
    notion_scoped = _agentui_notion_scoped(extra_env)
    if notion_scoped:
        # The official Notion MCP may still exist in ~/.claude.json. Blocking its
        # namespace removes the bypass while preserving the user's global config.
        disallowed_tools.append("mcp__notion__*")
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--permission-mode", "bypassPermissions",
           "--disallowed-tools", *disallowed_tools]
    if notion_scoped:
        cmd += ["--mcp-config", _claude_notion_mcp_config(extra_env)]
    cmd += ["--model", model]
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
                # claude -p exits 0 even when the RESULT is an error (is_error:
                # true), e.g. a GLM/Z.ai 529 "overloaded" after the CLI's own
                # internal retries are exhausted. Surface that as an `error`
                # event (not `done`) so _claude_stream_with_overload_retry can
                # detect the overload signature in the text and retry the whole
                # turn — otherwise the wrapper sees a clean `done` and yields it
                # immediately, giving the user a dead 529 with zero retries.
                if evt.get("is_error"):
                    yield {"type": "error",
                           "message": final or "claude -p result flagged is_error (no text)"}
                    return
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
# Codex adapter — uses the official Python SDK and its pinned app-server
# runtime. AgentUI keeps its provider-neutral event contract while Codex owns
# authentication, thread persistence, tool execution, and turn control.
# ---------------------------------------------------------------------------

def _enum_value(value) -> str:
    return str(getattr(value, "value", value) or "")


def _model_json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=False)
    return value


def _codex_usage(raw) -> dict:
    """Normalize SDK/CLI counters into the existing non-overlapping buckets."""
    if raw is None:
        raw = {}
    elif hasattr(raw, "last"):
        raw = raw.last
    raw = _model_json(raw) or {}
    if isinstance(raw, dict) and isinstance(raw.get("last"), dict):
        raw = raw["last"]
    if not isinstance(raw, dict):
        raw = {}
    total_input = int(raw.get("input_tokens") or raw.get("input") or 0)
    cached = int(raw.get("cached_input_tokens") or raw.get("cached_input") or 0)
    fresh = int(raw.get("fresh_input_tokens") or max(0, total_input - cached))
    return {
        "input_tokens": fresh,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": int(
            raw.get("cache_write_input_tokens")
            or raw.get("cache_creation_input_tokens")
            or raw.get("cache_write_tokens")
            or 0
        ),
        "output_tokens": int(raw.get("output_tokens") or raw.get("output") or 0),
    }


def _codex_sdk_settings(
    extra_env: dict | None,
    capability_policy: dict | None = None,
) -> tuple[CodexConfig, Sandbox, ApprovalMode, str]:
    sandbox_mode = os.environ.get(
        "AGENTUI_CODEX_SANDBOX_MODE", "danger-full-access").strip()
    if sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
        sandbox_mode = "danger-full-access"
    sandbox = {
        "read-only": Sandbox.read_only,
        "workspace-write": Sandbox.workspace_write,
        "danger-full-access": Sandbox.full_access,
    }[sandbox_mode]

    approval_policy = os.environ.get(
        "AGENTUI_CODEX_APPROVAL_POLICY", "never").strip()
    if approval_policy not in {"untrusted", "on-failure", "on-request", "never"}:
        approval_policy = "never"
    approval_mode = (
        ApprovalMode.deny_all
        if approval_policy == "never"
        else ApprovalMode.auto_review
    )

    overrides = ["features.multi_agent=false"]
    if _agentui_notion_scoped(extra_env):
        overrides.extend([
            "mcp_servers.notion.enabled=false",
            f"mcp_servers.agentui_notion_report.command={json.dumps(str(_NOTION_REPORT_PYTHON))}",
            f"mcp_servers.agentui_notion_report.args={json.dumps(_notion_mcp_args(extra_env))}",
            f"mcp_servers.agentui_notion_report.env_vars={json.dumps(_codex_notion_env_vars(extra_env))}",
        ])
    # Agent-local policy is appended last, so it can deliberately disable a
    # control-plane-added MCP server without mutating ~/.codex/config.toml.
    runtime_env = {**os.environ, **(extra_env or {})}
    inherited = inherited_capability_config(runtime_env)
    overrides.extend(codex_config_overrides(capability_policy, inherited, runtime_env))

    child_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", **(extra_env or {})}
    if os.environ.get("AGENTUI_EVALUATION_MODE", "").lower() in {"1", "true"}:
        # LangSmith credentials belong to AgentUI, not to the Codex runtime
        # being evaluated.
        child_env.pop("LANGSMITH_API_KEY", None)
        child_env.pop("LANGCHAIN_API_KEY", None)

    config = CodexConfig(
        config_overrides=tuple(overrides),
        env={str(k): str(v) for k, v in child_env.items()},
        client_name="agentui",
        client_title="AgentUI",
    )
    return config, sandbox, approval_mode, sandbox_mode


def _codex_sdk_tool_event(item) -> dict | None:
    root = getattr(item, "root", item)
    if isinstance(root, CommandExecutionThreadItem):
        return {
            "type": "tool_use",
            "tool": "command_execution",
            "input": {
                "command": root.command,
                "cwd": str(root.cwd),
                "status": _enum_value(root.status),
                "exit_code": root.exit_code,
            },
        }
    if isinstance(root, FileChangeThreadItem):
        return {
            "type": "tool_use",
            "tool": "file_change",
            "input": {
                "changes": [_model_json(change) for change in root.changes],
                "status": _enum_value(root.status),
            },
        }
    if isinstance(root, McpToolCallThreadItem):
        args = _model_json(root.arguments)
        inp = dict(args) if isinstance(args, dict) else {"arguments": args}
        inp.update({
            "server": root.server,
            "status": _enum_value(root.status),
        })
        if root.error is not None:
            inp["error"] = _model_json(root.error)
        return {"type": "tool_use", "tool": root.tool or "mcp_tool_call", "input": inp}
    if isinstance(root, WebSearchThreadItem):
        return {
            "type": "tool_use",
            "tool": "web_search",
            "input": {"query": root.query, "status": "completed"},
        }
    return None


def _codex_sdk_skill_reads(item, enabled_skills: list[dict]) -> list[dict]:
    """Identify completed command reads of an enabled Idle-owned SKILL.md.

    Codex app-server currently exposes explicit skills as turn inputs, but no
    public ``skill/invoked`` notification.  A completed command containing the
    canonical SKILL.md path is therefore the auditable signal for implicit
    skill use.  The database de-duplicates this with explicit input for the
    same skill and turn.
    """
    root = getattr(item, "root", item)
    if not isinstance(root, CommandExecutionThreadItem):
        return []
    if _enum_value(root.status) != "completed":
        return []
    command = root.command or ""
    repo_root = Path(__file__).resolve().parents[2]
    matches: list[dict] = []
    for skill in enabled_skills:
        path = str(skill.get("path") or "")
        if not path:
            continue
        aliases = {path}
        try:
            aliases.add(str(Path(path).resolve().relative_to(repo_root)))
        except ValueError:
            pass
        if any(alias and alias in command for alias in aliases):
            matches.append(skill)
    return matches


async def codex_stream(
    message: str,
    system_prompt: str,
    cwd: str,
    model: str = "gpt-5.6-terra",
    effort: str | None = None,
    resume_session_id: str | None = None,
    extra_env: dict | None = None,
    capability_policy: dict | None = None,
    requested_skills: list[dict] | None = None,
) -> AsyncIterator[dict]:
    config, sandbox, approval_mode, sandbox_mode = _codex_sdk_settings(
        extra_env, capability_policy
    )
    codex = AsyncCodex(config=config)
    turn = None
    stream = None
    assembled: list[str] = []
    streamed_message_ids: set[str] = set()
    thread_id = resume_session_id
    usage: dict = {}
    failed: str | None = None
    completed_status: str | None = None
    enabled_skills = enabled_inventory_skills(capability_policy)
    enabled_by_path = {skill["path"]: skill for skill in enabled_skills}
    turn_skills = [
        enabled_by_path[skill["path"]]
        for skill in (requested_skills or [])
        if isinstance(skill, dict) and skill.get("path") in enabled_by_path
    ]

    try:
        await codex.__aenter__()
        extra_skill_roots = plugin_extra_roots(
            capability_policy, {**os.environ, **(extra_env or {})}
        )
        if extra_skill_roots:
            # The high-level SDK has not surfaced this app-server method yet.
            # Keep the request scoped to this SDK process so downloaded plugin
            # skills can be enabled for one agent without changing global config.
            await codex._client.request(
                "skills/extraRoots/set",
                {"extraRoots": extra_skill_roots},
                response_model=SkillsExtraRootsSetResponse,
            )
        if resume_session_id:
            thread = await codex.thread_resume(
                resume_session_id,
                cwd=cwd,
                developer_instructions=system_prompt or "",
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
            )
        else:
            thread = await codex.thread_start(
                cwd=cwd,
                developer_instructions=system_prompt or "",
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
            )
        thread_id = thread.id
        yield {"type": "meta", "data": {
            "claude_session_id": thread_id,
            "cli_adapter": "codex",
            "codex_transport": "sdk",
            "model": model,
            "cwd": cwd,
            "permission_mode": sandbox_mode,
            "capability_revision": int((capability_policy or {}).get("revision") or 0),
            "init": True,
        }}

        turn_effort = None
        if effort and effort != "default":
            turn_effort = ReasoningEffort(effort)
        turn_input = [TextInput(message)]
        turn_input.extend(
            SkillInput(name=skill["name"], path=skill["path"])
            for skill in turn_skills
        )
        turn = await thread.turn(
            turn_input,
            effort=turn_effort,
            model=model,
            sandbox=sandbox,
            approval_mode=approval_mode,
        )
        for skill in turn_skills:
            yield {
                "type": "skill_use",
                "skill": skill["name"],
                "path": skill["path"],
                "source": "explicit_input",
                "thread_id": thread_id,
                "turn_id": turn.id,
            }
        stream = turn.stream()
        async for event in stream:
            payload = event.payload
            if isinstance(payload, TurnStartedNotification):
                yield {"type": "status", "status": "thinking"}
            elif isinstance(payload, AgentMessageDeltaNotification):
                if payload.delta:
                    streamed_message_ids.add(payload.item_id)
                    assembled.append(payload.delta)
                    yield {"type": "delta", "text": payload.delta}
            elif isinstance(payload, ReasoningSummaryTextDeltaNotification):
                if payload.delta:
                    yield {"type": "thinking", "text": payload.delta}
            elif isinstance(payload, ItemCompletedNotification):
                root = getattr(payload.item, "root", payload.item)
                if isinstance(root, AgentMessageThreadItem):
                    if root.id not in streamed_message_ids and root.text:
                        assembled.append(root.text)
                        yield {"type": "delta", "text": root.text}
                else:
                    tool_event = _codex_sdk_tool_event(payload.item)
                    if tool_event:
                        yield tool_event
                    for skill in _codex_sdk_skill_reads(payload.item, enabled_skills):
                        yield {
                            "type": "skill_use",
                            "skill": skill["name"],
                            "path": skill["path"],
                            "source": "skill_file_read",
                            "thread_id": payload.thread_id,
                            "turn_id": payload.turn_id,
                        }
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                usage = _codex_usage(payload.token_usage)
            elif isinstance(payload, ErrorNotification):
                if not payload.will_retry:
                    failed = payload.error.message or "Codex turn failed"
            elif isinstance(payload, TurnCompletedNotification):
                completed_status = _enum_value(payload.turn.status)
                if payload.turn.status == TurnStatus.completed:
                    failed = None
                elif payload.turn.error is not None:
                    failed = payload.turn.error.message or failed
    except asyncio.CancelledError:
        if turn is not None:
            try:
                await asyncio.shield(turn.interrupt())
            except Exception:
                pass
        raise
    except Exception as exc:
        failed = str(exc) or type(exc).__name__
    finally:
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:
                pass
        try:
            await asyncio.shield(codex.close())
        except Exception:
            pass

    if completed_status == TurnStatus.interrupted.value and not failed:
        failed = "Codex turn interrupted"
    elif completed_status == TurnStatus.failed.value and not failed:
        failed = "Codex turn failed"
    if failed:
        yield {"type": "error", "message": failed}
        return

    text = "".join(assembled)
    if usage:
        yield {"type": "meta", "data": {"usage": usage}}
    yield {"type": "done", "text": text, "meta": {
        "claude_session_id": thread_id,
        "cli_adapter": "codex",
        "codex_transport": "sdk",
        "usage": usage,
    }}


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
# backoff seconds before each retry (index 0 → before retry #1). Long tail for
# sustained evening overload on Z.ai — a short 3-step schedule gave up while the
# gateway was still saturated; this keeps retrying long enough for a slot to free.
_OVERLOAD_BACKOFF = (5.0, 12.0, 25.0, 45.0, 90.0)


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
            # Belt-and-suspenders: a `done` whose text carries an overload
            # signature (claude -p can surface 529 as an exit-0 result without
            # is_error in some CLI builds) is also held back for retry.
            if isinstance(ev, dict) and ev.get("type") == "done" and _event_is_overload(ev):
                terminal = ev
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
    runtime_env: dict | None = None,
) -> AsyncIterator[dict]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        yield {"type": "error",
               "message": "DEEPSEEK_API_KEY not set in the environment"}
        return
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    extra_env = {
        **(runtime_env or {}),
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
    runtime_env: dict | None = None,
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
        **(runtime_env or {}),
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
    if model == "codex":
        return codex_stream
    raise AdapterError(f"unknown model adapter: {model}")
