# agent_code_IDLE — AgentUI

Localhost control plane for multi-agent workflows. Each project declares agents
and their parent/child graph in `.agentui/project.yaml`; AgentUI runs their turns,
streams activity, and persists chat and dispatch results in SQLite.

Documentation checked against the working tree on **2026-09-07**. This describes
available code, not a claim that every integration is configured or running.
See [documentation audit](docs/documentation-audit.md) for evidence and limitations.

## Run

From this repository:

```bash
cd app
./run.sh
# http://127.0.0.1:5174
```

Requires Python 3.10+ and the selected provider runtime/authentication. The launcher
uses Python 3.12 with `uv` when creating a new environment if `uv` is available;
otherwise it uses `python3`. It installs [backend requirements](app/backend/requirements.txt).
`tmux` is required for the persistent terminal feature. Remote access uses an SSH
tunnel; see [DEPLOY.md](DEPLOY.md).

| Adapter (`model`) | Transport | Credentials | Default model in this checkout |
|---|---|---|---|
| `claude` | `claude -p`, PTY streaming | Authenticated Claude CLI | `claude-sonnet-4-6` |
| `codex` | `openai-codex` Python SDK | Saved Codex login | `gpt-5.6-terra` |
| `grok` | `aas` CLI | Configured `aas` runtime | `grok-build` |
| `deepseek` | Claude harness with per-process endpoint override | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` |
| `glm` | Claude harness with per-process endpoint override | `~/.config/glm/env` or `GLM_API_KEY` | `glm-4.6` |

Model names above are application defaults, not a provider availability list.
Claude/Codex reuse local login; DeepSeek/GLM require provider keys. Optional Notion
and Telegram integrations also require credentials.

## Features

- SVG agent graph with persisted node positions, pan/zoom, floating chats and file viewers.
- Real `<dispatch agent="WORKER">task</dispatch>` execution, a durable result ledger,
  and up to three root continuations to synthesize or chain worker results.
- Detached runs: closing the browser stops watching; the backend keeps working.
  Explicit Stop cancels the run and its tracked workers. Backend restart interrupts agent runs.
- Project and agent creation with file preview, generic templates or parent-generated bootstrap.
- Five provider adapters, per-agent model/effort overrides, session resume and compaction.
- Scheduler: interval, one-shot and goal loops, with a persistent global on/off toggle.
  SLURM submission returns the job ID and releases the turn; monitoring uses scheduled snapshots.
- Durable agent memory: slim overviews, manifest version checks, progress excerpts, optional
  owner reconciliation with receipt/hash validation, and optional progress archival.
- Persistent tmux terminals, nested splits, restored browser layouts, reopen and explicit Kill.
- Dashboard and workspace Capabilities drawer: local inventory of plugins, skills and MCP tools;
  isolated Codex policy toggles, prospective skill-use counts, and explicit global deletion.
- Project Papers/Models monitor, scoped Notion reporting, optional Telegram control,
  token/context telemetry, and offline evaluation harnesses.

The graph reports control-plane events. It does not certify artifact correctness.
Drift, dissent and goal acceptance include prompt-level guidance; see the
[implementation handbook](BUILD_HANDBOOK.md) for the actual enforcement boundaries.
The former graph Plan/todo feature is removed.

## Documentation

- [Application usage and configuration](app/README.md)
- [Maintainer guide and runtime invariants](CLAUDE.md)
- [Deployment and backup](DEPLOY.md)
- [Implementation handbook](BUILD_HANDBOOK.md) and [current checklist](BUILD_HANDBOOK_checklist.md)
- [Architecture rationale](system_architech.md)
- [Proposed Memory, Knowledge and Tools architecture](docs/agent-layers-design.md)
- [Dispatch lifecycle](docs/agentui-dispatch-spec.md) and [scheduler](docs/scheduler-spec.md)
- [Agent folder reference](TEMPLATE_AGENT/README.md)
- [Capability inventory](app/capability_inventory/README.md)
- [Notion reporting](notion_report/README.md), [evaluation](app/backend/evaluation_data/README.md)
- [PDF extractor](pdf_extractor/README.md) and [presentation assets](pitch-deck/README.md)
