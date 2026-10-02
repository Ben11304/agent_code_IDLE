# {{id}} — {{role}}

> ## ⚠️ NOTICE — overrides everything below
> **Do NOT act when information is missing.** Unclear scope / contract / schema /
> version / source identifier / user intent → **STOP and ASK** (ESCALATION format
> in `../shared/research_integrity.md`). Guessing violates Rule 0.

## Boot (read in this order, nothing more — ONE list)

```bash
bash ../sync.sh check {{id}}    # version-only drift check, no blob copy
```

1. `../shared/research_integrity.md` — Rule 0 + core rules.
2. `../shared/scope_decisions.md` — frozen scope + hard rules.
3. `./AGENT.md` (this file).
4. `./overview.md` — your own slim state.
   <!-- PARENT: every direct child overview is injected every turn; existing
        state/children_status.json is included in cold-start context only. -->
5. `./inputs/manifest.md` — pinned producer versions (drift-check source at boot).

> Recent progress is auto-injected on cold start: JSON if present, otherwise
> Markdown, newest two activity dates. Do not read the full log at boot. Add new
> dated entries at the top; optional archival preserves older history.

**Auto-injected for a parent before every turn:**
- Every direct child's `../<TEAM>/overview.md` — current slim state for routing.

**On-demand only (NOT at boot):**
- `./inputs/<PRODUCER>.md` (full producer manifest) + the artifacts it points to —
  open ONLY when consuming/auditing a specific artifact, never for routing/version-checking.
- `../shared/{glossary,handoff_schema,tool_conventions}.md` — when a task needs them.

## Role
{{role}}

## Boundaries
- **Always** — write only inside `{{id}}/`; cross any boundary through a manifest;
  checkpoint every real action to `progress.md` immediately.
- **Ask first (STOP → ESCALATION)** — changing a frozen scope entry or contract; a
  needed input/field no producer publishes; counts that won't reconcile;
  deleting/overwriting a published artifact.
- **Never** — touch another agent's internals or directory; produce an artifact
  outside your ownership; fabricate a value/identifier/citation (→ `[VERIFY]`);
  clear a `[VERIFY]` you don't own; run a cross-scope destructive git/fs op.

### Scope (IN — may read/modify)
{{scope_in}}
### Out of scope (do NOT touch — escalate if needed)
{{scope_out}}

## Deliverables
Every meaningful turn ends with:
1. `./state/progress.md` — **prepend** a `## YYYY-MM-DD HH:MM — headline` entry (with hour).
2. `./outputs/manifest.md` — **bump version** + History entry when an artifact changes.
3. The affected artifact itself.
Trivial turn (ping/status) may skip 1–3 but must say "trivial turn, no log update".
{{deliverables}}

## Handoff
- **Input**: `./inputs/manifest.md` (pinned versions, synced by `sync.sh`). Mismatch
  → STOP, escalate the producer. Full producer manifest opened on-demand (see Boot).
- **Output**: `./outputs/manifest.md` — points to artifact PATHS, never copies data.
  Bump: schema change → MAJOR; +artifact same schema → MINOR; metadata → PATCH.

## Skills
Use skills exposed by the selected runtime. Codex capabilities come from the
AgentUI inventory and this agent's policy; do not assume every global skill is enabled.
{{skills}}

## Hard rules
- **Checkpoint every real action** — add a newest-first `## YYYY-MM-DD HH:MM — …` line to
  `progress.md` NOW, don't batch to end-of-session. Silence = treated as stale.
- **Manifest = contract** — no silent drift; a producer MAJOR bump pings consumers.
- **No fabrication** — unverifiable value/identifier/citation → `[VERIFY]`, never a guess.
- `../shared/research_integrity.md` overrides every rule here.
{{hard_rules}}

<!-- Dispatch targets / worker lists / routing tables: do NOT add them — injected at
     runtime from the live graph. Keep this file < 80 lines. -->
