// AgentUI — vanilla JS, floating windows, multi-agent chats.

const state = {
  projects: [],
  openTabs: [],
  activeTab: null,
  projectCache: {},
  activeDispatches: new Set(),
  viewBoxes: {},          // slug -> {x,y,w,h}
  graphBounds: {},        // slug -> {x,y,w,h}
  tree: {},               // slug -> {expanded, cache, selectedAbs, flat}
  windows: [],            // [{id, projectSlug, type, agentId?, x, y, w, h, z, hidden, el, ...state}]
  zTop: 10,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => (s || "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

// ---------- Init ----------

async function init() {
  await loadProjects();
  renderProjectList();
  if (state.projects.length) await openProject(state.projects[0].slug);
  bindGlobalKeys();
  bindTreeKeys();
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
  }
  renderTabs();
  renderProjectList();
  ensureGraphWindow(slug);
  applyTabVisibility();
  await loadTreeRoot(slug);
  renderTree();
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
  w.z = nextZ();
  w.el.style.zIndex = w.z;
  for (const x of state.windows) x.el.classList.toggle("focused", x === w);
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
      <span class="close" title="đóng">×</span>`;
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
  const projName = proj ? proj.name : w.projectSlug;
  if (w.type === "graph") return `${projName} • graph`;
  if (w.type === "chat") return `${projName} • ${w.agentId}`;
  return w.id;
}

function createWindowDom(w) {
  const root = $("windowsRoot");
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
        <button class="window-btn hide" title="ẩn (minimize)">—</button>
        <button class="window-btn close" title="đóng">×</button>
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
      <div class="zoom-controls">
        <button data-z="in" title="zoom in">+</button>
        <button data-z="out" title="zoom out">−</button>
        <button data-z="fit" title="fit">⌖</button>
        <span class="zoom-level"></span>
      </div>`;
    bindGraphWindow(w);
    renderGraphInWindow(w);
  } else if (w.type === "chat") {
    c.innerHTML = `
      <div class="chat-header"></div>
      <div class="messages"></div>
      <div class="chatbox">
        <div class="chatbox-label">Chat session</div>
        <form class="chat-form">
          <textarea class="chat-input" rows="2"
            placeholder="Enter để gửi (Shift+Enter xuống dòng)"></textarea>
          <button class="chat-send" type="submit">Gửi</button>
        </form>
        <div class="chat-status"></div>
      </div>`;
    renderChatHeader(w);
    bindChatWindow(w);
    refreshChatSession(w);
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
  const svg = w.el.querySelector(".graph-svg");
  if (!svg) return;
  svg.innerHTML = "";
  const proj = state.projectCache[w.projectSlug];
  if (!proj) return;

  const W = svg.clientWidth || w.w - 20;
  const H = svg.clientHeight || w.h - 50;
  const { layers } = layoutAgents(proj.agents);
  const layerKeys = Object.keys(layers).map(Number).sort((a, b) => a - b);

  const nodeW = 150, nodeH = 56, vGap = 80, hGap = 26;
  const positions = {};
  const yStart = 50;
  layerKeys.forEach((lvl, li) => {
    const row = layers[lvl];
    const totalW = row.length * nodeW + (row.length - 1) * hGap;
    const xStart = Math.max(30, (W - totalW) / 2);
    row.forEach((id, i) => {
      positions[id] = { x: xStart + i * (nodeW + hGap), y: yStart + li * (nodeH + vGap) };
    });
  });

  const allPos = Object.values(positions);
  if (allPos.length) {
    const minX = Math.min(...allPos.map((p) => p.x)) - 24;
    const minY = Math.min(...allPos.map((p) => p.y)) - 24;
    const maxX = Math.max(...allPos.map((p) => p.x + nodeW)) + 24;
    const maxY = Math.max(...allPos.map((p) => p.y + nodeH)) + 24;
    state.graphBounds[proj.slug] = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  const ns = "http://www.w3.org/2000/svg";
  const defs = document.createElementNS(ns, "defs");
  defs.innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#5a6280"/></marker>`;
  svg.appendChild(defs);

  proj.edges.forEach((e) => {
    const s = positions[e.source], t = positions[e.target];
    if (!s || !t) return;
    const x1 = s.x + nodeW / 2, y1 = s.y + nodeH;
    const x2 = t.x + nodeW / 2, y2 = t.y;
    const my = (y1 + y2) / 2;
    const path = document.createElementNS(ns, "path");
    let edgeCls = "edge";
    if (state.activeDispatches.has(`${e.source}->${e.target}`)) edgeCls += " edge-active";
    path.setAttribute("class", edgeCls);
    path.setAttribute("d", `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`);
    svg.appendChild(path);
  });

  proj.agents.forEach((a) => {
    const pos = positions[a.id];
    if (!pos) return;
    const isOrchestrator = (a.parents || []).length === 0;
    const status = (proj.statuses && proj.statuses[a.id]) || "idle";
    const isOpenChat = state.windows.some(
      (x) => x.type === "chat" && x.projectSlug === proj.slug && x.agentId === a.id);

    const g = document.createElementNS(ns, "g");
    g.style.cursor = "pointer";
    g.onclick = () => openChat(proj.slug, a.id);

    const rect = document.createElementNS(ns, "rect");
    let cls = "node-rect";
    if (isOrchestrator) cls += " orchestrator";
    if (isOpenChat) cls += " selected";
    if (status === "running") cls += " pulse";
    rect.setAttribute("class", cls);
    rect.setAttribute("data-status", status);
    rect.setAttribute("x", pos.x);
    rect.setAttribute("y", pos.y);
    rect.setAttribute("rx", 8); rect.setAttribute("ry", 8);
    rect.setAttribute("width", nodeW); rect.setAttribute("height", nodeH);
    g.appendChild(rect);

    const label = document.createElementNS(ns, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("x", pos.x + nodeW / 2);
    label.setAttribute("y", pos.y + 22);
    label.setAttribute("text-anchor", "middle");
    label.textContent = a.id;
    g.appendChild(label);

    const model = document.createElementNS(ns, "text");
    model.setAttribute("class", "node-model");
    model.setAttribute("x", pos.x + nodeW / 2);
    model.setAttribute("y", pos.y + 40);
    model.setAttribute("text-anchor", "middle");
    model.textContent = modelLabel(a);
    g.appendChild(model);

    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("class", "status-dot");
    dot.setAttribute("cx", pos.x + 10);
    dot.setAttribute("cy", pos.y + 10);
    dot.setAttribute("r", 5);
    dot.setAttribute("fill", statusColor(status));
    g.appendChild(dot);

    svg.appendChild(g);
  });

  applyViewBox(svg, proj.slug, w);
}

function modelLabel(a) {
  if (a.model === "grok") {
    return (a.grok_model || "grok-build");
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
      }
    };
  });
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
  { value: "claude-opus-4-8",     label: "opus 4.8"   },
  { value: "claude-opus-4-7",     label: "opus 4.7"   },
  { value: "claude-sonnet-4-6",   label: "sonnet 4.6" },
  { value: "claude-haiku-4-5",    label: "haiku 4.5"  },
];
const EFFORT_LEVELS = [
  { value: "",       label: "default" },
  { value: "low",    label: "low"     },
  { value: "medium", label: "medium"  },
  { value: "high",   label: "high"    },
  { value: "max",    label: "max"     },
];

function renderChatHeader(w) {
  const header = w.el.querySelector(".chat-header");
  const proj = state.projectCache[w.projectSlug];
  const agent = proj?.agents.find((a) => a.id === w.agentId);
  if (!agent) { header.textContent = w.agentId; return; }

  const modelText = agent.model === "grok"
    ? (agent.grok_model || agent.default_grok_model || "grok-build")
    : (agent.claude_model || agent.default_claude_model || "claude-sonnet-4-6").replace(/^claude-/, "");
  const effortText = agent.effort ? `effort ${agent.effort}` : "";

  header.innerHTML = `
    <div class="header-top">
      <span class="header-name">${escapeHtml(agent.id)}</span>
      <span class="header-role">${escapeHtml(agent.role || "")}</span>
    </div>
    <div class="header-meta">
      <span class="meta-model">${escapeHtml(modelText)}</span>
      ${effortText ? `<span class="meta-effort">${escapeHtml(effortText)}</span>` : ""}
      <span class="meta-hint">gõ <code>/</code> để xem lệnh</span>
      <span class="save-hint"></span>
    </div>`;
}

async function updateAgentSettings(w, claudeModel, grokModel, effort) {
  const slug = w.projectSlug;
  const agentId = w.agentId;
  const hint = w.el.querySelector(".save-hint");
  if (hint) { hint.textContent = "lưu…"; hint.style.color = "var(--text-dim)"; }
  try {
    const r = await fetch(`/api/projects/${slug}/agents/${agentId}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        claude_model: claudeModel,
        grok_model: grokModel,
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
      hint.textContent = "✓ áp dụng lượt sau";
      hint.style.color = "var(--ok)";
      clearTimeout(updateAgentSettings._t);
      updateAgentSettings._t = setTimeout(() => { hint.textContent = ""; }, 2500);
    }
  } catch (err) {
    if (hint) { hint.textContent = "lỗi"; hint.style.color = "var(--err)"; }
  }
}

async function refreshChatSession(w) {
  try {
    const r = await fetch(`/api/projects/${w.projectSlug}/agents/${w.agentId}/session`);
    const j = await r.json();
    w.session = j.session;
    const msgRoot = w.el.querySelector(".messages");
    msgRoot.innerHTML = "";
    (j.messages || []).forEach((m) => addBubble(w, m.role, m.content));
  } catch {}
}

function addBubble(w, role, text) {
  const root = w.el.querySelector(".messages");
  const b = document.createElement("div");
  b.className = `bubble ${role}`;
  b.innerHTML = `<div class="role">${role}</div><div class="content"></div>`;
  setContent(b.querySelector(".content"), text);
  root.appendChild(b);
  root.scrollTop = root.scrollHeight;
  return b;
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
  const status = card.complete ? "đã gửi" : "đang viết tag…";
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

  sendBtn.onclick = async (e) => {
    e.preventDefault();
    if (w.streaming) { stopChat(w); return; }
    const text = input.value.trim();
    if (!text) return;
    if (text.startsWith("/")) {
      input.value = "";
      hideCommandMenu(w);
      await executeChatCommand(w, text);
      return;
    }
    input.value = "";
    await sendMessageInWindow(w, text);
  };

  input.addEventListener("input", () => maybeShowCommandMenu(w, input.value));

  input.addEventListener("keydown", (e) => {
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
        // if only command typed (no space yet), insert; else submit
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

function stopChat(w) {
  if (w.abortController) {
    try { w.abortController.abort(); } catch {}
  }
  setChatStatus(w, "đang dừng…");
}

// ---------- Slash commands ----------

const CHAT_COMMANDS = [
  { cmd: "/help",     hint: "",                       desc: "danh sách commands",                              exec: cmdHelp },
  { cmd: "/clear",    hint: "",                       desc: "tạo session mới (history cũ vẫn lưu trong db)",   exec: cmdClear },
  { cmd: "/model",    hint: "<opus-4-8|opus-4-7|sonnet|haiku>", desc: "đổi model agent này",                   exec: cmdModel },
  { cmd: "/effort",   hint: "<default|low|medium|high|max>",    desc: "đổi effort agent này",                  exec: cmdEffort },
  { cmd: "/focus",    hint: "<AGENT_ID>",             desc: "mở chat agent khác trong project",                exec: cmdFocus },
  { cmd: "/dispatch", hint: "<AGENT_ID> <task>",      desc: "mở chat agent đích và gửi task ngay",             exec: cmdDispatch },
  { cmd: "/stop",     hint: "",                       desc: "dừng stream hiện tại",                            exec: cmdStop },
  { cmd: "/status",   hint: "",                       desc: "trạng thái session, model, effort",               exec: cmdStatus },
];

function maybeShowCommandMenu(w, text) {
  if (!text.startsWith("/")) { hideCommandMenu(w); return; }
  const space = text.indexOf(" ");
  const head = space === -1 ? text.toLowerCase() : text.slice(0, space).toLowerCase();
  const matches = CHAT_COMMANDS.filter((c) => c.cmd.startsWith(head));
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
    addSystemBubble(w, `❓ command không hợp lệ: \`${cmd}\`. Gõ \`/help\` để xem danh sách.`);
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
  const lines = ["**Slash commands**", ""];
  for (const c of CHAT_COMMANDS) {
    const sig = c.hint ? `\`${c.cmd}\` \`${c.hint}\`` : `\`${c.cmd}\``;
    lines.push(`- ${sig} — ${c.desc}`);
  }
  lines.push("", "Phím tắt trong input: Tab chọn lệnh, ↑↓ duyệt, Esc đóng menu / dừng stream.");
  addSystemBubble(w, lines.join("\n"));
}

async function cmdClear(w) {
  const slug = w.projectSlug, agent = w.agentId;
  try {
    await fetch(`/api/projects/${slug}/agents/${agent}/clear`, { method: "POST" });
    await refreshChatSession(w);
    addSystemBubble(w, "✓ session mới đã tạo. History cũ vẫn còn trong db, không xoá vĩnh viễn.");
  } catch (err) {
    addSystemBubble(w, "lỗi khi tạo session mới: " + err.message);
  }
}

const _CLAUDE_MODEL_ALIAS = {
  "opus-4-8": "claude-opus-4-8",
  "opus-4-7": "claude-opus-4-7",
  "opus": "claude-opus-4-8",
  "sonnet": "claude-sonnet-4-6",
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

async function cmdModel(w, arg) {
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  const isGrok = agent.model === "grok";

  if (!arg) {
    addSystemBubble(w, isGrok
      ? "Cú pháp: `/model <grok-build|grok-composer>`"
      : "Cú pháp: `/model <opus-4-8|opus-4-7|sonnet|haiku>`");
    return;
  }

  const eff = agent.effort ?? "";
  if (isGrok) {
    const target = _GROK_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("grok-") ? arg : null);
    if (!target) { addSystemBubble(w, `model grok không hợp lệ: \`${arg}\``); return; }
    await updateAgentSettings(w, null, target, eff);
    addSystemBubble(w, `✓ grok_model → \`${target}\` (áp dụng lượt chat tiếp theo)`);
    return;
  }

  const target = _CLAUDE_MODEL_ALIAS[arg.toLowerCase()] || (arg.startsWith("claude-") ? arg : null);
  if (!target) { addSystemBubble(w, `model claude không hợp lệ: \`${arg}\``); return; }
  await updateAgentSettings(w, target, null, eff);
  addSystemBubble(w, `✓ claude_model → \`${target}\` (áp dụng lượt chat tiếp theo)`);
}

async function cmdEffort(w, arg) {
  const allowed = ["default", "low", "medium", "high", "xhigh", "max"];
  if (!arg || !allowed.includes(arg.toLowerCase())) {
    addSystemBubble(w, "Cú pháp: `/effort default|low|medium|high|xhigh|max`");
    return;
  }
  const eff = arg.toLowerCase() === "default" ? "" : arg.toLowerCase();
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  if (agent.model === "grok") {
    await updateAgentSettings(w, null, agent.grok_model || "grok-build", eff);
  } else {
    await updateAgentSettings(w, agent.claude_model || "claude-sonnet-4-6", null, eff);
  }
  addSystemBubble(w, `✓ effort → \`${arg}\``);
}

async function cmdFocus(w, arg) {
  if (!arg) { addSystemBubble(w, "Cú pháp: `/focus <AGENT_ID>`"); return; }
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id.toUpperCase() === arg.toUpperCase());
  if (!agent) { addSystemBubble(w, `không thấy agent: \`${arg}\``); return; }
  openChat(w.projectSlug, agent.id);
}

async function cmdDispatch(w, arg) {
  const space = arg.indexOf(" ");
  if (space === -1) {
    addSystemBubble(w, "Cú pháp: `/dispatch <AGENT_ID> <task>`");
    return;
  }
  const targetId = arg.slice(0, space).trim();
  const task = arg.slice(space + 1).trim();
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id.toUpperCase() === targetId.toUpperCase());
  if (!agent) { addSystemBubble(w, `không thấy agent: \`${targetId}\``); return; }
  const tw = openChat(w.projectSlug, agent.id);
  // small delay so the new window has bound its DOM before we call into it
  setTimeout(() => sendMessageInWindow(tw, task), 50);
}

async function cmdStop(w) {
  if (!w.streaming) { addSystemBubble(w, "không có stream nào đang chạy."); return; }
  stopChat(w);
}

async function cmdStatus(w) {
  const proj = state.projectCache[w.projectSlug];
  const agent = proj.agents.find((a) => a.id === w.agentId);
  const isGrok = agent.model === "grok";
  const cur = isGrok ? (agent.grok_model || "grok-build") : (agent.claude_model || "claude-sonnet-4-6");
  const def = isGrok ? (agent.default_grok_model || "grok-build") : (agent.default_claude_model || "claude-sonnet-4-6");
  const lines = [
    `**Agent**: \`${agent.id}\``,
    `**Project**: ${proj.name}`,
    `**Adapter**: ${isGrok ? "grok" : "claude"}`,
    `**Model**: \`${cur}\` (default: \`${def}\`)`,
    `**Effort**: \`${agent.effort || "default"}\``,
    `**Status**: ${proj.statuses[agent.id] || "idle"}`,
    `**Streaming**: ${w.streaming ? "yes" : "no"}`,
  ];
  addSystemBubble(w, lines.join("\n"));
}

function setSendBtn(w, mode) {
  const btn = w.el.querySelector(".chat-send");
  if (!btn) return;
  if (mode === "stop") {
    btn.textContent = "Stop";
    btn.classList.add("stop");
    btn.disabled = false;
  } else {
    btn.textContent = "Gửi";
    btn.classList.remove("stop");
    btn.disabled = false;
  }
}

function setChatStatus(w, s) {
  const el = w.el.querySelector(".chat-status");
  if (el) el.textContent = s;
}

async function sendMessageInWindow(w, text) {
  const slug = w.projectSlug;
  const rootAgent = w.agentId;
  const proj = state.projectCache[slug];
  addBubble(w, "user", text);
  const bubbles = {};
  function bubbleFor(agentId) {
    if (bubbles[agentId]) return bubbles[agentId];
    const b = addBubble(w, "assistant", "");
    b.querySelector(".role").textContent = "assistant • " + agentId;
    const contentEl = b.querySelector(".content");
    const thinkEl = document.createElement("div");
    thinkEl.className = "thinking";
    b.insertBefore(thinkEl, contentEl);
    bubbles[agentId] = { bubble: b, contentEl, thinkEl, assembled: "" };
    return bubbles[agentId];
  }
  w.streaming = true;
  setSendBtn(w, "stop");
  setChatStatus(w, "đang chạy... (Esc để dừng)");
  proj.statuses[rootAgent] = "running";
  rerenderGraphsForSlug(slug);

  try {
    w.abortController = new AbortController();
    const resp = await fetch(`/api/projects/${slug}/agents/${rootAgent}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
      signal: w.abortController.signal,
    });
    if (!resp.ok || !resp.body) {
      const t = await resp.text();
      const b = bubbleFor(rootAgent);
      setContent(b.contentEl, `(lỗi backend ${resp.status}) ${t}`);
      proj.statuses[rootAgent] = "error";
      rerenderGraphsForSlug(slug);
      return;
    }
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
        handleEventInWindow(w, slug, evt, bubbleFor, rootAgent);
      }
    }
    setChatStatus(w, "xong");
  } catch (err) {
    if (err.name === "AbortError") {
      setChatStatus(w, "đã dừng");
    } else {
      setChatStatus(w, "network error: " + err.message);
    }
  } finally {
    w.streaming = false;
    w.abortController = null;
    setSendBtn(w, "send");
  }
}

function handleEventInWindow(w, slug, evt, bubbleFor, rootAgent) {
  const proj = state.projectCache[slug];
  const agent = evt.agent || rootAgent;

  switch (evt.type) {
    case "delta": {
      const b = bubbleFor(agent);
      b.assembled += evt.text;
      setContent(b.contentEl, b.assembled);
      if (b.thinkEl.dataset.done !== "1") {
        b.thinkEl.style.display = "none";
        b.thinkEl.dataset.done = "1";
      }
      const m = w.el.querySelector(".messages");
      m.scrollTop = m.scrollHeight;
      break;
    }
    case "thinking": {
      const b = bubbleFor(agent);
      b.thinkEl.textContent = "đang suy nghĩ… " + (evt.text || "").slice(-80);
      const m = w.el.querySelector(".messages");
      m.scrollTop = m.scrollHeight;
      break;
    }
    case "status": {
      const b = bubbleFor(agent);
      if (evt.status === "thinking" && b.thinkEl.dataset.done !== "1") {
        b.thinkEl.textContent = "đang suy nghĩ…";
      } else if (evt.status === "responding") {
        b.thinkEl.style.display = "none";
        b.thinkEl.dataset.done = "1";
      }
      break;
    }
    case "agent_status": {
      proj.statuses[agent] = evt.status;
      rerenderGraphsForSlug(slug);
      break;
    }
    case "agent_done": {
      proj.statuses[agent] = evt.status || "ok";
      const b = bubbleFor(agent);
      b.thinkEl.style.display = "none";
      rerenderGraphsForSlug(slug);
      // mirror final to dispatched agent's window if open
      mirrorDispatchedMessages(slug, agent);
      break;
    }
    case "dispatch_started": {
      state.activeDispatches.add(`${evt.source}->${evt.target}`);
      proj.statuses[evt.target] = "running";
      rerenderGraphsForSlug(slug);
      setChatStatus(w, `${evt.source} → ${evt.target}: ${(evt.task || "").slice(0, 80)}`);
      // if target's chat window is open, refresh so the user sees the task message arrive
      mirrorDispatchedMessages(slug, evt.target);
      break;
    }
    case "dispatch_complete": {
      state.activeDispatches.delete(`${evt.source}->${evt.target}`);
      proj.statuses[evt.target] = evt.status === "ok" ? "ok" : "error";
      rerenderGraphsForSlug(slug);
      setChatStatus(w, `${evt.source} → ${evt.target}: ${evt.status}`);
      mirrorDispatchedMessages(slug, evt.target);
      break;
    }
    case "dispatch_rejected": {
      setChatStatus(w, `dispatch ${evt.target} bị từ chối: ${evt.reason}`);
      break;
    }
    case "error": {
      const b = bubbleFor(agent);
      setContent(b.contentEl, (b.assembled || "") + `\n\n> **[error]** ${evt.message || ""}`);
      proj.statuses[agent] = "error";
      rerenderGraphsForSlug(slug);
      break;
    }
  }
}

function mirrorDispatchedMessages(slug, agentId) {
  const target = state.windows.find(
    (x) => x.type === "chat" && x.projectSlug === slug && x.agentId === agentId);
  if (!target) return;
  // pull latest messages from server into the target window
  refreshChatSession(target);
}

// ---------- Folder tree ----------

function _treeState(slug) {
  if (!state.tree[slug]) {
    state.tree[slug] = { expanded: new Set([""]), cache: {}, selectedAbs: null, flat: [] };
  }
  return state.tree[slug];
}

async function loadTreeRoot(slug) {
  const t = _treeState(slug);
  if (!t.cache[""]) await fetchTreeLevel(slug, "");
}

async function fetchTreeLevel(slug, relPath) {
  const t = _treeState(slug);
  try {
    const r = await fetch(`/api/projects/${slug}/tree?path=${encodeURIComponent(relPath)}`);
    if (!r.ok) { t.cache[relPath] = []; return; }
    const j = await r.json();
    t.cache[relPath] = j.items || [];
  } catch { t.cache[relPath] = []; }
}

function buildFlatTree(slug) {
  const t = _treeState(slug);
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
  if (!state.activeTab) return;
  const slug = state.activeTab;
  const t = _treeState(slug);
  buildFlatTree(slug);
  if (!t.flat.length) { root.innerHTML = '<div class="tree-empty">(trống)</div>'; return; }
  for (const item of t.flat) {
    const node = document.createElement("div");
    node.className = "tree-node " + item.type;
    if (t.selectedAbs === item.abs_path) node.classList.add("selected");
    node.style.paddingLeft = (6 + item.depth * 14) + "px";
    const arrow = item.type === "folder"
      ? (t.expanded.has(item.rel_path) ? "▾" : "▸") : "";
    const icon = item.type === "folder" ? "▣" : "·";
    node.innerHTML = `<span class="arrow">${arrow}</span>` +
      `<span class="icon">${icon}</span>` +
      `<span class="label" title="${escapeHtml(item.abs_path)}">${escapeHtml(item.name)}</span>`;
    node.onclick = () => onTreeNodeClick(slug, item);
    node.ondblclick = () => { if (item.type === "folder") onTreeToggle(slug, item); };
    root.appendChild(node);
  }
}

async function onTreeNodeClick(slug, item) {
  const t = _treeState(slug);
  t.selectedAbs = item.abs_path;
  if (item.type === "folder") await onTreeToggle(slug, item);
  else renderTree();
}

async function onTreeToggle(slug, item) {
  const t = _treeState(slug);
  if (t.expanded.has(item.rel_path)) t.expanded.delete(item.rel_path);
  else {
    t.expanded.add(item.rel_path);
    if (!t.cache[item.rel_path]) await fetchTreeLevel(slug, item.rel_path);
  }
  renderTree();
}

function selectedTreeItem(slug) {
  const t = _treeState(slug);
  if (!t.selectedAbs) return null;
  return t.flat.find((it) => it.abs_path === t.selectedAbs) || null;
}

async function copySelectedPath() {
  const slug = state.activeTab;
  if (!slug) return;
  const item = selectedTreeItem(slug);
  if (!item) { flashHint("chưa chọn file/folder nào"); return; }
  try {
    await navigator.clipboard.writeText(item.abs_path);
    flashHint("copied: " + item.abs_path);
  } catch { flashHint("clipboard bị chặn, path: " + item.abs_path); }
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
    const slug = state.activeTab;
    if (!slug) return;
    const t = _treeState(slug);
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
      if (cur && cur.type === "folder" && !t.expanded.has(cur.rel_path)) onTreeToggle(slug, cur);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      const cur = t.flat[idx];
      if (cur && cur.type === "folder" && t.expanded.has(cur.rel_path)) onTreeToggle(slug, cur);
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
      const focused = state.windows.filter((w) => !w.hidden && w.projectSlug === state.activeTab)
        .sort((a, b) => b.z - a.z)[0];
      if (focused) { e.preventDefault(); closeWindow(focused); }
    }
  });
}

window.addEventListener("resize", () => {
  state.windows.filter((w) => w.type === "graph").forEach((w) => renderGraphInWindow(w));
});

init();
