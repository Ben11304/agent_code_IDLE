// Standalone check that getWebviewHtml's string/regex replacements all match the
// real app/frontend/index.html. Run with node; not shipped (see .vscodeignore).
const fs = require("fs");
const path = require("path");

const frontDir = path.resolve(__dirname, "..", "app", "frontend");
let html = fs.readFileSync(path.join(frontDir, "index.html"), "utf8");
const base = "http://127.0.0.1:5199";
const n = "TESTNONCE";
const checks = [];

function expect(name, before, after) {
  checks.push({ name, changed: before !== after });
  return after;
}

html = expect("csp", html, html.replace("<head>", `<head>\n  <meta http-equiv="Content-Security-Policy" content="x">`));
html = expect("styles", html, html.replace('href="/styles.css"', `href="STYLES"`));
html = expect("marked", html, html.replace(/<script src="https:\/\/cdn\.jsdelivr\.net\/npm\/marked[^"]*"><\/script>/, `<script nonce="${n}" src="MARKED"></script>`));
html = expect("dompurify", html, html.replace(/<script src="https:\/\/cdn\.jsdelivr\.net\/npm\/dompurify[^"]*"><\/script>/, `<script nonce="${n}" src="PURIFY"></script>`));
html = expect("appjs", html, html.replace('<script src="/app.js"></script>', `PREAMBLE\n  <script nonce="${n}" src="APP"></script>`));

let ok = true;
for (const c of checks) {
  console.log(`${c.changed ? "OK  " : "MISS"}  ${c.name}`);
  if (!c.changed) ok = false;
}
console.log(ok ? "\nALL REPLACEMENTS MATCHED" : "\nSOME REPLACEMENTS MISSED — webview would break");
process.exit(ok ? 0 : 1);
