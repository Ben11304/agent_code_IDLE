# {{id}} Progress log (newest on top)

> **Convention**: PREPEND a new entry at the TOP every meaningful turn. Format:
>
> ```
> ## YYYY-MM-DD HH:MM — one-line headline
> - bullet 1: what was done (with file:line or evidence)
> - bullet 2: result / decision / numbers
> - bullet 3: open question / follow-up
> ```
>
> The timestamp MUST include the time (HH:MM) — the control-plane reads this line
> for freshness. The control-plane rotates this file into `progress.json` (HOT =
> newest ~2 days, COLD = archive) and auto-injects the recent slice into your
> cold-start preamble — you do NOT read this full file at boot, you only APPEND.
> Trivial turns (ping, single fact) may be skipped.

## {{date}} 00:00 — Bootstrapped via AgentUI
- Parents in graph: {{parents_csv}}.
- Required reads loaded on the first turn (per AGENT.md).
- Waiting for the first real dispatch/task from parent or user.
