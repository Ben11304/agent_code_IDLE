# AgentUI — Maintainer instructions

Localhost control plane for multi-agent workflows. This file describes the working
tree checked on **2026-09-07**. Read [README.md](README.md) for setup and
[documentation audit](docs/documentation-audit.md) for scope, evidence and known gaps.

## Runtime model

- The durable agent is a SQLite session/history plus provider session identity and
  project-owned files. Claude-family turns spawn `claude -p`; Grok uses `aas`;
  Codex starts/resumes an SDK thread. A running CLI process is not the durable agent.
- `project.yaml` supplies the graph and baseline settings. SQLite model/effort/adapter
  overrides take precedence. Registry and project YAML are read on API requests.
- `claude_session_id` is a historical column name: it can also store a Codex thread
  ID, paired with `cli_adapter`. Resume requires a clean previous status and a
  compatible adapter; a torn session starts fresh with persistent context.
- Graph animations follow real dispatch events. The ledger is durable evidence of
  worker execution; neither the graph nor a successful provider turn certifies an artifact.
- Contracts limit agent ownership through instructions. They are not an OS sandbox.
  The app has no authentication/multi-user isolation and must remain on localhost.

## Source map

| File | Responsibility |
|---|---|
| `app/backend/main.py` | FastAPI routes, run/dispatch/scheduler lifecycle, memory and overview handling, dashboard/resource APIs |
| `app/backend/adapters.py` | Claude/Grok/DeepSeek/GLM/Codex event adapters |
| `app/backend/db.py` | Sessions, messages, overrides, ledgers, schedules, escalation/dissent/watermark state, telemetry |
| `app/backend/projects.py` | Registry/YAML loader and project/agent scaffold |
| `app/backend/templates/` | Runtime templates; `TEMPLATE_AGENT/` is the manual reference |
| `app/backend/progress_store.py` | Progress reading, explicit migration and archival |
| `app/backend/capabilities.py` | Local capability inventory and per-agent Codex policy |
| `app/backend/terminal_service.py` | tmux-backed persistent sessions and transient WebSocket attachments |
| `app/backend/notion_settings.py`, `notion_report/` | Scoped Notion settings, report schemas and MCP |
| `app/backend/telegram_bot.py` | Optional Telegram subscriber/control channel |
| `app/backend/tokens.py`, `agent_log.py`, `fixed_cost.py` | Token usage and CLI activity accounting |
| `app/backend/langsmith_tracing.py`, `*offline_eval.py`, `worker_reliability_eval.py` | Optional tracing and evaluation harnesses |
| `app/frontend/app.js`, `styles.css`, `index.html`, `dashboard.html` | Workspace and Dashboard UI |

## Run and reload

```bash
cd app
./run.sh
# http://127.0.0.1:5174
```

`run.sh` creates `.venv` and installs `backend/requirements.txt`. Python >=3.10 is
required; a new `uv` environment defaults to 3.12. `PORT` changes the port.
The tracked launcher includes site-specific path fallbacks; inspect these when
moving hosts. `app/.env.local` is sourced as shell code, so assignments there may
overwrite existing environment values.

Reload is off by default. `RELOAD=1 ./run.sh` watches **only `backend/`**, with DB
exclusions. Never broaden the watcher to the whole `app/` tree: database writes
would trigger reload during turns. Changes to `notion_report/` require an explicit
restart; registry/YAML changes only need a UI refresh. Frontend responses disable
cache, but an already-open page needs reload to execute changed JavaScript.

A restart interrupts agent runs. Terminal processes survive in tmux. For a port
conflict, identify the listener and stop it through its service manager, or use
another port; see [DEPLOY.md](DEPLOY.md).

## Provider adapters

| Adapter | Model field / checkout default | Auth and transport |
|---|---|---|
| `claude` | `claude_model: claude-sonnet-4-6` | Local Claude login; PTY `claude -p` |
| `grok` | `grok_model: grok-build` | Configured `aas` CLI |
| `deepseek` | `deepseek_model: deepseek-v4-flash` | `DEEPSEEK_API_KEY`; Claude harness |
| `glm` | `glm_model: glm-4.6` | GLM config/key; Claude harness |
| `codex` | `codex_model: gpt-5.6-terra` | Saved Codex login; pinned `openai-codex` SDK |

These names are application defaults, not provider availability/pricing claims.
DeepSeek uses `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com/anthropic`).
GLM reads `~/.config/glm/env` before environment `GLM_API_KEY`/`GLM_BASE_URL`
(default `https://api.z.ai/api/anthropic`). Both inject Anthropic-compatible endpoint
and key variables **per subprocess**, using `_claude_stream_with_overload_retry`.
Do not change global endpoint/auth for ordinary Claude nodes.

Claude uses `--permission-mode bypassPermissions`; native `Agent`/`Task` tools are
blocked so delegation goes through AgentUI. Codex disables `features.multi_agent`.
Codex defaults to sandbox `danger-full-access`, approval policy `never`; overrides
are `AGENTUI_CODEX_SANDBOX_MODE` and `AGENTUI_CODEX_APPROVAL_POLICY`.
`effort` is adapter-dependent; primary Codex turns accept default/low/medium/high/xhigh.

## Streaming and dispatch invariants

Preserve the Claude PTY (`pty.openpty`, raw termios/OPOST handling) and incremental
JSON parsing. `tool_use` arrives after assembling tool input chunks. Do not replace
this path with buffered output collection. SSE headers are `Cache-Control:
no-cache, no-transform` and `X-Accel-Buffering: no`; quiet streams send a heartbeat
about every 15 seconds.

The full [dispatch lifecycle](docs/agentui-dispatch-spec.md) is authoritative:

1. `_start_run` owns a detached root driver and `_Run` event buffer.
2. `_run_agent` recognizes complete `<dispatch agent="ID">task</dispatch>` tags,
   rejects non-child/ancestor targets, and starts `_dispatched_run` tasks.
3. Worker outputs enter `dispatch_results`; the root driver gathers a wave and
   starts up to three continuations to synthesize or dispatch again.
4. Ledger enrichment changes the **provider message**, not the original UI message.
   Results are consumed only after an `ok` turn. Long prompt excerpts retain head
   and tail; full results remain persisted.

Browser disconnect only unsubscribes. Explicit Stop cancels the root driver and
tracked workers. Finished in-memory runs remain replayable for ten minutes; a server
restart loses those buffers and startup marks orphan running sessions cancelled.
`GET /api/projects/{slug}/runs` and `GET .../agents/{id}/stream?since=N` support reattach.

HTTP 409/busy guards apply to root `_Run` records. Direct dispatched workers do not
have their own `_Run`, so this is not a global lock across different parents/chats.
Worker events share the parent stream; a separate worker chat reloads DB messages,
not a standalone live event bus. Native provider delegation must stay disabled.

## Memory, compaction and enforcement

[BUILD_HANDBOOK.md](BUILD_HANDBOOK.md) documents exact schemas and boundaries.

- Cold start: newest two activity dates from JSON, otherwise Markdown; own overview
  and input-contract excerpt; existing children rollup. A compact seed takes precedence.
- Every parent turn, including resume: inject current overviews of all direct children
  via `_children_overview_context`. Preserve machine footer when capping prompt BODY.
- `/stats` and explicit `/rollup` derive `children_status.json`; its `children` is an
  ID-keyed object. Digest comparison avoids rewriting unchanged projections.
- Auto-compaction runs before root and dispatched turns: 70% for Claude/Codex,
  40% for Grok/DeepSeek/GLM. It creates a fresh session with a recap seed.
- Context occupancy prefers per-request usage/iterations. If cumulative usage cannot
  represent occupancy, `_usage_ctx_tokens` returns `None`; fallback is persisted text
  size, which cannot account for every internal tool output.
- Progress rotation defaults **off** and has an independent global toggle. It archives
  older activity dates; Markdown rotation keeps Markdown. JSON migration is explicit.
- Owner memory reconciliation is opt-in per project and validates receipts, hashes and
  pointers before publishing overview provenance. Backend does not write technical prose.
- Version drift, goal acceptance, dissent and verify-delta include **prompt guidance**,
  not general tool/dispatch vetoes. `[HALT]` and `[DISSENT]` are parsed/persisted.
  There is no generic metric reader, plateau detector or enforcement of dispatch `budget`.
- Malformed optional structured output gets one corrective retry. Missing blocks are
  allowed; a second malformed reply has no separate hard rejection branch.

## Project schema and bootstrap

See the executable YAML example in [app/README.md](app/README.md). Agent fields:
`id`, `role`, `model`, provider-specific model, `effort`, `system_prompt_file`, `cwd`,
`parents`, optional `notion_url`. Project fields additionally include `resources`,
`memory`, and `notion_report`. `parents` drives dispatch eligibility; node positions
are independent SQLite state, with auto-layout only seeding unsaved nodes.

`+ agent` offers generic templates or first-parent generation. Both preview six files:
`AGENT.md`, `overview.md`, `inputs/manifest.md`, `outputs/manifest.md`,
`state/progress.md`, `context/code_map.md`. Parent generation uses six `<file path="…">`
blocks; validate paths before writing. `create_agent` rolls back the new directory
if writing/registration fails. Keep role prompts slim, use one boot read-list and
on-demand artifact reads; dispatch targets come from the graph.

Routes: `POST /api/projects/{slug}/agents/preview`, `/agents/preview-from-parent`
(SSE), `/agents`; project preview/create: `POST /api/projects/preview-create` and
`/api/projects/create`. There is no graph-agent delete/archive endpoint; settings,
adapter and capability edits have their own endpoints. Do not delete agent data
as a routine way to remove a node from YAML.

## Capabilities, resources and UI

`capabilities.discover_catalog` reads `app/capability_inventory/inventory.json` and
its referenced packages/manifests. Refresh rereads the snapshot; it does not scan
arbitrary home/cache directories or verify live remote MCP availability.
Per-agent plugin/skill/server/tool policies apply to **Codex** and are compiled into
its ephemeral SDK config without changing global config. A revision clears the
provider identity, while preserving AgentUI history and memory. Explicit inventory
Delete is global: package removal, tombstone, override cleanup and affected runtime resets.
See [inventory guide](app/capability_inventory/README.md).

Skill-use counters are prospective and deduplicate within a Codex turn. An explicit
SDK `SkillInput` or completed read of an inventory-owned `SKILL.md` counts; this
establishes invocation/read, not compliance with all instructions.

The graph fills the workspace. Chat/file windows float above it; node positions,
pan/zoom, minimized windows and terminal layout have distinct persistence paths.
Use CSS theme tokens (`--bg-code`, `--hover`, `--accent-border`, etc.), not hard-coded
colors. Slash commands are listed in [app/README.md](app/README.md); `COMMANDS` in
`app.js` is the source of truth. `/dispatch` is a direct user chat action, not a
parent-emitted dispatch tag.

SSE events include `start`, `agent_status`, `meta`, `status`, `thinking`, `delta`,
`tool_use`, `resource_access`, `skill_use`, `token_turn`, `continuation_round`,
`dispatch_started/complete/rejected`, `agent_done`, `error`, `complete`,
`bootstrap_done` and schedule events. Extend emitter and `handleEventInWindow`
together. `meta` may carry provider identity, init inventory, usage, memory receipts,
escalation/HALT/dissent updates. `status` also includes `reconciling_memory`.

## Scheduler and Telegram

See [scheduler spec](docs/scheduler-spec.md). Schedules persist in SQLite and use
`_start_run`, with 30-second ticks, a five-minute repeat floor and a default 48-fire
cap for goal loops. The UI polls active-tab runs every 20 seconds. Schedule instructions
are conditional on intent/active schedules; SLURM handoff instructions apply every turn.

Telegram is an optional second subscriber to the same run machinery. No
`TELEGRAM_BOT_TOKEN` means disabled; `TELEGRAM_CHAT_IDS` is a fail-closed allow-list.
Default target: `TELEGRAM_AGENT_SLUG=energy`, `TELEGRAM_AGENT_ID=BOSS`.
Commands: `/help /status /stop /clear /model /effort`. Configure through environment
or gitignored `.env.local`; see `app/.env.example`. The bot uses long polling.

## Persistent terminals

- **Terminal dock** (`#terminalDock`, bottom, VS Code-style). Terminal processes live in a
  dedicated tmux server (`tmux -L ${AGENTUI_TMUX_SOCKET:-agentui}`), independently of the
  browser and uvicorn. `WebSocket /api/terminal/ws` owns only a transient PTY-attached tmux
  client; omit `session_id` to create a session or pass it to reopen one. Protocol is JSON
  text `{"t":"i",d}` input / `{"t":"r",cols,rows}` resize, raw terminal bytes streamed back
  as binary frames, plus an initial text `{"t":"meta",session_id,title,persistent:true}`.
  The tmux client is given a clean env (pops `VIRTUAL_ENV`/`VIRTUAL_ENV_PROMPT`/`PS1` and
  strips the venv from PATH), so the user's normal shell prompt shows instead of `(.venv)`.
  tmux `mouse=on` routes the wheel into copy-mode scrollback instead of Bash Up/Down command
  history. AgentUI overrides tmux's default 5-line wheel jump to 1 line and xterm uses a
  160ms smooth-scroll duration. Both tmux and xterm keep 50,000 lines for newly-created panes.
  Although tmux mouse reporting normally owns ordinary drag events, AgentUI maps each plain
  left drag directly onto xterm's public buffer-selection API, so an unmodified drag selects
  text normally. `Ctrl/Cmd+C` when a selection exists or the pane's `⧉` button copies it.
  Existing sessions are upgraded on startup and whenever reopened. The `◫` control toggles
  split view.
  Split mode is a nested row/column layout tree rather than a flat grid: drag a pane's `⠿`
  handle (or its top tab) onto the left/right/top/bottom edge of another pane to place it there;
  dropping in the center swaps the two panes. This permits arbitrary columns, rows, and nested
  grids. Drag the divider between siblings to resize their ratio. Each pane also has quick
  split-right, split-down, and close controls. Single mode shows only the active tab without
  destroying the saved in-memory tree. Every visible xterm is fitted independently.
  Closing a pane is a **hide/detach** action, never a kill. The `☷` process panel polls
  `GET /api/terminal/sessions`, shows visible and background sessions (command, PID, cwd,
  activity), and provides **Open/Focus** and explicit **Kill** actions; Kill calls
  `DELETE /api/terminal/sessions/{session_id}`. Sessions have no 24-hour or other TTL and
  survive a hidden dock, browser disconnect, backend reload, and AgentUI shutdown. Front-end
  sends an empty lease-refresh frame every 60s while a WebSocket is visible; the 30-minute
  attachment reaper only detaches abandoned clients. Shutdown also detaches clients only.
  Front-end uses xterm.js + fit addon from CDN. **Safeguards:** at most 6 simultaneous visible
  attachments, strict 12-hex session IDs, and a dedicated tmux socket so AgentUI cannot list
  or kill unrelated user tmux sessions. The number of detached sessions is intentionally not
  time-limited; the user owns their lifetime through the Kill action.

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
- **How to restore** (if needed in future): revert the removal commits / search-replaces. The original design is only in repository history; the current scheduler spec does not implement a plan protocol.

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
  - Toggle can be turned back on at any time. Overdue repeating schedules move to the next cycle; overdue one-shots are retired. Turning it off does not stop already-running fires.
- **Files changed**:
  - backend/db.py: settings table + get/set.
  - backend/main.py: enable check, new APIs, guards in creation & tick.
  - frontend/app.js: toggle UI + fetch logic.
  - frontend/styles.css: Apple-style switch CSS.

**Rationale**: User wanted a simple, complete "off switch" for the whole recurring/scheduling system without deleting code, so it can be turned off when not needed (e.g. to avoid token burn or unwanted background activity).

**How future agents can intervene**:
- Check current state: `sqlite3 app/agentui.db "SELECT * FROM settings WHERE key='scheduler_enabled';"` (from repository root)
- Force on/off from code: call the POST endpoint or directly `db.set_setting("scheduler_enabled", "0")`.
- Re-enable plan/todo if desired: the removal is surgical — search for the deleted symbols above and restore the blocks (plan protocol is still described in the scheduler-spec.md history and older CLAUDE.md).
- Debug scheduler while disabled: look at `_scheduler_enabled()` and the guards in `_build_schedule` / `_scheduler_tick`.

### 3. Live resource telemetry icons on graph nodes
- Every node shows an **Overview** document icon and an **Internet** globe icon. The root/orchestrator also owns the project-level **Notion** icon, rendered from `logo/Notion_app_logo.png` via `GET /api/ui-assets/notion-logo`.
- Frontend consumes the existing SSE `tool_use` events; no extra model prompt or agent protocol is added.
- Color is based on resource ownership, not read-vs-write:
  - **Green pulse**: the resource owner is reading/writing its own resource (`actor == owner`).
  - **Yellow pulse**: another agent is reading/writing that resource (`actor != owner`).
- `overview.md` ownership is inferred from the tool input/path. Example: BOSS reads `MODEL/overview.md` → the Overview icon on MODEL pulses yellow; MODEL reads/writes the same file → it pulses green.
- The mandatory dispatch prompt explicitly exempts the slim routing-state files `overview.md` and `state/children_status.json`: an orchestrator must read the files required by its own PRE-FLIGHT directly instead of dispatching a child to read them. The exception is read-only across agent boundaries and does not extend to manifests, code, data, configs, or artifacts.
- The Overview icon is interactive: clicking it opens that exact agent's `overview.md` in the existing floating file viewer. Path resolution mirrors `_agent_dir` (`cwd` → system-prompt parent → agent id fallback).
- Resource badges are placed inside an extra 42px `foreignObject` hit-test gutter while remaining visually above the card; this is required because browsers may paint overflow outside an SVG `foreignObject` but ignore pointer events there.
- Notion is treated as a project-level resource owned by the first root agent. A worker accessing Notion therefore pulses the root's Notion icon yellow. Web access belongs to the acting agent and pulses its globe green.
- Activity begins on `tool_use` and clears on the next `thinking`/`responding`, `agent_done`, or `error` event, with a 60-second stale-event failsafe.
- Implementation: `state.resourceActivity`, `recordToolTelemetry()`, resource classifiers/owner inference, and `resourceIconsHtml()` in `frontend/app.js`; animations in `frontend/styles.css`.

### 4. Project Papers / Models monitor
- Every newly scaffolded project gets `paper_collection/` as its canonical paper folder. `paper_collection/CATALOG.md` uses `- **ShortID** — citation · URL` rows and may contain metadata-only papers; local PDF filenames start with the same ShortID. The `.agentui/project.yaml` `resources.papers` block declares both `roots` (local files) and `catalogs` (full inventory). Existing projects can opt into the same convention.
- `GET /api/projects/{slug}/resources` resolves both local PDFs and catalog-only references within the workspace boundary. Only a real local `.pdf` is clickable and opens in the built-in viewer. Metadata-only rows are visibly `MISSING`, disabled, and never open an external URL. A paper row pulses green while an agent tool event references its file, ShortID, DOI, arXiv ID, or URL, with a 2.4-second minimum visibility window so fast completed-tool events remain observable.
- The graph's Notion badge is a real link and opens `resources.notion.url` in a new browser tab. New projects default to `https://www.notion.so/`; set that URL to a project page/database for direct navigation. An optional per-agent `notion_url` overrides the project URL.
- **Unified Notion connector v2 (2026-08-16):** bind one verified parent from Dashboard → **Notion System Hub**. An unbound project then creates/reuses one direct child named `<project name> [<slug>]`, persists a root-owned destination, and all project agents inherit only that subtree. Provisioning is serialized by a file lock. Existing project bindings remain valid.
- AgentUI subprocesses expose only `agentui_notion_report`; raw/global Notion MCP is blocked. The injected control-plane contract forbids agents from asking the user to install/connect another Notion plugin. “All Notion” means `list_notion_project_tree` plus `read_notion_project_page` within the bound project subtree, never the personal workspace.
- Broad report rewrites use `sync_notion_managed_report`: dry-run defaults on, only marker-delimited AgentUI content may be replaced, manual blocks are preserved, and success requires read-back verification.
- Model state is evidence-derived: red `declared` when checkpoint or result evidence is missing; yellow `training` when a configured job-name pattern matches the live federated SLURM queue; green `ready` only when at least one checkpoint and one result file both exist. Active training takes precedence over an older ready state.
- The right-side graph panel uses collapsible, independently scrollable Papers and Models sections. It is hidden for projects without a configured resource inventory.

### 5. Non-blocking SLURM handoff
- `_SLURM_HANDOFF_INSTRUCTIONS` is appended to every primary agent system prompt, regardless of adapter or whether the original request explicitly says “monitor”.
- A successful `sbatch` is the handoff boundary: the agent records/reports cluster + job ID and releases the turn. Foreground `while`/`until`/`for` polling, `watch`, `tail -f`/`tail -F`, and sleep-and-check loops are forbidden, including detached variants.
- One-shot `squeue`/`sacct` snapshots remain valid. Ongoing monitoring must use an AgentUI `<schedule>` goal loop, where each fire performs one snapshot and exits; if Scheduler is disabled, the agent must say so and return.
- `_should_include_schedule_instructions()` also recognizes explicit SLURM submission intent and English `monitor …` phrases. The global **Process running** panel remains an agent-independent live view of active jobs.
- Root cause confirmed on GelSight MODEL session `cb25c701-da31-4fbf-9b8d-4200f1e8ba42`: its CLI event ran `while squeue ...; sleep 30`, keeping the session `running` even though SLURM job `51737633` was independent.

## Maintenance and verification

- Keep the removed Plan/todo protocol absent and preserve the user's global scheduler toggle.
- Check host-specific paths and the `.maintenance` gate before service maintenance;
  [DEPLOY.md](DEPLOY.md) covers the launcher/keepalive distinction.
- Use focused existing tests for changed contracts. Some backend modules initialize DB
  state during import: point tests at temporary DB/registry before importing `main`.
- Read-only API checks can still refresh derived state (`/stats`, `/rollup`). Model
  smoke tests, scheduler fires, Notion writes and remote evaluation runs are separate
  operational actions; they are not required to validate a documentation edit.
- DevTools → Network → EventStream shows SSE; stdout/service journal holds backend logs.
  SQL inspection must use `app/agentui.db` from the repository root.
