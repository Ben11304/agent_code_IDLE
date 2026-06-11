# AgentUI — Claude Instructions

Localhost control plane for multi-agent workflows. Wraps subscription-backed CLIs (`claude`, `aas`) so the UI never needs an API key. Each declared project gets a directed graph of agents with per-agent floating chat windows, and orchestrator agents can auto-dispatch tasks to their workers via XML tags parsed in the streaming response.

This file is read on session start. Follow it.

## Mental model

- **Subscription wrappers, not API**. Every agent call goes through a local CLI (`claude -p` for Claude nodes, `aas ask` for Grok). No `ANTHROPIC_API_KEY` is read, no token billing on top of subscription.
- **Agents live in Python, not CLI processes**. The "living" agent is the Python session row + SQLite history + persisted `claude_session_id`. Each turn spawns `claude -p --resume <id>` then exits. Do not bet on a long-running stream-json input mode; it is undocumented and brittle.
- **Dispatch is verifiable on the graph**. Orchestrator emits `<dispatch agent="ID">task</dispatch>`. Backend parses live, fires a background worker chat, and emits `dispatch_started` / `dispatch_complete` SSE events. UI animates edge + worker node. If the model only narrates "I will dispatch" without the tag, the graph does not move — the user sees the lie immediately.
- **Graph is the canvas; chats float above it**. The graph is no longer a window — it is the full-bleed workspace background itself (`.graph-canvas`, z-index 0, no chrome). Chat/file windows are draggable, resizable, hidable floating windows on top. Multiple agents can chat simultaneously. Nodes are draggable with positions persisted in SQLite (`node_positions`) — layout is not derived from hierarchy, so non-top-down topologies (cycles, peer mesh, multi-root) render fine.
- **Streaming integrity matters**. Claude CLI is Node; piped stdout is block-buffered. We attach stdout to a PTY (raw termios) so each JSON event arrives line-by-line in real time. Touch `adapters.py:claude_stream` carefully.

## File map

```
app/
├── backend/
│   ├── main.py          FastAPI: /api/projects*, /tree, /file + /raw, /workspace/*, SSE chat with dispatch parsing + ledger enrichment + auto-continuation
│   ├── adapters.py      claude_stream (PTY) + grok_stream (PTY, streaming-json, --resume, --best-of-n, --check, --memory)
│   ├── projects.py      registry + project.yaml loader, graph edges, workspace_root, agent bootstrap templates + create_agent atomic
│   └── db.py            SQLite sessions, messages, agent_overrides, dispatch_results, node_positions; cleanup_stale_running on startup
├── frontend/
│   ├── index.html       Loads marked + DOMPurify from CDN, sidebar + workspace + taskbar
│   ├── app.js           Vanilla JS, window manager, SVG graph canvas (drag nodes + persist), SSE parser, markdown render, slash commands
│   └── styles.css       Theme tokens (dark + light via :root[data-theme]), window chrome, edge/node animations, dispatch card, command menu
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

- **Sidebar (left)**: project cards + **workspace-wide folder tree** (rooted at `workspace_root` from registry.yaml or the common parent of all registered projects; folders that are themselves registered projects get a diamond ◆ accent). Click any file to open it in a floating viewer window (markdown rendered, code in monospace, PDF + images via browser-native preview, binary fallback with a "download raw" link). ⌥⌘C copies absolute path of the selected file/folder.
- **Tabs (top)**: open projects. Switch tabs to change the active project; windows from other projects are hidden but kept in memory.
- **Workspace (center)**: the graph canvas is the background (auto-created per project); chat/file windows float above it. Click an agent node to open its chat window, drag a node to rearrange the graph. Drag title bar, resize bottom-right corner, hide (minimize) to taskbar, close with × or Cmd+W (the canvas itself can't be closed).
- **Taskbar (bottom)**: minimized windows; click to restore.

### Graph canvas (the workspace background)

- Click node → open / focus chat window for that agent. **Drag node → move it** (≥4px movement distinguishes drag from click; the trailing click is swallowed).
- Node positions persist per project in the `node_positions` table: payload of `GET /api/projects/{slug}` includes `positions`; drags save via debounced (600ms) `POST /api/projects/{slug}/positions`; `DELETE` clears all.
- Auto-layout (layered by `parents`) only **seeds nodes that have no saved position** — a hand-placed node is never silently moved.
- Edges are direction-agnostic beziers between border anchors (`edgePathD` + `rectAnchor`), arrowhead via `.arrow-head` CSS class; A↔B pairs bend apart. During a node drag, `updateEdgesLive` rewrites only the `d` attributes.
- Scroll to zoom (anchored at cursor), drag empty area to pan.
- Corner buttons: + / − zoom, ⌖ **fit** (viewBox only, positions untouched), ⟲ **re-layout** (confirm → wipe saved positions → re-seed auto-layout).
- Node color: idle gray / running yellow pulse / ok green / error red.
- Edge: pulsing purple dash during active dispatch.
- Each node shows its current model in the badge (e.g. `opus-4-8`).

### Theme

Light (default) / dark toggle — ◐ button in the sidebar title, persisted in `localStorage.theme`, applied as `data-theme` on `<html>`. All colors in `styles.css` are CSS custom properties; the light palette lives in `:root[data-theme="light"]`. When adding UI, never hard-code colors — use the tokens (`--bg-code`, `--hover`, `--glass`, `--accent-border`, `--edge`, `--orch-bg`, …).

### Chat window

- Header: agent name, role, current model + effort (text only — change via `/model` / `/effort`)
- Messages: full markdown rendering (marked + DOMPurify); dispatch tags become collapsed cards
- Input: Enter sends, Shift+Enter newlines, Esc stops streaming, plus slash commands
- "Send" button toggles to red "Stop" while streaming

### Add agent (UI form + parent-generated bootstrap)

Click `+ agent` on the graph window. The form collects:

- `id` (uppercase, `[A-Z][A-Z0-9_]*`)
- `role` (one-line description)
- `model` (`claude` | `grok`)
- `claude_model` / `grok_model` (filtered by adapter)
- `effort` (optional)
- `system_prompt_file` (relative; if blank → `<ID>/AGENT.md`)
- `cwd` (relative; if blank → `<ID>`)
- `parents` (multi-select from existing agents)
- **Bootstrap mode** — radio:
  - **Generate from parent** (default if a parent is selected): the system sends a `[CONTROL-PLANE BOOTSTRAP REQUEST]` to the first parent in the list (e.g. BOSS). The parent emits 5 `<file path="...">...</file>` blocks based on its project knowledge. The modal streams that output live; on `bootstrap_done` the parsed files become the preview.
  - **Template generic**: render a static skeleton (used if no parent, or for fast iteration without burning quota).

Both modes lead to the same **preview pane** — collapsible blocks of each file's content + warnings (missing required file, path outside agent folder, `shared/` absent in project, etc.). Click `← Back to edit` to edit the form, or `✓ Create agent` to write.

Writing is atomic: backend creates `<project_root>/<ID>/`, writes every file, then appends to `project.yaml` via ruamel.yaml (preserves comments). Any failure rolls back the folder. On success, the graph rerenders, the folder tree refreshes, and the new node is dispatchable from any orchestrator that lists it as a child.

Required files generated per agent:

- `<ID>/AGENT.md` — system prompt (Role, Required reads, Scope, Pre-flight, Output contract, Escalation triggers). Under ~80 lines. **No routing tables, no worker lists** — that information is injected at runtime by `_dispatch_instructions` from the live graph.
- `<ID>/inputs/manifest.md` — YAML frontmatter + table of upstream artifacts (one row per parent).
- `<ID>/outputs/manifest.md` — YAML frontmatter + table of produced artifacts.
- `<ID>/state/progress.md` — initial bootstrap entry.
- `<ID>/context/code_map.md` — owned files + read-only references.

The parent-generated bootstrap is what makes the agent contextually correct: BOSS that knows `paper/` exists will reference it directly in `inputs/manifest.md`, instead of leaving `(TBD)` placeholders.

### Slash commands (in chat input)

Type `/` to open the command menu. Tab to insert, ↑↓ to navigate, Esc to close.

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/clear` | Create a fresh session (history kept in db) |
| `/model <fable-5\|opus-4-8\|opus-4-7\|sonnet\|haiku>` | Change this agent's model |
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
    claude_model: claude-fable-5 | claude-opus-4-8 | claude-opus-4-7 | claude-sonnet-4-6 | claude-haiku-4-5
    grok_model: grok-build | grok-composer-2.5-fast       # only when model: grok
    effort: low | medium | high | xhigh | max   # optional
    system_prompt_file: <relative path to AGENT.md>
    cwd: <relative working dir>
    parents: [<id>, ...]          # upstream nodes; determines graph layer + dispatch eligibility
```

`claude_model` and `effort` are baseline; the UI's `/model` and `/effort` commands store **runtime overrides** in `agent_overrides` table that win over yaml.

Layout rule: an agent with no parents is rendered as orchestrator (top layer). An agent's `children` (any agent listing this one as parent) are the only valid dispatch targets — the system rejects dispatches outside that set.

## Dispatch ledger (orchestrator sees worker outputs)

The "blind dispatch" problem — orchestrator emits `<dispatch>`, user sees the worker reply on screen, but the orchestrator's `--resume` context never contains it — is solved by a SQLite ledger + message-string enrichment + one bounded auto-continuation. Designed via Grok best-of-3 spec at [docs/agentui-dispatch-spec.md](docs/agentui-dispatch-spec.md).

Flow per user turn:

1. User → orchestrator. Orchestrator streams an `<dispatch agent="WORKER">…</dispatch>` tag.
2. Backend parses live, fires `_dispatched_run` for the worker.
3. Worker runs through the same `_run_agent`, streams to UI, returns its `final_text`.
4. `_dispatched_run` writes a row into `dispatch_results` table: `(project_slug, source_agent, target_agent, task, result_text, status, completed_at)`.
5. Driver `gather`s all tracker tasks, then fires ONE auto-continuation: `_run_agent(orchestrator, "[CONTROL-PLANE CONTINUATION] …")`.
6. Inside `_run_agent`, ENRICHMENT block queries the ledger for `source_agent = orchestrator AND consumed_at IS NULL`, formats them as `<dispatch_result from="WORKER">…</dispatch_result>` blocks, and **prepends them to the `message` string sent to the CLI**. The original message stays unchanged in the messages table for UI fidelity; only the CLI prompt is enriched.
7. Orchestrator's CLI sees the real worker outputs and synthesises / chains / reports — all inside the same SSE response the user is watching.
8. On clean turn end (`final_status == "ok"`), the consumed ledger rows are marked. They never resurface.

Truncation: head 6.8 KB + tail 1 KB + "[… truncated; full output in <worker> chat or its `state/` files]" marker when a single worker result exceeds 8 KB. Full text stays in the worker's session db.

Cancellation: `_dispatched_run` catches `CancelledError`, recovers partial assembled text from the worker's session, writes a `status="cancelled"` row, propagates. Orchestrator's next turn sees the partial/cancelled result and decides.

Why not MCP today: the proper Anthropic pattern (orchestrator calls a `dispatch_to_worker` MCP tool, gets the result as a tool_result inside the same turn) requires changing `adapters.py` cmd construction (risk to PTY contract), a cross-process trigger from a stdio MCP child of the claude CLI back into uvicorn's driver/tracker/emit, and a different mechanism for grok which has no MCP support today. The ledger is the persistence layer a future MCP tool would use anyway, so the work isn't thrown away — when MCP is added, the tool handler will just call `_run_agent` and `record_dispatch_result` exactly as `_dispatched_run` does today.

When extending this: never write worker results into the orchestrator's `messages` table as fake rows. The `messages` table only affects UI replay; the CLI never reads it. Only enrichment of the `message=` argument actually reaches the model.

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
| `status` | `agent`, `status=thinking\|responding` | Drives the "thinking…" indicator. |
| `thinking` | `agent`, `text` | Recent thinking chunk (visible in indicator). |
| `delta` | `agent`, `text` | Assistant text delta. Appended to bubble. |
| `dispatch_started` | `source`, `target`, `task` | Animate edge, mark target running. |
| `dispatch_complete` | `source`, `target`, `status`, `message` | Stop animation, mark target ok/error. |
| `dispatch_rejected` | `source`, `target`, `reason` | Show in status bar. |
| `agent_done` | `agent`, `text`, `status` | Final state for that agent. |
| `error` | `agent`, `message` | Surface in bubble as quote. |
| `complete` | — | Queue closed, all dispatches finished. |
| `bootstrap_done` | `files`, `warnings`, `target_folder` | Emitted only by `/agents/preview-from-parent` at end of stream. Frontend uses this to build the preview pane. |

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

## Add-agent endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/projects/{slug}/agents/preview` | POST | Render the static template for the form's values. Returns `{target_folder, files, warnings}` synchronously. |
| `/api/projects/{slug}/agents/preview-from-parent` | POST (SSE) | Run the first parent as a bootstrap writer. Streams normal chat events (`delta`, `thinking`, `status`, etc) plus a final `bootstrap_done` with `files` and `warnings`. |
| `/api/projects/{slug}/agents` | POST | Atomic create. Body is `NewAgent`. If `custom_files: [{path, content}]` is present (sent after preview-from-parent), those files are written verbatim; else the template is rendered. Each path must start with `<ID>/` and contain no `..`. Failure rolls back the directory. |

The bootstrap-from-parent prompt (in `main.py:_bootstrap_prompt`) is a strict envelope: the parent must emit exactly the listed file blocks, each wrapped in `<file path="...">...</file>`, with no prose. The control plane parses with `_FILE_BLOCK_RE`. If the parent violates the format (no blocks, wrong paths), the modal surfaces it and the user is sent back to the form.

Template generation lives in `projects.py:_AGENT_FILE_TEMPLATES` (5 templates) + `render_agent_files()`. Keep the AGENT.md template under ~80 lines and free of routing tables — the same rule we apply when prompting the parent.

## Adding a new project

1. Create `.agentui/project.yaml` in the project root (schema above).
2. Reference each agent's existing `AGENT.md` as `system_prompt_file` so the wrapped CLI inherits the project's full role context.
3. Append the absolute path to `app/registry.yaml`.
4. Reload uvicorn (auto-reload picks up registry changes).
5. Refresh browser — project appears in left sidebar.

## Known limitations / next iterations

- **No project-wide event bus**. Worker SSE events flow only through the parent chat that triggered them. If you open a separate worker chat window during a BOSS-triggered dispatch, it refreshes from db on `dispatch_started` / `agent_done` / `dispatch_complete` (mirror). Live token-by-token mirroring would need a pub/sub channel.
- **Worker result feedback to orchestrator is solved by the dispatch ledger** (see the dedicated section above). Future work: an optional MCP tool surface so claude-native tool_result UX is available for Claude orchestrators without changing the persistence model.
- **No idle reaper**. Sessions live forever. Plan: idle timeout + startup reaping with pid groups.
- **Compaction: solved.** `/compact` (manual) and auto-compact (fires before a user turn when the session's last-turn context ≥ `_AUTO_COMPACT_PCT` = 80%): the agent summarises its own context, a fresh session is created seeded with that recap (`sessions.seed`, prepended once then cleared). Empty recap → rotate anyway, cold-start preamble covers recovery.
- **Cold-start preamble.** Any turn that cannot `--resume` (fresh/torn session) gets a deterministic recap prepended: latest `state/progress.md` sections + unfinished `state/plan.md` + `state/children_status.json` (parents) + `inputs/manifest.md` head. Built in `_session_preamble`; a /compact seed takes precedence.
- **Children rollup.** Every parent gets `state/children_status.json` auto-derived on each `/stats` call (and via `POST /api/projects/{slug}/rollup`): per-child status, context %, memory freshness + `stale_memory` flag, sha256 of progress.md. Read-only projection — never hand-edited, never a second source of truth.
- **Plan ledger.** All agents get `_PLAN_INSTRUCTIONS` appended to their system prompt: multi-step tasks must emit `<plan>1. …</plan>` then `<step n="1" status="done">note</step>` (doing|done|blocked). Parsed live like dispatch tags, persisted to the agent's `state/plan.md` (survives sessions, resumed via preamble), surfaced as `plan` in `/stats` and a progress row on the node panel. SSE events: `plan_updated`, `plan_step`.
- **Continuation is multi-round.** Up to `_MAX_CONT_ROUNDS` = 3 continuations per user turn: each wave of dispatches is gathered, then the orchestrator reacts (synthesise or chain new dispatches) in the same SSE response. The final round's prompt forbids further dispatches; any emitted anyway still run, results land in the ledger for the next turn.
- **Dispatch contract verify.** `_dispatched_run` snapshots the worker's `outputs/manifest.md` (mtime + version) before/after; an unchanged manifest on an ok dispatch appends a `[control-plane verify]` warning into the ledger text so the orchestrator demands a manifest bump before consuming artifact-producing work.
- **Tab close cancels SSE**. Dispatched workers can be killed mid-turn if the browser disconnects (driver cascades cancel). On startup the orphan reaper marks any leftover `running` sessions as `cancelled` so the UI is never permanently stuck.
- **Resume guard**. `--resume` is only used when `last_status == "ok"`. Any prior turn that was `running` / `cancelled` / `error` is treated as torn (claude server state may be mid-reply); next turn starts a fresh CLI session. This avoids the "agent silently returns nothing" failure mode after a stop / orphan.
- **SSE heartbeat**. The chat stream emits `: keepalive` every 15s of quiet. Required because Opus extended thinking can sit 10–30s without bytes — without the heartbeat, browsers and proxies close the SSE and the UI shows "stopped" with no response.
- **Add-agent is creation-only**. Edit and delete (with archive) are Sprint 1. To remove an agent today: stop the server, delete the folder + remove the yaml entry by hand, restart.

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
