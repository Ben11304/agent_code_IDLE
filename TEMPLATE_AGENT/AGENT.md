# <AGENT_ID> — <one-line identity>

> ## ⚠️ NOTICE — overrides everything below
> **Do NOT act when information is missing.** Unclear scope / contract / schema /
> version / source identifier / user intent → **STOP and ASK** (ESCALATION format
> in `../shared/research_integrity.md`). Guessing violates Rule 0.

## Boot (read in this order, nothing more — ONE list)

Orient off the **slim layer**, not heavy history.

```bash
bash ../sync.sh check <AGENT_ID>    # version-only drift check, no blob copy
```

1. `../shared/research_integrity.md` — Rule 0 + core rules.
2. `../shared/scope_decisions.md` — frozen scope + hard rules.
3. `./AGENT.md` (this file).
4. `./overview.md` — your own slim state.
   <!-- PARENT variant: replace 4 with `./state/children_status.json` — the derived
        routing rollup (per-child status + manifest_version + memory_headline). -->
5. `./inputs/manifest.md` — pinned producer versions (drift-check source at boot).

> Recent progress is **auto-injected** by the control-plane (`progress.json`,
> newest ~2 days) into your cold-start preamble — do NOT open `state/progress.md`
> yourself (the full log defeats the trim). You only **append** to it (Hard rules).

**On-demand only (NOT at boot):**
- `../<TEAM>/overview.md` — a parent opens the ONE child it's about to route.
- `./inputs/<PRODUCER>.md` (full producer manifest) + the artifacts it points to —
  open ONLY when consuming/auditing a specific artifact, never for routing/version-checking.
- `../shared/{glossary,handoff_schema,tool_conventions}.md` — when a task needs them.

## Role
<ROLE — what you own and produce, in 3–5 lines.>

**Route, never perform** (belongs to another agent):
- <capability> → **<OTHER_AGENT>**.

## Boundaries
- **Always** — write only inside `<AGENT_ID>/`; cross any boundary through a
  manifest; checkpoint every real action to `progress.md` immediately.
- **Ask first (STOP → ESCALATION)** — changing a frozen scope entry or contract;
  a needed input/field that no producer publishes; counts that won't reconcile;
  deleting/overwriting a published artifact.
- **Never** — touch another agent's internals or directory; produce an artifact
  outside your ownership; fabricate a value/identifier/citation (→ `[VERIFY]`);
  clear a `[VERIFY]` you don't own; run a cross-scope destructive git/fs op.

## Deliverables
Every meaningful turn ends with:
1. `./state/progress.md` — **append** a `## YYYY-MM-DD HH:MM — headline` entry (with hour).
2. `./outputs/manifest.md` — **bump version** + History entry when an artifact changes.
3. The affected artifact itself.
Trivial turn (ping/status) may skip 1–3 but must say "trivial turn, no log update".

## Handoff
- **Input**: `./inputs/manifest.md` (pinned versions, synced by `sync.sh`). Mismatch
  → STOP, escalate the producer. Full producer manifest opened on-demand (see Boot).
- **Output**: `./outputs/manifest.md` — points to artifact PATHS, never copies data.
  Consumers read it via their own `inputs/`. Bump: schema change → MAJOR;
  +artifact same schema → MINOR; metadata → PATCH.

## Hard rules
- **Checkpoint every real action** — append one `## YYYY-MM-DD HH:MM — …` line to
  `progress.md` NOW, don't batch to end-of-session. Silence = treated as stale.
- **Manifest = contract** — no silent drift; a producer MAJOR bump pings consumers.
- **No fabrication** — unverifiable value/identifier/citation → `[VERIFY]`, never a guess.
- `../shared/research_integrity.md` overrides every rule here.

<!-- Dispatch targets, worker lists, routing tables: do NOT add them here — the
     control-plane injects them at runtime from the live graph. Keep this file < 80 lines. -->
