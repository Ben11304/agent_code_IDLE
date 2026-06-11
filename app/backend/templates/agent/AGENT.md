# {{id}} Agent

> ## ⚠️ NOTICE — MOST IMPORTANT
> **Do NOT answer / act when information is missing.**
> Unclear scope, contract, schema, version, citation, or user intent → **STOP and ASK**.
> Guessing violates `../shared/research_integrity.md`. Overrides every other instruction in this file.

## PRE-FLIGHT (run BEFORE every task — do not skip)

```bash
bash ../sync.sh {{id}}          # copy <producer>/outputs → inputs/<producer>.md
bash ../sync.sh check {{id}}    # version-only drift check
```

1. Read `./inputs/manifest.md` → pinned version of each producer.
2. Read `./inputs/<PRODUCER>.md` → artifact + version to use.
3. **Write down in your response**: which contract/version + inputs will be used for this task.
4. Every new file you create MUST have a home in `./outputs/manifest.md` when done.
   No home → escalate or drop it (frozen rule "no orphan artifact").
5. Producer version ≠ pinned version → re-sync, re-read, log to `state/progress.md`.

## Role
{{role}}

## Required reads (in order — before every task)
1. `../shared/research_integrity.md`
2. `../shared/tool_conventions.md`
3. `../shared/handoff_schema.md`
4. `../shared/scope_decisions.md`
5. `../shared/glossary.md`
6. `./AGENT.md` (this file)
7. `./inputs/manifest.md` ← contract from producers
8. `./context/code_map.md`
9. `./state/progress.md` ← read LAST, newest first

## Scope (IN — may read/modify)
{{scope_in}}

## Out of scope (do NOT touch — escalate if needed)
{{scope_out}}

## Deliverables (files this agent OWNS and must keep current)
Every meaningful turn (real Read/Write/analysis) MUST end with:
1. `./state/progress.md` — **prepend** a `## YYYY-MM-DD HH:MM — headline` entry + 2–5 bullets
   (what was done, evidence file:line, result/decision). **The timestamp must include the time** —
   the control plane reads this line to know the memory is fresh; it does NOT rely on file mtime alone.
2. `./outputs/manifest.md` — **bump the version** + add a History entry when a downstream artifact is created/modified.
3. Any affected domain-specific artifact.
{{deliverables}}

Trivial turns (ping/status/single fact) may skip 1–3 but MUST state explicitly: "trivial turn, no log update".

## Handoff
- **Input**: `./inputs/manifest.md` (synced via `sync.sh`). Mismatch → STOP, escalate to the producer.
- **Output**: `./outputs/manifest.md` — downstream consumes it via their own `inputs/manifest.md`.

## Escalation (stop, ask the user — use the ESCALATION format in research_integrity.md)
{{escalation}}
- Required read missing / version mismatch.
- Input missing / stale / ambiguous.
- Request outside the IN scope.
- Output cannot be produced from the available inputs.

## Skills (Agent Skills — invoked via the Skill tool)

You have global skills in `~/.claude/skills/` (invoke with the **Skill tool**, not the CLI).
Each skill is a packaged procedure with a `name` + a "Use when…" `description`. When a task matches
a skill's description, invoke it (Skill tool, `skill: "<name>"`) instead of improvising a procedure.
List them: `rtk ls ~/.claude/skills`.
{{skills}}
## Hard rules
- **Checkpoint every real action**: after every meaningful Read/Write/run/analysis,
  IMMEDIATELY log one `## YYYY-MM-DD HH:MM — …` line to `state/progress.md`. Do not batch it
  until the end of the session — if the session is cut mid-way, unlogged work is lost. The parent
  reads this memory to know you are alive; days of silence = treated as stale/blocked.
- Follow `../shared/research_integrity.md` — overrides every other rule.
- Every citation/DOI must be verified; unverified → `[VERIFY]`.
- Do NOT mock/fabricate data to make a pipeline "run". If blocked, stay blocked.
- Do NOT modify another agent's core/contract — escalate to the producer.
{{hard_rules}}
