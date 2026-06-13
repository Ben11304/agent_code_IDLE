// AgentUI VSCode extension host.
//
// Boots the (pristine) Python backend as a child process bound to 127.0.0.1 on a
// free port, then renders the existing web UI inside a Webview. The frontend's
// app.js is loaded verbatim from ../app/frontend; a small shim (media/shim.js)
// rewrites its root-relative URLs to the backend. Nothing in app/ is modified.

const vscode = require("vscode");
const cp = require("child_process");
const http = require("http");
const net = require("net");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

const IS_WIN = process.platform === "win32";

let panel = null;
let serverProc = null;
let serverPort = 0;
let output = null;

function log(msg) {
  if (output) output.appendLine(`[${new Date().toISOString()}] ${msg}`);
}

// --------------------------------------------------------------------------
// activation
// --------------------------------------------------------------------------

function activate(context) {
  output = vscode.window.createOutputChannel("AgentUI");
  context.subscriptions.push(output);

  context.subscriptions.push(
    vscode.commands.registerCommand("agentui.open", () => openPanel(context)),
    vscode.commands.registerCommand("agentui.showLogs", () => output.show()),
    vscode.commands.registerCommand("agentui.restartBackend", async () => {
      killServer();
      await startServer(context);
      if (panel) panel.webview.html = getWebviewHtml(context, panel.webview);
      vscode.window.showInformationMessage("AgentUI backend restarted.");
    })
  );
}

function deactivate() {
  killServer();
}

// --------------------------------------------------------------------------
// panel
// --------------------------------------------------------------------------

async function openPanel(context) {
  if (panel) {
    panel.reveal();
    return;
  }
  try {
    if (!serverProc) await startServer(context);
  } catch (err) {
    vscode.window.showErrorMessage(`AgentUI: failed to start backend — ${err.message}`);
    output.show();
    return;
  }

  panel = vscode.window.createWebviewPanel(
    "agentui",
    "AgentUI",
    vscode.ViewColumn.Active,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [
        vscode.Uri.file(path.join(context.extensionPath, "media")),
        vscode.Uri.file(appFrontendDir(context)),
      ],
    }
  );
  panel.webview.html = getWebviewHtml(context, panel.webview);
  panel.onDidDispose(() => {
    panel = null;
    killServer();
  }, null, context.subscriptions);
}

// --------------------------------------------------------------------------
// python / deps
// --------------------------------------------------------------------------

function runCapture(cmd, args, opts) {
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let child;
    try {
      child = cp.spawn(cmd, args, Object.assign({ windowsHide: true }, opts || {}));
    } catch (e) {
      resolve({ code: -1, stdout: "", stderr: String(e) });
      return;
    }
    child.stdout && child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr && child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (e) => resolve({ code: -1, stdout, stderr: stderr + String(e) }));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

const DEP_PROBE =
  "import fastapi,uvicorn,yaml,ruamel.yaml" + (IS_WIN ? ",winpty" : "");

async function depsOk(py) {
  const r = await runCapture(py, ["-c", DEP_PROBE]);
  return r.code === 0;
}

async function detectBasePython() {
  const candidates = IS_WIN
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [bin, pre] of candidates) {
    const r = await runCapture(bin, pre.concat(["--version"]));
    if (r.code === 0) return { bin, pre };
  }
  throw new Error("no python found on PATH (set agentui.pythonPath)");
}

// Returns the interpreter to launch the backend with, creating a managed venv
// (one-time) when the configured/system python lacks the dependencies.
async function ensurePython(context) {
  const cfg = vscode.workspace.getConfiguration("agentui");
  const configured = (cfg.get("pythonPath") || "").trim();

  if (configured && (await depsOk(configured))) return configured;

  const venvDir = path.join(context.globalStorageUri.fsPath, "venv");
  const venvPy = IS_WIN
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");

  if (fs.existsSync(venvPy) && (await depsOk(venvPy))) return venvPy;

  const reqPath = path.join(context.extensionPath, "pybridge", "requirements.txt");
  const base = configured ? { bin: configured, pre: [] } : await detectBasePython();

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "AgentUI: setting up Python environment…" },
    async (progress) => {
      fs.mkdirSync(context.globalStorageUri.fsPath, { recursive: true });
      progress.report({ message: "creating venv" });
      let r = await runCapture(base.bin, base.pre.concat(["-m", "venv", venvDir]));
      if (r.code !== 0) throw new Error("venv creation failed: " + (r.stderr || r.stdout));
      progress.report({ message: "upgrading pip" });
      await runCapture(venvPy, ["-m", "pip", "install", "--upgrade", "pip", "--quiet"]);
      progress.report({ message: "installing dependencies (one-time)" });
      r = await runCapture(venvPy, ["-m", "pip", "install", "-r", reqPath, "--quiet"]);
      if (r.code !== 0) throw new Error("pip install failed: " + (r.stderr || r.stdout));
    }
  );

  if (!(await depsOk(venvPy))) throw new Error("dependencies still missing after install");
  return venvPy;
}

// --------------------------------------------------------------------------
// server lifecycle
// --------------------------------------------------------------------------

function appRoot(context) {
  const cfg = vscode.workspace.getConfiguration("agentui");
  const configured = (cfg.get("appPath") || "").trim();
  return configured || path.resolve(context.extensionPath, "..", "app");
}

function appFrontendDir(context) {
  return path.join(appRoot(context), "frontend");
}

function getFreePort(preferred) {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(preferred || 0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(
        { host: "127.0.0.1", port, path: "/api/projects", timeout: 2000 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) return resolve();
          retry();
        }
      );
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error("backend did not become healthy in time"));
      setTimeout(tick, 300);
    };
    tick();
  });
}

async function startServer(context) {
  const root = appRoot(context);
  if (!fs.existsSync(path.join(root, "backend", "main.py"))) {
    throw new Error(`app backend not found at ${root} (set agentui.appPath)`);
  }
  const py = await ensurePython(context);

  const cfg = vscode.workspace.getConfiguration("agentui");
  const fixed = cfg.get("port") || 0;
  serverPort = await getFreePort(fixed);

  const serverPy = path.join(context.extensionPath, "pybridge", "server.py");
  const args = [serverPy, "--app-root", root, "--host", "127.0.0.1", "--port", String(serverPort)];

  log(`spawn: ${py} ${args.join(" ")}`);
  serverProc = cp.spawn(py, args, {
    cwd: root,
    env: process.env,
    detached: !IS_WIN, // own process group on Unix for clean tree-kill
    windowsHide: true,
  });
  serverProc.stdout.on("data", (d) => log("out: " + d.toString().trimEnd()));
  serverProc.stderr.on("data", (d) => log("err: " + d.toString().trimEnd()));
  serverProc.on("exit", (code, sig) => {
    log(`backend exited code=${code} sig=${sig}`);
    serverProc = null;
  });

  await waitForHealth(serverPort, 40000);
  log(`backend healthy on http://127.0.0.1:${serverPort}`);
}

function killServer() {
  if (!serverProc) return;
  const proc = serverProc;
  serverProc = null;
  try {
    if (IS_WIN) {
      cp.spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { windowsHide: true });
    } else {
      try { process.kill(-proc.pid, "SIGTERM"); } catch (e) { proc.kill("SIGTERM"); }
      setTimeout(() => { try { process.kill(-proc.pid, "SIGKILL"); } catch (e) {} }, 2000);
    }
  } catch (e) {
    log("killServer error: " + e);
  }
}

// --------------------------------------------------------------------------
// webview html
// --------------------------------------------------------------------------

function nonce() {
  return crypto.randomBytes(16).toString("hex");
}

function getWebviewHtml(context, webview) {
  const base = `http://127.0.0.1:${serverPort}`;
  const mediaDir = path.join(context.extensionPath, "media");
  const frontDir = appFrontendDir(context);

  const uri = (p) => webview.asWebviewUri(vscode.Uri.file(p)).toString();
  const stylesUri = uri(path.join(frontDir, "styles.css"));
  const appUri = uri(path.join(frontDir, "app.js"));
  const markedUri = uri(path.join(mediaDir, "vendor", "marked.min.js"));
  const purifyUri = uri(path.join(mediaDir, "vendor", "purify.min.js"));
  const shimUri = uri(path.join(mediaDir, "shim.js"));

  const n = nonce();
  const csp = [
    "default-src 'none'",
    `img-src ${webview.cspSource} ${base} https: data: blob:`,
    `media-src ${webview.cspSource} ${base} blob:`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `font-src ${webview.cspSource} ${base} data:`,
    `script-src 'nonce-${n}' ${webview.cspSource}`,
    `connect-src ${base}`,
    `frame-src ${base} ${webview.cspSource} data:`,
  ].join("; ");

  let html;
  try {
    html = fs.readFileSync(path.join(frontDir, "index.html"), "utf8");
  } catch (e) {
    return `<!doctype html><meta charset="utf-8"><body style="font-family:sans-serif;padding:2rem">
      <h2>AgentUI</h2><p>Could not read frontend at <code>${frontDir}</code>.</p>
      <p>Set <code>agentui.appPath</code> to your app directory.</p></body>`;
  }

  // Inject CSP at the top of <head>.
  html = html.replace(
    "<head>",
    `<head>\n  <meta http-equiv="Content-Security-Policy" content="${csp}">`
  );
  // Local stylesheet.
  html = html.replace('href="/styles.css"', `href="${stylesUri}"`);
  // CDN libs -> bundled local copies (nonce'd).
  html = html.replace(
    /<script src="https:\/\/cdn\.jsdelivr\.net\/npm\/marked[^"]*"><\/script>/,
    `<script nonce="${n}" src="${markedUri}"></script>`
  );
  html = html.replace(
    /<script src="https:\/\/cdn\.jsdelivr\.net\/npm\/dompurify[^"]*"><\/script>/,
    `<script nonce="${n}" src="${purifyUri}"></script>`
  );
  // Inject API base + shim BEFORE app.js, then app.js verbatim.
  const preamble =
    `<script nonce="${n}">window.__AGENTUI_API_BASE__=${JSON.stringify(base)};</script>\n` +
    `  <script nonce="${n}" src="${shimUri}"></script>`;
  html = html.replace(
    '<script src="/app.js"></script>',
    `${preamble}\n  <script nonce="${n}" src="${appUri}"></script>`
  );

  return html;
}

module.exports = { activate, deactivate };
