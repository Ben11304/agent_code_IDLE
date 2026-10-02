# Dispatch lifecycle — implemented behavior

Checked against `app/backend/main.py`, `db.py`, `adapters.py` and
`app/frontend/app.js` on 2026-09-07.

## From user request to worker result

1. `POST /api/projects/{slug}/agents/{agent_id}/chat` validates the agent and
   rejects an already-active root `_Run` with HTTP 409. `_start_run` creates a
   detached task and an in-memory event buffer; the response is an SSE subscriber.
2. `_run_agent` loads the system prompt, provider settings, pending ledger entries,
   memory notices and every direct child's current overview. Cold starts also get
   a persistent-state preamble; a compaction seed takes precedence over that preamble.
3. The provider emits `<dispatch agent="WORKER">task</dispatch>`. The parser accepts
   only direct children outside the current ancestor chain. Each complete tag is
   deduplicated by text offset and target, not by task meaning.
4. `_dispatched_run` auto-compacts if needed, calls the worker's `_run_agent`, and
   records its output/status in SQLite `dispatch_results`. Worker events use the
   parent's stream; nested dispatches share the tracker.
5. The root driver awaits the worker wave and starts another root turn with
   `[CONTROL-PLANE CONTINUATION n/3]`. It can do this up to three times.
6. `_format_results_as_context` prepends unconsumed results as `<dispatch_result>`
   blocks to the actual provider message. Original UI messages are not replaced
   with fake worker rows. A successful turn marks the selected ledger rows consumed.

A worker's full output remains in the ledger/chat. Prompt formatting caps a long
result at an 8,000-character threshold, retaining 6,800 leading and 1,000 trailing
characters plus a truncation notice. A digest extracts summary/status/goal fields.

The last continuation asks the root to finish without new dispatches. If it still
emits tags, those workers run and persist results, but no fourth root continuation
is created. A separate tracking-intent safety net may add one schedule-nudge turn.
This is not a per-task token budget or a generic plateau detector.

## Cancellation and persistence

Browser disconnect removes only the subscriber. `POST .../stop` cancels the root
task and its tracked workers; cancelled worker outputs are retained when available.
`GET /api/projects/{slug}/runs` lists root runs and `GET .../agents/{id}/stream?since=N`
replays from event index N before following live output. Finished buffers are retained
for ten minutes; SQLite history survives longer. Backend restart loses in-memory
runs/buffers and startup marks orphan running sessions cancelled.

Direct worker dispatches do not create their own `_Run`. The root-run busy check
is therefore not a global worker lock across different parents or direct chats.
A separately opened worker chat reloads persisted messages; there is no independent
live worker event bus. The frontend polls active-tab runs every 20 seconds.

## What is enforced

- Direct-child and ancestor-loop checks reject invalid dispatch targets.
- Claude's native `Agent`/`Task` tools and Codex's native multi-agent feature are
  disabled in their adapters so delegation uses the visible dispatch protocol.
- An unchanged worker manifest mtime adds a publication warning; this does not
  prove a semantic version bump or validate the produced artifact.
- Outcome goals add an external-acceptance instruction to the ledger. The backend
  does not evaluate arbitrary metrics, automatically invoke the named verifier,
  or enforce the payload's `budget` fields.
- Escalations, HALT and DISSENT have persistent records and prompt feedback;
  their exact limits are in [BUILD_HANDBOOK.md](../BUILD_HANDBOOK.md).

The graph is evidence of emitted control-plane events. Artifact correctness still
requires the project's verification process.
