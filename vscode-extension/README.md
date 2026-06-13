# AgentUI — VSCode Extension

Runs your existing AgentUI control plane **inside VSCode**, cross-platform
(Windows + macOS + Linux). This folder is fully self-contained and **does not
modify `app/`** — the backend and frontend there are referenced read-only.

## How it works

```
VSCode
 └── extension (src/extension.js)
      ├── spawns:  python pybridge/server.py --app-root ../app --port <free>
      │              └── imports the pristine backend (app/backend) and serves it on 127.0.0.1
      └── opens:   a Webview that loads app/frontend/app.js verbatim
                     + media/shim.js rewrites its root-relative URLs to the backend
```

Two thin layers make it cross-platform without touching the main app:

1. **`pybridge/server.py`** — launcher. Puts `app/` on `sys.path`, then on Windows
   installs the Proactor event loop and monkeypatches ConPTY adapters over the
   Unix-only ones (the main `adapters.py` is never edited; empty `fcntl`/`pty`/
   `termios` stubs let it import).
2. **`pybridge/win_adapters.py`** — Windows streaming via ConPTY (pywinpty). A
   plain Windows pipe block-buffers the whole response (verified); ConPTY streams
   incrementally (verified). It also resolves npm's `claude.cmd` shim to its real
   `node cli.js` invocation, so an arbitrary prompt is never re-parsed by cmd.exe.
3. **`media/shim.js`** — injected before `app.js` in the webview; wraps `fetch`
   (covers the streaming-fetch SSE), `EventSource`, and DOM `src`/`href` so the
   frontend's `/api/...` calls reach the backend without changing app.js.

On macOS/Linux nothing is monkeypatched — the original PTY path runs as-is.

## Run it (development)

1. Open **this folder** (`vscode-extension/`) in VSCode.
2. Press **F5** (“Run AgentUI Extension”). A second VSCode window opens.
3. In it: `Ctrl/Cmd+Shift+P` → **AgentUI: Open Workspace**.

First launch creates a managed Python venv and installs deps (one-time, ~30s).
The graph window opens once the backend is healthy.

## Configuration (Settings → AgentUI)

| Setting | Default | Meaning |
|---|---|---|
| `agentui.pythonPath` | _(blank)_ | Python to use. If it already has the deps it's used directly; otherwise a managed venv is built. Blank = auto-detect. |
| `agentui.appPath` | _(blank)_ | Absolute path to the `app` dir (parent of `backend/`). Blank = `../app` next to this extension. |
| `agentui.port` | `0` | Fixed backend port. `0` = pick a free one. |

Commands: **AgentUI: Open Workspace**, **Restart Backend**, **Show Backend Logs**.

## Dependencies

Backend (installed into the managed venv from `pybridge/requirements.txt`):
`fastapi`, `uvicorn[standard]`, `pyyaml`, `ruamel.yaml`, and `pywinpty` (Windows only).

Requires the `claude` CLI (and optionally `grok`) on PATH, same as the standalone app.

## Package as a .vsix

```
npm install
npx vsce package
```

Note: packaging bundles only this folder. For a redistributable build, copy
`app/backend` and `app/frontend` into the extension (or set `agentui.appPath` on
the target machine). For personal local use the default `../app` reference is fine.

## Verified

- Backend boots and serves natively on Windows (`/api/projects`, `/`, assets → 200).
- ConPTY streams `claude` output incrementally (token-by-token), JSON intact.
- npm shim → `node cli.js` resolution (no cmd.exe metacharacter hazard).
- All webview HTML transforms match `index.html` (`node verify-html.js`).
