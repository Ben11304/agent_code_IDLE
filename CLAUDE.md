# AgentUI — Claude Instructions

Localhost control plane for multi-agent workflows. Wraps subscription-backed CLIs (`claude`, `aas`) so the UI never needs an API key. Each declared project gets a directed graph of agents with per-agent chat sessions, and orchestrator agents can auto-dispatch tasks to their workers via XML tags parsed in the streaming response.

This file is read on session start. Follow it.

## Mental model

- **Subscription wrappers, not API**. Every agent call goes through a local CLI (`claude -p` for Claude nodes, `aas ask` for Grok). No `ANTHROPIC_API_KEY` is read, no token billing on top of subscription. If you change adapters, preserve this invariant.
- **Agents live in Python, not CLI processes**. The "living" agent is the Python session row + SQLite history + persisted `claude_session_id`. Each turn spawns `claude -p --resume <id>` then exits. Do not bet on a long-running stream-json input mode; it is undocumented and brittle.
- **Dispatch is verifiable on the graph**. Orchestrator emits `<dispatch agent="ID">task</dispatch>`. Backend parses live, fires a background worker chat, and emits `dispatch_started` / `dispatch_complete` SSE events. UI animates edge + worker node. If the model only narrates "I will dispatch" without the tag, the graph does not move — the user sees the lie immediately.
- **Streaming integrity matters**. Claude CLI is Node; piped stdout is block-buffered. We attach stdout to a PTY (raw termios) so each JSON event arrives line-by-line in real time. Touch `adapters.py:claude_stream` carefully.

## File map

```
app/
├── backend/
│   ├── main.py          FastAPI app, /api/projects*, SSE chat with dispatch parsing
│   ├── adapters.py      claude_stream (PTY) + grok_stream (aas subprocess)
│   ├── projects.py      registry + project.yaml loader, graph edges
│   └── db.py            SQLite sessions + messages, persists claude_session_id
├── frontend/
│   ├── index.html       Loads marked + DOMPurify from CDN
│   ├── app.js           Vanilla JS, SVG graph, SSE stream parser, markdown render
│   └── styles.css       Dark theme, edge/node animations, dispatch card
├── registry.yaml        List of project root paths to scan
├── agentui.db           SQLite, auto-created on first run
└── run.sh               venv bootstrap + uvicorn launcher
```

Project configs live with the projects themselves:

```
<project_root>/.agentui/project.yaml
```

Currently wired:
- `/Users/viethuy/Working_space/ConstructionVLM-Eval-AGENT` (5 agents: FRAMEWORK → DATASET → VLM → DASHBOARD → AUDIT)
- `/Users/viethuy/Working_space/energy-infrastructure-risk/.claude/AGENT` (BOSS orchestrates PIPELINE, ANALYSIS, WEB, DOCS, INTEGRITY)

## How to run

```bash
cd /Users/viethuy/Working_space/UI_agentcoding/app
./run.sh
# → http://127.0.0.1:5174
```

`run.sh` creates `.venv`, installs `fastapi uvicorn pyyaml`, runs uvicorn with `--reload`. Frontend changes need browser hard refresh (Cmd+Shift+R) because CDN scripts cache.

Port conflicts: `lsof -ti tcp:5174 | xargs kill -9`.

## project.yaml schema

```yaml
name: <human readable>
slug: <url slug>
description: <one line>
agents:
  - id: <UPPERCASE_ID>           # used in dispatch tags
    role: <one-line description>
    model: claude | grok          # adapter selector
    claude_model: claude-sonnet-4-6 | claude-opus-4-7 | claude-haiku-4-5
    effort: low | medium | high | max   # optional, omit = claude default
    system_prompt_file: <relative path to AGENT.md>
    cwd: <relative working dir>
    parents: [<id>, ...]          # upstream nodes; determines graph layer + dispatch eligibility
```

Layout rule: an agent with no parents is rendered as orchestrator (top layer). An agent's `children` (any agent listing this one as parent) are the only valid dispatch targets — the system rejects dispatches outside that set.

## Dispatch protocol

When a project chat hits an agent that has children, the backend automatically appends mandatory dispatch instructions to its system prompt. The agent emits:

```
<dispatch agent="WORKER_ID">Self-contained task text.</dispatch>
```

Backend behaviour:
1. Regex-scans accumulated stream text after each text delta.
2. On the first complete tag for a `(start_offset, target)` key, fires `_dispatched_run` as `asyncio.create_task`.
3. Emits `dispatch_started` SSE event immediately.
4. Worker turn runs through the same `_run_agent` (recursive — multi-level orchestration works, with chain-loop guard).
5. Emits `dispatch_complete` when worker finishes.
6. `driver()` `asyncio.gather`s all tracker tasks before closing the SSE queue, so the parent chat stays open until all dispatched workers complete.

Hard constraints on dispatch:
- Target must be a direct child in the graph. Else `dispatch_rejected`.
- Target must not be an ancestor in the current dispatch chain. Else `dispatch_rejected` ("would create dispatch loop").

When editing the dispatch prompt, remember the user verifies on the graph. Keep the instruction strict about "narrating without emitting tag = lying" — the model rationalises (`"simpler"`, `"faster"`) and skips dispatch otherwise.

## SSE event types (backend → frontend)

| Event | Fields | Meaning |
|---|---|---|
| `start` | `agent` | Stream opened. |
| `agent_status` | `agent`, `status` | Node color update (`running` mid-turn, etc). |
| `meta` | `agent`, `data` | Includes `claude_session_id`. Persisted for `--resume`. |
| `status` | `agent`, `status=thinking\|responding` | Drives the "đang suy nghĩ…" indicator. |
| `thinking` | `agent`, `text` | Recent thinking chunk (visible in indicator). |
| `delta` | `agent`, `text` | Assistant text delta. Appended to bubble. |
| `dispatch_started` | `source`, `target`, `task` | Animate edge, mark target running. |
| `dispatch_complete` | `source`, `target`, `status`, `message` | Stop animation, mark target ok/error. |
| `dispatch_rejected` | `source`, `target`, `reason` | Show in status bar. |
| `agent_done` | `agent`, `text`, `status` | Final state for that agent. |
| `error` | `agent`, `message` | Surface in bubble as quote. |
| `complete` | — | Queue closed, all dispatches finished. |

If you add an event type, update both `_run_agent` / `_dispatched_run` (emit) and `handleEvent` in `app.js` (consume).

## Streaming integrity (do not break)

`adapters.py:claude_stream` attaches the child's stdout to a PTY (`pty.openpty`) and sets termios raw mode (`OPOST` cleared) so that:
- Node CLIs do not block-buffer 4 KB before flushing.
- The terminal does not translate `\n` to `\r\n`.

`main.py:api_chat` returns `StreamingResponse` with `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. Don't introduce middleware that buffers.

Default `claude_model` is `claude-sonnet-4-6` — fast and cheap. Opus 4.7 has expensive extended thinking by default; use only for orchestrators if needed.

## Things NOT to do

- ❌ Do not introduce `ANTHROPIC_API_KEY` paths. Subscription is the design choice. If you need an API path, gate it behind an explicit `model: claude-api` adapter, never silently.
- ❌ Do not replace the PTY with `subprocess.PIPE`. Streaming will regress to "lag 2-5s then dump everything".
- ❌ Do not rely on `claude -p --input-format stream-json` as a long-running input loop. It is not documented for that purpose; multiple turns happen via `--resume`.
- ❌ Do not store secrets in `registry.yaml` or `project.yaml`. They are not gitignored.
- ❌ Do not run the UI publicly. Subscription terms permit personal use; shared hosting against your login would be reselling.

## Adding a new project

1. Create `.agentui/project.yaml` in the project root (schema above).
2. Reference each agent's existing `AGENT.md` as `system_prompt_file` so the wrapped CLI inherits the project's full role context.
3. Append the absolute path to `app/registry.yaml`.
4. Reload uvicorn (auto-reload picks up registry changes).
5. Refresh browser — project appears in left sidebar.

## Known limitations / next iterations

- **No project-wide event bus**. Worker SSE events flow only through the parent chat that triggered them. Switching to the worker node mid-dispatch reloads from DB, not live. Plan: pub/sub on a `/api/projects/{slug}/events` SSE channel.
- **No worker result feedback to orchestrator**. Dispatch is fire-and-forget; BOSS does not see PIPELINE's output for the next decision. Plan: MCP server exposing `dispatch_to_worker(id, task)` as a real tool, response returned in the tool result.
- **No idle reaper**. Sessions live forever, claude session IDs accumulate. Plan: idle timeout + startup reaping with pid groups.
- **No compaction**. Long chats grow `--resume` context indefinitely. Plan: periodic summarise + new session ID.
- **Tab close cancels SSE**. Dispatched workers may get cancelled mid-turn if the browser disconnects. Plan: detach background tasks from request lifecycle.

If you implement any of these, update this section to reflect what was done.

## Quick debugging

- Backend logs: stdout of `run.sh`. Add `print(...)` freely; uvicorn reloads.
- DB inspect: `sqlite3 agentui.db ".schema"`, then `select * from sessions; select * from messages where session_id=...`.
- Test streaming alone (no UI): `python -c "import asyncio; from backend.adapters import claude_stream; ..."`
- Test API: `curl -N -X POST http://127.0.0.1:5174/api/projects/energy/agents/BOSS/chat -H 'Content-Type: application/json' -d '{"message":"hello"}'` (the `-N` disables curl buffering).
- Frontend: DevTools → Network → filter `chat` → click row → EventStream tab shows raw SSE events.

---

**Bottom line**: this is a personal localhost dashboard for verifying multi-agent orchestration with your own eyes. The graph is the source of truth; if it doesn't light up, dispatch didn't happen. Keep that invariant intact when extending.
