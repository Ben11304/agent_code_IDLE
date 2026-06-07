# agent_code_IDLE

Localhost UI control plane for multi-agent workflows. Wraps subscription-backed CLIs (`claude`, `aas`) — no API key needed.

Each project declares a graph of agents in `.agentui/project.yaml`. Orchestrator agents can auto-dispatch tasks to workers via XML tags parsed live from the stream; the user verifies dispatch happens by watching the graph light up. Floating windows let you chat with multiple agents simultaneously, VSCode-style.

## Quick start (local)

```bash
cd app
./run.sh
# → http://127.0.0.1:5174
```

Requires `claude` CLI authenticated (subscription) and optionally `aas` for Grok nodes.

## Deploy on a remote server

See [DEPLOY.md](DEPLOY.md) — SSH tunnel + systemd service.

## Features

- **Graph of agents** with live status (idle / running pulse / ok / error)
- **Floating windows**, draggable, resizable, hidable. Open multiple agents at once.
- **Auto-dispatch**: orchestrator emits `<dispatch agent="X">task</dispatch>`, backend parses live, fires worker, animates edge + worker node
- **Slash commands** in chat input (`/help`, `/clear`, `/model`, `/effort`, `/focus`, `/dispatch`, `/stop`, `/status`)
- **Folder tree** sidebar with ⌥⌘C copy-path shortcut
- **Markdown rendering** of agent output (marked + DOMPurify); dispatch tags become collapsed cards
- **Streaming via PTY** so Claude CLI doesn't block-buffer; tokens arrive in real time
- **Stop button** + Esc to cancel mid-stream, cascade-cancels all in-flight dispatched workers
- **Per-agent overrides** (model + effort) persisted in SQLite, applied next turn

## Add a project

1. Create `.agentui/project.yaml` in the project root.
2. Append the absolute path to `app/registry.yaml`.
3. Reload uvicorn; project appears in the sidebar.

See [CLAUDE.md](CLAUDE.md) for the full architecture, dispatch protocol, SSE event reference, slash command list, and known limitations.

## Stack

- Backend: FastAPI + SQLite, PTY-wrapped `claude -p` subprocess for true streaming
- Frontend: vanilla JS + SVG graph, marked.js for markdown, DOMPurify for sanitization
- Dispatch: `<dispatch agent="WORKER_ID">task</dispatch>` parsed live in the SSE stream
- Models: Claude opus 4.8 / 4.7, sonnet 4.6, haiku 4.5; Grok via `aas`
