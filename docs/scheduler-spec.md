# Scheduler — design spec

Recurring / deferred / goal-driven re-invocation of an agent turn. Closes the gap
where `claude -p` (headless) has no way to wake itself up later, so an agent that
"promises to monitor every 30 min" goes silent — there was never any mechanism
behind the promise. A scheduled fire is, by design, **identical to a `POST /chat`**:
it runs through the same `_Run`/`driver`/`_run_agent` path, streams into the agent's
chat window, and persists to the `messages` table — so it is visible and replayable.

## Three modes

| Mode | Stops when | Decided by | Tag / slash |
|---|---|---|---|
| `interval` | `runs_done == max_runs` | counter | `<schedule every="30m" max="8">…` · `/schedule 30m …` |
| `once` | after 1 fire | counter (max=1) | `<schedule in="2h">…` · `/schedule once 2h …` |
| `until` | agent emits `<schedule_stop>` (or hits the hard ceiling) | the **agent**, each round | `<schedule every="30m" until="goal">…` · `/track 30m …` |

`until` is the "check every 30m **until done**; done → report & stop; error → fix
& continue" loop. The termination condition is *semantic* — the agent judges each
round whether the objective is met and self-terminates.

**Note for agents**: A global scheduler on/off toggle was added (see CLAUDE.md "Active user custom modifications"). When the toggle is off, `<schedule>` tags and creation APIs are rejected, and the background loop never fires.

## Tag contract (parsed live in `_run_agent`, like `<dispatch>`)

```
<schedule every="30m" max="8">Task statement.</schedule>      # repeat, stop after 8
<schedule in="2h">Task statement.</schedule>                  # one-shot, fire once after 2h
<schedule every="30m" until="SLURM job 123 finished">         # goal loop
  Check job 123. DONE → report results + <schedule_stop>. FAILED → fix & rerun.
  Still running → note progress (it persists to state/progress.md), you'll be re-invoked.
</schedule>

<schedule_stop reason="job finished, results in results.md"/>  # agent self-terminates its until-loop
```

- `every` / `in` accept `30m | 2h | 90s | 1d`. **Floor: 300s (5 min) for `every`.**
- Default target = the agent that emitted the tag (self-monitor). `agent="ID"` attr
  optional (must be a direct child, same rule as dispatch) — reserved, MVP fires self.
- The graph lights a **🕒 badge + next-fire countdown** on the owning node, so the
  user verifies a real schedule was registered (same "verify on the graph" invariant
  as dispatch — narrating without the tag registers nothing).

## Safety caps (mandatory for `until`)

1. **Hard ceiling** `max_runs` applies even in `until` mode — default **48**
   (~24h at 30m cadence). Ceiling reached without `<schedule_stop>` → auto-deactivate
   + inject a `schedule_exhausted` system message into the chat ("gave up after 48
   checks, still not done").
2. **Zombie guard** — a schedule untouched for **7 days** auto-deactivates.
3. **Interval floor** 5 min, to bound subscription-quota burn.

## Firing

- **One scheduler loop** (startup asyncio task, modelled on `_terminal_reaper`),
  tick every 30s. Selects `active AND next_run_at <= now`.
- **Skip-on-busy**: if `_active_run(slug, agent)` exists, skip this tick (do not
  queue/overlap — respects "one active run per agent"); `next_run_at` is recomputed
  at the *next* completion.
- Fire = `_start_run(slug, agent, wrapped_prompt, origin="schedule:<id>")`.
- **`next_run_at` is recomputed at fire completion** (`= completion_time + interval`),
  not pre-scheduled — avoids drift and overlap when a "fix" round runs long.
- The fire prompt is wrapped: `[SCHEDULED CHECK #k] <prompt>` (+ for `until`: the
  goal, the done→`<schedule_stop>` / error→fix-and-continue semantics, and a reminder
  to write `state/progress.md` so a cold-start round can recover).

## Restart behaviour

Schedules are SQLite-backed → survive restart (the in-memory `_RUNS` do not). On
startup the loop simply resumes reading the table. A `next_run_at` in the past
(server was down at fire time) fires **exactly once** on the first tick, then
returns to normal cadence — no catch-up burst.

## Data model — `scheduled_tasks`

```
id INTEGER PK
project_slug TEXT
agent_id TEXT            -- owner + default fire target
prompt TEXT             -- raw task; wrapped at fire time
kind TEXT               -- 'interval' | 'once' | 'until'
interval_seconds INTEGER -- null for 'once'
until_goal TEXT         -- human-readable goal, only 'until'
next_run_at REAL
last_run_at REAL
last_status TEXT        -- last fire's run status
runs_done INTEGER
max_runs INTEGER        -- hard ceiling (null only for unbounded interval; until→48 default)
active INTEGER          -- 1/0
origin TEXT             -- 'agent' | 'user'
created_at REAL
updated_at REAL
```

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/projects/{slug}/schedules` | GET | list (active + recently-finished) |
| `/api/projects/{slug}/schedules` | POST | create (slash command path) |
| `/api/projects/{slug}/schedules/{id}` | PATCH | pause / resume |
| `/api/projects/{slug}/schedules/{id}` | DELETE | cancel |

`GET /api/projects/{slug}` payload also gains `schedules` so the UI seeds badges on
project open.

## SSE events

| Event | Fields | Meaning |
|---|---|---|
| `schedule_created` | `agent`, `schedule` | a tag/slash registered a schedule |
| `schedule_fired` | `agent`, `id`, `run`, `n`, `max` | a fire just started (→ toast + node pulse) |
| `schedule_done` | `agent`, `id`, `reason` | `<schedule_stop>` / once-complete |
| `schedule_exhausted` | `agent`, `id`, `n` | hit the hard ceiling without stopping |
| `schedule_cancelled` | `agent`, `id` | user paused/deleted |

## UI feedback (the load-bearing part)

`reattachActiveRuns()` currently runs only on project open. A scheduled fire that
starts while you are idle would otherwise run silently — exactly the failure this
whole feature exists to remove. So:

- **Poll `/runs` on `setInterval` (~20s)**, dedup by `run_id`; a scheduled run that
  appears auto-opens/streams its chat window.
- **Toast + node 🕒 pulse + taskbar badge** on `schedule_fired`, even when the chat
  window is closed.
- **🕒 Schedules panel** (modelled on the cluster-jobs dropdown): all schedules in the
  project with next-fire countdown + pause/delete buttons.

## Honesty guard

Appended to the dispatch/system instructions:

> You cannot run background timers or detached processes yourself; a turn ends and
> the CLI exits. To run recurring/deferred work, emit `<schedule …>` — the control
> plane is the only thing that can re-invoke you. If you cannot, say so plainly.
> Never claim you "spawned a process" / "set a timer" to monitor something — that is
> a lie the user catches because the graph shows no 🕒 badge.

## OSC caveat

A scheduler firing `claude -p` every 30 min on an OSC **login node** is exactly the
persistent-agent activity OSC flagged (killed ~7GB of processes, threatened account
restriction). Run AgentUI **off-cluster** (Pi / mini-PC / laptop) before enabling
recurring schedules. (Not enforced in code; documented here and in CLAUDE.md.)
