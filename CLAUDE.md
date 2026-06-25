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
│   ├── main.py          FastAPI: /api/projects*, /tree, /file + /raw, /workspace/*, SSE chat with dispatch parsing + ledger enrichment + auto-continuation; _start_run (shared by /chat + scheduler), _scheduler_loop, <schedule>/<schedule_stop> parsing
│   ├── adapters.py      claude_stream (PTY) + grok_stream (PTY, streaming-json, --resume, --best-of-n, --check, --memory)
│   ├── projects.py      registry + project.yaml loader, graph edges, workspace_root, agent bootstrap templates + create_agent atomic
│   └── db.py            SQLite sessions, messages, agent_overrides, dispatch_results, node_positions, scheduled_tasks; cleanup_stale_running on startup
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

`run.sh` creates `.venv`, installs `fastapi uvicorn pyyaml`, runs uvicorn **without `--reload`**. Frontend changes need browser hard refresh (Cmd+Shift+R) because CDN scripts cache; **backend changes need a manual restart** (no reloader).

> ⚠️ Do NOT re-add `--reload`. `agentui.db` (+ `-wal`/`-journal`) lives inside the watched `app/` tree, so every DB write during an agent turn used to trigger a reload that cancelled the driver task and killed the `claude -p` subprocess — the "agent cut off mid-answer" bug. Run.sh is env-specific and not committed.

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
- **Turn strip** (below header, only during multi-agent turns): `BOSS ▸ [VLM ✓] [DATASET ⏳] · round 2/3` — chips driven by `dispatch_started/complete`, round by `continuation_round`.
- **Worker cards**: a dispatched worker's entire output streams INTO one collapsible card (`ensureWorkerCard`) instead of interleaving top-level bubbles. Card head shows a live status line (status events → "✓ <first line of final text>" on agent_done); body (hidden by default) holds the full transcript; footer button opens the worker's own chat. Sub-dispatches nest inside the source's card. Only the root agent writes top-level bubbles.
- **Control-plane separators**: `[CONTROL-PLANE …]` user messages (continuation prompts) render as a thin labelled rule (`addCtrlSeparator`), live (via `continuation_round` event) and on db replay — never as user bubbles. Each continuation round also starts a fresh orchestrator bubble (`bubbleFor.reset`).
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
| `/schedule <30m\|once 2h> <task>` | Recurring or one-shot scheduled run of this agent |
| `/track <30m> <goal task>` | Goal loop — re-run every interval until the agent self-terminates |
| `/schedules` | List this project's schedules |
| `/unschedule <id>` | Cancel a schedule by id |

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

## Scheduler (recurring / deferred / goal-driven turns)

`claude -p` headless has no way to wake itself up, so an agent that "promises to monitor every 30 min" used to go silent — there was never any mechanism behind the promise. The scheduler adds one. **A scheduled fire is identical to a `POST /chat`**: it runs through the same `_start_run` → `_Run`/`driver`/`_run_agent` path, streams into the agent's chat, and persists to `messages` — so it is visible and replayable. Full design in [docs/scheduler-spec.md](docs/scheduler-spec.md).

Three modes (SQLite `scheduled_tasks` table, survives restart):
- `interval` — `<schedule every="30m" max="8">…</schedule>` — fixed count.
- `once` — `<schedule in="2h">…</schedule>` — one-shot.
- `until` — `<schedule every="30m" until="goal">…</schedule>` — **goal loop**: re-runs until the agent emits `<schedule_stop reason="…"/>` (semantic termination, agent judges each round; "done→report+stop, error→fix&continue"). Hard ceiling `max_runs` default **48** + a `schedule_exhausted` event if it never stops.

Both tags (parsed live in `_run_agent` like `<dispatch>`) and the `/schedule` `/track` `/schedules` `/unschedule` slash commands write the same table; `_build_schedule` is the shared core, `_register_schedule` wraps it for the tag path. Interval floor 5 min; 7-day zombie guard.

`_scheduler_loop` (startup task, modelled on `_terminal_reaper`) ticks every 30s, selects `active AND next_run_at<=now`, **skips if the agent already has an active run** (one-active-run-per-agent — defers a short retry, never overlaps), else fires. `next_run_at` is recomputed **at fire completion** (`= completion + interval`), not pre-scheduled, so a long "fix" round can't cause overlap or drift. `_sched_inflight` guards against double-firing the same row across ticks.

**UI feedback is load-bearing**: a scheduled fire starts a server-side run with no browser attached. `app.js` polls `/runs` every 20s (`pollActiveRunsActiveTab`) so the run auto-opens + streams instead of running silently — the exact failure the scheduler exists to remove. Plus toasts + a node 🕒 badge + the **🕒 Schedules dropdown** (`#schedDropdown`, GET `/api/projects/{slug}/schedules`).

**Honesty guard** (`_SCHEDULE_INSTRUCTIONS`): appended **conditionally** (only for agents with active schedules, when the user message shows tracking intent, or during scheduled fires) to save tokens (~524 tokens). The honesty rules and safety-net still apply when the instructions are present.

Endpoints: `GET/POST /api/projects/{slug}/schedules`, `PATCH /…/{id}?active=` (pause/resume), `DELETE /…/{id}`.

> ⚠️ A scheduler firing `claude -p` every 30 min on an OSC **login node** is exactly the persistent-agent activity OSC flagged (killed ~7GB of processes, threatened account restriction). Run AgentUI **off-cluster** before enabling recurring schedules. Not enforced in code.

## SSE event types (backend → frontend)

| Event | Fields | Meaning |
|---|---|---|
| `start` | `agent` | Stream opened. |
| `agent_status` | `agent`, `status` | Node color update (`running` mid-turn, etc). |
| `meta` | `agent`, `data` | Includes `claude_session_id`. Persisted for `--resume`. |
| `status` | `agent`, `status=thinking\|responding` | Drives the "thinking…" indicator. |
| `thinking` | `agent`, `text` | Recent thinking chunk (visible in indicator). |
| `delta` | `agent`, `text` | Assistant text delta. Appended to bubble. |
| `continuation_round` | `agent`, `round`, `max` | Round separator + turn-strip round badge; resets the orchestrator's live bubble. |

| `dispatch_started` | `source`, `target`, `task` | Animate edge, mark target running, create worker card + turn-strip chip. |
| `dispatch_complete` | `source`, `target`, `status`, `message` | Stop animation, mark target ok/error. |
| `dispatch_rejected` | `source`, `target`, `reason` | Show in status bar. |
| `agent_done` | `agent`, `text`, `status` | Final state for that agent. |
| `error` | `agent`, `message` | Surface in bubble as quote. |
| `complete` | — | Queue closed, all dispatches finished. |
| `bootstrap_done` | `files`, `warnings`, `target_folder` | Emitted only by `/agents/preview-from-parent` at end of stream. Frontend uses this to build the preview pane. |
| `schedule_created` | `agent`, `schedule` | A `<schedule>` tag (or `/schedule`) registered a recurring/deferred run. |
| `schedule_fired` | `agent`, `id`, `run`, `n`, `max` | A scheduled fire just started → toast + node 🕒 pulse. |
| `schedule_done` | `agent`, `id`, `reason` | `<schedule_stop>` / one-shot complete. |
| `schedule_exhausted` | `agent`, `id`, `n` | An until-loop hit the hard ceiling without stopping. |

If you add an event type, update both `_run_agent` / `_dispatched_run` (emit) and `handleEventInWindow` in `app.js` (consume).

## Streaming integrity (do not break)

`adapters.py:claude_stream` attaches the child's stdout to a PTY (`pty.openpty`) and sets termios raw mode (`OPOST` cleared) so that:
- Node CLIs do not block-buffer 4 KB before flushing.
- The terminal does not translate `\n` to `\r\n`.

`main.py:api_chat` returns `StreamingResponse` with `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. Don't introduce middleware that buffers.

Default `claude_model` is `claude-sonnet-4-6` — fast and cheap. Opus 4.7 / 4.8 have expensive extended thinking by default; use only for orchestrators if needed.

## Side panels & terminal (UI-only surfaces)

These are presentation surfaces wired to read-only endpoints; they never touch agent
sessions, the dispatch ledger, or orchestration state.

- **Top bar** (`.topbar` wraps `#tabs`). Right-aligned `#procBtn` "Process running" +
  `#skillsBtn` "Skills". Skills was relocated here from the old fixed right-edge tab
  (`.skills-tab` now `display:none`); it opens the same `#skillsPanel`.
- **Schedules dropdown** (`#schedDropdown`, `🕒 Schedules` button). Lists the active
  project's `scheduled_tasks` with a live next-fire countdown + pause/resume/delete.
  This one is NOT purely read-only — pause/delete hit `PATCH`/`DELETE
  /api/projects/{slug}/schedules/{id}`. See the **Scheduler** section.
- **Process running dropdown** (`#procDropdown`). `GET /api/cluster/jobs` runs
  `squeue --clusters=all -u $USER -o '%i|%P|%j|%t|%M|%D|%R|%C'`, parses the `CLUSTER:`
  markers, returns `{clusters:{ascend|cardinal|pitzer:[…]}}`. Polls every 15s while open.
- **Usage widget** (`#usageWidget`, bottom-right). `GET /api/usage` reads the OAuth bearer
  from `~/.claude/.credentials.json` (`.claudeAiOauth.accessToken` — subscription auth, NOT
  an `ANTHROPIC_API_KEY`) and GETs `https://api.anthropic.com/api/oauth/usage` with header
  `anthropic-beta: oauth-2025-04-20`; the JSON body's `five_hour`/`seven_day`
  `{utilization, resets_at}` drive the two bars. Graceful `{available:false}` → "usage n/a".
  Token is never logged or returned. Polls every 60s.
- **Terminal dock** (`#terminalDock`, bottom, VS Code-style). `WebSocket /api/terminal/ws`
  spawns one `bash -l -i` per tab on a PTY (same termios raw-mode trick as
  `adapters.py:claude_stream`); protocol is JSON text `{"t":"i",d}` input / `{"t":"r",cols,rows}`
  resize, raw shell bytes streamed back as binary frames. The child shell is given a CLEAN
  env (pops `VIRTUAL_ENV`/`VIRTUAL_ENV_PROMPT`/`PS1`, strips the venv from PATH, `cd ~`) so the
  user's normal login prompt shows instead of `(.venv)`. Front-end uses xterm.js + fit addon
  from CDN. **Safeguards:** cap 6 shells, kill-on-disconnect, idle reap (`_TERM_IDLE_S`=30min),
  kill-all on `@app.on_event("shutdown")`. Needs the `websockets` package (in
  `requirements.txt`). These are real persistent shells on the login node — bounded but
  remember they exist.

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
- **Cold-start preamble.** Any turn that cannot `--resume` (fresh/torn session) gets a deterministic recap prepended: latest `state/progress.md` sections + `state/children_status.json` (parents) + `inputs/manifest.md` head. Built in `_session_preamble`; a /compact seed takes precedence.
- **Children rollup.** Every parent gets `state/children_status.json` auto-derived on each `/stats` call (and via `POST /api/projects/{slug}/rollup`): per-child status, context %, memory freshness + `stale_memory` flag, sha256 of progress.md. Read-only projection — never hand-edited, never a second source of truth.

- **Continuation is multi-round.** Up to `_MAX_CONT_ROUNDS` = 3 continuations per user turn: each wave of dispatches is gathered, then the orchestrator reacts (synthesise or chain new dispatches) in the same SSE response. The final round's prompt forbids further dispatches; any emitted anyway still run, results land in the ledger for the next turn.
- **Dispatch contract verify.** `_dispatched_run` snapshots the worker's `outputs/manifest.md` (mtime + version) before/after; an unchanged manifest on an ok dispatch appends a `[control-plane verify]` warning into the ledger text so the orchestrator demands a manifest bump before consuming artifact-producing work.
- **Turns are detached from the browser (solved: "send work, close the laptop").** Each chat turn runs as a server-side task publishing to an in-memory per-run event buffer (`_Run` registry in main.py, every event stamped with `seq`). The SSE returned by POST /chat is just the first subscriber — disconnect only unsubscribes, the turn (and all its dispatched workers) runs to completion and persists everything. Re-attach: `GET /api/projects/{slug}/runs` lists active runs (UI calls it on project open and auto-opens those chats), `GET .../agents/{id}/stream?since=N` replays buffered events from `since` then follows live (finished runs stay replayable `_RUN_KEEP_DONE_S` = 10 min). Stopping is ONLY explicit `POST .../agents/{id}/stop` (UI Stop/Esc calls it; local fetch abort merely stops watching). One active run per agent — a second POST /chat gets 409 and the UI queues the message + re-attaches. Caveat: the buffer is in-memory, so a **server restart** still kills in-flight runs (startup reaper marks them `cancelled`). Also note: cron keepalive restarts run.sh with cron's bare PATH — run.sh asserts `~/.local/bin` (claude) and `~/.grok/bin` (grok) explicitly; do not remove that export.
- **Resume guard**. `--resume` is only used when `last_status == "ok"`. Any prior turn that was `running` / `cancelled` / `error` is treated as torn (claude server state may be mid-reply); next turn starts a fresh CLI session. This avoids the "agent silently returns nothing" failure mode after a stop / orphan.
- **SSE heartbeat**. The chat stream emits `: keepalive` every 15s of quiet. Required because Opus extended thinking can sit 10–30s without bytes — without the heartbeat, browsers and proxies close the SSE and the UI shows "stopped" with no response.
- **Add-agent is creation-only**. Edit and delete (with archive) are Sprint 1. To remove an agent today: stop the server, delete the folder + remove the yaml entry by hand, restart.

If you implement any of these, update this section.

## Active user custom modifications (for future agents to understand & intervene)

These changes were made at user request. Documented here so later agents can inspect, revert, extend, or debug without surprise.

### 1. Plan / todo-panel feature — completely removed
- **What was removed**:
  - `_PLAN_INSTRUCTIONS` no longer appended to any agent's system prompt.
  - No parsing of `<plan>...</plan>` or `<step n="..." status="...">` tags in `_run_agent`.
  - No automatic writing / updating of `state/plan.md`.
  - `plans` field removed from `GET /api/projects/{slug}` and per-agent stats.
  - Cold-start preamble no longer injects unfinished plan.
  - Frontend: `state.plans` removed, no more `.todo-panel` foreignObjects rendered next to graph nodes, no `buildTodoPanel` / `updateTodoPanelsLive`, no `plan_updated`/`plan_step` events, no `autoTickStep`.
  - All related CSS deleted.
- **Effects**:
  - Agents will **never** see plan protocol instructions.
  - Emitting `<plan>` or `<step>` tags does nothing (silently ignored).
  - No todo list ever appears on the graph canvas.
  - Existing `state/plan.md` files in agent folders are ignored.
- **Location of removed code** (search for remnants):
  - backend/main.py: PLAN_RE, STEP_RE, _PLAN_INSTRUCTIONS, _read_plan/_write_plan_file etc., plan handling in preamble & stream loop.
  - frontend/app.js: all plan + todo panel logic.
  - styles.css: .todo-panel rules.
- **How to restore** (if needed in future): revert the removal commits / search-replaces. The original design is still described in older versions of this file and scheduler-spec.md (plan part).

**Rationale**: User explicitly requested full removal ("tôi không cần nhìn todo list như vậy") to reduce prompt bloat and UI clutter.

### 2. Global scheduler on/off toggle (Apple-style switch)
- **Feature added**:
  - In the 🕒 **Schedules** dropdown (topbar), the very first element is now a toggle switch labeled "Scheduler".
  - Looks like iOS switch (`.sched-toggle`, `.sched-toggle-slider` in CSS).
- **Backend implementation**:
  - New `settings` table in `agentui.db`.
  - Key: `scheduler_enabled` ("1" = on, "0" = off). Default = on.
  - Helpers: `db.get_setting()`, `db.set_setting()`.
  - `_scheduler_enabled()` helper.
  - `_scheduler_tick()` early-returns if disabled → no fires, no scheduled runs.
  - Schedule creation paths (`_build_schedule`, `api_create_schedule`, tag parsing via `_register_schedule`) reject with "scheduler is globally disabled".
  - New endpoints:
    - `GET /api/scheduler/enabled` → `{ "enabled": true/false }`
    - `POST /api/scheduler/enabled` with body `{ "enabled": true/false }`
- **Frontend**:
  - `renderScheduleDropdown()` fetches status and renders the toggle at the top of the dropdown.
  - Toggling calls the POST endpoint and re-renders.
  - When disabled: shows red warning message in the dropdown. Existing schedules are still listed (so you can delete/pause them), but nothing will fire.
  - Count badge on the 🕒 button only counts active ones (same as before).
- **Effects when off**:
  - No new `<schedule>` / `/schedule` / `/track` will succeed.
  - Scheduler loop sleeps (still wakes every 30s but does nothing).
  - Existing schedules stay in DB but are inert.
  - User can still use the dropdown to delete old ones.
  - Toggle can be turned back on at any time.
- **Files changed**:
  - backend/db.py: settings table + get/set.
  - backend/main.py: enable check, new APIs, guards in creation & tick.
  - frontend/app.js: toggle UI + fetch logic.
  - frontend/styles.css: Apple-style switch CSS.

**Rationale**: User wanted a simple, complete "off switch" for the whole recurring/scheduling system without deleting code, so it can be turned off when not needed (e.g. to avoid token burn or unwanted background activity).

**How future agents can intervene**:
- Check current state: `sqlite3 agentui.db "SELECT * FROM settings WHERE key='scheduler_enabled';"`
- Force on/off from code: call the POST endpoint or directly `db.set_setting("scheduler_enabled", "0")`.
- Re-enable plan/todo if desired: the removal is surgical — search for the deleted symbols above and restore the blocks (plan protocol is still described in the scheduler-spec.md history and older CLAUDE.md).
- Debug scheduler while disabled: look at `_scheduler_enabled()` and the guards in `_build_schedule` / `_scheduler_tick`.

### Notes for agents working on this codebase
- These are **user-driven customizations**, not core design. They take precedence over the original "always-on" descriptions elsewhere in this file.
- When adding new features (new slash commands, new UI panels, new control-plane tags), consider whether they should also respect the scheduler_enabled flag or plan-absence.
- Token-related context: Removing plan saved ~240 tokens per turn. The scheduler toggle allows turning off the ~524-token `_SCHEDULE_INSTRUCTIONS` overhead + all recurring fires when not wanted (see earlier token analysis in conversation).
- Always test with real `/schedule` and the toggle after changes.

---

## Quick debugging

- Backend logs: stdout of `run.sh`. Add `print(...)` freely; uvicorn reloads.
- DB inspect: `sqlite3 agentui.db ".schema"`, then `select * from sessions; select * from messages where session_id=...; select * from agent_overrides;`.
- Test streaming alone (no UI): `python -c "import asyncio; from backend.adapters import claude_stream; ..."`
- Test API: `curl -N -X POST http://127.0.0.1:5174/api/projects/energy/agents/BOSS/chat -H 'Content-Type: application/json' -d '{"message":"hello"}'` (the `-N` disables curl buffering).
- Frontend: DevTools → Network → filter `chat` → click row → EventStream tab shows raw SSE events.
- Stuck "running" status: restart uvicorn; startup reaper auto-flips orphans to `cancelled`.

---

**Bottom line**: this is a personal localhost dashboard for verifying multi-agent orchestration with your own eyes. The graph is the source of truth; if it doesn't light up, dispatch didn't happen. Keep that invariant intact when extending.
