# <AGENT_ID> Progress log (newest on top)

<!-- Convention: PREPEND a `## YYYY-MM-DD HH:MM — headline` block each real action
     (the timestamp MUST include the time — the control-plane reads this line for
     freshness). Append-only. The control-plane rotates this into progress.json
     (HOT = newest ~2 days, COLD = archive) and auto-injects the recent slice into
     your cold-start preamble — you do NOT read this full file at boot, you only
     write to it. -->

## <YYYY-MM-DD HH:MM> — bootstrap
- Agent folder created from TEMPLATE_AGENT.
