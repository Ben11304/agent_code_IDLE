# <AGENT_ID> code map

<!-- Local, agent-owned reference: what this agent owns + what it reads read-only.
     Keep it small — it is read at boot. Not a machine-stamped file. -->

## Owns (writes under <AGENT_ID>/)
- outputs/<...>            — <what>
- state/progress.md        — append-only log

## Reads (read-only)
- ../shared/*.md           — project rules (integrity, scope, handoff, glossary)
- ./inputs/manifest.md     — pinned producer versions
- ./inputs/<PRODUCER>.md   — full producer manifest (ON-DEMAND, not at boot)
