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

## Using it (daily journey)

```
AgentUI: Open Workspace
   │  (first run only: "setting up Python environment…" ~30s)
   ▼
Webview panel opens inside VSCode  →  sidebar projects + workspace folder tree
   │
   ├─ click a project          → graph window renders its agents
   ├─ click an agent node       → floating chat window opens
   │     ├─ send a task to the orchestrator → streams token-by-token
   │     ├─ <dispatch> tags light the edge + run the worker node live
   │     └─ slash commands: /model /effort /dispatch /focus /status /compact /clear
   ├─ "+ agent"                 → create an agent (template or parent-bootstrapped)
   ├─ "+ project"               → scaffold a new project (writes shared/, sync.sh, project.yaml)
   ├─ click a file in the tree  → floating viewer (markdown / code / image / pdf)
   └─ ⚡ Skills panel            → insert a skill call into the open chat
```

Same control plane as the standalone web app — now docked in VSCode, no browser,
no `run.sh`. The new app features all work through the extension: detached-run
re-attach (`/stream?since=`), per-model context gauge, `/stats` + children rollup,
project/agent scaffolding, and manual/auto `/compact`.

Lifecycle:

| Action | Result |
|---|---|
| Close the webview tab (×) | Backend stops; child `claude`/`node` processes are killed |
| Re-run **Open Workspace** | Fresh backend on a new free port |
| Backend hung / errored | **AgentUI: Restart Backend** |
| Debugging | **AgentUI: Show Backend Logs** (output channel) |
| Close VSCode | `deactivate()` cleans up the backend |

> **`registry.yaml`** ships pointing at macOS paths. On a new machine, edit
> `app/registry.yaml` to your real project paths or the project list is empty.
> `claude` must already be logged in (subscription) — the extension never touches auth.
> Project scaffolding writes a `sync.sh` (bash) — run it via Git Bash/WSL on Windows.

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

## Distribute & publish

### Before any distribution: bundle `app/`

During development the extension references `../app`. That path does **not** exist
on anyone else's machine, so a shared build must carry the backend + frontend
inside the extension folder:

```
# from vscode-extension/
cp -r ../app/backend  ./app/backend
cp -r ../app/frontend ./app/frontend
cp    ../app/registry.yaml ./app/registry.yaml   # then edit to portable paths
# point the extension at the bundled copy:
#   set "agentui.appPath" default, or change appRoot() in src/extension.js to
#   path.join(extensionPath, "app")
```

Each user still needs, on their own machine: **Python 3.10+**, the **`claude` CLI
logged in**, and **Node** (claude is a Node CLI). The extension builds the Python
venv automatically on first run; it cannot provide the subscription login.

### 1) Sideload a `.vsix` (recommended for personal / team use)

```
npm install
npx vsce package          # → agentui-0.1.0.vsix
```

Install: VSCode → Extensions panel → `…` menu → **Install from VSIX…**, or
`code --install-extension agentui-0.1.0.vsix`. No marketplace account needed.
This is the right path for a personal multi-agent tool.

### 2) Publish to the VS Code Marketplace

```
# one-time setup
npm install -g @vscode/vsce
# create a publisher at https://marketplace.visualstudio.com/manage
# create an Azure DevOps Personal Access Token (scope: Marketplace > Manage)
vsce login <publisher>    # paste the PAT
# then, from vscode-extension/
vsce publish              # or: vsce publish minor / patch
```

Requirements the Marketplace enforces: a real `publisher` in `package.json`, an
`icon`, a `repository` field, and a `LICENSE`. Add those before publishing.

> ⚠️ **Consider before publishing publicly.** This tool wraps your personal
> `claude`/`grok` **subscription** CLIs. CLAUDE.md is explicit that this is a
> personal-use control plane and that hosting it against your login is effectively
> reselling. Publishing the *extension* (which runs against each user's *own*
> login) is fine in principle, but make sure the listing makes clear that users
> bring their own subscription + CLI — and never embed your credentials or
> session files. For a small team, the private `.vsix` route above avoids all of
> this. Alternatively use **Open VSX** (`ovsx publish`) if targeting non-Microsoft
> editors like Cursor / VSCodium.

## Verified

- Backend boots and serves natively on Windows (`/api/projects`, `/`, assets → 200).
- ConPTY streams `claude` output incrementally (token-by-token), JSON intact.
- npm shim → `node cli.js` resolution (no cmd.exe metacharacter hazard).
- All webview HTML transforms match `index.html` (`node verify-html.js`).
