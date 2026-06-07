# AgentUI — Claude Instructions

Localhost control plane for multi-agent workflows. Wraps subscription-backed CLIs (`claude`, `aas`) so the UI never needs an API key. Each declared project gets a directed graph of agents with per-agent floating chat windows, and orchestrator agents can auto-dispatch tasks to their workers via XML tags parsed in the streaming response.

This file is read on session start. Follow it.

## Mental model

- **Subscription wrappers, not API**. Every agent call goes through a local CLI (`claude -p` for Claude nodes, `aas ask` for Grok). No `ANTHROPIC_API_KEY` is read, no token billing on top of subscription.
- **Agents live in Python, not CLI processes**. The "living" agent is the Python session row + SQLite history + persisted `claude_session_id`. Each turn spawns `claude -p --resume <id>` then exits. Do not bet on a long-running stream-json input mode; it is undocumented and brittle.
- **Dispatch is verifiable on the graph**. Orchestrator emits `<dispatch agent="ID">task</dispatch>`. Backend parses live, fires a background worker chat, and emits `dispatch_started` / `dispatch_complete` SSE events. UI animates edge + worker node. If the model only narrates "I will dispatch" without the tag, the graph does not move — the user sees the lie immediately.
- **Floating windows, not panels**. The workspace is a VSCode-like desktop with draggable, resizable, hidable windows. Graph and each agent chat are independent windows. Multiple agents can chat simultaneously.
- **Streaming integrity matters**. Claude CLI is Node; piped stdout is block-buffered. We attach stdout to a PTY (raw termios) so each JSON event arrives line-by-line in real time. Touch `adapters.py:claude_stream` carefully.

## File map

```
app/
├── backend/
│   ├── main.py          FastAPI: /api/projects*, /tree, /session, /clear, /settings, SSE chat with dispatch parsing
│   ├── adapters.py      claude_stream (PTY) + grok_stream (aas subprocess)
│   ├── projects.py      registry + project.yaml loader, graph edges
│   └── db.py            SQLite sessions, messages, agent_overrides; cleanup_stale_running on startup
├── frontend/
│   ├── index.html       Loads marked + DOMPurify from CDN, sidebar + workspace + taskbar
│   ├── app.js           Vanilla JS, window manager, SVG graph, SSE parser, markdown render, slash commands
│   └── styles.css       Dark theme, window chrome, edge/node animations, dispatch card, command menu
├── registry.yaml        List of project root paths to scan
├── agentui.db           SQLite, auto-created on first run
└── run.sh               venv bootstrap + uvicorn launcher
```

Project configs live with the projects themselves:

```
<project_root>/.agentui/project.yaml
```

Currently wired (edit absolute paths in `app/registry.yaml`):
- `/Users/viethuy/Working_space/ConstructionVLM-Eval-AGENT` (5 agents: FRAMEWORK → DATASET → VLM → DASHBOARD → AUDIT)
- `/Users/viethuy/Working_space/energy-infrastructure-risk/.claude/AGENT` (BOSS orchestrates PIPELINE, ANALYSIS, WEB, DOCS, INTEGRITY)

## How to run

```bash
cd app
./run.sh
# → http://127.0.0.1:5174
```

`run.sh` creates `.venv`, installs `fastapi uvicorn pyyaml`, runs uvicorn with `--reload`. Frontend changes need browser hard refresh (Cmd+Shift+R) because CDN scripts cache.

Port conflicts: `lsof -ti tcp:5174 | xargs kill -9`.

## UI overview

- **Sidebar (left)**: project cards + folder tree. ⌥⌘C copies absolute path of the selected file/folder.
- **Tabs (top)**: open projects. Switch tabs to change the active project; windows from other projects are hidden but kept in memory.
- **Workspace (center)**: floating windows. Graph window auto-opens per project. Click an agent node to open its chat window. Drag title bar, resize bottom-right corner, hide (minimize) to taskbar, close with × or Cmd+W.
- **Taskbar (bottom)**: minimized windows; click to restore.

### Graph window

- Click node → open / focus chat window for that agent
- Scroll to zoom (anchored at cursor), drag empty area to pan
- + / − / ⌖ buttons in the corner; ⌖ refits the graph
- Node color: idle gray / running yellow pulse / ok green / error red
- Edge: pulsing purple dash during active dispatch
- Each node shows its current model in the badge (e.g. `opus-4-8`)

### Chat window

- Header: agent name, role, current model + effort (text only — change via `/model` / `/effort`)
- Messages: full markdown rendering (marked + DOMPurify); dispatch tags become collapsed cards
- Input: Enter sends, Shift+Enter newlines, Esc stops streaming, gồm slash commands
- "Gửi" button toggles to red "Stop" while streaming

### Slash commands (in chat input)

Type `/` to open the command menu. Tab to insert, ↑↓ to navigate, Esc to close.

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/clear` | Create a fresh session (history kept in db) |
| `/model <opus-4-8\|opus-4-7\|sonnet\|haiku>` | Change this agent's model |
| `/effort <default\|low\|medium\|high\|max>` | Change this agent's effort |
| `/focus <AGENT_ID>` | Open another agent's chat window |
| `/dispatch <AGENT_ID> <task>` | Open target's chat and send task immediately |
| `/stop` | Same as Stop button |
| `/status` | Show current model, effort, status, streaming state |

## project.yaml schema

```yaml
name: <human readable>
slug: <url slug>
description: <one line>
agents:
  - id: <UPPERCASE_ID>           # used in dispatch tags
    role: <one-line description>
    model: claude | grok          # adapter selector
    claude_model: claude-opus-4-8 | claude-opus-4-7 | claude-sonnet-4-6 | claude-haiku-4-5
    effort: low | medium | high | max   # optional
    system_prompt_file: <relative path to AGENT.md>
    cwd: <relative working dir>
    parents: [<id>, ...]          # upstream nodes; determines graph layer + dispatch eligibility
```

`claude_model` and `effort` are baseline; the UI's `/model` and `/effort` commands store **runtime overrides** in `agent_overrides` table that win over yaml.

Layout rule: an agent with no parents is rendered as orchestrator (top layer). An agent's `children` (any agent listing this one as parent) are the only valid dispatch targets — the system rejects dispatches outside that set.

## Dispatch protocol

When a chat is sent to an agent that has children, the backend automatically appends mandatory dispatch instructions to its system prompt. The agent emits:

```
<dispatch agent="WORKER_ID">Concise task statement.</dispatch>
```

The dispatch instructions tell the agent NOT to repeat reading lists or role briefings inside the task body — the worker already has its `AGENT.md` injected as system prompt and resumes its prior session, so it knows its scope. Verbose dispatch tasks are flagged as bad style.

Backend behaviour:
1. Regex-scans accumulated stream text after each text delta.
2. On the first complete tag for a `(start_offset, target)` key, fires `_dispatched_run` as `asyncio.create_task`.
3. Emits `dispatch_started` SSE event immediately.
4. Worker turn runs through the same `_run_agent` (recursive — multi-level orchestration works, with chain-loop guard).
5. Emits `dispatch_complete` when worker finishes.
6. `driver()` `asyncio.gather`s all tracker tasks before closing the SSE queue, so the parent chat stays open until all dispatched workers complete.
7. On client abort/disconnect: driver catches `CancelledError`, cascade-cancels all in-flight workers.

Hard constraints on dispatch:
- Target must be a direct child in the graph. Else `dispatch_rejected`.
- Target must not be an ancestor in the current dispatch chain. Else `dispatch_rejected` ("would create dispatch loop").

When editing the dispatch prompt, remember the user verifies on the graph. Keep the instruction strict about "narrating without emitting tag = lying" and forbid verbose reading lists in task bodies — models tend to regress without explicit ban.

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

If you add an event type, update both `_run_agent` / `_dispatched_run` (emit) and `handleEventInWindow` in `app.js` (consume).

## Streaming integrity (do not break)

`adapters.py:claude_stream` attaches the child's stdout to a PTY (`pty.openpty`) and sets termios raw mode (`OPOST` cleared) so that:
- Node CLIs do not block-buffer 4 KB before flushing.
- The terminal does not translate `\n` to `\r\n`.

`main.py:api_chat` returns `StreamingResponse` with `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. Don't introduce middleware that buffers.

Default `claude_model` is `claude-sonnet-4-6` — fast and cheap. Opus 4.7 / 4.8 have expensive extended thinking by default; use only for orchestrators if needed.

## Things NOT to do

- ❌ Do not introduce `ANTHROPIC_API_KEY` paths. Subscription is the design choice. If you need an API path, gate it behind an explicit `model: claude-api` adapter, never silently.
- ❌ Do not replace the PTY with `subprocess.PIPE`. Streaming will regress to "lag 2-5s then dump everything".
- ❌ Do not rely on `claude -p --input-format stream-json` as a long-running input loop. It is not documented for that purpose; multiple turns happen via `--resume`.
- ❌ Do not store secrets in `registry.yaml` or `project.yaml`. They are not gitignored but `agentui.db` is.
- ❌ Do not run the UI publicly. Subscription terms permit personal use; shared hosting against your login would be reselling. Use SSH tunnel for remote access (see `DEPLOY.md`).
- ❌ Do not bind uvicorn to 0.0.0.0. Always `127.0.0.1`.

## Adding a new project

1. Create `.agentui/project.yaml` in the project root (schema above).
2. Reference each agent's existing `AGENT.md` as `system_prompt_file` so the wrapped CLI inherits the project's full role context.
3. Append the absolute path to `app/registry.yaml`.
4. Reload uvicorn (auto-reload picks up registry changes).
5. Refresh browser — project appears in left sidebar.

## Known limitations / next iterations

- **No project-wide event bus**. Worker SSE events flow only through the parent chat that triggered them. If you open a separate worker chat window during a BOSS-triggered dispatch, it refreshes from db on `dispatch_started` / `agent_done` / `dispatch_complete` (mirror). Live token-by-token mirroring would need a pub/sub channel.
- **No worker result feedback to orchestrator**. Dispatch is fire-and-forget; BOSS does not see PIPELINE's output for the next decision. Plan: MCP server exposing `dispatch_to_worker(id, task)` as a real tool, response returned in the tool result.
- **No idle reaper**. Sessions live forever. Plan: idle timeout + startup reaping with pid groups.
- **No compaction**. Long chats grow `--resume` context indefinitely. Plan: periodic summarise + new session ID. `/clear` is the manual escape hatch.
- **Tab close cancels SSE**. Dispatched workers can be killed mid-turn if the browser disconnects (driver cascades cancel). On startup the orphan reaper marks any leftover `running` sessions as `cancelled` so the UI is never permanently stuck.

If you implement any of these, update this section.

## Quick debugging

- Backend logs: stdout of `run.sh`. Add `print(...)` freely; uvicorn reloads.
- DB inspect: `sqlite3 agentui.db ".schema"`, then `select * from sessions; select * from messages where session_id=...; select * from agent_overrides;`.
- Test streaming alone (no UI): `python -c "import asyncio; from backend.adapters import claude_stream; ..."`
- Test API: `curl -N -X POST http://127.0.0.1:5174/api/projects/energy/agents/BOSS/chat -H 'Content-Type: application/json' -d '{"message":"hello"}'` (the `-N` disables curl buffering).
- Frontend: DevTools → Network → filter `chat` → click row → EventStream tab shows raw SSE events.
- Stuck "running" status: restart uvicorn; startup reaper auto-flips orphans to `cancelled`.

---

**Bottom line**: this is a personal localhost dashboard for verifying multi-agent orchestration with your own eyes. The graph is the source of truth; if it doesn't light up, dispatch didn't happen. Keep that invariant intact when extending.
