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
> Include HH:MM for ordering/freshness. Cold starts read the newest two activity
> dates from JSON if present, otherwise Markdown. Optional rotation keeps hot
> Markdown as Markdown and archives older entries; JSON migration is explicit.
> Do not read the whole log at boot or write a second independent hot log.
> Trivial turns (ping, single fact) may be skipped.

## {{date}} 00:00 — Bootstrapped via AgentUI
- Parents in graph: {{parents_csv}}.
- Required reads are to be loaded on the first turn (per AGENT.md).
- Waiting for the first real dispatch/task from parent or user.
