# agent_code_IDLE

Localhost UI control plane for multi-agent workflows. Wraps subscription-backed CLIs (`claude`, `aas`) — no API key needed.

Each project declares a graph of agents in `.agentui/project.yaml`. Orchestrator agents can auto-dispatch tasks to workers via XML tags parsed live from the stream; the user verifies dispatch happens by watching the graph light up.

## Quick start

```bash
cd app
./run.sh
# → http://127.0.0.1:5174
```

Requires `claude` CLI authenticated (subscription) and optionally `aas` for Grok nodes.

## Add a project

1. Create `.agentui/project.yaml` in the project root.
2. Append the absolute path to `app/registry.yaml`.
3. Reload uvicorn; project appears in the sidebar.

See [CLAUDE.md](CLAUDE.md) for the full architecture, dispatch protocol, SSE event reference, and known limitations.

## Stack

- Backend: FastAPI + SQLite, PTY-wrapped `claude -p` subprocess for true streaming
- Frontend: vanilla JS + SVG graph, marked.js for markdown, DOMPurify for sanitization
- Dispatch: `<dispatch agent="WORKER_ID">task</dispatch>` parsed live in the SSE stream
