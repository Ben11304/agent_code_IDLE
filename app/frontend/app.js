// AgentUI — vanilla JS, floating windows, multi-agent chats.

const state = {
  projects: [],
  openTabs: [],
  activeTab: null,
  projectCache: {},
  skills: [],             // installed Agent Skills (name + description)
  expandedSkills: new Set(),
  statsCache: {},         // slug -> { agentId -> stats }
  initInfo: {},           // slug -> { agentId -> {model, cwd, claude_session_id, tools[]} } from system/init meta
  expandedNodes: new Set(), // "slug:agentId" set of expanded graph panels
  activeDispatches: new Set(),
  viewBoxes: {},          // slug -> {x,y,w,h}
  graphBounds: {},        // slug -> {x,y,w,h}
  nodePositions: {},      // slug -> {agentId: {x,y}} — manual layout, persisted in db
  schedules: {},          // slug -> [ {id, agent_id, kind, next_run_at, ...} ] — recurring/deferred tasks
  tree: {},               // slug -> {expanded, cache, selectedAbs, flat}
  windows: [],            // [{id, projectSlug, type, agentId?, x, y, w, h, z, hidden, el, ...state}]
  zTop: 10,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => (s || "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

// ---------- Init ----------

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("theme", t);
  const btn = $("themeToggle");
  if (btn) btn.textContent = t === "light" ? "◐" : "◑";
}

function bindThemeToggle() {
  applyTheme(localStorage.getItem("theme") || "light");
  const btn = $("themeToggle");
  if (btn) btn.onclick = () =>
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

async function init() {
  bindThemeToggle();
  await loadProjects();
  renderProjectList();
  if (state.projects.length) await openProject(state.projects[0].slug);
  bindGlobalKeys();
  bindTreeKeys();
  await initWorkspaceTree();
  const npb = $("newProjectBtn");
  if (npb) npb.onclick = openNewProjectDialog;
  bindSidebarResizer();
  bindSkillsPanel();
  loadSkills();
  bindProcDropdown();
  bindScheduleDropdown();
  bindTerminalDock();
  startUsageWidget();
  // The load-bearing part of the scheduler: a scheduled fire starts a server-side
  // run with no browser attached. Poll /runs so it auto-opens + streams instead of
  // running silently (the exact failure the scheduler exists to fix).
  setInterval(pollActiveRunsActiveTab, 20000);
  setInterval(refreshSchedulesActiveTab, 30000);
}

// ---------- Scheduler (recurring / deferred / goal-driven agent turns) ----------

function pollActiveRunsActiveTab() {
  if (state.activeTab) reattachActiveRuns(state.activeTab);
}

async function refreshSchedulesActiveTab() {
  const slug = state.activeTab;
  if (!slug) return;
  try {
    const r = await fetch(`/api/projects/${slug}/schedules`);
    if (!r.ok) return;
    state.schedules[slug] = (await r.json()).schedules || [];
  } catch { return; }
  renderScheduleDropdown();
  rerenderGraphsForSlug(slug);
}

function activeSchedulesFor(slug, agentId) {
  return (state.schedules[slug] || []).filter(
    (s) => s.active && s.agent_id === agentId);
}

function fmtCountdown(ts) {
  const secs = Math.round(ts - Date.now() / 1000);
  if (secs <= 0) return "due";
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  if (h >= 1) return `${h}h${m ? m + "m" : ""}`;
  if (m >= 1) return `${m}m`;
  return `${s}s`;
}

function schedLabel(s) {
  if (s.kind === "once") return "once";
  const every = s.interval_seconds >= 3600
    ? Math.round(s.interval_seconds / 3600) + "h"
    : Math.round(s.interval_seconds / 60) + "m";
  if (s.kind === "until") return `every ${every} · until`;
  return `every ${every}${s.max_runs ? ` · ${s.runs_done}/${s.max_runs}` : ""}`;
}

function bindScheduleDropdown() {
  const btn = $("schedBtn"), dd = $("schedDropdown");
  if (!btn || !dd) return;
  const open = () => {
    dd.hidden = false; btn.classList.add("active");
    refreshSchedulesActiveTab();
    state._schedPoll = setInterval(updateScheduleCountdowns, 1000); // live countdown only
  };
  const hide = () => {
    dd.hidden = true; btn.classList.remove("active");
    clearInterval(state._schedPoll); state._schedPoll = null;
  };
  btn.onclick = () => dd.hidden ? open() : hide();
  document.addEventListener("mousedown", (e) => {
    if (dd.hidden) return;
    if (!dd.contains(e.target) && e.target !== btn && !btn.contains(e.target)) hide();
  });
}

async function renderScheduleDropdown() {
  const dd = $("schedDropdown");
  if (!dd || dd.hidden) return;
  const slug = state.activeTab;
  const all = (state.schedules[slug] || []);
  const active = all.filter((s) => s.active);
  const btn = $("schedBtn");
  if (btn) {
    let cnt = btn.querySelector(".sched-count");
    if (!cnt) { cnt = document.createElement("span"); cnt.className = "sched-count"; btn.appendChild(cnt); }
    cnt.textContent = active.length || "";
    cnt.style.display = active.length ? "" : "none";
  }
  // fetch scheduler global state
  let schedEnabled = true;
  try {
    const r = await fetch(`/api/scheduler/enabled`);
    if (r.ok) schedEnabled = (await r.json()).enabled;
  } catch {}
  const toggleHtml =
    `<button id="schedPowerBtn" class="sched-power ${schedEnabled ? "on" : "off"}">`
    + `Scheduler: ${schedEnabled ? "ON" : "OFF"}</button>`;
  let html = `<div class="sched-head">`
    + toggleHtml
    + `<span>Schedules · ${active.length} active</span>`
    + `<button class="sched-refresh">↻</button></div>`;
  if (!schedEnabled) {
    html += `<div class="sched-empty" style="color:var(--run)">Scheduler disabled globally (toggle above to enable).</div>`;
  } else if (!all.length) {
    html += `<div class="sched-empty">no schedules. Use <code>/track 30m &lt;task&gt;</code> or <code>/schedule</code> in a chat.</div>`;
  } else {
    for (const s of all) {
      const cls = s.kind === "until" ? "until" : (s.kind === "once" ? "once" : "interval");
      const when = s.active ? `next ${fmtCountdown(s.next_run_at)}` : "stopped";
      html += `<div class="sched-row ${s.active ? "" : "inactive"}" data-id="${s.id}">`
        + `<span class="sr-dot ${cls}"></span>`
        + `<span class="sr-agent">${escapeHtml(s.agent_id)}</span>`
        + `<span class="sr-kind" title="${escapeHtml(s.until_goal || "")}">${escapeHtml(schedLabel(s))}</span>`
        + `<span class="sr-when">${when}</span>`
        + `<span class="sr-task" title="${escapeHtml(s.prompt || "")}">${escapeHtml((s.prompt || "").slice(0, 60))}</span>`
        + `<span class="sr-actions">`
        + (s.active ? `<button class="sr-pause" title="pause">⏸</button>`
                    : `<button class="sr-resume" title="resume">▶</button>`)
        + `<button class="sr-del" title="delete">✕</button></span>`
        + `</div>`;
    }
  }
  dd.innerHTML = html;
  // Scheduler on/off — a plain <button> using the SAME reliable .onclick path as
  // the pause/resume/delete buttons below. The old iOS-switch (a display:none
  // checkbox toggled via <label> activation) never registered the click.
  const powerBtn = dd.querySelector("#schedPowerBtn");
  if (powerBtn) {
    powerBtn.addEventListener("mousedown", (e) => e.stopPropagation()); // keep dropdown open
    powerBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const next = !schedEnabled;          // schedEnabled = state fetched this render
      powerBtn.disabled = true;
      powerBtn.textContent = "…";
      try {
        const r = await fetch(`/api/scheduler/enabled`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next })
        });
        if (!r.ok) throw new Error("http " + r.status);
      } catch (err) {
        console.error("scheduler toggle failed:", err);
      }
      renderScheduleDropdown();             // re-fetch authoritative state + rebind
    });
  }
  dd.querySelector(".sched-refresh") && (dd.querySelector(".sched-refresh").onclick = refreshSchedulesActiveTab);
  dd.querySelectorAll(".sched-row").forEach((row) => {
    const id = row.dataset.id;
    const pause = row.querySelector(".sr-pause"), resume = row.querySelector(".sr-resume"),
          del = row.querySelector(".sr-del");
    if (pause) pause.onclick = () => patchSchedule(slug, id, false);
    if (resume) resume.onclick = () => patchSchedule(slug, id, true);
    if (del) del.onclick = () => deleteSchedule(slug, id);
  });
}

function updateScheduleCountdowns() {
  const dd = $("schedDropdown");
  if (!dd || dd.hidden) return;
  const slug = state.activeTab;
  const all = (state.schedules[slug] || []);
  dd.querySelectorAll(".sched-row").forEach((row) => {
    const id = row.dataset.id;
    const s = all.find(x => String(x.id) === id);
    if (!s) return;
    const whenEl = row.querySelector(".sr-when");
    if (whenEl) {
      const when = s.active ? `next ${fmtCountdown(s.next_run_at)}` : "stopped";
      whenEl.textContent = when;
    }
  });
}

async function patchSchedule(slug, id, active) {
  try { await fetch(`/api/projects/${slug}/schedules/${id}?active=${active}`, { method: "PATCH" }); }
  catch {}
  await refreshSchedulesActiveTab();
}

async function deleteSchedule(slug, id) {
  try { await fetch(`/api/projects/${slug}/schedules/${id}`, { method: "DELETE" }); }
  catch {}
  await refreshSchedulesActiveTab();
}

// transient corner toast (scheduled fires surface here even with chats closed)
function showToast(msg, kind) {
  let host = $("toastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "toastHost"; host.className = "toast-host";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " toast-" + kind : "");
  t.textContent = msg;
  host.appendChild(t);
  setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 400); }, 5200);
}

// ---------- Skills panel (right side, manual use) ----------

async function loadSkills() {
  try {
    const r = await fetch("/api/skills");
    if (r.ok) state.skills = (await r.json()).skills || [];
  } catch { /* ignore */ }
  renderSkills();
}

function bindSkillsPanel() {
  const tab = $("skillsTab"), panel = $("skillsPanel"), close = $("skillsClose");
  const btn = $("skillsBtn");
  if (!panel) return;
  const open = () => { panel.classList.add("open"); if (btn) btn.classList.add("active"); };
  const hide = () => { panel.classList.remove("open"); if (btn) btn.classList.remove("active"); };
  const toggle = () => panel.classList.contains("open") ? hide() : open();
  if (tab) tab.onclick = open;       // legacy edge tab (now hidden via CSS)
  if (btn) btn.onclick = toggle;     // top-bar trigger
  if (close) close.onclick = hide;
}

function renderSkills() {
  const root = $("skillsList");
  if (!root) return;
  root.innerHTML = "";
  if (!state.skills.length) {
    root.innerHTML = `<div class="skills-empty">No skills found in ~/.claude/skills</div>`;
    return;
  }
  state.skills.forEach((s) => {
    const expanded = state.expandedSkills.has(s.name);
    const row = document.createElement("div");
    row.className = "skill-row" + (expanded ? " expanded" : "");
    row.innerHTML = `
      <div class="skill-top">
        <span class="skill-name">${escapeHtml(s.name)}</span>
        <button class="skill-expand" title="${expanded ? "collapse" : "show full"}">${expanded ? "▾" : "▸"}</button>
      </div>
      <div class="skill-desc">${escapeHtml(s.description || "(no description)")}</div>
      <div class="skill-actions"><button class="skill-use">Use ↗</button></div>`;
    row.querySelector(".skill-expand").onclick = () => {
      if (state.expandedSkills.has(s.name)) state.expandedSkills.delete(s.name);
      else state.expandedSkills.add(s.name);
      renderSkills();
    };
    row.querySelector(".skill-use").onclick = () => useSkill(s.name);
    root.appendChild(row);
  });
}

// ---------- Process running (SLURM jobs across clusters) ----------

function bindProcDropdown() {
  const btn = $("procBtn"), dd = $("procDropdown");
  if (!btn || !dd) return;
  const open = () => {
    dd.hidden = false; btn.classList.add("active");
    loadProcJobs();
    state._procPoll = setInterval(loadProcJobs, 15000);  // refresh while open only
  };
  const hide = () => {
    dd.hidden = true; btn.classList.remove("active");
    clearInterval(state._procPoll); state._procPoll = null;
  };
  btn.onclick = () => dd.hidden ? open() : hide();
  // click-away closes the dropdown
  document.addEventListener("mousedown", (e) => {
    if (dd.hidden) return;
    if (!dd.contains(e.target) && e.target !== btn && !btn.contains(e.target)) hide();
  });
}

async function loadProcJobs() {
  const dd = $("procDropdown");
  if (!dd || dd.hidden) return;
  if (!dd.dataset.loaded) dd.innerHTML = `<div class="proc-empty">loading jobs…</div>`;
  try {
    const r = await fetch("/api/cluster/jobs");
    const data = await r.json();
    renderProcJobs(data);
    dd.dataset.loaded = "1";
  } catch (e) {
    dd.innerHTML = `<div class="proc-error">failed to query clusters: ${escapeHtml(String(e))}</div>`;
  }
}

function procStateClass(st) {
  const s = (st || "").toUpperCase();
  if (s === "R" || s === "RUNNING" || s === "CG") return "run";
  if (s === "PD" || s === "PENDING") return "pend";
  if (s === "F" || s === "FAILED" || s === "TO" || s === "NF" || s === "CA") return "err";
  return "";
}

function renderProcJobs(data) {
  const dd = $("procDropdown");
  if (!dd) return;
  const clusters = (data && data.clusters) || {};
  const names = Object.keys(clusters);
  const total = names.reduce((n, c) => n + clusters[c].length, 0);
  const btn = $("procBtn");
  if (btn) {
    let cnt = btn.querySelector(".proc-count");
    if (!cnt) { cnt = document.createElement("span"); cnt.className = "proc-count"; btn.appendChild(cnt); }
    cnt.textContent = total;
  }
  let html = `<div class="proc-head"><span>SLURM jobs · ${total} active</span>`
    + `<button class="proc-refresh">↻ refresh</button></div>`;
  if (data && data.error) {
    html += `<div class="proc-error">${escapeHtml(data.error)}</div>`;
  } else if (!total) {
    html += `<div class="proc-empty">no active jobs across ${names.length || 3} clusters</div>`;
  } else {
    for (const c of names) {
      const jobs = clusters[c];
      html += `<div class="proc-cluster"><div class="proc-cluster-name">${escapeHtml(c)} · ${jobs.length}</div>`;
      if (!jobs.length) { html += `<div class="proc-empty">—</div>`; }
      for (const j of jobs) {
        html += `<div class="proc-job">`
          + `<span class="pj-dot ${procStateClass(j.state)}" title="${escapeHtml(j.state || "")}"></span>`
          + `<span class="pj-id">${escapeHtml(j.id || "")}</span>`
          + `<span class="pj-name" title="${escapeHtml((j.name || "") + " · " + (j.reason || ""))}">${escapeHtml(j.name || "")}</span>`
          + `<span class="pj-meta">${escapeHtml(j.state || "")} ${escapeHtml(j.time || "")}</span>`
          + `</div>`;
      }
      html += `</div>`;
    }
  }
  dd.innerHTML = html;
  const rb = dd.querySelector(".proc-refresh");
  if (rb) rb.onclick = () => { dd.dataset.loaded = ""; loadProcJobs(); };
}

// ---------- Usage widget (Claude subscription rate limits) ----------

function startUsageWidget() {
  if (!$("usageWidget")) return;
  loadUsage();
  setInterval(loadUsage, 60000);
}

async function loadUsage() {
  const el = $("usageWidget");
  if (!el) return;
  let data;
  try { data = await (await fetch("/api/usage")).json(); }
  catch { data = { available: false }; }
  el.hidden = false;
  const body = (!data || !data.available)
    ? `<div class="usage-na" title="${escapeHtml((data && data.reason) || "unavailable")}">usage n/a</div>`
    : usageRow("5 hrs", data.five_hour) + usageRow("weekly", data.weekly);
  el.innerHTML = body + await tokenToggleHtml();
  wireTokenToggle(el);
}

// Global switch for exact token accounting. When on, every turn that completes gets its
// CLI transcript scanned and the real per-request usage booked into token_turns.
async function tokenToggleHtml() {
  let on = false;
  try { on = !!(await (await fetch("/api/tokens/enabled")).json()).enabled; } catch {}
  return `<label class="usage-row tok-toggle" title="Count exact input/output tokens from the CLI transcript (free, no API key). Applies from the next turn on.">
    <span class="u-label">count tokens</span>
    <span class="tok-switch${on ? " on" : ""}"><span class="tok-knob"></span></span>
    <input type="checkbox" id="tokToggle" ${on ? "checked" : ""} hidden>
  </label>`;
}

function wireTokenToggle(el) {
  const box = el.querySelector("#tokToggle");
  const lbl = el.querySelector(".tok-toggle");
  if (!box || !lbl) return;
  lbl.onclick = async (e) => {
    e.preventDefault();
    const next = !box.checked;
    try {
      await fetch("/api/tokens/enabled", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      showToast(next ? "🧮 token counting ON — applies from the next turn"
                     : "🧮 token counting OFF", "sched");
    } catch {}
    loadUsage();
  };
}

function usageRow(label, p) {
  if (!p || p.pct == null)
    return `<div class="usage-row"><span class="u-label">${label}</span><span class="u-na">n/a</span></div>`;
  const pct = Math.max(0, Math.min(100, p.pct));
  const cls = pct >= 90 ? "high" : pct >= 70 ? "mid" : "low";
  return `<div class="usage-row">
    <div class="u-top">
      <span class="u-label">${label}</span>
      <div class="u-bar"><div class="u-fill ${cls}" style="width:${pct}%"></div></div>
      <span class="u-pct">${pct}%</span>
    </div>
    ${p.reset_at ? `<span class="u-reset">${fmtReset(p.reset_at)}</span>` : ""}
  </div>`;
}

function fmtReset(ts) {
  const secs = ts - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "reset soon";
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  if (h >= 24) return "reset " + Math.floor(h / 24) + "d";
  if (h >= 1) return "reset " + h + "h" + (m ? m + "m" : "");
  return "reset " + m + "m";
}

// ---------- Terminal dock (VS Code-style, real PTY over WebSocket) ----------

function bindTerminalDock() {
  const dock = $("terminalDock");
  if (!dock) return;
  state.terminals = [];
  state.termSeq = 0;
  dock.innerHTML = `
    <div class="term-resize" id="termResize" title="drag to resize"></div>
    <div class="term-bar">
      <span class="term-label" id="termLabel">▴ Terminal</span>
      <div class="term-tabs" id="termTabs"></div>
      <button class="term-add" id="termAdd" title="new terminal">+ new</button>
      <button class="term-collapse" id="termCollapse" title="show / hide terminal">▴</button>
    </div>
    <div class="term-panes" id="termPanes"></div>`;
  $("termAdd").onclick = () => newTerminal();
  $("termCollapse").onclick = () => toggleTerminalDock();
  $("termLabel").onclick = () => toggleTerminalDock();
  bindTermResize();
  window.addEventListener("resize", () => {
    const act = state.terminals && state.terminals.find((t) => t.paneEl.classList.contains("active"));
    if (act) { act.fit.fit(); sendResize(act); }
  });
}

function toggleTerminalDock(forceOpen) {
  const dock = $("terminalDock");
  const collapsed = forceOpen === undefined ? !dock.classList.contains("collapsed") : !forceOpen;
  dock.classList.toggle("collapsed", collapsed);
  const btn = $("termCollapse");
  if (btn) btn.textContent = collapsed ? "▴" : "▾";
  const lbl = $("termLabel");
  if (lbl) lbl.textContent = (collapsed ? "▴" : "▾") + " Terminal";
  if (!collapsed) {
    if (!state.terminals.length) { newTerminal(); return; }
    const act = state.terminals.find((t) => t.paneEl.classList.contains("active")) || state.terminals[0];
    if (act) setTimeout(() => { act.fit.fit(); sendResize(act); act.term.focus(); }, 50);
  }
}

function newTerminal() {
  const dock = $("terminalDock");
  if (dock.classList.contains("collapsed")) toggleTerminalDock(true);
  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
    flashHint("xterm.js not loaded (check network / CDN)"); return;
  }
  if (state.terminals.length >= 6) { flashHint("terminal limit (6) reached"); return; }

  const id = ++state.termSeq;
  const tabEl = document.createElement("div");
  tabEl.className = "term-tab";
  tabEl.innerHTML = `<span>term ${id}</span><span class="tt-close" title="close">×</span>`;
  $("termTabs").appendChild(tabEl);
  const paneEl = document.createElement("div");
  paneEl.className = "term-pane";
  $("termPanes").appendChild(paneEl);

  const term = new Terminal({
    fontSize: 12, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    cursorBlink: true, theme: { background: "#1d212c", foreground: "#e6e8ee", cursor: "#c2c7d0" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(paneEl);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/terminal/ws`);
  ws.binaryType = "arraybuffer";
  const rec = { id, term, fit, ws, tabEl, paneEl };
  state.terminals.push(rec);

  ws.onopen = () => { fit.fit(); sendResize(rec); term.focus(); };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") term.write(e.data);
    else term.write(new Uint8Array(e.data));
  };
  ws.onclose = () => { try { term.write("\r\n\x1b[2m[session closed]\x1b[0m\r\n"); } catch {} };
  ws.onerror = () => { try { term.write("\r\n\x1b[31m[connection error]\x1b[0m\r\n"); } catch {} };
  term.onData((d) => { if (ws.readyState === 1) ws.send(JSON.stringify({ t: "i", d })); });

  tabEl.onclick = (e) => {
    if (e.target.classList.contains("tt-close")) { e.stopPropagation(); closeTerminal(rec); }
    else activateTerminal(rec);
  };
  activateTerminal(rec);
}

function sendResize(rec) {
  if (rec.ws.readyState === 1 && rec.term.cols)
    rec.ws.send(JSON.stringify({ t: "r", cols: rec.term.cols, rows: rec.term.rows }));
}

function activateTerminal(rec) {
  state.terminals.forEach((t) => {
    t.tabEl.classList.toggle("active", t === rec);
    t.paneEl.classList.toggle("active", t === rec);
  });
  setTimeout(() => { rec.fit.fit(); sendResize(rec); rec.term.focus(); }, 30);
}

function closeTerminal(rec) {
  try { rec.ws.close(); } catch {}
  try { rec.term.dispose(); } catch {}
  rec.tabEl.remove(); rec.paneEl.remove();
  state.terminals = state.terminals.filter((t) => t !== rec);
  if (state.terminals.length) activateTerminal(state.terminals[state.terminals.length - 1]);
}

function bindTermResize() {
  const handle = $("termResize"), dock = $("terminalDock");
  if (!handle) return;
  let startY, startH;
  const onMove = (e) => {
    const dy = startY - e.clientY;
    const h = Math.max(120, Math.min(window.innerHeight * 0.8, startH + dy));
    dock.style.setProperty("--term-h", h + "px");
    const act = state.terminals.find((t) => t.paneEl.classList.contains("active"));
    if (act) { act.fit.fit(); sendResize(act); }
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  handle.addEventListener("mousedown", (e) => {
    startY = e.clientY;
    startH = $("termPanes").offsetHeight || 280;
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    e.preventDefault();
  });
}

function focusedChatWindow() {
  const chats = state.windows.filter((w) => w.type === "chat" && !w.hidden);
  if (!chats.length) return null;
  return chats.reduce((a, b) => (b.z > a.z ? b : a));
}

function useSkill(name) {
  const w = focusedChatWindow();
  if (!w) { flashHint("Open an agent chat first (click a node in the graph)."); return; }
  const input = w.el.querySelector(".chat-input");
  if (!input) return;
  const prefix = input.value.trim() ? input.value.trim() + "\n" : "";
  input.value = `${prefix}Use skill \`${name}\` for: `;
  focusWindow(w);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  flashHint(`inserted \`${name}\` call into ${w.agentId} chat — edit & send`);
}

function bindSidebarResizer() {
  const app = document.querySelector(".app");
  const rez = $("sidebarResizer");
  if (!app || !rez) return;
  const MIN = 170, MAX = 620;
  const saved = parseInt(localStorage.getItem("sidebarW") || "", 10);
  if (saved >= MIN && saved <= MAX) app.style.setProperty("--sidebar-w", saved + "px");
  let dragging = false;
  rez.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragging = true;
    rez.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(MAX, e.clientX));
    app.style.setProperty("--sidebar-w", w + "px");
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    rez.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const w = parseInt(getComputedStyle(app).getPropertyValue("--sidebar-w"), 10);
    if (w) localStorage.setItem("sidebarW", w);
  });
  // Double-click resets to default width.
  rez.addEventListener("dblclick", () => {
    app.style.removeProperty("--sidebar-w");
    localStorage.removeItem("sidebarW");
  });
}

async function loadProjects() {
  const r = await fetch("/api/projects");
  state.projects = (await r.json()).projects || [];
}

function renderProjectList() {
  const root = $("projectList");
  root.innerHTML = "";
  state.projects.forEach((p) => {
    const div = document.createElement("div");
    div.className = "project-card" + (state.activeTab === p.slug ? " active" : "");
    div.innerHTML = `<div class="name">${escapeHtml(p.name)}</div>
      <div class="meta">${p.agent_count} agents</div>`;
    div.onclick = () => openProject(p.slug);
    root.appendChild(div);
  });
}

async function openProject(slug) {
  if (!state.openTabs.includes(slug)) state.openTabs.push(slug);
  state.activeTab = slug;
  if (!state.projectCache[slug]) {
    const r = await fetch(`/api/projects/${slug}`);
    state.projectCache[slug] = await r.json();
    state.nodePositions[slug] = { ...(state.projectCache[slug].positions || {}) };
    state.schedules[slug] = state.projectCache[slug].schedules || [];
  }
  renderTabs();
  renderProjectList();
  ensureGraphWindow(slug);
  applyTabVisibility();
  reattachActiveRuns(slug);
}

function closeTab(slug) {
  state.openTabs = state.openTabs.filter((s) => s !== slug);
  if (state.activeTab === slug) {
    state.activeTab = state.openTabs[state.openTabs.length - 1] || null;
  }
  // remove all windows for this project
  for (const w of state.windows.filter((w) => w.projectSlug === slug)) {
    closeWindow(w, true);
  }
  renderTabs();
  renderProjectList();
  applyTabVisibility();
  renderTree();
}

function renderTabs() {
  const root = $("tabs");
  root.innerHTML = "";
  state.openTabs.forEach((slug) => {
    const proj = state.projectCache[slug];
    const tab = document.createElement("div");
    tab.className = "tab" + (state.activeTab === slug ? " active" : "");
    tab.innerHTML = `<span>${escapeHtml(proj ? proj.name : slug)}</span>
      <span class="close">×</span>`;
    tab.onclick = (e) => {
      if (e.target.classList.contains("close")) {
        e.stopPropagation();
        closeTab(slug);
      } else {
        state.activeTab = slug;
        renderTabs();
        renderProjectList();
        applyTabVisibility();
        renderTree();
      }
    };
    root.appendChild(tab);
  });
}

// ---------- Window manager ----------

function applyTabVisibility() {
  const slug = state.activeTab;
  for (const w of state.windows) {
    if (w.projectSlug !== slug || w.hidden) {
      w.el.style.display = "none";
    } else {
      w.el.style.display = "";
    }
  }
  const anyVisible = state.windows.some((w) => w.projectSlug === slug && !w.hidden);
  $("canvasEmpty").style.display = anyVisible ? "none" : "flex";
  renderTaskbar();
}

function nextZ() { state.zTop += 1; return state.zTop; }

function focusWindow(w) {
  if (w.type === "graph") return; // canvas layer: always at the bottom, never raised
  w.z = nextZ();
  w.el.style.zIndex = w.z;
  for (const x of state.windows) {
    if (x.type === "graph") continue;
    x.el.classList.toggle("focused", x === w);
  }
}

function hideWindow(w) {
  w.hidden = true;
  w.el.style.display = "none";
  renderTaskbar();
}

function showWindow(w) {
  w.hidden = false;
  if (w.projectSlug === state.activeTab) w.el.style.display = "";
  focusWindow(w);
  renderTaskbar();
}

function closeWindow(w, silent) {
  // tear down per-type
  if (w.type === "chat" && w.streaming && w.abortController) {
    try { w.abortController.abort(); } catch {}
  }
  w.el.remove();
  state.windows = state.windows.filter((x) => x !== w);
  if (!silent) renderTaskbar();
}

function renderTaskbar() {
  const tb = $("taskbar");
  tb.innerHTML = "";
  const hidden = state.windows.filter((w) => w.projectSlug === state.activeTab && w.hidden);
  hidden.forEach((w) => {
    const it = document.createElement("div");
    it.className = "taskbar-item";
    it.innerHTML = `<span>${escapeHtml(windowTitle(w))}</span>
      <span class="close" title="close">×</span>`;
    it.onclick = (e) => {
      if (e.target.classList.contains("close")) {
        e.stopPropagation();
        closeWindow(w);
      } else {
        showWindow(w);
      }
    };
    tb.appendChild(it);
  });
}

function windowTitle(w) {
  const proj = state.projectCache[w.projectSlug];
  const projName = proj ? proj.name : (w.projectSlug || "");
  if (w.type === "graph") return `${projName} • graph`;
  if (w.type === "chat") return `${projName} • ${w.agentId}`;
  if (w.type === "file") {
    const name = (w.rel_path || "").split("/").pop() || w.rel_path || "file";
    return name;
  }
  return w.id;
}

function createWindowDom(w) {
  const root = $("windowsRoot");
  // The graph is not a floating window: it is the workspace canvas itself,
  // full-bleed behind every other window, with no chrome.
  if (w.type === "graph") {
    const el = document.createElement("div");
    el.className = "graph-canvas";
    el.dataset.id = w.id;
    el.style.zIndex = 0; // pin canvas below every floating chat/file window
    root.appendChild(el);
    w.el = el;
    w.contentEl = el;
    renderWindowContent(w);
    return;
  }
  const el = document.createElement("div");
  el.className = "window focused";
  el.dataset.id = w.id;
  el.style.left = w.x + "px";
  el.style.top = w.y + "px";
  el.style.width = w.w + "px";
  el.style.height = w.h + "px";
  el.style.zIndex = w.z;
  el.innerHTML = `
    <div class="window-titlebar">
      <span class="window-title">${escapeHtml(windowTitle(w))}<span class="badge">${w.type}</span></span>
      <div class="window-controls">
        <button class="window-btn hide" title="hide (minimize)">—</button>
        <button class="window-btn close" title="close">×</button>
      </div>
    </div>
    <div class="window-content"></div>
    <div class="window-resize" title="resize"></div>
  `;
  root.appendChild(el);
  w.el = el;
  w.contentEl = el.querySelector(".window-content");

  bindWindowChrome(w);
  renderWindowContent(w);
  focusWindow(w);
}

function bindWindowChrome(w) {
  const tb = w.el.querySelector(".window-titlebar");
  tb.addEventListener("mousedown", (e) => {
    if (e.target.classList.contains("window-btn")) return;
    startWindowDrag(w, e);
  });
  w.el.querySelector(".close").onclick = (e) => {
    e.stopPropagation();
    closeWindow(w);
  };
  w.el.querySelector(".hide").onclick = (e) => {
    e.stopPropagation();
    hideWindow(w);
  };
  w.el.querySelector(".window-resize").addEventListener("mousedown", (e) => startWindowResize(w, e));
  w.el.addEventListener("mousedown", () => focusWindow(w));
}

let _drag = null;
function startWindowDrag(w, e) {
  e.preventDefault();
  _drag = { w, dx: e.clientX - w.x, dy: e.clientY - w.y };
  w.el.classList.add("dragging");
  document.addEventListener("mousemove", _onDragMove);
  document.addEventListener("mouseup", _endDrag);
}
function _onDragMove(e) {
  if (!_drag) return;
  const { w, dx, dy } = _drag;
  w.x = Math.max(0, e.clientX - dx);
  w.y = Math.max(0, e.clientY - dy);
  w.el.style.left = w.x + "px";
  w.el.style.top = w.y + "px";
}
function _endDrag() {
  if (!_drag) return;
  _drag.w.el.classList.remove("dragging");
  _drag = null;
  document.removeEventListener("mousemove", _onDragMove);
  document.removeEventListener("mouseup", _endDrag);
}

let _resize = null;
function startWindowResize(w, e) {
  e.preventDefault(); e.stopPropagation();
  _resize = { w, sx: e.clientX, sy: e.clientY, w0: w.w, h0: w.h };
  document.addEventListener("mousemove", _onResizeMove);
  document.addEventListener("mouseup", _endResize);
}
function _onResizeMove(e) {
  if (!_resize) return;
  const w = _resize.w;
  w.w = Math.max(280, _resize.w0 + (e.clientX - _resize.sx));
  w.h = Math.max(180, _resize.h0 + (e.clientY - _resize.sy));
  w.el.style.width = w.w + "px";
  w.el.style.height = w.h + "px";
  if (w.type === "graph") renderGraphInWindow(w);
}
function _endResize() {
  if (!_resize) return;
  _resize = null;
  document.removeEventListener("mousemove", _onResizeMove);
  document.removeEventListener("mouseup", _endResize);
}

function renderWindowContent(w) {
  const c = w.contentEl;
  if (w.type === "graph") {
    c.innerHTML = `<svg class="graph-svg" xmlns="http://www.w3.org/2000/svg"></svg>
      <div class="graph-toolbar">
        <button class="toolbar-btn add-agent-btn" title="add agent to project">+ agent</button>
      </div>
      <div class="zoom-controls">
        <button data-z="in" title="zoom in (Ctrl/Cmd+scroll)">+</button>
        <button data-z="out" title="zoom out">−</button>
        <button data-z="fit" title="fit — zoom to fit, node positions unchanged">⌖</button>
        <button data-z="relayout" title="re-layout — auto-arrange all nodes">⟲</button>
        <span class="zoom-level"></span>
      </div>`;
    bindGraphWindow(w);
    renderGraphInWindow(w);
  } else if (w.type === "file") {
    c.innerHTML = `
      <div class="file-header">
        <span class="file-path" title=""></span>
        <span class="file-info"></span>
        <button class="toolbar-btn file-reload" title="reload">⟳</button>
        <button class="toolbar-btn file-copy" title="copy abs path">⌘</button>
      </div>
      <div class="file-body">loading…</div>`;
    c.querySelector(".file-path").textContent = w.rel_path || "";
    c.querySelector(".file-path").title = w.abs_path || "";
    c.querySelector(".file-reload").onclick = () => loadFileContent(w);
    c.querySelector(".file-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(w.abs_path); flashHint("copied: " + w.abs_path); }
      catch { flashHint(w.abs_path); }
    };
    loadFileContent(w);
  } else if (w.type === "chat") {
    c.innerHTML = `
      <div class="chat-header"></div>
      <div class="turn-strip" hidden></div>
      <div class="messages"></div>
      <div class="chatbox">
        <div class="chatbox-label">Chat session</div>
        <form class="chat-form">
          <textarea class="chat-input" rows="2"
            placeholder="Enter to send (Shift+Enter for newline)"></textarea>
          <button class="chat-stop" type="button" hidden>Stop</button>
          <button class="chat-send" type="submit">Send</button>
        </form>
        <div class="chat-status"></div>
      </div>`;
    renderChatHeader(w);
    bindChatWindow(w);
    // Restore messages queued before the last reload BEFORE rendering history, so
    // refreshChatSession's renderQueue paints them. Then hand them off: if a run is
    // already in flight, its finally→drainQueue fires them; if the agent is idle,
    // nothing else ever would, so kick the drain here. The delay lets
    // reattachActiveRuns claim an in-flight run first — and if it wins the race, the
    // POST simply 409s and the message goes back on the queue.
    loadQueue(w);
    refreshChatSession(w);
    if (w.queue && w.queue.length) {
      setTimeout(() => { if (!w.streaming) drainQueue(w); }, 2000);
    }
  }
}

// ---------- Graph window ----------

function ensureGraphWindow(slug) {
  let w = state.windows.find((x) => x.projectSlug === slug && x.type === "graph");
  if (!w) {
    w = {
      id: `graph-${slug}`, projectSlug: slug, type: "graph",
      x: 16, y: 12, w: 760, h: 460, z: nextZ(), hidden: false,
    };
    state.windows.push(w);
    createWindowDom(w);
  } else if (w.hidden) {
    showWindow(w);
  } else {
    focusWindow(w);
  }
  ensureStats(slug);
  return w;
}

function layoutAgents(agents) {
  const ids = agents.map((a) => a.id);
  const parentMap = Object.fromEntries(agents.map((a) => [a.id, a.parents || []]));
  const depth = {};
  function d(id, stack = new Set()) {
    if (id in depth) return depth[id];
    if (stack.has(id)) return 0;
    stack.add(id);
    const parents = parentMap[id] || [];
    depth[id] = parents.length ? Math.max(...parents.map((p) => d(p, stack) + 1)) : 0;
    stack.delete(id);
    return depth[id];
  }
  ids.forEach((id) => d(id));
  const layers = {};
  ids.forEach((id) => { (layers[depth[id]] = layers[depth[id]] || []).push(id); });
  return { depth, layers };
}

function renderGraphInWindow(w) {
  // An SSE event mid-drag would rebuild the svg and detach the dragged node;
  // skip and let _endNodeDrag re-render when the drag finishes.
  if (_nodeDrag && _nodeDrag.gw === w) { _nodeDrag.rerenderPending = true; return; }
  const svg = w.el.querySelector(".graph-svg");
  if (!svg) return;
  svg.innerHTML = "";
  const proj = state.projectCache[w.projectSlug];
  if (!proj) return;

  const W = svg.clientWidth || 760;
  const saved = state.nodePositions[proj.slug] || (state.nodePositions[proj.slug] = {});

  const nodeW = 150, nodeH = 56;
  // Auto-layout only seeds nodes that were never placed by hand — a saved
  // position is layout truth and is never silently overridden.
  const unplaced = proj.agents.filter((a) => !saved[a.id]);
  if (unplaced.length) {
    const auto = autoLayoutPositions(proj.agents, W, nodeW, nodeH);
    unplaced.forEach((a) => { saved[a.id] = auto[a.id] || { x: 30, y: 30 }; });
  }

  const sizes = {};
  proj.agents.forEach((a) => {
    const expanded = state.expandedNodes.has(`${proj.slug}:${a.id}`);
    sizes[a.id] = expanded ? { w: PANEL_W, h: PANEL_H } : { w: nodeW, h: nodeH };
  });
  w._sizes = sizes;

  const allPos = proj.agents.filter((a) => saved[a.id])
    .map((a) => ({ ...saved[a.id], ...sizes[a.id] }));
  if (allPos.length) {
    const minX = Math.min(...allPos.map((p) => p.x)) - 40;
    const minY = Math.min(...allPos.map((p) => p.y)) - 40;
    const maxX = Math.max(...allPos.map((p) => p.x + p.w)) + 40;
    const maxY = Math.max(...allPos.map((p) => p.y + p.h)) + 40;
    state.graphBounds[proj.slug] = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  const ns = "http://www.w3.org/2000/svg";
  const defs = document.createElementNS(ns, "defs");
  defs.innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path class="arrow-head" d="M0,0 L10,5 L0,10 z"/></marker>`;
  svg.appendChild(defs);

  // Edges: bezier between border anchors, direction-agnostic. A↔B pairs get
  // opposite perpendicular bends so the two curves don't overlap.
  const edgeSet = new Set(proj.edges.map((e) => `${e.source}->${e.target}`));
  w._edges = [];
  proj.edges.forEach((e) => {
    if (!saved[e.source] || !saved[e.target]) return;
    const path = document.createElementNS(ns, "path");
    let edgeCls = "edge";
    if (state.activeDispatches.has(`${e.source}->${e.target}`)) edgeCls += " edge-active";
    path.setAttribute("class", edgeCls);
    const bend = edgeSet.has(`${e.target}->${e.source}`) ? 16 : 0;
    path.setAttribute("d", edgePathD(saved[e.source], sizes[e.source], saved[e.target], sizes[e.target], bend));
    svg.appendChild(path);
    w._edges.push({ el: path, source: e.source, target: e.target, bend });
  });

  // Expanded panels are painted last so they overlay neighbouring nodes/edges
  // instead of being clipped behind them.
  const deferred = [];
  proj.agents.forEach((a) => {
    const pos = saved[a.id];
    if (!pos) return;
    const g = buildAgentNode(proj, a, pos, nodeW, nodeH, w);
    if (state.expandedNodes.has(`${proj.slug}:${a.id}`)) deferred.push(g);
    else svg.appendChild(g);
  });
  deferred.forEach((g) => svg.appendChild(g));

  applyViewBox(svg, proj.slug, w);
}

function autoLayoutPositions(agents, W, nodeW, nodeH) {
  const { layers } = layoutAgents(agents);
  const layerKeys = Object.keys(layers).map(Number).sort((a, b) => a - b);
  const vGap = 80, hGap = 26, yStart = 50;
  const out = {};
  layerKeys.forEach((lvl, li) => {
    const row = layers[lvl];
    const totalW = row.length * nodeW + (row.length - 1) * hGap;
    const xStart = Math.max(30, (W - totalW) / 2);
    row.forEach((id, i) => {
      out[id] = { x: xStart + i * (nodeW + hGap), y: yStart + li * (nodeH + vGap) };
    });
  });
  return out;
}

// Point on the border of rect (pos,size) from its centre toward (tx,ty).
function rectAnchor(pos, size, tx, ty) {
  const cx = pos.x + size.w / 2, cy = pos.y + size.h / 2;
  const dx = tx - cx, dy = ty - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const sx = dx ? (size.w / 2) / Math.abs(dx) : Infinity;
  const sy = dy ? (size.h / 2) / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy);
  return { x: cx + dx * s, y: cy + dy * s };
}

function edgePathD(sPos, sSize, tPos, tSize, bend) {
  const scx = sPos.x + sSize.w / 2, scy = sPos.y + sSize.h / 2;
  const tcx = tPos.x + tSize.w / 2, tcy = tPos.y + tSize.h / 2;
  const a1 = rectAnchor(sPos, sSize, tcx, tcy);
  const a2 = rectAnchor(tPos, tSize, scx, scy);
  const dx = a2.x - a1.x, dy = a2.y - a1.y;
  const len = Math.hypot(dx, dy) || 1;
  const px = (-dy / len) * bend, py = (dx / len) * bend;
  const c1 = { x: a1.x + dx / 3 + px, y: a1.y + dy / 3 + py };
  const c2 = { x: a2.x - dx / 3 + px, y: a2.y - dy / 3 + py };
  return `M ${a1.x} ${a1.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${a2.x} ${a2.y}`;
}

function updateEdgesLive(w) {
  const saved = state.nodePositions[w.projectSlug] || {};
  const sizes = w._sizes || {};
  (w._edges || []).forEach(({ el, source, target, bend }) => {
    if (!saved[source] || !saved[target] || !sizes[source] || !sizes[target]) return;
    el.setAttribute("d", edgePathD(saved[source], sizes[source], saved[target], sizes[target], bend));
  });
}

// Expanded-panel geometry (viewBox units, matches collapsed node width baseline).
const PANEL_W = 212, PANEL_H = 430; // headroom for warn rows + init (cwd/tools/skills/servers)

function buildAgentNode(proj, a, pos, nodeW, nodeH, gw) {
  const ns = "http://www.w3.org/2000/svg";
  const status = (proj.statuses && proj.statuses[a.id]) || "idle";
  const isOrchestrator = (a.parents || []).length === 0;
  const isOpenChat = state.windows.some(
    (x) => x.type === "chat" && x.projectSlug === proj.slug && x.agentId === a.id);
  const key = `${proj.slug}:${a.id}`;
  const expanded = state.expandedNodes.has(key);
  const stats = (state.statsCache[proj.slug] || {})[a.id];

  const g = document.createElementNS(ns, "g");
  const fo = document.createElementNS(ns, "foreignObject");
  fo.setAttribute("x", pos.x);
  fo.setAttribute("y", pos.y);
  fo.setAttribute("width", expanded ? PANEL_W : nodeW);
  fo.setAttribute("height", expanded ? PANEL_H : nodeH);
  fo.setAttribute("overflow", "visible");

  let cls = "agent-card status-" + status;
  if (isOrchestrator) cls += " orchestrator";
  if (isOpenChat) cls += " selected";
  if (status === "running") cls += " pulse";
  if (expanded) cls += " expanded";

  fo.innerHTML =
    `<div xmlns="http://www.w3.org/1999/xhtml" class="${cls}" style="min-height:${nodeH}px">`
    + nodeCardHtml(a, status, expanded, stats, proj.slug) + `</div>`;
  g.appendChild(fo);

  const card = fo.querySelector(".agent-card");
  if (card && gw) card.addEventListener("mousedown", (e) => onNodeMouseDown(e, gw, proj, a, fo));

  const chev = fo.querySelector(".ac-expand");
  if (chev) chev.onclick = (e) => { e.stopPropagation(); toggleNode(proj.slug, a.id); };
  const head = fo.querySelector(".ac-head");
  if (head) head.onclick = (e) => {
    if (e.target.closest(".ac-expand")) return;
    openChat(proj.slug, a.id);
  };
  const openBtn = fo.querySelector(".ac-open");
  if (openBtn) openBtn.onclick = (e) => { e.stopPropagation(); openChat(proj.slug, a.id); };
  return g;
}

function nodeCardHtml(a, status, expanded, stats, slug) {
  const chev = expanded ? "▾" : "▸";
  const scheds = slug ? activeSchedulesFor(slug, a.id) : [];
  let schedBadge = "";
  if (scheds.length) {
    const next = scheds.reduce((m, s) => Math.min(m, s.next_run_at), Infinity);
    const title = scheds.map((s) => `${schedLabel(s)} · next ${fmtCountdown(s.next_run_at)}`).join("\n");
    schedBadge = `<span class="ac-sched" title="${escapeHtml(title)}">🕒 ${fmtCountdown(next)}</span>`;
  }
  let botBadge = "";
  if (a.telegram_bot) {
    botBadge = `<span class="ac-bot" title="Agent này đang được điều khiển qua Telegram bot ${escapeHtml(a.telegram_bot)}">🤖</span>`;
  }
  let html = `
    <div class="ac-head">
      <span class="ac-dot" style="background:${statusColor(status)}"></span>
      <span class="ac-id">${escapeHtml(a.id)}</span>
      ${schedBadge}
      ${botBadge}
      <span class="ac-expand" title="${expanded ? "collapse" : "expand"}">${chev}</span>
    </div>
    <div class="ac-model">${escapeHtml(modelLabel(a))}</div>`;
  if (expanded) html += `<div class="ac-body">${nodeBodyHtml(a, stats, slug)}</div>`;
  return html;
}

function nodeBodyHtml(a, stats, slug) {
  // Live stream copy first (fresh within the turn), else the persisted init_meta
  // that /stats carries — that fallback is what makes the tools list show on an
  // idle agent and after a browser reload, when state.initInfo is empty.
  const init = (slug && (state.initInfo[slug] || {})[a.id]) || (stats && stats.init);
  const initHtml = initSectionHtml(init);
  if (!stats) return (initHtml || `<div class="ac-loading">loading stats…</div>`);
  const pct = stats.context_pct || 0;
  const barCls = pct > 80 ? "hot" : (pct > 50 ? "warm" : "");
  const mem = stats.memory;
  const memTime = mem && mem.mtime ? fmtRelTime(mem.mtime) : "—";
  const memHead = mem && mem.headline ? escapeHtml(mem.headline) : "";
  // Worked recently but memory file untouched for >6h → working without persisting.
  const memStale = !!(mem && mem.mtime && stats.updated_at && (stats.updated_at - mem.mtime) > 6 * 3600);
  const lastAct = stats.updated_at ? fmtRelTime(stats.updated_at) : "—";
  const effort = stats.effort ? escapeHtml(stats.effort) : "default";
  const sessTxt = stats.has_session
    ? `live${stats.num_sessions > 1 ? " · " + stats.num_sessions : ""}`
    : "fresh";
  const exact = stats.token_source === "exact" || stats.token_source === "transcript";
  const tokK = exact ? "tokens" : "≈ tokens";
  const ctxTitle = stats.token_source === "transcript"
    ? "exact occupancy — largest single API request in the CLI transcript"
    : (exact ? "actual token count from CLI (latest turn)" : "chars/4 estimate — no completed turn yet");
  // Billing totals (lifetime, from token_turns). Distinct from context occupancy above:
  // cache_read is re-charged on every request, so this climbs far past the window.
  const tk = stats.tokens;
  const billed = tk
    ? `<div class="ac-row" title="lifetime billing across ${tk.turns} turns / ${tk.requests} API requests — cache_read is re-charged per request, so this is NOT context size">
         <span class="ac-k">billed</span>
         <span class="ac-v">${fmtTokens((tk.input_tokens || 0) + (tk.cache_creation || 0) + (tk.cache_read || 0) + (tk.output_tokens || 0))}</span>
       </div>
       <div class="ac-row" title="output tokens generated (the part that is not cache)">
         <span class="ac-k">out</span><span class="ac-v">${fmtTokens(tk.output_tokens)}</span>
       </div>`
    : "";
  return `
    <div class="ac-row ac-ctx" title="${ctxTitle}">
      <span class="ac-k">context${exact ? "" : " ≈"}</span>
      <span class="ac-bar"><i class="${barCls}" style="width:${Math.min(100, pct)}%"></i></span>
      <span class="ac-v">${pct}%</span>
    </div>
    <div class="ac-row"><span class="ac-k">${tokK}</span><span class="ac-v">${fmtTokens(stats.context_tokens)} / ${fmtTokens(stats.context_window)}</span></div>
    ${billed}
    ${pct >= 80 ? `<div class="ac-warn" title="auto-compact threshold: 80%">🔄 auto-compact will run next turn</div>` : ""}
    <div class="ac-row"><span class="ac-k">memory</span><span class="ac-v${memStale ? " ac-stale" : ""}">${memTime}${memStale ? " ⚠" : ""}</span></div>
    ${memStale ? `<div class="ac-warn">⚠ recent activity but memory not written</div>` : ""}
    ${memHead ? `<div class="ac-headline" title="${memHead}">${memHead}</div>` : ""}
    <div class="ac-row"><span class="ac-k">activity</span><span class="ac-v">${lastAct}</span></div>
    <div class="ac-row"><span class="ac-k">messages</span><span class="ac-v">${stats.message_count}</span></div>
    <div class="ac-row"><span class="ac-k">effort</span><span class="ac-v">${effort}</span></div>
    <div class="ac-row" title="${init && init.claude_session_id ? "claude session " + escapeHtml(init.claude_session_id) : ""}"><span class="ac-k">session</span><span class="ac-v">${sessTxt}</span></div>
    ${initHtml}
    <div class="ac-actions"><button class="ac-open">open chat ↗</button></div>`;
}

function toggleNode(slug, id) {
  const key = `${slug}:${id}`;
  if (state.expandedNodes.has(key)) {
    state.expandedNodes.delete(key);
  } else {
    state.expandedNodes.add(key);
    if (!state.statsCache[slug]) ensureStats(slug);
  }
  rerenderGraphsForSlug(slug);
}

// ---------- Init payload: default-vs-added split ----------
// Every stock `claude` CLI session ships the same builtin tools and skills; listing
// them on every card is noise. Show only what was ADDED for this agent (MCP tools,
// user/plugin skills, plugins, MCP servers) and collapse the stock set to a count.
// These lists are a manually maintained baseline of CLI builtins — when a CLI update
// ships a new builtin it will show up as "added" until appended here (visible and
// harmless), which beats the reverse failure of hiding a real addition.
const DEFAULT_TOOLS = new Set([
  "Agent", "AskUserQuestion", "Bash", "BashOutput", "CronCreate", "CronDelete",
  "CronList", "DesignSync", "Edit", "EnterPlanMode", "EnterWorktree", "ExitPlanMode",
  "ExitWorktree", "Glob", "Grep", "KillShell", "ListMcpResourcesTool", "Monitor",
  "NotebookEdit", "PushNotification", "Read", "ReadMcpResourceDirTool",
  "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings", "ScheduleWakeup",
  "SendMessage", "Skill", "SlashCommand", "Task", "TaskCreate", "TaskGet", "TaskList",
  "TaskOutput", "TaskStop", "TaskUpdate", "TodoWrite", "ToolSearch", "WebFetch",
  "WebSearch", "Workflow", "Write",
]);
const DEFAULT_SKILLS = new Set([
  "artifact-design", "artifact-capabilities", "batch", "claude-api", "code-review",
  "dataviz", "debug", "deep-research", "design-sync", "doctor",
  "fewer-permission-prompts", "init", "keybindings-help", "loop", "review", "run",
  "run-skill-generator", "schedule", "security-review", "simplify", "update-config",
  "verify",
]);

function splitInitLists(init) {
  const tools = Array.isArray(init.tools) ? init.tools : [];
  const skills = Array.isArray(init.skills) ? init.skills : [];
  return {
    mcpTools: tools.filter((t) => String(t).startsWith("mcp__")),
    addedTools: tools.filter((t) => !String(t).startsWith("mcp__") && !DEFAULT_TOOLS.has(t)),
    defaultToolCount: tools.filter((t) => DEFAULT_TOOLS.has(t)).length,
    addedSkills: skills.filter((s) => !DEFAULT_SKILLS.has(s)),
    defaultSkillCount: skills.filter((s) => DEFAULT_SKILLS.has(s)).length,
  };
}

// Init section for the expanded node panel — shows cwd + what was ADDED for this
// agent (MCP/extra tools, non-default skills, servers); stock builtins collapse
// to a count. Captured from the system/init meta of the agent's last turn.
function initSectionHtml(init) {
  if (!init) return "";
  const sp = splitInitLists(init);
  const CAP = 10;
  const chipList = (list, cap = CAP) => {
    const shown = list.slice(0, cap);
    const extra = list.length - shown.length;
    const chips = shown
      .map((t) => `<span class="ac-tool" title="${escapeHtml(t)}">${escapeHtml(t)}</span>`)
      .join("");
    return `<div class="ac-tool-list">${chips}${extra > 0 ? `<span class="ac-tool-more" title="${escapeHtml(list.join(", "))}">+${extra}</span>` : ""}</div>`;
  };
  const cwd = init.cwd
    ? `<div class="ac-row"><span class="ac-k">cwd</span><span class="ac-v mono" title="${escapeHtml(init.cwd)}">${escapeHtml(init.cwd)}</span></div>`
    : "";
  const servers = (Array.isArray(init.mcp_servers) ? init.mcp_servers : [])
    .map((s) => s && s.name).filter(Boolean);
  const row = (label, list) =>
    list.length
      ? `<div class="ac-init-tools"><span class="ac-k">${label} · ${list.length}</span>${chipList(list)}</div>`
      : "";
  const defaults = (sp.defaultToolCount || sp.defaultSkillCount)
    ? `<div class="ac-row" title="stock CLI builtins — identical for every agent, hidden from the chip list"><span class="ac-k">defaults</span><span class="ac-v">${sp.defaultToolCount} tools · ${sp.defaultSkillCount} skills</span></div>`
    : "";
  const toolsRow =
    row("+tools", sp.addedTools) + row("mcp", sp.mcpTools) +
    row("+skills", sp.addedSkills) + row("servers", servers) + defaults;
  const sep = cwd || toolsRow ? `<div class="ac-sep">init</div>` : "";
  return sep + cwd + toolsRow;
}

// ---------- Node drag (rearrange agents on canvas, persisted in db) ----------

const NODE_DRAG_THRESHOLD = 4; // px on screen — below this it's a click, not a drag
let _nodeDrag = null;

function onNodeMouseDown(e, gw, proj, a, fo) {
  if (e.button !== 0) return;
  if (e.target.closest(".ac-expand, .ac-open, button, a, textarea, input, select")) return;
  const svg = gw.el.querySelector(".graph-svg");
  const vb = state.viewBoxes[proj.slug];
  const pos = (state.nodePositions[proj.slug] || {})[a.id];
  if (!svg || !vb || !pos) return;
  const rect = svg.getBoundingClientRect();
  _nodeDrag = {
    gw, slug: proj.slug, id: a.id, fo,
    sx: e.clientX, sy: e.clientY,
    x0: pos.x, y0: pos.y,
    scaleX: vb.w / rect.width, scaleY: vb.h / rect.height,
    moved: false,
  };
  e.stopPropagation();
  document.addEventListener("mousemove", _onNodeDragMove);
  document.addEventListener("mouseup", _endNodeDrag);
}

function _onNodeDragMove(e) {
  const d = _nodeDrag;
  if (!d) return;
  const dxs = e.clientX - d.sx, dys = e.clientY - d.sy;
  if (!d.moved && Math.hypot(dxs, dys) < NODE_DRAG_THRESHOLD) return;
  if (!d.moved) {
    d.moved = true;
    const card = d.fo.querySelector(".agent-card");
    if (card) card.classList.add("node-dragging");
  }
  e.preventDefault();
  const pos = state.nodePositions[d.slug][d.id];
  pos.x = d.x0 + dxs * d.scaleX;
  pos.y = d.y0 + dys * d.scaleY;
  d.fo.setAttribute("x", pos.x);
  d.fo.setAttribute("y", pos.y);
  updateEdgesLive(d.gw);
}

function _endNodeDrag() {
  const d = _nodeDrag;
  if (!d) return;
  _nodeDrag = null;
  document.removeEventListener("mousemove", _onNodeDragMove);
  document.removeEventListener("mouseup", _endNodeDrag);
  const card = d.fo.querySelector(".agent-card");
  if (card) card.classList.remove("node-dragging");
  if (d.moved) {
    // swallow the click that follows mouseup so it doesn't open the chat;
    // disarm on next tick in case no click fires (released off-element)
    const swallow = (ce) => { ce.stopPropagation(); ce.preventDefault(); };
    document.addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => document.removeEventListener("click", swallow, { capture: true }), 0);
    schedulePositionSave(d.slug);
  }
  if (d.rerenderPending) renderGraphInWindow(d.gw);
}

const _posSaveTimers = {};
function schedulePositionSave(slug) {
  clearTimeout(_posSaveTimers[slug]);
  _posSaveTimers[slug] = setTimeout(() => {
    fetch(`/api/projects/${slug}/positions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: state.nodePositions[slug] || {} }),
    }).catch(() => {});
  }, 600);
}

async function ensureStats(slug, force) {
  if (!force && state.statsCache[slug]) return;
  try {
    const r = await fetch(`/api/projects/${slug}/stats`);
    if (r.ok) state.statsCache[slug] = (await r.json()).stats || {};
  } catch (e) { /* keep stale cache on network error */ }
  rerenderGraphsForSlug(slug);
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k";
  return String(n);
}

function fmtRelTime(epochSec) {
  if (!epochSec) return "—";
  let d = Date.now() / 1000 - epochSec;
  if (d < 0) d = 0;
  if (d < 45) return "just now";
  if (d < 90) return "1 min ago";
  if (d < 3600) return Math.round(d / 60) + " min ago";
  if (d < 7200) return "1 hr ago";
  if (d < 86400) return Math.round(d / 3600) + " hr ago";
  if (d < 172800) return "yesterday";
  return Math.round(d / 86400) + " d ago";
}

function modelLabel(a) {
  if (a.model === "grok") {
    return (a.grok_model || "grok-build");
  }
  if (a.model === "deepseek") {
    return (a.deepseek_model || "deepseek-v4-flash");
  }
  if (a.model === "glm") {
    return (a.glm_model || "glm-4.6");
  }
  return (a.claude_model || "claude-sonnet-4-6").replace(/^claude-/, "");
}

function statusColor(s) {
  switch (s) {
    case "ok": return "#45d18d";
    case "error": return "#ff6868";
    case "running": return "#f8c450";
    case "cancelled": return "#8a91a3";
    default: return "#5a6173";
  }
}

// ---------- File viewer window ----------

function openFileViewer(absPath, relPath) {
  const id = `file:${absPath}`;
  let w = state.windows.find((x) => x.id === id);
  if (w) {
    if (w.hidden) showWindow(w);
    else focusWindow(w);
    return w;
  }
  const offset = state.windows.filter((x) => x.type === "file").length * 28;
  w = {
    id, projectSlug: state.activeTab || "", type: "file",
    abs_path: absPath, rel_path: relPath,
    x: 220 + offset, y: 80 + offset, w: 640, h: 520,
    z: nextZ(), hidden: false,
  };
  state.windows.push(w);
  createWindowDom(w);
  return w;
}

const _IMG_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);

async function loadFileContent(w) {
  const body = w.contentEl.querySelector(".file-body");
  const info = w.contentEl.querySelector(".file-info");
  const ext = (w.rel_path.split(".").pop() || "").toLowerCase();
  const rawUrl = `/api/workspace/raw?path=${encodeURIComponent(w.rel_path)}`;

  // PDF and images: render via browser, no need to fetch JSON wrapper
  if (ext === "pdf") {
    body.style.padding = "0";
    body.innerHTML = `<embed class="file-pdf" src="${rawUrl}" type="application/pdf">`;
    if (info) info.textContent = ".pdf";
    return;
  }
  if (_IMG_EXTS.has(ext)) {
    body.style.padding = "0";
    body.innerHTML = `<div class="file-img-wrap"><img class="file-img" src="${rawUrl}" alt="${escapeHtml(w.rel_path)}"></div>`;
    if (info) info.textContent = "." + ext;
    return;
  }
  body.style.padding = "";

  // Text / markdown: go through /file (UTF-8 decode + size cap)
  body.textContent = "loading…";
  try {
    const r = await fetch(`/api/workspace/file?path=${encodeURIComponent(w.rel_path)}`);
    const j = await r.json();
    if (!r.ok) {
      body.textContent = `error ${r.status}: ${j.detail || ""}`;
      if (info) info.textContent = "";
      return;
    }
    if (info) info.textContent = `${j.size}c • .${ext}`;
    if (j.is_binary) {
      body.innerHTML = `<div class="file-binary">
        ${escapeHtml(j.content)}<br>
        <a href="${rawUrl}" target="_blank" rel="noopener">download raw</a>
      </div>`;
      return;
    }
    if (ext === "md" || ext === "markdown") {
      body.innerHTML = `<div class="file-md content"></div>`;
      setContent(body.querySelector(".file-md"), j.content);
    } else {
      body.innerHTML = `<pre class="file-code"></pre>`;
      body.querySelector("pre").textContent = j.content;
    }
  } catch (err) {
    body.textContent = "network error: " + err.message;
  }
}

function rerenderGraphsForSlug(slug) {
  state.windows.filter((w) => w.type === "graph" && w.projectSlug === slug)
    .forEach((w) => renderGraphInWindow(w));
}

// ---------- Graph viewBox / zoom / pan ----------

function applyViewBox(svg, slug, w) {
  if (!state.viewBoxes[slug]) {
    const b = state.graphBounds[slug];
    if (b) state.viewBoxes[slug] = { ...b };
  }
  const vb = state.viewBoxes[slug];
  if (!vb) return;
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  const zl = w.el.querySelector(".zoom-level");
  if (zl) {
    const b = state.graphBounds[slug];
    zl.textContent = b ? `${Math.round((b.w / vb.w) * 100)}%` : "";
  }
}

function bindGraphWindow(w) {
  const svg = w.el.querySelector(".graph-svg");
  svg.addEventListener("wheel", (e) => onGraphWheel(e, w), { passive: false });
  svg.addEventListener("mousedown", (e) => onGraphMouseDown(e, w));
  w.el.querySelectorAll(".zoom-controls button").forEach((btn) => {
    btn.onclick = () => {
      const act = btn.dataset.z;
      if (act === "in") zoomBy(w, 0.9);
      else if (act === "out") zoomBy(w, 1.111);
      else if (act === "fit") {
        delete state.viewBoxes[w.projectSlug];
        renderGraphInWindow(w);
      } else if (act === "relayout") {
        if (!confirm("Auto-arrange all nodes? Hand-dragged positions will be lost.")) return;
        fetch(`/api/projects/${w.projectSlug}/positions`, { method: "DELETE" }).catch(() => {});
        state.nodePositions[w.projectSlug] = {};
        delete state.viewBoxes[w.projectSlug];
        renderGraphInWindow(w);
      }
    };
  });
  const addBtn = w.el.querySelector(".add-agent-btn");
  if (addBtn) addBtn.onclick = () => openAddAgentDialog(w.projectSlug);
}

function zoomBy(w, factor, px, py) {
  const slug = w.projectSlug;
  const vb = state.viewBoxes[slug];
  if (!vb) return;
  const newW = vb.w * factor, newH = vb.h * factor;
  if (px !== undefined && py !== undefined) {
    vb.x += (vb.w - newW) * px;
    vb.y += (vb.h - newH) * py;
  } else {
    vb.x += (vb.w - newW) / 2;
    vb.y += (vb.h - newH) / 2;
  }
  vb.w = newW; vb.h = newH;
  const svg = w.el.querySelector(".graph-svg");
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const zl = w.el.querySelector(".zoom-level");
  if (zl) {
    const b = state.graphBounds[slug];
    zl.textContent = b ? `${Math.round((b.w / vb.w) * 100)}%` : "";
  }
}

function onGraphWheel(e, w) {
  const vb = state.viewBoxes[w.projectSlug];
  if (!vb) return;
  e.preventDefault();
  const svg = e.currentTarget;
  const rect = svg.getBoundingClientRect();

  if (e.ctrlKey || e.metaKey) {
    // Ctrl/Cmd + wheel: zoom anchored at cursor, gentle.
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    // Normalize wheel delta: trackpads send ~1-10, mice ~100 per notch.
    // Cap so a fast scroll doesn't jump too far.
    const norm = Math.max(-50, Math.min(50, e.deltaY));
    const factor = 1 + (norm / 50) * 0.08;  // ±8% max per event
    zoomBy(w, factor, px, py);
  } else {
    // Plain wheel: pan in viewBox space.
    const sx = vb.w / rect.width;
    const sy = vb.h / rect.height;
    vb.x += e.deltaX * sx;
    vb.y += e.deltaY * sy;
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  }
}

let _pan = null;
function onGraphMouseDown(e, w) {
  let t = e.target;
  while (t && t !== e.currentTarget) {
    if (t.tagName === "g") return;
    t = t.parentNode;
  }
  const vb = state.viewBoxes[w.projectSlug];
  if (!vb) return;
  _pan = { w, sx: e.clientX, sy: e.clientY, vb: { ...vb } };
  e.currentTarget.classList.add("panning");
  document.addEventListener("mousemove", _onPanMove);
  document.addEventListener("mouseup", _endPan);
}
function _onPanMove(e) {
  if (!_pan) return;
  const svg = _pan.w.el.querySelector(".graph-svg");
  const rect = svg.getBoundingClientRect();
  const dx = ((e.clientX - _pan.sx) / rect.width) * _pan.vb.w;
  const dy = ((e.clientY - _pan.sy) / rect.height) * _pan.vb.h;
  const vb = state.viewBoxes[_pan.w.projectSlug];
  vb.x = _pan.vb.x - dx;
  vb.y = _pan.vb.y - dy;
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}
function _endPan() {
  if (!_pan) return;
  _pan.w.el.querySelector(".graph-svg").classList.remove("panning");
  _pan = null;
  document.removeEventListener("mousemove", _onPanMove);
  document.removeEventListener("mouseup", _endPan);
}

// ---------- Chat window ----------

function openChat(slug, agentId) {
  let w = state.windows.find(
    (x) => x.type === "chat" && x.projectSlug === slug && x.agentId === agentId);
  if (w) {
    if (w.hidden) showWindow(w);
    else focusWindow(w);
  } else {
    const offset = state.windows.filter((x) => x.type === "chat").length * 28;
    w = {
      id: `chat-${slug}-${agentId}`, projectSlug: slug, type: "chat", agentId,
      x: 80 + offset, y: 60 + offset, w: 460, h: 540, z: nextZ(), hidden: false,
      streaming: false,
    };
    state.windows.push(w);
    createWindowDom(w);
  }
  rerenderGraphsForSlug(slug);
  return w;
}

const CLAUDE_MODELS = [
  { value: "claude-fable-5",      label: "fable 5"    },
  { value: "claude-opus-4-8",     label: "opus 4.8"   },
  { value: "claude-opus-4-7",     label: "opus 4.7"   },
  { value: "claude-sonnet-5",     label: "sonnet 5"   },
  { value: "claude-sonnet-4-6",   label: "sonnet 4.6" },
  { value: "claude-haiku-4-5",    label: "haiku 4.5"  },
];
const EFFORT_LEVELS = [
  { value: "",       label: "default" },
  { value: "low",    label: "low"     },
  { value: "medium", label: "medium"  },
  { value: "high",   label: "high"    },
  { value: "xhigh",  label: "xhigh"   },
  { value: "max",    label: "max"     },
];

function renderChatHeader(w) {
  const header = w.el.querySelector(".chat-header");
  const proj = state.projectCache[w.projectSlug];
  const agent = proj?.agents.find((a) => a.id === w.agentId);
  if (!agent) { header.textContent = w.agentId; return; }

  const modelText = agent.model === "grok"
    ? (agent.grok_model || agent.default_grok_model || "grok-build")
    : agent.model === "deepseek"
    ? (agent.deepseek_model || agent.default_deepseek_model || "deepseek-v4-flash")
    : agent.model === "glm"
    ? (agent.glm_model || agent.default_glm_model || "glm-4.6")
    : (agent.claude_model || agent.default_claude_model || "claude-sonnet-4-6").replace(/^claude-/, "");
  const curEffort = agent.effort || "";

  const effortOpts = EFFORT_LEVELS.map((e) =>
    `<option value="${e.value}"${e.value === curEffort ? " selected" : ""}>${e.label}</option>`
  ).join("");

  header.innerHTML = `
    <div class="header-top">
      <span class="header-name">${escapeHtml(agent.id)}</span>
      <span class="header-role">${escapeHtml(agent.role || "")}</span>
    </div>
    <div class="header-meta">
      <span class="meta-model">${escapeHtml(modelText)}</span>
      <label class="meta-effort-ctl" title="Reasoning effort — applies from the next turn">
        <span class="meta-effort-label">effort</span>
        <select class="meta-effort-select">${effortOpts}</select>
      </label>
      <span class="meta-hint">type <code>/</code> for commands</span>
      <span class="save-hint"></span>
    </div>`;

  const sel = header.querySelector(".meta-effort-select");
  if (sel) {
    sel.addEventListener("change", async () => {
      const eff = sel.value;  // "" = default
      // Preserve the agent's current model; only effort changes.
      const a = state.projectCache[w.projectSlug]?.agents.find((x) => x.id === w.agentId) || agent;
      if (a.model === "grok") {
        await updateAgentSettings(w, null, a.grok_model || "grok-build", null, null, eff);
      } else if (a.model === "deepseek") {
        await updateAgentSettings(w, null, null, a.deepseek_model || "deepseek-v4-flash", null, eff);
      } else if (a.model === "glm") {
        await updateAgentSettings(w, null, null, null, a.glm_model || "glm-4.6", eff);
      } else {
        await updateAgentSettings(w, a.claude_model || "claude-sonnet-4-6", null, null, null, eff);
      }
    });
    // Stop drags on the select from moving the window / pannning the canvas.
    sel.addEventListener("mousedown", (e) => e.stopPropagation());
  }
}

async function updateAgentSettings(w, claudeModel, grokModel, deepseekModel, glmModel, effort) {
  const slug = w.projectSlug;
  const agentId = w.agentId;
  const hint = w.el.querySelector(".save-hint");
  if (hint) { hint.textContent = "saving…"; hint.style.color = "var(--text-dim)"; }
  try {
    const r = await fetch(`/api/projects/${slug}/agents/${agentId}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        claude_model: claudeModel,
        grok_model: grokModel,
        deepseek_model: deepseekModel,
        glm_model: glmModel,
        effort: effort || null,
      }),
    });
    if (!r.ok) throw new Error("save failed");

    const pr = await fetch(`/api/projects/${slug}`);
    state.projectCache[slug] = await pr.json();
    rerenderGraphsForSlug(slug);
    state.windows
      .filter((x) => x.type === "chat" && x.projectSlug === slug && x.agentId === agentId)
      .forEach((x) => renderChatHeader(x));
    if (hint) {
      hint.textContent = "✓ applies next turn";
      hint.style.color = "var(--ok)";
      clearTimeout(updateAgentSettings._t);
      updateAgentSettings._t = setTimeout(() => { hint.textContent = ""; }, 2500);
    }
  } catch (err) {
    if (hint) { hint.textContent = "error"; hint.style.color = "var(--err)"; }
  }
}

// beforeTs: when re-attaching to a run, render ONLY the history that predates it.
// A multi-round turn persists each round's assistant message as that round ends, while
// the _Run stays active until every continuation finishes — so a re-attach that renders
// all of db AND replays the run from seq 0 draws those rounds twice (once as "assistant"
// from db, once as "assistant • BOSS" from the replay). The replay owns everything from
// the run's start onward; db owns what came before.
async function refreshChatSession(w, beforeTs) {
  // Never wipe the messages area mid-stream: live bubbles hold DOM references
  // that a rebuild would orphan. (attachDetachedRun refreshes BEFORE it sets
  // w.streaming, so its history render still goes through.)
  if (w.streaming) return;
  try {
    const r = await fetch(`/api/projects/${w.projectSlug}/agents/${w.agentId}/session`);
    const j = await r.json();
    w.session = j.session;
    const msgRoot = w.el.querySelector(".messages");
    msgRoot.innerHTML = "";
    // Replay the init card at the top of the transcript from the persisted payload,
    // so it is there on open / reload instead of only during a live turn. The seen-set
    // is cleared first because the innerHTML wipe just removed every card this window
    // had drawn; renderInitWidget re-adds this agent's key as it redraws.
    w.seenInits = new Set();
    if (j.session && j.session.init_meta) {
      try { renderInitWidget(w, w.agentId, JSON.parse(j.session.init_meta)); } catch {}
    }
    (j.messages || []).forEach((m) => {
      // Everything from the re-attached run's start onward belongs to the replay.
      if (beforeTs && (m.created_at || 0) >= beforeTs) return;
      if (m.role === "user" && /^\[CONTROL-PLANE /.test(m.content || "")) {
        addCtrlSeparator(w, m.content);
        return;
      }
      addBubble(w, m.role, m.content);
    });
    // The wipe above also removed any pending-queue bubbles. They are a projection
    // of w.queue, so repaint them — otherwise queued messages go invisible and then
    // fire later out of nowhere.
    renderQueue(w);
  } catch {}
}

function addBubble(w, role, text, container) {
  const root = container || w.el.querySelector(".messages");
  const b = document.createElement("div");
  b.className = `bubble ${role}`;
  b.innerHTML = `<div class="role">${role}</div><div class="content"></div>`;
  setContent(b.querySelector(".content"), text);
  root.appendChild(b);
  // Anything appended to the transcript must not jump ABOVE the pending queue —
  // a still-running turn kept burying queued questions mid-transcript. Skipped
  // while rendering the queue itself (its own bubbles are the ones being placed).
  if (!b.classList.contains("queued") && !w._renderingQueue) parkQueueAtBottom(w);
  const m = w.el.querySelector(".messages");
  if (m) m.scrollTop = m.scrollHeight;
  return b;
}

// Control-plane messages (continuation prompts, compact seeds) are machine-to-
// machine traffic — render them as a thin labelled separator, not a user bubble.
function addCtrlSeparator(w, content) {
  const root = w.el.querySelector(".messages");
  const d = document.createElement("div");
  d.className = "ctrl-sep";
  let label = "control-plane";
  const m = (content || "").match(/^\[CONTROL-PLANE ([A-Z][A-Z -]*?)\s*([0-9]+\/[0-9]+)?\]/);
  if (m) label = m[1].toLowerCase().trim() + (m[2] ? " " + m[2] : "");
  d.innerHTML = `<span class="ctrl-sep-label">⟳ ${escapeHtml(label)}</span>`;
  d.title = (content || "").slice(0, 400);
  root.appendChild(d);
  root.scrollTop = root.scrollHeight;
  return d;
}

// ---------- Worker cards ----------
// Each dispatched worker gets ONE collapsible card in the parent chat: a live
// status line (driven by plan/status events) on top, the full streamed
// transcript hidden inside. Parallel workers stop interleaving — the parent
// chat reads as: orchestrator text → cards → orchestrator synthesis.

function ensureWorkerCard(w, source, target, task, rootAgent) {
  if (!w._workerCards) w._workerCards = {};
  if (w._workerCards[target]) return w._workerCards[target];
  const card = document.createElement("div");
  card.className = "worker-card status-running";
  card.dataset.agent = target;
  const taskLine = (task || "").split("\n")[0].slice(0, 110);
  card.innerHTML = `
    <div class="wc-head">
      <span class="wc-arrow">➜</span>
      <span class="wc-agent">${escapeHtml(target)}</span>
      <span class="wc-status">⏳ starting…</span>
      <span class="wc-toggle" title="show full transcript">▸</span>
    </div>
    ${taskLine ? `<div class="wc-task" title="${escapeHtml((task || "").slice(0, 400))}">${escapeHtml(taskLine)}</div>` : ""}
    <div class="wc-body" hidden></div>
    <div class="wc-foot" hidden>
      <button class="wc-open">open ${escapeHtml(target)} chat ↗</button>
    </div>`;
  card.querySelector(".wc-head").onclick = () => {
    const body = card.querySelector(".wc-body");
    const foot = card.querySelector(".wc-foot");
    body.hidden = !body.hidden;
    foot.hidden = body.hidden;
    card.querySelector(".wc-toggle").textContent = body.hidden ? "▸" : "▾";
  };
  card.querySelector(".wc-open").onclick = (e) => {
    e.stopPropagation();
    openChat(w.projectSlug, target);
  };
  // A worker's own sub-dispatch (e.g. DATASET → PAPER) nests inside the
  // source's card so the hierarchy stays visible.
  const parentCard = (source && source !== rootAgent && w._workerCards[source]) || null;
  const container = parentCard ? parentCard.querySelector(".wc-body") : w.el.querySelector(".messages");
  container.appendChild(card);
  const m = w.el.querySelector(".messages");
  if (m) m.scrollTop = m.scrollHeight;
  w._workerCards[target] = card;
  return card;
}

function workerCardStatus(w, agentId, text, cls) {
  const card = w._workerCards && w._workerCards[agentId];
  if (!card) return false;
  if (cls) {
    card.classList.remove("status-running", "status-ok", "status-error");
    card.classList.add(cls);
  }
  // Once the worker finished, its summary line wins over late status noise.
  if (card.dataset.done && !cls) return true;
  const st = card.querySelector(".wc-status");
  if (st && text) st.textContent = text;
  return true;
}

// ---------- Turn progress strip ----------

function renderTurnStrip(w, rootAgent) {
  const el = w.el.querySelector(".turn-strip");
  if (!el) return;
  const ws = w._turnWorkers || {};
  const ids = Object.keys(ws);
  if (!ids.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  const icon = (s) => (s === "ok" ? "✓" : (s === "error" ? "✗" : "⏳"));
  el.innerHTML =
    `<span class="ts-root">${escapeHtml(rootAgent)}</span><span class="ts-sep">▸</span>` +
    ids.map((id) =>
      `<span class="ts-chip ts-${ws[id]}">${icon(ws[id])} ${escapeHtml(id)}</span>`).join("") +
    (w._turnRound ? `<span class="ts-round">round ${w._turnRound}</span>` : "");
}

const DISPATCH_TAG_RE = /<dispatch\s+agent="([^"]+)"\s*>([\s\S]*?)<\/dispatch>/gi;
const DISPATCH_OPEN_RE = /<dispatch\s+agent="([^"]+)"\s*>([\s\S]*)$/i;

function setContent(el, text) {
  if (!text) { el.innerHTML = ""; return; }
  el.innerHTML = renderMessage(text);
}

function renderMessage(text) {
  const cards = [];
  let stripped = text.replace(DISPATCH_TAG_RE, (_m, target, body) => {
    const idx = cards.length;
    cards.push({ target, body, complete: true });
    return `\n\n@@DISPATCH_CARD_${idx}@@\n\n`;
  });
  const openMatch = stripped.match(DISPATCH_OPEN_RE);
  if (openMatch) {
    const idx = cards.length;
    cards.push({ target: openMatch[1], body: openMatch[2], complete: false });
    stripped = stripped.replace(DISPATCH_OPEN_RE, `\n\n@@DISPATCH_CARD_${idx}@@\n\n`);
  }
  let html;
  try {
    html = window.marked
      ? marked.parse(stripped, { breaks: true, gfm: true })
      : escapeHtml(stripped).replace(/\n/g, "<br>");
  } catch {
    html = escapeHtml(stripped).replace(/\n/g, "<br>");
  }
  html = html.replace(/@@DISPATCH_CARD_(\d+)@@/g, (_m, i) => dispatchCardHtml(cards[+i]));
  if (window.DOMPurify) html = DOMPurify.sanitize(html, { ADD_TAGS: ["details", "summary"] });
  return html;
}

function dispatchCardHtml(card) {
  const status = card.complete ? "sent" : "writing tag…";
  const body = escapeHtml(card.body.trim());
  const preview = card.body.trim().split("\n")[0].slice(0, 80);
  return `<div class="dispatch-card">
    <span class="arrow">➜ dispatch</span>
    <span class="target">${escapeHtml(card.target)}</span>
    <span style="color:var(--text-dim)">• ${escapeHtml(status)}</span>
    <details><summary>${escapeHtml(preview)} (xem task)</summary><pre>${body}</pre></details>
  </div>`;
}

function bindChatWindow(w) {
  const form = w.el.querySelector(".chat-form");
  const input = w.el.querySelector(".chat-input");
  const sendBtn = w.el.querySelector(".chat-send");

  form.onsubmit = (e) => e.preventDefault();

  const stopBtn = w.el.querySelector(".chat-stop");
  if (stopBtn) stopBtn.onclick = (e) => { e.preventDefault(); stopChat(w); };

  sendBtn.onclick = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    if (text.startsWith("/")) {
      input.value = "";
      hideCommandMenu(w);
      await executeChatCommand(w, text);
      return;
    }
    input.value = "";
    hideCommandMenu(w);
    // While a turn is streaming, a normal message goes into the per-window
    // queue and is auto-sent when the current turn finishes — instead of the
    // old behaviour where the button only meant "Stop".
    if (w.streaming) { enqueueMessage(w, text); return; }
    await sendMessageInWindow(w, text);
  };

  input.addEventListener("input", () => maybeShowCommandMenu(w, input.value));

  input.addEventListener("keydown", (e) => {
    // IME composition guard. Vietnamese Telex/VNI (and Chinese/Japanese IMEs)
    // fire keydown with key="Enter" while the IME is still composing, BEFORE
    // the user actually wants to submit. If we treat that as submit, the real
    // Enter that follows hits the now-streaming state and aborts the request.
    // The keyCode 229 fallback covers older browsers that don't expose
    // isComposing.
    const composing = e.isComposing || e.keyCode === 229;
    if (composing) return;

    // command menu navigation
    if (w.commandMenu) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        commandMenuMove(w, +1);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        commandMenuMove(w, -1);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        const items = w.commandMenu.items;
        pickCommand(w, items[w.commandMenu.selectedIdx].cmd);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        hideCommandMenu(w);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const items = w.commandMenu.items;
        const m = items[w.commandMenu.selectedIdx];
        if (!input.value.includes(" ")) {
          pickCommand(w, m.cmd);
        } else {
          sendBtn.click();
        }
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendBtn.click();
    }
    if (e.key === "Escape" && w.streaming) {
      e.preventDefault();
      stopChat(w);
    }
  });
}

async function stopChat(w) {
  // Stop = full halt: drop anything still queued, then cancel the run ON THE
  // SERVER. Since turns are detached from the SSE connection, aborting the
  // local fetch alone would only stop *watching* — the turn would keep
  // running. (Clearing the queue first means the finally→drainQueue sees an
  // empty queue and does not auto-fire the next message.)
  if (w.queue && w.queue.length) {
    w.queue = [];
    saveQueue(w);     // Stop is an explicit discard — clear the persisted copy too
    renderQueue(w);   // queue is the source of truth — empty it, then repaint (removes bubbles)
  }
  setChatStatus(w, "stopping…");
  let stopped = false;
  try {
    const r = await fetch(`/api/projects/${w.projectSlug}/agents/${w.agentId}/stop`,
      { method: "POST" });
    if (r.ok) stopped = !!(await r.json()).stopped;
  } catch {}
  // Fallback for attached-style streams with no server run (e.g. /compact):
  // abort the local reader.
  if (!stopped && w.abortController) {
    try { w.abortController.abort(); } catch {}
  }
}

// ---------- Chat queue (send while streaming) ----------

// ---------- Queue persistence ----------
// A queued message exists ONLY here until its turn fires — it has never reached the
// server, so nothing else in the system has a copy. Keeping it in RAM alone meant a
// reload (or a crash, or closing the tab) destroyed unsent user messages with no trace:
// the "tin nhắn bị mất" report. localStorage makes the queue survive the page.
function queueKey(w) { return `queue:${w.projectSlug}:${w.agentId}`; }

function saveQueue(w) {
  try {
    const items = (w.queue || []).map((q) => q.text);
    if (items.length) localStorage.setItem(queueKey(w), JSON.stringify(items));
    else localStorage.removeItem(queueKey(w));
  } catch {}   // quota / private mode — never let persistence break sending
}

function loadQueue(w) {
  try {
    const raw = localStorage.getItem(queueKey(w));
    if (!raw) return;
    const items = JSON.parse(raw);
    if (Array.isArray(items) && items.length) {
      w.queue = items.filter((t) => typeof t === "string" && t).map((text) => ({ text }));
      renderQueue(w);
      updateQueueStatus(w);
    }
  } catch {}
}

function enqueueMessage(w, text) {
  if (!w.queue) w.queue = [];
  w.queue.push({ text });
  saveQueue(w);
  renderQueue(w);
  updateQueueStatus(w);
}

// The queue is the source of truth; its bubbles are a pure projection of w.queue,
// rebuilt (never mutated in place) and always parked at the BOTTOM of .messages.
//
// Two bugs this fixes, both from treating a DOM node as the queue's storage:
//  1. Messages VANISHED. Each item used to hold its own .el, but refreshChatSession
//     wipes .messages wholesale. Its `if (w.streaming) return` guard does not cover
//     the queue: between drainQueue firing item #1 and sendMessageInWindow setting
//     streaming=true, any refresh (mirrorDispatchedMessages, openChat) erased the
//     bubbles of items #2+ while w.queue still held them — so they later fired out
//     of nowhere, having been invisible in the meantime.
//  2. Messages DRIFTED UPWARD. enqueueMessage appended at the moment of typing, then
//     the still-running turn kept appending deltas/worker cards BELOW it, leaving the
//     pending question stranded mid-transcript looking already answered.
function renderQueue(w) {
  const root = w.el && w.el.querySelector(".messages");
  if (!root) return;
  root.querySelectorAll(".bubble.queued").forEach((el) => el.remove());
  // Flag: addBubble parks the queue at the bottom on every append, but these ARE the
  // queue bubbles being placed — reentering that from here would fight this loop.
  w._renderingQueue = true;
  try {
    (w.queue || []).forEach((q, i) => {
      const el = addBubble(w, "user", q.text);
      el.classList.add("queued");
      const role = el.querySelector(".role");
      if (role) role.innerHTML = `user <span class="queue-tag">⏳ queue #${i + 1}</span>`;
    });
  } finally {
    w._renderingQueue = false;
  }
}

// Keep pending bubbles last after anything else appends to the transcript.
function parkQueueAtBottom(w) {
  if (!w.queue || !w.queue.length) return;
  const root = w.el && w.el.querySelector(".messages");
  if (!root) return;
  root.querySelectorAll(".bubble.queued").forEach((el) => root.appendChild(el));
  root.scrollTop = root.scrollHeight;
}

function updateQueueStatus(w) {
  const n = (w.queue || []).length;
  if (w.streaming) {
    setChatStatus(w, n
      ? `running • ${n} queued (Esc/Stop to stop all)`
      : "running... (Esc to stop)");
  }
}

// PEEK, never shift here. The item stays queued (and persisted) until the server has
// actually accepted it — see dequeueSent(). Shifting first meant a failed POST (network
// drop, backend error) silently destroyed the message: it was already out of the queue,
// had never reached the server, and no copy existed anywhere.
//
// Keep-until-accepted has a flip side: on a PERSISTENT failure the head item never
// leaves, and since sendMessageInWindow's finally re-drains unconditionally, a plain
// retry here would hammer the server in a tight loop (send→fail→finally→send…).
// _drainBackoff breaks that: a failed attempt (set in the failure paths) delays the
// next drain and schedules exactly one deferred retry.
const _QUEUE_RETRY_MS = 15000;
function drainQueue(w) {
  if (!w.queue || !w.queue.length) return;
  const now = Date.now();
  if (w._drainBackoff && now < w._drainBackoff) {
    if (!w._drainTimer) {
      w._drainTimer = setTimeout(() => {
        w._drainTimer = null;
        if (!w.streaming) drainQueue(w);
      }, w._drainBackoff - now + 200);
    }
    return;
  }
  const item = w.queue[0];
  // Fire the next turn. Its own finally calls drainQueue again, chaining until
  // the queue is empty. Not awaited — let it run as the next streaming turn.
  sendMessageInWindow(w, item.text, { fromQueue: true });
}

// Called only once the server has taken the message (2xx on POST /chat). Until then a
// reload replays it from localStorage instead of losing it.
function dequeueSent(w, text) {
  if (!w.queue || !w.queue.length) return;
  const i = w.queue.findIndex((q) => q.text === text);
  if (i === -1) return;
  w.queue.splice(i, 1);
  saveQueue(w);
  renderQueue(w);
  updateQueueStatus(w);
}

// ---------- Slash commands ----------

// adapter: "*" = both, "claude" / "grok" = adapter-specific
const CHAT_COMMANDS = [
  { cmd: "/help",     adapter: "*",     hint: "",                                    desc: "list commands",                                exec: cmdHelp },
  { cmd: "/clear",    adapter: "*",     hint: "",                                    desc: "new session (old history kept in db)",     exec: cmdClear },
  { cmd: "/compact",  adapter: "*",     hint: "",                                    desc: "agent summarizes context → new session seeded with recap (reduces context %)", exec: cmdCompact },
  { cmd: "/model",    adapter: "*",     hint: "<...>",                               desc: "change this agent's model",                              exec: cmdModel },
  { cmd: "/adapter",  adapter: "*",     hint: "<claude|grok|deepseek|glm> [model]", desc: "switch adapter (e.g. claude→glm-5.2); no restart",          exec: cmdAdapter },
  { cmd: "/effort",   adapter: "*",     hint: "<default|low|medium|high|max>",       desc: "change this agent's effort",                             exec: cmdEffort },
  { cmd: "/focus",    adapter: "*",     hint: "<AGENT_ID>",                          desc: "open another agent's chat in this project",                 exec: cmdFocus },
  { cmd: "/dispatch", adapter: "*",     hint: "<AGENT_ID> <task>",                   desc: "open target agent's chat and send the task now",              exec: cmdDispatch },
  { cmd: "/stop",     adapter: "*",     hint: "",                                    desc: "stop the current stream",                             exec: cmdStop },
  { cmd: "/status",   adapter: "*",     hint: "",                                    desc: "session, model, effort, next-options status",  exec: cmdStatus },
  { cmd: "/schedule", adapter: "*",     hint: "<30m|once 2h> <task>",                desc: "recurring or one-shot scheduled run of this agent", exec: cmdSchedule },
  { cmd: "/track",    adapter: "*",     hint: "<30m> <goal task>",                   desc: "goal loop: re-run every interval until the agent finishes", exec: cmdTrack },
  { cmd: "/schedules",adapter: "*",     hint: "",                                    desc: "list this project's schedules",                       exec: cmdSchedules },
  { cmd: "/unschedule",adapter: "*",    hint: "<id>",                                desc: "cancel a schedule by id",                             exec: cmdUnschedule },
  // grok-only one-shot modifiers (consumed by next message)
  { cmd: "/best-of",  adapter: "grok",  hint: "<2..5>",                              desc: "NEXT turn: run N attempts in parallel, pick best",    exec: cmdBestOf },
  { cmd: "/check",    adapter: "grok",  hint: "",                                    desc: "NEXT turn: add self-verification loop",             exec: cmdCheck },
  { cmd: "/memory",   adapter: "grok",  hint: "<on|off>",                            desc: "NEXT turn: toggle cross-session memory",            exec: cmdMemory },
  { cmd: "/reset-next", adapter: "*",   hint: "",                                    desc: "cancel next-options already set (best-of, check, memory)",  exec: cmdResetNext },
];

function _agentAdapter(w) {
  const proj = state.projectCache[w.projectSlug];
  const a = proj?.agents.find((x) => x.id === w.agentId);
  return a?.model || "claude";
}

function _hintForCmd(c, adapter) {
  if (c.cmd === "/model") {
    return adapter === "grok"
      ? "<grok-build|grok-composer>"
      : "<fable-5|opus-4-8|opus-4-7|sonnet|haiku>";
  }
  return c.hint;
}

function maybeShowCommandMenu(w, text) {
  if (!text.startsWith("/")) { hideCommandMenu(w); return; }
  const space = text.indexOf(" ");
  const head = space === -1 ? text.toLowerCase() : text.slice(0, space).toLowerCase();
  const adapter = _agentAdapter(w);
  const matches = CHAT_COMMANDS
    .filter((c) => c.adapter === "*" || c.adapter === adapter)
    .filter((c) => c.cmd.startsWith(head))
    .map((c) => ({ ...c, hint: _hintForCmd(c, adapter) }));
  if (!matches.length) { hideCommandMenu(w); return; }
  showCommandMenu(w, matches);
}

function showCommandMenu(w, items) {
  let menu = w.el.querySelector(".command-menu");
  if (!menu) {
    menu = document.createElement("div");
    menu.className = "command-menu";
    w.el.querySelector(".chatbox").appendChild(menu);
  }
  menu.innerHTML = items.map((it, i) =>
    `<div class="command-item ${i === 0 ? "selected" : ""}" data-idx="${i}">
      <span class="ci-cmd">${escapeHtml(it.cmd)}</span>
      <span class="ci-hint">${escapeHtml(it.hint || "")}</span>
      <span class="ci-desc">${escapeHtml(it.desc)}</span>
    </div>`).join("");
  menu.style.display = "block";
  w.commandMenu = { items, selectedIdx: 0 };
  menu.querySelectorAll(".command-item").forEach((el, i) => {
    el.onmousedown = (e) => {
      e.preventDefault();
      pickCommand(w, items[i].cmd);
    };
  });
}

function commandMenuMove(w, delta) {
  if (!w.commandMenu) return;
  const m = w.commandMenu;
  m.selectedIdx = (m.selectedIdx + delta + m.items.length) % m.items.length;
  w.el.querySelectorAll(".command-item").forEach((el, i) =>
    el.classList.toggle("selected", i === m.selectedIdx));
}

function hideCommandMenu(w) {
  const menu = w.el.querySelector(".command-menu");
  if (menu) menu.style.display = "none";
  w.commandMenu = null;
}

function pickCommand(w, cmd) {
  const input = w.el.querySelector(".chat-input");
  input.value = cmd + " ";
  hideCommandMenu(w);
  input.focus();
  // shift cursor to end
  input.selectionStart = input.selectionEnd = input.value.length;
}

async function executeChatCommand(w, text) {
  const space = text.indexOf(" ");
  const cmd = (space === -1 ? text : text.slice(0, space)).toLowerCase();
  const arg = space === -1 ? "" : text.slice(space + 1).trim();
  const handler = CHAT_COMMANDS.find((c) => c.cmd === cmd);
  if (!handler) {
    addSystemBubble(w, `❓ invalid command: \`${cmd}\`. Type \`/help\` for the list.`);
    return;
  }
  await handler.exec(w, arg);
}

function addSystemBubble(w, markdown) {
  const root = w.el.querySelector(".messages");
  const b = document.createElement("div");
  b.className = "bubble system";
  b.innerHTML = `<div class="content"></div>`;
  setContent(b.querySelector(".content"), markdown);
  root.appendChild(b);
  root.scrollTop = root.scrollHeight;
}

async function cmdHelp(w) {
  const adapter = _agentAdapter(w);
  const lines = [`**Slash commands** (adapter: \`${adapter}\`)`, ""];
  for (const c of CHAT_COMMANDS) {
    if (c.adapter !== "*" && c.adapter !== adapter) continue;
    const hint = _hintForCmd(c, adapter);
    const sig = hint ? `\`${c.cmd}\` \`${hint}\`` : `\`${c.cmd}\``;
    lines.push(`- ${sig} — ${c.desc}`);
  }
  lines.push("", "Input shortcuts: Tab to pick a command, ↑↓ to browse, Esc to close the menu / stop the stream.");
  addSystemBubble(w, lines.join("\n"));
}

async function cmdClear(w) {
  const slug = w.projectSlug, agent = w.agentId;
  try {
    await fetch(`/api/projects/${slug}/agents/${agent}/clear`, { method: "POST" });
    await refreshChatSession(w);
    ensureStats(slug, true);
    addSystemBubble(w, "✓ new session created. Old history remains in db, not deleted permanently.");
  } catch (err) {
    addSystemBubble(w, "error creating new session: " + err.message);
  }
}

async function cmdCompact(w) {
  if (w.streaming) { addSystemBubble(w, "streaming — type /stop before compacting."); return; }
  const slug = w.projectSlug, agent = w.agentId;
  addSystemBubble(w, "📦 Compacting: agent is summarizing its current context…");
  const b = addBubble(w, "assistant", "");
  b.querySelector(".role").textContent = "compact • " + agent;
  const contentEl = b.querySelector(".content");
  let assembled = "";
  w.streaming = true;
  setSendBtn(w, "stop");
  setChatStatus(w, "compacting… (Esc to stop)");
  try {
    w.abortController = new AbortController();
    const resp = await fetch(`/api/projects/${slug}/agents/${agent}/compact`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      signal: w.abortController.signal,
    });
    if (!resp.ok || !resp.body) {
      setContent(contentEl, `(backend error ${resp.status})`);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let ok = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const chunk of parts) {
        const line = chunk.trim();
        if (!line.startsWith("data:")) continue;
        let evt; try { evt = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (evt.type === "delta") {
          assembled += evt.text;
          contentEl.textContent = assembled;
          const m = w.el.querySelector(".messages"); m.scrollTop = m.scrollHeight;
        } else if (evt.type === "compacted") {
          ok = true;
        } else if (evt.type === "error") {
          addSystemBubble(w, "compact error: " + (evt.message || ""));
        }
      }
    }
    if (ok) {
      // The new seeded session is now active; reload it (shows the recap boundary).
      await refreshChatSession(w);
      ensureStats(slug, true);
      addSystemBubble(w, "✓ Compacted. New session seeded with recap — the next turn continues with a smaller context. Old session still kept in db.");
    } else {
      setContent(contentEl, assembled || "(compact incomplete)");
    }
  } catch (err) {
    if (err.name !== "AbortError") addSystemBubble(w, "compact error: " + err.message);
    else setChatStatus(w, "stopped");
  } finally {
    w.streaming = false;
    w.abortController = null;
    setSendBtn(w, "send");
  }
}

const _CLAUDE_MODEL_ALIAS = {
  "fable-5": "claude-fable-5",
  "fable": "claude-fable-5",
  "opus-4-8": "claude-opus-4-8",
  "opus-4-7": "claude-opus-4-7",
  "opus": "claude-opus-4-8",
  "sonnet": "claude-sonnet-5",
  "sonnet-5": "claude-sonnet-5",
  "sonnet-4-6": "claude-sonnet-4-6",
  "haiku": "claude-haiku-4-5",
  "haiku-4-5": "claude-haiku-4-5",
};
const _GROK_MODEL_ALIAS = {
  "build": "grok-build",
  "grok-build": "grok-build",
  "composer": "grok-composer-2.5-fast",
  "grok-composer": "grok-composer-2.5-fast",
  "composer-2.5": "grok-composer-2.5-fast",
  "fast": "grok-composer-2.5-fast",
};
const _DEEPSEEK_MODEL_ALIAS = {
  "flash": "deepseek-v4-flash",
  "deepseek-v4-flash": "deepseek-v4-flash",
  "v4-flash": "deepseek-v4-flash",
  "pro": "deepseek-v4-pro",
  "deepseek-v4-pro": "deepseek-v4-pro",
  "v4-pro": "deepseek-v4-pro",
  // legacy aliases (retire 2026-07-24)
  "chat": "deepseek-chat",
  "reasoner": "deepseek-reasoner",
};
const _GLM_MODEL_ALIAS = {
  "4.6": "glm-4.6",
  "glm-4.6": "glm-4.6",
  "5.2": "glm-5.2",
  "glm-5.2": "glm-5.2",
};

async function cmdAdapter(w, arg) {
  const slug = w.projectSlug;
  const proj = state.projectCache[slug];
  const agent = proj?.agents.find((a) => a.id === w.agentId);
  const parts = (arg || "").trim().split(/\s+/).filter(Boolean);
  const VALID = { claude: "claude-sonnet-4-6", grok: "grok-build", deepseek: "deepseek-v4-flash", glm: "glm-4.6" };

  // Infer adapter from a model id (so `/adapter glm-5.2` works, not just `/adapter glm`).
  const inferAdapter = (tok) => {
    const t = tok.toLowerCase();
    if (t.startsWith("glm-")) return "glm";
    if (t.startsWith("deepseek-")) return "deepseek";
    if (t.startsWith("grok-")) return "grok";
    if (t.startsWith("claude-") || ["fable-5","opus-4-8","opus-4-7","sonnet","sonnet-5","haiku"].includes(t)) return "claude";
    return null;
  };

  if (!parts.length) {
    addSystemBubble(w, "Syntax: `/adapter <claude|grok|deepseek|glm> [model]` OR `/adapter <model-id>` — e.g. `/adapter glm glm-5.2` or `/adapter glm-5.2`. Switches the endpoint/adapter of this agent (no restart).");
    return;
  }
  let effAdapter, model;
  const first = parts[0].toLowerCase();
  if (first in VALID) {
    // `/adapter <adapter> [model]`
    effAdapter = first;
    model = parts[1] || null;
  } else {
    // `/adapter <model-id>` — infer adapter from the model id itself
    const inferred = inferAdapter(first);
    if (!inferred) {
      addSystemBubble(w, `invalid adapter/model: \`${parts[0]}\`. Use an adapter (claude|grok|deepseek|glm) or a model id (e.g. glm-5.2, opus-4-8).`);
      return;
    }
    effAdapter = inferred;
    model = parts[0];
  }
  const hint = w.el.querySelector(".save-hint");
  if (hint) { hint.textContent = "switching…"; hint.style.color = "var(--text-dim)"; }
  try {
    const r = await fetch(`/api/projects/${slug}/agents/${w.agentId}/adapter`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ adapter: effAdapter, model }),
    });
    if (!r.ok) throw new Error("adapter switch failed");
    const pr = await fetch(`/api/projects/${slug}`);
    state.projectCache[slug] = await pr.json();
    rerenderGraphsForSlug(slug);
    state.windows
      .filter((x) => x.type === "chat" && x.projectSlug === slug && x.agentId === w.agentId)
      .forEach((x) => renderChatHeader(x));
    const cur = (effAdapter === "glm" ? (model || "glm-4.6") : (model || VALID[effAdapter]));
    addSystemBubble(w, `✓ adapter → \`${effAdapter}\` (model \`${cur}\`) — applies from the next chat turn`);
    if (hint) { hint.textContent = "✓ applies next turn"; hint.style.color = "var(--ok)"; }
  } catch (e) {
    addSystemBubble(w, `✗ ${e.message}`);
    if (hint) { hint.textContent = "✗ failed"; hint.style.color = "var(--err)"; }
  }
}

async function cmdModel(w, arg) {
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  const isGrok = agent.model === "grok";
  const isDeepseek = agent.model === "deepseek";
  const isGlm = agent.model === "glm";

  if (!arg) {
    addSystemBubble(w, isGrok
      ? "Syntax: `/model <grok-build|grok-composer>`"
      : isDeepseek
      ? "Syntax: `/model <deepseek-v4-flash|deepseek-v4-pro>`"
      : isGlm
      ? "Syntax: `/model <glm-4.6|glm-5.2>`"
      : "Syntax: `/model <fable-5|opus-4-8|opus-4-7|sonnet|haiku>`");
    return;
  }

  const eff = agent.effort ?? "";
  if (isGrok) {
    const target = _GROK_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("grok-") ? arg : null);
    if (!target) { addSystemBubble(w, `invalid grok model: \`${arg}\``); return; }
    await updateAgentSettings(w, null, target, null, null, eff);
    addSystemBubble(w, `✓ grok_model → \`${target}\` (applies from the next chat turn)`);
    return;
  }
  if (isDeepseek) {
    const target = _DEEPSEEK_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("deepseek-") ? arg : null);
    if (!target) { addSystemBubble(w, `invalid deepseek model: \`${arg}\``); return; }
    await updateAgentSettings(w, null, null, target, null, eff);
    addSystemBubble(w, `✓ deepseek_model → \`${target}\` (applies from the next chat turn)`);
    return;
  }
  if (isGlm) {
    const target = _GLM_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("glm-") ? arg : null);
    if (!target) { addSystemBubble(w, `invalid glm model: \`${arg}\``); return; }
    await updateAgentSettings(w, null, null, null, target, eff);
    addSystemBubble(w, `✓ glm_model → \`${target}\` (applies from the next chat turn)`);
    return;
  }

  const target = _CLAUDE_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("claude-") ? arg : null);
  if (!target) { addSystemBubble(w, `invalid claude model: \`${arg}\``); return; }
  await updateAgentSettings(w, target, null, null, null, eff);
  addSystemBubble(w, `✓ claude_model → \`${target}\` (applies from the next chat turn)`);
}

async function cmdEffort(w, arg) {
  const allowed = ["default", "low", "medium", "high", "xhigh", "max"];
  if (!arg || !allowed.includes(arg.toLowerCase())) {
    addSystemBubble(w, "Syntax: `/effort default|low|medium|high|xhigh|max`");
    return;
  }
  const eff = arg.toLowerCase() === "default" ? "" : arg.toLowerCase();
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  if (agent.model === "grok") {
    await updateAgentSettings(w, null, agent.grok_model || "grok-build", null, null, eff);
  } else if (agent.model === "deepseek") {
    await updateAgentSettings(w, null, null, agent.deepseek_model || "deepseek-v4-flash", null, eff);
  } else if (agent.model === "glm") {
    await updateAgentSettings(w, null, null, null, agent.glm_model || "glm-4.6", eff);
  } else {
    await updateAgentSettings(w, agent.claude_model || "claude-sonnet-4-6", null, null, null, eff);
  }
  addSystemBubble(w, `✓ effort → \`${arg}\``);
}

async function cmdFocus(w, arg) {
  if (!arg) { addSystemBubble(w, "Syntax: `/focus <AGENT_ID>`"); return; }
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id.toUpperCase() === arg.toUpperCase());
  if (!agent) { addSystemBubble(w, `agent not found: \`${arg}\``); return; }
  openChat(w.projectSlug, agent.id);
}

async function cmdDispatch(w, arg) {
  const space = arg.indexOf(" ");
  if (space === -1) {
    addSystemBubble(w, "Syntax: `/dispatch <AGENT_ID> <task>`");
    return;
  }
  const targetId = arg.slice(0, space).trim();
  const task = arg.slice(space + 1).trim();
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id.toUpperCase() === targetId.toUpperCase());
  if (!agent) { addSystemBubble(w, `agent not found: \`${targetId}\``); return; }
  const tw = openChat(w.projectSlug, agent.id);
  // small delay so the new window has bound its DOM before we call into it
  setTimeout(() => sendMessageInWindow(tw, task), 50);
}

async function cmdStop(w) {
  if (!w.streaming) { addSystemBubble(w, "no stream is running."); return; }
  stopChat(w);
}

async function _postSchedule(w, body) {
  const slug = w.projectSlug;
  try {
    const r = await fetch(`/api/projects/${slug}/schedules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: w.agentId, ...body }),
    });
    if (!r.ok) { addSystemBubble(w, `❌ schedule failed: ${escapeHtml(await r.text())}`); return; }
    const s = (await r.json()).schedule;
    const arr = state.schedules[slug] || (state.schedules[slug] = []);
    arr.push(s);
    addSystemBubble(w, `🕒 Scheduled (${schedLabel(s)}) → fires ${fmtCountdown(s.next_run_at)}. See the 🕒 Schedules panel / graph badge.`);
    renderScheduleDropdown();
    rerenderGraphsForSlug(slug);
  } catch (e) {
    addSystemBubble(w, `❌ schedule error: ${escapeHtml(String(e))}`);
  }
}

async function cmdSchedule(w, arg) {
  arg = (arg || "").trim();
  if (!arg) { addSystemBubble(w, "Syntax: `/schedule 30m <task>` (repeat) or `/schedule once 2h <task>` (one-shot). Min interval 5m."); return; }
  if (arg.toLowerCase().startsWith("once ")) {
    const rest = arg.slice(5).trim();
    const sp = rest.indexOf(" ");
    if (sp === -1) { addSystemBubble(w, "Syntax: `/schedule once <2h> <task>`"); return; }
    await _postSchedule(w, { delay: rest.slice(0, sp).trim(), prompt: rest.slice(sp + 1).trim() });
    return;
  }
  const sp = arg.indexOf(" ");
  if (sp === -1) { addSystemBubble(w, "Syntax: `/schedule <30m> <task>`"); return; }
  await _postSchedule(w, { every: arg.slice(0, sp).trim(), prompt: arg.slice(sp + 1).trim() });
}

async function cmdTrack(w, arg) {
  arg = (arg || "").trim();
  const sp = arg.indexOf(" ");
  if (sp === -1) { addSystemBubble(w, "Syntax: `/track <30m> <goal task>` — re-runs until the agent emits done. Min 5m."); return; }
  const every = arg.slice(0, sp).trim();
  const task = arg.slice(sp + 1).trim();
  await _postSchedule(w, { every, until: task, prompt: task });
}

async function cmdSchedules(w) {
  const slug = w.projectSlug;
  await refreshSchedulesActiveTab();
  const all = state.schedules[slug] || [];
  if (!all.length) { addSystemBubble(w, "No schedules in this project. Use `/schedule` or `/track`."); return; }
  const lines = ["**Schedules** (this project)", ""];
  for (const s of all) {
    const when = s.active ? `next ${fmtCountdown(s.next_run_at)}` : "stopped";
    lines.push(`- \`#${s.id}\` ${s.agent_id} — ${schedLabel(s)} · ${when}${s.until_goal ? ` · until: ${s.until_goal.slice(0, 50)}` : ""} — cancel with \`/unschedule ${s.id}\``);
  }
  addSystemBubble(w, lines.join("\n"));
}

async function cmdUnschedule(w, arg) {
  const id = (arg || "").trim().replace(/^#/, "");
  if (!id) { addSystemBubble(w, "Syntax: `/unschedule <id>` (see `/schedules`)"); return; }
  await deleteSchedule(w.projectSlug, id);
  addSystemBubble(w, `🕒 schedule #${id} cancelled.`);
}

// ---------- Add agent dialog ----------

// ---------- New project dialog ----------

function openNewProjectDialog() {
  let overlay = document.getElementById("newProjectOverlay");
  if (overlay) overlay.remove();
  overlay = document.createElement("div");
  overlay.id = "newProjectOverlay";
  overlay.className = "modal-overlay";

  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <span>Create new project</span>
        <button class="modal-close" title="close">×</button>
      </div>
      <form class="modal-body" id="newProjectForm">
        <div class="form-row">
          <label class="full">Folder path (absolute)
            <input name="root" type="text" autocomplete="off" spellcheck="false"
              placeholder="VD: /users/PGS0407/binben14/VietHuy/MyProject" />
            <span class="hint" id="npPathHint">Existing code folder or a new one. shared/ + sync.sh + agent folders will be scaffolded here.</span>
          </label>
        </div>
        <div class="form-row">
          <label>Project name
            <input name="name" type="text" placeholder="auto from folder name if empty" />
          </label>
          <label>Slug
            <input name="slug" type="text" placeholder="auto" autocomplete="off" />
          </label>
        </div>
        <div class="form-row">
          <label class="full">Description (1 line)
            <input name="description" type="text" placeholder="e.g. VLM benchmark on dataset X" />
          </label>
        </div>

        <div class="form-row">
          <label class="full">Agents (graph definition — leave empty to add later via "+ agent")
            <div id="npAgents" class="np-agents"></div>
            <button type="button" class="btn-secondary" id="npAddAgent" style="margin-top:6px">+ add agent</button>
            <span class="hint">Parents = comma-separated IDs of parent agents in this list. An agent with no parent = orchestrator.</span>
          </label>
        </div>

        <div class="modal-msg" id="newProjectMsg"></div>
        <div class="modal-actions">
          <button type="button" class="btn-secondary" id="cancelNewProject">Cancel</button>
          <button type="submit" class="btn-primary">Create project</button>
        </div>
      </form>
    </div>`;

  document.body.appendChild(overlay);
  const form = overlay.querySelector("#newProjectForm");
  const msg = overlay.querySelector("#newProjectMsg");
  const agentsBox = overlay.querySelector("#npAgents");
  const pathHint = overlay.querySelector("#npPathHint");

  function addAgentRow(preset) {
    const row = document.createElement("div");
    row.className = "np-agent-row";
    row.innerHTML = `
      <input class="np-id" placeholder="ID" pattern="[A-Z][A-Z0-9_]*" autocomplete="off"
        value="${preset && preset.id ? escapeHtml(preset.id) : ""}" />
      <input class="np-role" placeholder="role (1 line)"
        value="${preset && preset.role ? escapeHtml(preset.role) : ""}" />
      <select class="np-model">
        <option value="claude" selected>claude</option>
        <option value="grok">grok</option>
        <option value="deepseek">deepseek</option>
        <option value="glm">glm</option>
      </select>
      <input class="np-parents" placeholder="parents (CSV)"
        value="${preset && preset.parents ? escapeHtml(preset.parents) : ""}" />
      <button type="button" class="np-del" title="delete">×</button>`;
    row.querySelector(".np-del").onclick = () => row.remove();
    agentsBox.appendChild(row);
  }
  // seed a sensible default orchestrator
  addAgentRow({ id: "BOSS", role: "Orchestrator — analyze requests, dispatch to child agents", parents: "" });
  overlay.querySelector("#npAddAgent").onclick = () => addAgentRow();

  // live path validation
  const rootInput = form.querySelector('input[name="root"]');
  let pathTimer = null;
  rootInput.addEventListener("input", () => {
    clearTimeout(pathTimer);
    const p = rootInput.value.trim();
    if (!p) { pathHint.textContent = "Existing code folder or a new one."; pathHint.className = "hint"; return; }
    pathTimer = setTimeout(async () => {
      try {
        const r = await fetch(`/api/fs/validate?path=${encodeURIComponent(p)}`);
        const j = await r.json();
        if (j.is_project) { pathHint.textContent = "⚠ Folder is already an AgentUI project."; pathHint.className = "hint err"; }
        else if (!j.exists) { pathHint.textContent = j.parent_exists ? "✓ Will create a new folder here." : "⚠ Parent folder does not exist."; pathHint.className = j.parent_exists ? "hint ok" : "hint err"; }
        else if (j.is_dir) { pathHint.textContent = j.non_empty ? "✓ Folder exists (scaffold will be added, no files deleted)." : "✓ Folder is empty."; pathHint.className = "hint ok"; }
        else { pathHint.textContent = "⚠ Path exists but is not a folder."; pathHint.className = "hint err"; }
      } catch { /* ignore */ }
    }, 350);
  });

  function close() { overlay.remove(); }
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector("#cancelNewProject").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });

  form.onsubmit = async (e) => {
    e.preventDefault();
    msg.textContent = "";
    const root = rootInput.value.trim();
    if (!root) { msg.textContent = "Folder path is required."; msg.className = "modal-msg err"; return; }
    const agents = [];
    for (const row of agentsBox.querySelectorAll(".np-agent-row")) {
      const id = row.querySelector(".np-id").value.trim();
      if (!id) continue;
      const parents = row.querySelector(".np-parents").value.split(",").map((s) => s.trim()).filter(Boolean);
      agents.push({
        id,
        role: row.querySelector(".np-role").value.trim(),
        model: row.querySelector(".np-model").value,
        parents,
      });
    }
    const payload = {
      root,
      name: form.querySelector('input[name="name"]').value.trim() || null,
      slug: form.querySelector('input[name="slug"]').value.trim() || null,
      description: form.querySelector('input[name="description"]').value.trim(),
      agents,
    };
    const btn = form.querySelector(".btn-primary");
    btn.disabled = true; btn.textContent = "Creating…";
    try {
      const r = await fetch("/api/projects/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) { msg.textContent = "Error: " + (j.detail || r.status); msg.className = "modal-msg err"; btn.disabled = false; btn.textContent = "Create project"; return; }
      state.projects = j.projects || state.projects;
      renderProjectList();
      close();
      await openProject(j.slug);
    } catch (err) {
      msg.textContent = "network error: " + err.message; msg.className = "modal-msg err";
      btn.disabled = false; btn.textContent = "Create project";
    }
  };
}

function openAddAgentDialog(slug) {
  const proj = state.projectCache[slug];
  if (!proj) return;
  const existing = proj.agents.map((a) => a.id);

  let overlay = document.getElementById("addAgentOverlay");
  if (overlay) overlay.remove();
  overlay = document.createElement("div");
  overlay.id = "addAgentOverlay";
  overlay.className = "modal-overlay";

  const claudeOpts = CLAUDE_MODELS.map((o) =>
    `<option value="${o.value}">${o.label}</option>`).join("");
  const grokOpts = `<option value="grok-build">grok-build</option>
    <option value="grok-composer-2.5-fast">grok-composer 2.5</option>`;
  const deepseekOpts = `<option value="deepseek-v4-flash">deepseek-v4-flash (fast)</option>
    <option value="deepseek-v4-pro">deepseek-v4-pro (top)</option>`;
  const glmOpts = `<option value="glm-4.6">glm-4.6 (200K)</option>
    <option value="glm-5.2">glm-5.2 (top, 1M)</option>`;
  const parentOpts = existing.map((id) =>
    `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");

  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <span>Add agent to <strong>${escapeHtml(proj.name)}</strong></span>
        <button class="modal-close" title="close">×</button>
      </div>
      <form class="modal-body" id="addAgentForm">
        <div class="form-row">
          <label>ID
            <input name="id" type="text" required pattern="[A-Z][A-Z0-9_]*"
              placeholder="VD: REVIEWER" autocomplete="off" />
          </label>
          <label>Role (1-line description)
            <input name="role" type="text"
              placeholder="VD: Adversarial code reviewer." />
          </label>
        </div>

        <div class="form-row">
          <label>Adapter
            <select name="model">
              <option value="claude" selected>claude</option>
              <option value="grok">grok</option>
              <option value="deepseek">deepseek</option>
              <option value="glm">glm</option>
            </select>
          </label>
          <label data-for="claude">Claude model
            <select name="claude_model">
              <option value="">(default sonnet 4.6)</option>
              ${claudeOpts}
            </select>
          </label>
          <label data-for="grok" style="display:none">Grok model
            <select name="grok_model">
              <option value="">(default grok-build)</option>
              ${grokOpts}
            </select>
          </label>
          <label data-for="deepseek" style="display:none">DeepSeek model
            <select name="deepseek_model">
              <option value="">(default deepseek-v4-flash)</option>
              ${deepseekOpts}
            </select>
          </label>
          <label data-for="glm" style="display:none">GLM model
            <select name="glm_model">
              <option value="">(default glm-4.6)</option>
              ${glmOpts}
            </select>
          </label>
          <label>Effort
            <select name="effort">
              <option value="">default</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="xhigh">xhigh</option>
              <option value="max">max</option>
            </select>
          </label>
        </div>

        <div class="form-row">
          <label>System prompt file (relative)
            <input name="system_prompt_file" type="text"
              placeholder="e.g. REVIEWER/AGENT.md (may be left empty)" />
          </label>
          <label>cwd (relative)
            <input name="cwd" type="text" value="." />
          </label>
        </div>

        <div class="form-row">
          <label class="full">Parents (orchestrators that can dispatch to this agent)
            <select name="parents" multiple size="${Math.min(6, Math.max(3, existing.length))}">
              ${parentOpts}
            </select>
            <span class="hint">Ctrl/Cmd+click to select multiple. Leave empty if this agent is a root.</span>
          </label>
        </div>

        <div class="form-row">
          <label class="full">How to generate the bootstrap files
            <div class="radio-group">
              <label class="radio-inline"><input type="radio" name="bootstrap_mode" value="from_parent" checked>
                <span>Let the first parent agent write them (based on the project context the parent knows)</span></label>
              <label class="radio-inline"><input type="radio" name="bootstrap_mode" value="template">
                <span>Use a generic template (fast, no quota cost)</span></label>
            </div>
            <span class="hint">The parent streams output in realtime; you review before anything is written to disk.</span>
          </label>
        </div>

        <div class="modal-msg" id="addAgentMsg"></div>
        <div class="modal-actions">
          <button type="button" class="btn-secondary" id="cancelAddAgent">Cancel</button>
          <button type="submit" class="btn-primary">Create agent</button>
        </div>
      </form>
    </div>`;

  document.body.appendChild(overlay);

  const form = overlay.querySelector("#addAgentForm");
  const msg = overlay.querySelector("#addAgentMsg");
  const modelSelect = form.querySelector('select[name="model"]');
  const claudeWrap = form.querySelector('[data-for="claude"]');
  const grokWrap = form.querySelector('[data-for="grok"]');
  const deepseekWrap = form.querySelector('[data-for="deepseek"]');
  const glmWrap = form.querySelector('[data-for="glm"]');

  function syncAdapter() {
    const v = modelSelect.value;
    claudeWrap.style.display = v === "claude" ? "" : "none";
    grokWrap.style.display = v === "grok" ? "" : "none";
    deepseekWrap.style.display = v === "deepseek" ? "" : "none";
    glmWrap.style.display = v === "glm" ? "" : "none";
  }
  modelSelect.onchange = syncAdapter;
  syncAdapter();

  function close() { overlay.remove(); }
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector("#cancelAddAgent").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });

  let stage = "form";  // 'form' | 'generating' | 'preview'
  let currentBody = null;
  let previewFiles = null;  // [{path, content}] to send on create

  function readForm() {
    const fd = new FormData(form);
    const parents = Array.from(form.querySelector('select[name="parents"]').selectedOptions)
      .map((o) => o.value);
    const adapter = fd.get("model");
    const isGrok = adapter === "grok";
    const isDeepseek = adapter === "deepseek";
    const isGlm = adapter === "glm";
    return {
      id: (fd.get("id") || "").trim().toUpperCase(),
      role: (fd.get("role") || "").trim(),
      model: adapter,
      claude_model: (isGrok || isDeepseek || isGlm) ? null : ((fd.get("claude_model") || "").trim() || null),
      grok_model: isGrok ? ((fd.get("grok_model") || "").trim() || null) : null,
      deepseek_model: isDeepseek ? ((fd.get("deepseek_model") || "").trim() || null) : null,
      glm_model: isGlm ? ((fd.get("glm_model") || "").trim() || null) : null,
      effort: (fd.get("effort") || "").trim() || null,
      system_prompt_file: (fd.get("system_prompt_file") || "").trim() || null,
      cwd: (fd.get("cwd") || ".").trim() || ".",
      parents,
      bootstrap_mode: fd.get("bootstrap_mode") || "from_parent",
    };
  }

  const submitBtn = form.querySelector('button[type="submit"]');

  function showPreview(data) {
    stage = "preview";
    previewFiles = data.files || [];
    submitBtn.textContent = "✓ Create agent";
    submitBtn.disabled = false;
    const cancelBtn = overlay.querySelector("#cancelAddAgent");
    cancelBtn.textContent = "← Back to edit";

    form.querySelectorAll(".form-row").forEach((row) => row.style.display = "none");
    const genPanel = form.querySelector(".gen-panel");
    if (genPanel) genPanel.style.display = "none";

    let preview = form.querySelector(".agent-preview");
    if (!preview) {
      preview = document.createElement("div");
      preview.className = "agent-preview";
      msg.parentNode.insertBefore(preview, msg);
    }
    const warnHtml = (data.warnings || []).length
      ? `<div class="preview-warnings">${data.warnings.map(w => `<div class="warn-row">⚠ ${escapeHtml(w)}</div>`).join("")}</div>`
      : "";
    const fileHtml = (data.files || []).map((f) => `
      <details class="preview-file" ${f.path.endsWith("AGENT.md") ? "open" : ""}>
        <summary>${escapeHtml(f.path)} <span class="file-size">${f.content.length}c</span></summary>
        <pre>${escapeHtml(f.content)}</pre>
      </details>`).join("");
    preview.innerHTML = `
      <div class="preview-header">
        <strong>Preview</strong>
        <span class="preview-target">${escapeHtml(data.target_folder || "")}</span>
      </div>
      ${warnHtml}
      <div class="preview-files">${fileHtml}</div>`;
  }

  function backToForm() {
    stage = "form";
    submitBtn.textContent = "Generate / Preview →";
    submitBtn.disabled = false;
    const cancelBtn = overlay.querySelector("#cancelAddAgent");
    cancelBtn.textContent = "Cancel";
    form.querySelectorAll(".form-row").forEach((row) => row.style.display = "");
    const preview = form.querySelector(".agent-preview");
    if (preview) preview.remove();
    const genPanel = form.querySelector(".gen-panel");
    if (genPanel) genPanel.remove();
    previewFiles = null;
  }

  let genStream = null;

  function showGenerating(parentId) {
    stage = "generating";
    submitBtn.textContent = "generating…";
    submitBtn.disabled = true;
    const cancelBtn = overlay.querySelector("#cancelAddAgent");
    cancelBtn.textContent = "Stop & go back";
    form.querySelectorAll(".form-row").forEach((row) => row.style.display = "none");
    let panel = form.querySelector(".gen-panel");
    if (panel) panel.remove();
    panel = document.createElement("div");
    panel.className = "gen-panel";
    panel.innerHTML = `
      <div class="gen-header">
        <strong>${escapeHtml(parentId)}</strong> is generating the bootstrap files…
      </div>
      <pre class="gen-output"></pre>`;
    msg.parentNode.insertBefore(panel, msg);
  }

  function abortGen() {
    if (genStream) { try { genStream.abort(); } catch {} genStream = null; }
  }

  // Override cancel button to support back-from-preview
  const cancelBtn = overlay.querySelector("#cancelAddAgent");
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = () => {
    if (stage === "preview") backToForm();
    else if (stage === "generating") { abortGen(); backToForm(); }
    else close();
  };

  // Initial submit label
  submitBtn.textContent = "Generate / Preview →";

  async function streamFromParent(body) {
    showGenerating(body.parents[0]);
    const outEl = form.querySelector(".gen-output");
    let assembled = "";
    const ctrl = new AbortController();
    genStream = ctrl;
    try {
      const resp = await fetch(`/api/projects/${slug}/agents/preview-from-parent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) {
        const t = await resp.text();
        msg.textContent = "error: " + (resp.status) + " " + t.slice(0, 200);
        msg.className = "modal-msg error";
        backToForm();
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let done = null;
      while (true) {
        const { value, done: doneRead } = await reader.read();
        if (doneRead) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const chunk of parts) {
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let evt;
          try { evt = JSON.parse(payload); } catch { continue; }
          if (evt.type === "delta" && evt.text) {
            assembled += evt.text;
            outEl.textContent = assembled.slice(-4000);
            outEl.scrollTop = outEl.scrollHeight;
          } else if (evt.type === "thinking") {
            outEl.dataset.thinking = "1";
          } else if (evt.type === "error") {
            msg.textContent = "parent error: " + (evt.message || "unknown");
            msg.className = "modal-msg error";
          } else if (evt.type === "bootstrap_done") {
            done = evt;
          }
        }
      }
      if (done) {
        msg.textContent = "";
        if (!done.files || !done.files.length) {
          msg.textContent = "parent emitted no file blocks. Retry or use the template.";
          msg.className = "modal-msg error";
          backToForm();
          return;
        }
        showPreview(done);
      } else {
        msg.textContent = "stream ended without receiving bootstrap_done.";
        msg.className = "modal-msg error";
        backToForm();
      }
    } catch (err) {
      if (err.name === "AbortError") return;
      msg.textContent = "network error: " + err.message;
      msg.className = "modal-msg error";
      backToForm();
    } finally {
      genStream = null;
    }
  }

  form.onsubmit = async (e) => {
    e.preventDefault();
    currentBody = readForm();
    if (stage === "form") {
      msg.textContent = "";
      const useParent = currentBody.bootstrap_mode === "from_parent" && currentBody.parents.length > 0;
      if (useParent) {
        await streamFromParent(currentBody);
      } else {
        msg.textContent = "generating template…";
        msg.className = "modal-msg working";
        try {
          const r = await fetch(`/api/projects/${slug}/agents/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(currentBody),
          });
          const j = await r.json();
          if (!r.ok) {
            msg.textContent = "error: " + (j.detail || r.statusText);
            msg.className = "modal-msg error";
            return;
          }
          msg.textContent = "";
          showPreview(j);
        } catch (err) {
          msg.textContent = "network error: " + err.message;
          msg.className = "modal-msg error";
        }
      }
      return;
    }
    // stage === "preview" — actually create
    msg.textContent = "writing folder + yaml…";
    msg.className = "modal-msg working";
    submitBtn.disabled = true;
    const payload = { ...currentBody, custom_files: previewFiles };
    delete payload.bootstrap_mode;
    try {
      const r = await fetch(`/api/projects/${slug}/agents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) {
        msg.textContent = "error: " + (j.detail || r.statusText);
        msg.className = "modal-msg error";
        submitBtn.disabled = false;
        return;
      }
      state.projectCache[slug] = j.project || state.projectCache[slug];
      rerenderGraphsForSlug(slug);
      // refresh workspace tree so the new folder shows up
      const t = _treeState();
      t.cache = {};
      await fetchTreeLevel("");
      renderTree();
      msg.textContent = "✓ created " + currentBody.id;
      msg.className = "modal-msg ok";
      setTimeout(close, 700);
    } catch (err) {
      msg.textContent = "network error: " + err.message;
      msg.className = "modal-msg error";
      submitBtn.disabled = false;
    }
  };
}

async function cmdStatus(w) {
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  const isGrok = agent.model === "grok";
  const isDeepseek = agent.model === "deepseek";
  const isGlm = agent.model === "glm";
  const cur = isGrok ? (agent.grok_model || "grok-build")
    : isDeepseek ? (agent.deepseek_model || "deepseek-v4-flash")
    : isGlm ? (agent.glm_model || "glm-4.6")
    : (agent.claude_model || "claude-sonnet-4-6");
  const def = isGrok ? (agent.default_grok_model || "grok-build")
    : isDeepseek ? (agent.default_deepseek_model || "deepseek-v4-flash")
    : isGlm ? (agent.default_glm_model || "glm-4.6")
    : (agent.default_claude_model || "claude-sonnet-4-6");
  const lines = [
    `**Agent**: \`${agent.id}\``,
    `**Project**: ${proj.name}`,
    `**Adapter**: ${agent.model || "claude"}`,
    `**Model**: \`${cur}\` (default: \`${def}\`)`,
    `**Effort**: \`${agent.effort || "default"}\``,
    `**Status**: ${proj.statuses[agent.id] || "idle"}`,
    `**Streaming**: ${w.streaming ? "yes" : "no"}`,
  ];
  if (isGrok && w.nextOptions) {
    const opts = [];
    if (w.nextOptions.best_of_n) opts.push(`best-of ${w.nextOptions.best_of_n}`);
    if (w.nextOptions.check_loop) opts.push("check");
    if (w.nextOptions.memory_mode) opts.push(`memory ${w.nextOptions.memory_mode}`);
    if (opts.length) lines.push(`**Next-options**: ${opts.join(", ")}`);
  }
  addSystemBubble(w, lines.join("\n"));
}

async function cmdBestOf(w, arg) {
  const n = parseInt(arg, 10);
  if (!n || n < 2 || n > 5) {
    addSystemBubble(w, "Syntax: `/best-of <2..5>`");
    return;
  }
  w.nextOptions = w.nextOptions || {};
  w.nextOptions.best_of_n = n;
  addSystemBubble(w, `✓ NEXT turn will run \`best-of ${n}\` (consumed after send)`);
}

async function cmdCheck(w) {
  w.nextOptions = w.nextOptions || {};
  w.nextOptions.check_loop = true;
  addSystemBubble(w, "✓ NEXT turn will add a self-verification loop (consumed after send)");
}

async function cmdMemory(w, arg) {
  const v = (arg || "").trim().toLowerCase();
  if (v !== "on" && v !== "off") {
    addSystemBubble(w, "Syntax: `/memory <on|off>`");
    return;
  }
  w.nextOptions = w.nextOptions || {};
  w.nextOptions.memory_mode = v;
  addSystemBubble(w, `✓ NEXT turn memory \`${v}\` (consumed after send)`);
}

async function cmdResetNext(w) {
  if (!w.nextOptions) {
    addSystemBubble(w, "no next-options to cancel.");
    return;
  }
  w.nextOptions = null;
  addSystemBubble(w, "✓ next-options cancelled.");
}

function setSendBtn(w, mode) {
  const stopBtn = w.el.querySelector(".chat-stop");
  if (stopBtn) stopBtn.hidden = (mode !== "stop");
  const btn = w.el.querySelector(".chat-send");
  if (!btn) return;
  btn.disabled = false;
  btn.classList.remove("stop");
  // The send button always sends; while a turn streams it adds to the queue.
  btn.textContent = (mode === "stop") ? "+ queue" : "Send";
}

function setChatStatus(w, s) {
  const el = w.el.querySelector(".chat-status");
  if (el) el.textContent = s;
}

function makeBubbleFactory(w, rootAgent) {
  const bubbles = {};
  function bubbleFor(agentId) {
    if (bubbles[agentId]) return bubbles[agentId];
    // Worker output renders INSIDE its dispatch card (collapsed by default);
    // only the root agent writes top-level bubbles.
    const card = (agentId !== rootAgent && w._workerCards && w._workerCards[agentId]) || null;
    const b = card
      ? addBubble(w, "assistant worker", "", card.querySelector(".wc-body"))
      : addBubble(w, "assistant", "");
    b.querySelector(".role").textContent = "assistant • " + agentId;
    const contentEl = b.querySelector(".content");

    const thinkBlock = document.createElement("div");
    thinkBlock.className = "thinking-block collapsed";
    thinkBlock.innerHTML = `
      <div class="think-header">
        <span class="think-toggle">▶</span>
        <span class="think-label">waiting for response…</span>
        <span class="think-count"></span>
      </div>
      <div class="think-body"></div>`;
    const thinkBody = thinkBlock.querySelector(".think-body");
    thinkBlock.querySelector(".think-header").onclick = () => {
      thinkBlock.classList.toggle("collapsed");
      thinkBlock.querySelector(".think-toggle").textContent =
        thinkBlock.classList.contains("collapsed") ? "▶" : "▼";
      if (!thinkBlock.classList.contains("collapsed")) {
        thinkBody.scrollTop = thinkBody.scrollHeight;
      }
    };
    b.insertBefore(thinkBlock, contentEl);

    bubbles[agentId] = {
      bubble: b,
      contentEl,
      thinkBlock,
      thinkBody,
      thinkLabel: thinkBlock.querySelector(".think-label"),
      thinkCount: thinkBlock.querySelector(".think-count"),
      assembled: "",
      thinkAccum: "",
      streamingStarted: false,
    };
    return bubbles[agentId];
  }
  // Drop the cached bubble so the next delta opens a fresh one — used between
  // continuation rounds to keep each round visually separate.
  bubbleFor.reset = (agentId) => { delete bubbles[agentId]; };
  return bubbleFor;
}

// ---------- Tool-access card (which files/commands an agent touched) ----------
// One collapsible card per agent bubble; each tool_use event appends a row so
// the user can SEE what the agent loaded/edited/ran instead of guessing.
function _toolItem(tool, input) {
  const t = (tool || "").toLowerCase();
  const j = input || {};
  // MCP tools arrive as mcp__server__method; show the leaf method name.
  const leaf = tool && tool.indexOf("__") >= 0 ? tool.split("__").pop() : tool;
  let glyph = "🛠", text = tool || "?";
  if (t === "read" && j.file_path) { glyph = "📖"; text = j.file_path; }
  else if (t === "edit" && j.file_path) { glyph = "✏️"; text = j.file_path; }
  else if (t === "write" && j.file_path) { glyph = "📝"; text = j.file_path; }
  else if (t === "glob" && j.pattern) { glyph = "🔎"; text = j.pattern; }
  else if (t === "grep" && (j.pattern || j.query)) { glyph = "🔎"; text = j.pattern || j.query; }
  else if (t === "bash" && j.command) { glyph = "$"; text = j.command; }
  else if (leaf && leaf !== tool) { glyph = "🔍"; text = leaf + (j.query ? ": " + j.query : ""); }
  return { glyph, text };
}
function ensureToolCard(b) {
  if (b.toolCard) return;
  const card = document.createElement("div");
  card.className = "tool-card";
  card.innerHTML = `
    <div class="tc-head"><span class="tc-glyph">📂</span><span class="tc-summary">0 accesses</span><span class="tc-toggle" title="show files/commands used this turn">▸</span></div>
    <div class="tc-body" hidden></div>`;
  const body = card.querySelector(".tc-body");
  card.querySelector(".tc-head").onclick = () => {
    body.hidden = !body.hidden;
    card.querySelector(".tc-toggle").textContent = body.hidden ? "▸" : "▾";
  };
  b.bubble.insertBefore(card, b.contentEl);
  b.toolCard = card;
  b.toolList = body;
  b.toolCount = 0;
}
function addToolItem(b, tool, input) {
  ensureToolCard(b);
  b.toolCount += 1;
  const { glyph, text } = _toolItem(tool, input);
  const short = text.length > 90 ? text.slice(0, 87) + "…" : text;
  const row = document.createElement("div");
  row.className = "tc-item";
  row.title = text;
  row.innerHTML = `<span class="tc-ic">${escapeHtml(String(glyph))}</span><span class="tc-tx">${escapeHtml(short)}</span>`;
  b.toolList.appendChild(row);
  b.toolCard.querySelector(".tc-summary").textContent =
    `${b.toolCount} ${b.toolCount === 1 ? "access" : "accesses"}`;
}

// Read an SSE response into the window. Tracks w.lastSeq / w.sawComplete /
// w.runId so a dropped connection can re-attach to the detached run and
// resume from the next event.
async function pumpSse(w, slug, rootAgent, resp, bubbleFor) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() || "";
    for (const chunk of parts) {
      const line = chunk.trim();
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      let evt;
      try { evt = JSON.parse(payload); } catch { continue; }
      if (evt.seq !== undefined) w.lastSeq = evt.seq;
      if (evt.type === "start" && evt.run_id) w.runId = evt.run_id;
      if (evt.type === "complete") w.sawComplete = true;
      handleEventInWindow(w, slug, evt, bubbleFor, rootAgent);
    }
  }
}

// The server keeps the run alive after a disconnect; try to re-attach and
// resume from the last seen event. Bounded retries with a short backoff.
async function reattachAfterDrop(w, slug, rootAgent, bubbleFor) {
  for (let attempt = 0; attempt < 3 && !w.sawComplete; attempt++) {
    await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
    try {
      setChatStatus(w, `connection dropped — re-attaching (${attempt + 1}/3)…`);
      const resp = await fetch(
        `/api/projects/${slug}/agents/${rootAgent}/stream?since=${(w.lastSeq ?? -1) + 1}`,
        { signal: w.abortController ? w.abortController.signal : undefined });
      if (resp.status === 404) return; // run gone (server restart) — nothing to attach
      if (!resp.ok || !resp.body) continue;
      await pumpSse(w, slug, rootAgent, resp, bubbleFor);
      if (w.sawComplete) return;
    } catch (err) {
      if (err.name === "AbortError") return;
    }
  }
}

// opts.fromQueue: this text is currently sitting at the head of w.queue. It is removed
// only after the server accepts it (dequeueSent), so any failure leaves it queued and a
// reload retries it rather than dropping it.
async function sendMessageInWindow(w, text, opts = {}) {
  const slug = w.projectSlug;
  const rootAgent = w.agentId;
  const proj = state.projectCache[slug];
  // Always draw the user bubble — including for a queued send. The queued item's own
  // "⏳ queue #n" bubble is removed by dequeueSent→renderQueue the moment the server
  // accepts, so skipping this left the message with NO bubble at all: the agent
  // answered a question that had visibly vanished ("UI nuốt tin nhắn").
  const userBubble = addBubble(w, "user", text);
  const bubbleFor = makeBubbleFactory(w, rootAgent);
  w.streaming = true;
  w.sawComplete = false;
  w.lastSeq = -1;
  w._workerCards = {};
  w._turnWorkers = {};
  w._turnRound = null;
  renderTurnStrip(w, rootAgent);
  setSendBtn(w, "stop");
  setChatStatus(w, "running... (Esc to stop)");
  updateQueueStatus(w);
  proj.statuses[rootAgent] = "running";
  rerenderGraphsForSlug(slug);

  try {
    w.abortController = new AbortController();
    const nextOpts = w.nextOptions || {};
    w.nextOptions = null;  // consume one-shot
    const reqBody = { message: text };
    if (nextOpts.best_of_n) reqBody.best_of_n = nextOpts.best_of_n;
    if (nextOpts.check_loop) reqBody.check_loop = true;
    if (nextOpts.memory_mode) reqBody.memory_mode = nextOpts.memory_mode;
    const resp = await fetch(`/api/projects/${slug}/agents/${rootAgent}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reqBody),
      signal: w.abortController.signal,
    });
    if (resp.status === 409) {
      // A detached run is already in flight for this agent (e.g. started before a page
      // reload). The message was NOT accepted — put it back on the queue and attach to
      // the live run; the finally below drains it once that run ends.
      // Drop the bubble drawn on entry first: enqueueMessage paints its own "⏳ queue #n"
      // bubble, so leaving this one would show the same message twice.
      if (userBubble && userBubble.parentNode) userBubble.parentNode.removeChild(userBubble);
      addSystemBubble(w, "⏳ A turn is already running for this agent — message queued; re-attaching to the live stream…");
      // Already at the head of the queue when it came from there — re-adding would duplicate.
      if (!opts.fromQueue) enqueueMessage(w, text);
      await attachRunStream(w, slug, rootAgent, bubbleFor, 0);
      return;
    }
    if (!resp.ok || !resp.body) {
      const t = await resp.text();
      const b = bubbleFor(rootAgent);
      setContent(b.contentEl, `(backend error ${resp.status}) ${t}`);
      proj.statuses[rootAgent] = "error";
      rerenderGraphsForSlug(slug);
      // Rejected, not accepted: keep it queued (and persisted) so it is not lost. A
      // reload retries it; Stop discards it. Back off before the next drain attempt —
      // without this, finally→drainQueue would retry the same failing POST in a tight
      // loop for as long as the backend keeps erroring.
      if (!opts.fromQueue) enqueueMessage(w, text);
      w._drainBackoff = Date.now() + _QUEUE_RETRY_MS;
      if (userBubble && userBubble.parentNode) userBubble.parentNode.removeChild(userBubble);
      return;
    }
    // 2xx: the server owns the message now — safe to drop our copy (and any backoff).
    w._drainBackoff = 0;
    if (opts.fromQueue) dequeueSent(w, text);
    await pumpSse(w, slug, rootAgent, resp, bubbleFor);
    if (!w.sawComplete) await reattachAfterDrop(w, slug, rootAgent, bubbleFor);
    setChatStatus(w, w.sawComplete ? "done" : "stream detached — turn continues on the server");
  } catch (err) {
    if (err.name === "AbortError") {
      setChatStatus(w, "stopped");
    } else {
      if (!w.sawComplete) await reattachAfterDrop(w, slug, rootAgent, bubbleFor);
      if (!w.sawComplete) {
        setChatStatus(w, "network error: " + err.message);
        // The turn never started (no run id ⇒ the POST never reached the server), so
        // this message is nowhere: not on the server, not in the queue. Persist it or
        // it dies with the tab. Guarded on runId to avoid re-sending work the server
        // did accept and is still running.
        if (!w.runId) {
          if (!opts.fromQueue) enqueueMessage(w, text);
          // Either way the text now lives (only) in the queue — drop the plain user
          // bubble so it doesn't sit next to the queued copy as a duplicate.
          if (userBubble && userBubble.parentNode) userBubble.parentNode.removeChild(userBubble);
          w._drainBackoff = Date.now() + _QUEUE_RETRY_MS;
        }
      } else setChatStatus(w, "done");
    }
  } finally {
    w.streaming = false;
    w.abortController = null;
    setSendBtn(w, "send");
    // If the user queued messages while this turn ran, fire the next one now.
    drainQueue(w);
  }
}

async function attachRunStream(w, slug, rootAgent, bubbleFor, since) {
  const resp = await fetch(
    `/api/projects/${slug}/agents/${rootAgent}/stream?since=${since}`,
    { signal: w.abortController ? w.abortController.signal : undefined });
  if (!resp.ok || !resp.body) return false;
  await pumpSse(w, slug, rootAgent, resp, bubbleFor);
  return w.sawComplete;
}

// On project open: find turns that kept running while the browser was away
// and re-attach their chat windows with full event replay.
async function reattachActiveRuns(slug) {
  let runs = [];
  try {
    const r = await fetch(`/api/projects/${slug}/runs`);
    if (!r.ok) return;
    runs = (await r.json()).runs || [];
  } catch { return; }
  for (const run of runs) {
    const w = openChat(slug, run.agent_id);
    if (w.streaming) continue; // this tab already follows it
    attachDetachedRun(w, slug, run.agent_id, run.started_at);
  }
}

async function attachDetachedRun(w, slug, rootAgent, startedAt) {
  const proj = state.projectCache[slug];
  // Render db history first so replayed live bubbles append after it — but only the
  // part that predates this run, since the replay below redraws the run itself.
  try { await refreshChatSession(w, startedAt); } catch {}
  const bubbleFor = makeBubbleFactory(w, rootAgent);
  w.streaming = true;
  w.sawComplete = false;
  w.lastSeq = -1;
  w._workerCards = {};
  w._turnWorkers = {};
  w._turnRound = null;
  renderTurnStrip(w, rootAgent);
  setSendBtn(w, "stop");
  setChatStatus(w, "re-attached to a running turn (replaying)…");
  if (proj) { proj.statuses[rootAgent] = "running"; rerenderGraphsForSlug(slug); }
  try {
    w.abortController = new AbortController();
    await attachRunStream(w, slug, rootAgent, bubbleFor, 0);
    if (!w.sawComplete) await reattachAfterDrop(w, slug, rootAgent, bubbleFor);
    setChatStatus(w, w.sawComplete ? "done" : "stream detached — turn continues on the server");
  } catch (err) {
    if (err.name === "AbortError") setChatStatus(w, "stopped");
    else setChatStatus(w, "network error: " + err.message);
  } finally {
    w.streaming = false;
    w.abortController = null;
    setSendBtn(w, "send");
    drainQueue(w);
  }
}

function handleEventInWindow(w, slug, evt, bubbleFor, rootAgent) {
  const proj = state.projectCache[slug];
  const agent = evt.agent || rootAgent;

  switch (evt.type) {
    case "delta": {
      const b = bubbleFor(agent);
      b.assembled += evt.text;
      // Plain text streaming during the turn — token-by-token smooth, no
      // marked.parse cost per delta. We do the full markdown render at
      // agent_done.
      b.contentEl.textContent = b.assembled;
      if (!b.streamingStarted) {
        b.streamingStarted = true;
        // collapse thinking label since we're now in response phase
        if (b.thinkAccum) {
          b.thinkLabel.textContent = `thinking done (${b.thinkAccum.length}c) — click to view`;
        } else {
          // no thinking at all — hide the block to save space
          b.thinkBlock.style.display = "none";
        }
      }
      // Worker (dispatched child): mirror a short live tail into the card's
      // status line so the user sees what the child is currently writing,
      // without auto-expanding the (collapsed) transcript body.
      if (agent !== rootAgent) {
        const tail = (b.assembled.replace(/\s+/g, " ").trim()).slice(-80);
        if (tail) workerCardStatus(w, agent, "✎ " + tail);
      }
      const m = w.el.querySelector(".messages");
      m.scrollTop = m.scrollHeight;
      break;
    }
    case "thinking": {
      const b = bubbleFor(agent);
      const chunk = evt.text || "";
      b.thinkAccum += chunk;
      b.thinkBody.textContent = b.thinkAccum;
      b.thinkCount.textContent = `${b.thinkAccum.length}c`;
      if (!b.streamingStarted) {
        b.thinkLabel.textContent = "thinking…";
      }
      if (!b.thinkBlock.classList.contains("collapsed")) {
        b.thinkBody.scrollTop = b.thinkBody.scrollHeight;
      }
      // Worker: mirror a short live tail of the thinking into the card status
      // so the user sees the child reasoning, not just a static "thinking…".
      if (agent !== rootAgent) {
        const tail = (b.thinkAccum.replace(/\s+/g, " ").trim()).slice(-80);
        if (tail) workerCardStatus(w, agent, "💭 " + tail);
      }
      const m = w.el.querySelector(".messages");
      m.scrollTop = m.scrollHeight;
      break;
    }
    case "tool_use": {
      // An agent invoked a tool (Read/Grep/Glob/Edit/Write/Bash/MCP...).
      // Append a row to that agent's tool-access card so the user can see
      // exactly which files/commands were loaded this turn.
      const tb = bubbleFor(agent);
      addToolItem(tb, evt.tool, evt.input || {});
      break;
    }
    case "status": {
      const b = bubbleFor(agent);
      if (evt.status === "thinking" && !b.streamingStarted) {
        b.thinkLabel.textContent = "thinking…";
        if (agent !== rootAgent) workerCardStatus(w, agent, "⏳ thinking…");
      } else if (evt.status === "responding") {
        b.streamingStarted = true;
        if (b.thinkAccum) {
          b.thinkLabel.textContent = `thinking done (${b.thinkAccum.length}c) — click to view`;
        } else {
          b.thinkBlock.style.display = "none";
        }
        if (agent !== rootAgent) workerCardStatus(w, agent, "⏳ writing…");
      }
      break;
    }
    case "continuation_round": {
      // Round boundary: thin separator + fresh bubble for the orchestrator's
      // synthesis, so each round reads as its own paragraph block.
      addCtrlSeparator(w, `[CONTROL-PLANE CONTINUATION ${evt.round}/${evt.max}]`);
      if (bubbleFor.reset) bubbleFor.reset(evt.agent || rootAgent);
      w._turnRound = `${evt.round}/${evt.max}`;
      renderTurnStrip(w, rootAgent);
      break;
    }
    case "agent_status": {
      proj.statuses[agent] = evt.status;
      rerenderGraphsForSlug(slug);
      if (evt.status === "running") {
        const b = bubbleFor(agent);
        if (!b.streamingStarted && !b.thinkAccum) {
          b.thinkLabel.textContent = "waiting for response…";
        }
      }
      break;
    }
    case "agent_done": {
      proj.statuses[agent] = evt.status || "ok";
      const b = bubbleFor(agent);
      // Final pass: render markdown over the full accumulated text.
      const finalText = evt.text || b.assembled;
      setContent(b.contentEl, finalText);
      // freeze thinking label final
      if (b.thinkAccum) {
        b.thinkLabel.textContent = `thinking (${b.thinkAccum.length}c) — click to view`;
      } else {
        b.thinkBlock.style.display = "none";
      }
      // Worker finished: promote its first meaningful line to the card summary.
      if (agent !== rootAgent && w._workerCards && w._workerCards[agent]) {
        const firstLine = (finalText || "").split("\n").map((l) => l.trim())
          .find((l) => l && !l.startsWith("<")) || "";
        const ok = (evt.status || "ok") === "ok";
        workerCardStatus(w, agent,
          (ok ? "✓ " : "✗ ") + (firstLine.replace(/^#+\s*/, "").slice(0, 90) || (ok ? "done" : "error")),
          ok ? "status-ok" : "status-error");
        w._workerCards[agent].dataset.done = "1";
      }
      rerenderGraphsForSlug(slug);
      ensureStats(slug, true);
      mirrorDispatchedMessages(slug, agent);
      break;
    }
    case "compact_started": {
      addSystemBubble(w, `📦 Context ${evt.pct || "?"}% — auto-compacting before this turn runs (agent summarizes itself, then a new session opens)…`);
      setChatStatus(w, "auto-compacting…");
      break;
    }
    case "compacted": {
      ensureStats(slug, true);
      if (evt.auto) {
        addSystemBubble(w, "✓ Auto-compact done — new session seeded with the recap. Your turn continues below.");
      }
      break;
    }
    case "dispatch_started": {
      state.activeDispatches.add(`${evt.source}->${evt.target}`);
      proj.statuses[evt.target] = "running";
      rerenderGraphsForSlug(slug);
      ensureWorkerCard(w, evt.source, evt.target, evt.task || "", rootAgent);
      if (!w._turnWorkers) w._turnWorkers = {};
      w._turnWorkers[evt.target] = "running";
      renderTurnStrip(w, rootAgent);
      setChatStatus(w, `${evt.source} → ${evt.target}: ${(evt.task || "").slice(0, 80)}`);
      // if target's chat window is open, refresh so the user sees the task message arrive
      mirrorDispatchedMessages(slug, evt.target);
      break;
    }
    case "dispatch_complete": {
      state.activeDispatches.delete(`${evt.source}->${evt.target}`);
      proj.statuses[evt.target] = evt.status === "ok" ? "ok" : "error";
      rerenderGraphsForSlug(slug);
      ensureStats(slug, true);
      // Icon/border only — agent_done already wrote the summary line.
      const card = w._workerCards && w._workerCards[evt.target];
      if (card) {
        workerCardStatus(w, evt.target, card.dataset.done ? "" :
          (evt.status === "ok" ? "✓ done" : "✗ " + (evt.message || evt.status)),
          evt.status === "ok" ? "status-ok" : "status-error");
      }
      if (!w._turnWorkers) w._turnWorkers = {};
      w._turnWorkers[evt.target] = evt.status === "ok" ? "ok" : "error";
      renderTurnStrip(w, rootAgent);
      setChatStatus(w, `${evt.source} → ${evt.target}: ${evt.status}`);
      mirrorDispatchedMessages(slug, evt.target);
      break;
    }
    case "dispatch_rejected": {
      setChatStatus(w, `dispatch ${evt.target} rejected: ${evt.reason}`);
      break;
    }
    case "error": {
      const b = bubbleFor(agent);
      setContent(b.contentEl, (b.assembled || "") + `\n\n> **[error]** ${evt.message || ""}`);
      if (!b.thinkAccum) b.thinkBlock.style.display = "none";
      proj.statuses[agent] = "error";
      rerenderGraphsForSlug(slug);
      break;
    }
    case "schedule_created": {
      const s = evt.schedule;
      const arr = state.schedules[slug] || (state.schedules[slug] = []);
      if (!arr.some((x) => x.id === s.id)) arr.push(s);
      addSystemBubble(w, `🕒 Scheduled (${schedLabel(s)}) → fires ${fmtCountdown(s.next_run_at)}. Verify on the graph badge / Schedules panel.`);
      renderScheduleDropdown();
      rerenderGraphsForSlug(slug);
      break;
    }
    case "schedule_fired": {
      showToast(`🕒 ${agent}: scheduled check #${evt.n}${evt.max ? "/" + evt.max : ""} started`, "sched");
      setChatStatus(w, `🕒 scheduled run #${evt.n} started`);
      break;
    }
    case "schedule_done": {
      addSystemBubble(w, `🕒 Schedule #${evt.id} finished — ${escapeHtml(evt.reason || "complete")}.`);
      showToast(`🕒 ${agent}: schedule done — ${evt.reason || "complete"}`, "sched");
      refreshSchedulesActiveTab();
      break;
    }
    case "schedule_exhausted": {
      addSystemBubble(w, `🕒 Schedule #${evt.id} stopped after ${evt.n} checks without completing (hit the safety ceiling).`);
      showToast(`🕒 ${agent}: schedule exhausted after ${evt.n} checks`, "warn");
      refreshSchedulesActiveTab();
      break;
    }
    case "schedule_cancelled": {
      refreshSchedulesActiveTab();
      break;
    }
    case "meta": {
      // Agent initialization (claude stream-json `system/init`): show a compact
      // card with session_id, model, cwd, and loaded tools — once per session.
      const d = evt.data || {};
      if (d.init) {
        // Only the window's OWN agent gets an init card. A dispatched worker's init
        // also arrives here (worker events flow through the parent chat), and drawing
        // it would wedge RESEARCHER's/CRAFTER's init panel into the middle of BOSS's
        // answer. The worker's card belongs in the worker's own chat window.
        if (agent === w.agentId) renderInitWidget(w, agent, d);
        // Stash for the graph node expand-panel + live-refresh if open.
        if (!state.initInfo[slug]) state.initInfo[slug] = {};
        state.initInfo[slug][agent] = d;
        if (state.expandedNodes.has(`${slug}:${agent}`)) rerenderGraphsForSlug(slug);
      }
      break;
    }
  }
}

// Render the agent-initialization card (ported from opcode's SystemInitializedWidget,
// translated to vanilla JS). Shown once per claude session, not on every --resume turn.
function renderInitWidget(w, agent, data) {
  const root = w.el.querySelector(".messages");
  if (!root) return;
  const sid = data.claude_session_id;
  // Dedupe key is agent+session, held in a Set. A single `lastInitSid` scalar was the
  // bug: a parent window sees meta from BOSS *and* every dispatched worker, each with a
  // different session id, so consecutive agents kept invalidating each other's key and
  // the card re-drew on every single turn, interleaved with the answer text.
  if (sid) {
    if (!w.seenInits) w.seenInits = new Set();
    const key = `${agent}:${sid}`;
    if (w.seenInits.has(key)) return;
    w.seenInits.add(key);
  }

  const sp = splitInitLists(data);

  const chip = (label, cls) =>
    `<span class="init-tool ${cls || ""}">${escapeHtml(String(label))}</span>`;
  const toolRow = (label, list, cls) =>
    list.length
      ? `<div class="init-tool-group"><span class="init-tool-label">${label}</span>` +
        list.map((t) => chip(t, cls)).join("") +
        `</div>`
      : "";

  const rows = [];
  if (data.model) rows.push(`<div class="init-row"><span class="init-k">model</span><span class="init-v">${escapeHtml(data.model)}</span></div>`);
  if (data.cwd) rows.push(`<div class="init-row"><span class="init-k">cwd</span><span class="init-v mono">${escapeHtml(data.cwd)}</span></div>`);
  if (sid) rows.push(`<div class="init-row"><span class="init-k">session</span><span class="init-v mono">${escapeHtml(sid)}</span></div>`);

  // Only what was ADDED for this agent (MCP tools, non-default skills, plugins,
  // servers); the stock builtin tools/skills — identical for every agent — collapse
  // to one summary line. Hooks (rtk) are absent from the payload and cannot be shown.
  const plugins = (Array.isArray(data.plugins) ? data.plugins : []).map((p) => p && p.name).filter(Boolean);
  const servers = (Array.isArray(data.mcp_servers) ? data.mcp_servers : [])
    .map((s) => s && (s.status && s.status !== "connected" ? `${s.name} (${s.status})` : s.name))
    .filter(Boolean);

  const hasAdded = sp.addedTools.length || sp.mcpTools.length || sp.addedSkills.length
    || servers.length || plugins.length;
  if (hasAdded || sp.defaultToolCount || sp.defaultSkillCount)
    rows.push(
      `<div class="init-tools">` +
        toolRow(`+tools (${sp.addedTools.length})`, sp.addedTools, "builtin") +
        toolRow(`mcp tools (${sp.mcpTools.length})`, sp.mcpTools, "mcp") +
        toolRow(`+skills (${sp.addedSkills.length})`, sp.addedSkills, "skill") +
        toolRow(`plugins (${plugins.length})`, plugins, "plugin") +
        toolRow(`mcp servers (${servers.length})`, servers, "mcp") +
        `<div class="init-row" title="stock CLI builtins, identical for every agent — hidden from the chip list"><span class="init-k">defaults</span><span class="init-v">${sp.defaultToolCount} tools · ${sp.defaultSkillCount} skills</span></div>` +
        `</div>`
    );

  const card = document.createElement("div");
  card.className = "init-card";
  card.innerHTML =
    `<div class="init-head"><span class="init-dot"></span>agent initialized</div>` +
    rows.join("");
  root.appendChild(card);
  root.scrollTop = root.scrollHeight;
}

function mirrorDispatchedMessages(slug, agentId) {
  const target = state.windows.find(
    (x) => x.type === "chat" && x.projectSlug === slug && x.agentId === agentId);
  if (!target) return;
  // Don't wipe the messages area while the root agent is still streaming —
  // dispatched sub-bubbles are live in the DOM and would be destroyed.
  if (target.streaming) return;
  refreshChatSession(target);
}

// ---------- Folder tree ----------

// Workspace-wide tree (was per-project before). Single state for all projects.
const _TREE_KEY = "__workspace";

function _treeState() {
  if (!state.tree[_TREE_KEY]) {
    state.tree[_TREE_KEY] = {
      workspace_root: null,
      expanded: new Set([""]),
      cache: {},
      selectedAbs: null,
      flat: [],
    };
  }
  return state.tree[_TREE_KEY];
}

async function initWorkspaceTree() {
  try {
    const r = await fetch("/api/workspace/info");
    const j = await r.json();
    const t = _treeState();
    t.workspace_root = j.workspace_root || null;
    // Write the label into the inner <span>, NOT the .sidebar-title itself —
    // setting textContent on the title would wipe its children (the + project button).
    const titleEl = document.querySelector(".sidebar-title");
    const labelEl = titleEl && titleEl.querySelector("span:first-child");
    if (labelEl && t.workspace_root) {
      const base = t.workspace_root.split("/").filter(Boolean).pop() || t.workspace_root;
      labelEl.textContent = base + "/";
      labelEl.title = t.workspace_root;
    }
  } catch {}
  await fetchTreeLevel("");
  renderTree();
}

async function fetchTreeLevel(relPath) {
  const t = _treeState();
  try {
    const r = await fetch(`/api/workspace/tree?path=${encodeURIComponent(relPath)}`);
    if (!r.ok) { t.cache[relPath] = []; return; }
    const j = await r.json();
    t.cache[relPath] = j.items || [];
  } catch { t.cache[relPath] = []; }
}

function buildFlatTree() {
  const t = _treeState();
  const flat = [];
  function walk(relPath, depth) {
    const items = t.cache[relPath];
    if (!items) return;
    for (const item of items) {
      flat.push({ ...item, depth });
      if (item.type === "folder" && t.expanded.has(item.rel_path)) walk(item.rel_path, depth + 1);
    }
  }
  walk("", 0);
  t.flat = flat;
  return flat;
}

function renderTree() {
  const root = $("treeRoot");
  if (!root) return;
  root.innerHTML = "";
  const t = _treeState();
  buildFlatTree();
  if (!t.flat.length) { root.innerHTML = '<div class="tree-empty">(empty)</div>'; return; }
  for (const item of t.flat) {
    const node = document.createElement("div");
    node.className = "tree-node " + item.type;
    if (item.is_project) node.classList.add("is-project");
    if (t.selectedAbs === item.abs_path) node.classList.add("selected");
    node.style.paddingLeft = (6 + item.depth * 14) + "px";
    const arrow = item.type === "folder"
      ? (t.expanded.has(item.rel_path) ? "▾" : "▸") : "";
    const icon = item.is_project ? "◆" : (item.type === "folder" ? "▣" : "·");
    node.innerHTML = `<span class="arrow">${arrow}</span>` +
      `<span class="icon">${icon}</span>` +
      `<span class="label" title="${escapeHtml(item.abs_path)}">${escapeHtml(item.name)}</span>`;
    node.onclick = () => onTreeNodeClick(item);
    node.ondblclick = () => { if (item.type === "folder") onTreeToggle(item); };
    root.appendChild(node);
  }
}

async function onTreeNodeClick(item) {
  const t = _treeState();
  t.selectedAbs = item.abs_path;
  if (item.type === "folder") {
    await onTreeToggle(item);
  } else {
    openFileViewer(item.abs_path, item.rel_path);
    renderTree();
  }
}

async function onTreeToggle(item) {
  const t = _treeState();
  if (t.expanded.has(item.rel_path)) t.expanded.delete(item.rel_path);
  else {
    t.expanded.add(item.rel_path);
    if (!t.cache[item.rel_path]) await fetchTreeLevel(item.rel_path);
  }
  renderTree();
}

function selectedTreeItem() {
  const t = _treeState();
  if (!t.selectedAbs) return null;
  return t.flat.find((it) => it.abs_path === t.selectedAbs) || null;
}

async function copySelectedPath() {
  const item = selectedTreeItem();
  if (!item) { flashHint("no file/folder selected"); return; }
  try {
    await navigator.clipboard.writeText(item.abs_path);
    flashHint("copied: " + item.abs_path);
  } catch { flashHint("clipboard blocked, path: " + item.abs_path); }
}

function flashHint(msg) {
  const el = $("treeHint");
  if (!el) return;
  const prev = el.textContent;
  el.textContent = msg;
  el.style.color = "var(--accent)";
  clearTimeout(flashHint._t);
  flashHint._t = setTimeout(() => { el.textContent = prev; el.style.color = ""; }, 2200);
}

function bindTreeKeys() {
  const root = $("treeRoot");
  if (!root) return;
  root.addEventListener("keydown", (e) => {
    const t = _treeState();
    if (!t.flat.length) return;
    const idx = t.flat.findIndex((it) => it.abs_path === t.selectedAbs);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = t.flat[Math.min(t.flat.length - 1, Math.max(0, idx + 1))];
      if (next) { t.selectedAbs = next.abs_path; renderTree(); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const prev = t.flat[Math.max(0, idx - 1)];
      if (prev) { t.selectedAbs = prev.abs_path; renderTree(); }
    } else if (e.key === "ArrowRight" || e.key === "Enter") {
      e.preventDefault();
      const cur = t.flat[idx];
      if (cur && cur.type === "folder" && !t.expanded.has(cur.rel_path)) onTreeToggle(cur);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      const cur = t.flat[idx];
      if (cur && cur.type === "folder" && t.expanded.has(cur.rel_path)) onTreeToggle(cur);
    }
  });
}

// ---------- Global keys ----------

function bindGlobalKeys() {
  window.addEventListener("keydown", (e) => {
    // Cmd+Alt+C — copy selected tree path
    if (e.metaKey && e.altKey && (e.key === "c" || e.key === "C" || e.code === "KeyC")) {
      e.preventDefault();
      copySelectedPath();
      return;
    }
    // Cmd+W — close focused window
    if (e.metaKey && (e.key === "w" || e.key === "W")) {
      const focused = state.windows.filter((w) => !w.hidden && w.type !== "graph" && w.projectSlug === state.activeTab)
        .sort((a, b) => b.z - a.z)[0];
      if (focused) { e.preventDefault(); closeWindow(focused); }
    }
  });
}

window.addEventListener("resize", () => {
  state.windows.filter((w) => w.type === "graph").forEach((w) => renderGraphInWindow(w));
});

init();
