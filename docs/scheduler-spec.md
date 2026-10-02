# Scheduler — implemented behavior

Checked against `app/backend/main.py`, `db.py`, and `app/frontend/app.js` on
2026-09-07. A schedule starts a new agent run through `_start_run`, the same path
used by chat. An agent promising to monitor does not create a schedule by itself.

SLURM submission is asynchronous: after successful `sbatch`, report cluster/job ID
and release the turn. Each monitoring fire should take one status snapshot and exit.

## Modes and tags

| Mode | Registration | Termination |
|---|---|---|
| `interval` | `<schedule every="30m" max="8">Task</schedule>` | Optional max count or explicit pause/delete |
| `once` | `<schedule in="2h">Task</schedule>` | One fire |
| `until` | `<schedule every="30m" until="goal">Task</schedule>` | Stop tag or max count (default 48) |

```text
<schedule every="30m" until="SLURM job 123 finishes">
Take one status snapshot for job 123. If done, report results and emit schedule_stop;
if failed, report the failure or perform the authorized fix; then release the turn.
</schedule>

<schedule_stop reason="job finished; results recorded"/>
```

Durations support seconds/minutes/hours/days. Repeating intervals below 300 seconds
are clamped to 300. An `until` without a valid `every` uses that floor. Ordinary
interval schedules can have no max; goal loops default to 48. The goal is judged
by the model, not an external automatic metric validator.

The tag parser schedules the emitting agent. An `agent="…"` attribute is not
implemented as child targeting. REST creation uses `ScheduleBody.agent_id`.
Stop tags deactivate that agent's active `until` schedules created **before** the
current turn began; they do not stop ordinary interval schedules or instantly cancel
a new goal loop merely echoed in its registration turn.

## Global toggle

`settings.scheduler_enabled` defaults to `"1"`. When off:

- the scheduler tick does nothing;
- new API/tag schedules are rejected;
- existing rows stay stored and can be paused/deleted;
- already-running fires continue unless explicitly stopped.

On the **off → on** transition, `_skip_overdue_on_resume` moves overdue repeating
rows to `now + interval` and retires overdue one-shots. It does not replay the disabled
period. Resuming one paused row via PATCH is separate and only changes its active flag.

## Firing and restart

`_scheduler_loop` ticks every 30 seconds. `_scheduler_tick` selects due active rows,
retires owners removed from the project and rows untouched for more than seven days,
and avoids a second fire for the same row with `_sched_inflight`.

If a root `_Run` for the agent is active, the row is deferred by at most 120 seconds;
at most one fire per agent is scheduled per tick. Direct dispatched workers do not
own separate `_Run` records, so this is not a universal worker lock.

`_run_scheduled_fire` calls `_start_run(..., origin="schedule:<id>")`, waits for it,
records status/count, and either deactivates the row or sets the next time to
`completion + interval`. Failed runs count toward max runs. Error/stop behavior is
not an unlimited automatic retry policy.

Rows survive restart; run buffers do not. With the scheduler left enabled, a due
row is eligible on the next tick after startup (subject to busy/zombie checks), with
no accumulated catch-up burst. If the backend died during a fire, its completion
may not have been booked, so the due work can run again; no exactly-once guarantee.
This differs from explicitly turning the global toggle back on.

## APIs and persistence

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/projects/{slug}/schedules` | GET / POST | List / create |
| `/api/projects/{slug}/schedules/{task_id}?active=true` | PATCH | Resume; false pauses |
| `/api/projects/{slug}/schedules/{task_id}` | DELETE | Deactivate |
| `/api/scheduler/enabled` | GET / POST | Global toggle; POST body `{"enabled": true}` |

SQLite `scheduled_tasks` stores ID, project/agent, prompt, kind, interval, goal,
next/last fire times, last status, count/max, active/origin, created/updated times.
Slash commands `/schedule`, `/track`, `/schedules`, `/unschedule` use these paths.
`GET /api/projects/{slug}` also includes schedules for UI initialization.

## Visibility and limits

The UI polls active-tab `/runs` every 20 seconds, refreshes schedules every 30 seconds,
and updates displayed countdowns every second. Node badges and the Schedules dropdown
show saved schedules. Events include `schedule_created`, `schedule_fired`,
`schedule_done`, `schedule_exhausted`, and registration errors. Pause/delete state is
refreshed through API/UI; there is no generic `schedule_cancelled` emitter in this code.

Completion events can be published after the root run has finished; they remain in
its event buffer but delivery to an already-closed subscriber is not guaranteed.
`scheduled_tasks` plus API refresh is the persisted status source. No durable
`schedule_exhausted` chat message is inserted by the current completion path.

Schedule instructions are injected conditionally for active schedules, scheduled
fires and detected tracking/submission intent. For an explicit user tracking request
with no schedule tag, the driver can give one extra schedule-nudge turn. Off disables
that nudge. Prompt instructions require scheduled work rather than background shell
pollers; this is an agent instruction, not a shell-command enforcement layer.

`app/keepalive.sh` contains host-specific deployment assumptions. Its presence is not
proof of an installed cron job or permission to run recurring work on that host.
See [DEPLOY.md](../DEPLOY.md).
