# {{project_name}} — Agent system

{{project_description}}

Multi-agent system orchestrated through AgentUI. Each agent is a self-contained
context boundary, communicating via manifests (no reading each other's internals).

## Agents
{{agent_table}}

## Structure
```
<project>/
├── .agentui/project.yaml     # agent graph declaration (id, role, model, parents)
├── shared/                    # common rules every agent reads at pre-flight
│   ├── research_integrity.md
│   ├── tool_conventions.md    # rtk + aas
│   ├── handoff_schema.md      # manifest format = contract between agents
│   ├── scope_decisions.md     # closed (frozen) scope decisions
│   └── glossary.md            # authoritative names
├── sync.sh                    # pre-flight: copy producer/outputs → consumer/inputs
└── <AGENT>/                   # one folder per agent
    ├── AGENT.md               # system prompt (NOTICE→PRE-FLIGHT→Role→…→Hard rules)
    ├── inputs/manifest.md     # contract received from producers (synced)
    ├── outputs/manifest.md    # artifacts published downstream (versioned)
    ├── context/code_map.md    # files the agent owns/references
    └── state/progress.md      # per-turn log (prepend)
```

## Operating rules
- Communicate only via manifests; bump the version on any contract change.
- Pre-flight `bash sync.sh <AGENT>` every session before acting.
- Missing information → STOP, escalate (see `shared/research_integrity.md`).
