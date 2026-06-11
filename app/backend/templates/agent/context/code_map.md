# {{id}} Code map

Files/modules this agent owns or references.

## Owned (agent is author/maintainer — may write)
- `./AGENT.md` — role + contract (rarely edited).
- `./inputs/manifest.md` — synced by `sync.sh`, do not edit by hand.
- `./outputs/manifest.md` — bump per the Bump rule after each artifact.
- `./state/progress.md` — prepend every meaningful turn.
- `./context/code_map.md` — this file; update when scope expands.
{{owned_extra}}

## Read-only references (consume, do not modify)
- `../shared/` — research_integrity, tool_conventions, handoff_schema,
  scope_decisions, glossary.
- Upstream agents listed in `./inputs/manifest.md`.

## Out of scope (do NOT touch)
- Other agents' folders.
- Config files at the project root unless listed under Owned.
