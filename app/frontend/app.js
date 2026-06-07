// AgentUI frontend, vanilla JS + SVG.
// State shape:
//   projects: list from /api/projects
//   openTabs: [slug, ...]
//   activeTab: slug
//   projectCache: { slug: detail }
//   selectedAgent: { slug, agentId } | null
//   currentSession: { id, claude_session_id, messages: [...] } | null

const state = {
  projects: [],
  openTabs: [],
  activeTab: null,
  projectCache: {},
  selectedAgent: null,
  currentSession: null,
  streaming: false,
  activeDispatches: new Set(),
  viewBoxes: {},   // slug -> {x,y,w,h}
  graphBounds: {}, // slug -> {x,y,w,h} of fitted content
};

const $ = (id) => document.getElementById(id);

async function init() {
  await loadProjects();
  renderProjectList();
  if (state.projects.length) {
    await openProject(state.projects[0].slug);
  }
  bindChat();
  bindZoomControls();
}

async function loadProjects() {
  const r = await fetch("/api/projects");
  const j = await r.json();
  state.projects = j.projects || [];
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
  renderGraph();
}

function closeTab(slug) {
  state.openTabs = state.openTabs.filter((s) => s !== slug);
  if (state.activeTab === slug) {
    state.activeTab = state.openTabs[state.openTabs.length - 1] || null;
  }
  if (state.selectedAgent && state.selectedAgent.slug === slug) {
    state.selectedAgent = null;
    state.currentSession = null;
    renderRightPanel();
  }
  renderTabs();
  renderProjectList();
  renderGraph();
}

function renderTabs() {
  const root = $("tabs");
  root.innerHTML = "";
  state.openTabs.forEach((slug) => {
    const proj = state.projectCache[slug];
    const tab = document.createElement("div");
    tab.className = "tab" + (state.activeTab === slug ? " active" : "");
    tab.innerHTML = `<span>${escapeHtml(proj ? proj.name : slug)}</span>
      <span class="close" data-slug="${slug}">×</span>`;
    tab.onclick = (e) => {
      if (e.target.classList.contains("close")) {
        e.stopPropagation();
        closeTab(slug);
      } else {
        state.activeTab = slug;
        renderTabs();
        renderGraph();
        renderProjectList();
      }
    };
    root.appendChild(tab);
  });
}

// ---------- Graph layout & render ----------

function layoutAgents(agents, edges) {
  // Layer by longest path from a root.
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
  ids.forEach((id) => {
    const lvl = depth[id];
    (layers[lvl] = layers[lvl] || []).push(id);
  });
  return { depth, layers };
}

function renderGraph() {
  const svg = $("graph");
  const empty = $("canvasEmpty");
  svg.innerHTML = "";
  const proj = state.projectCache[state.activeTab];
  if (!proj) {
    empty.style.display = "flex";
    updateZoomLevel();
    return;
  }
  empty.style.display = "none";

  const W = svg.clientWidth || 800;
  const H = svg.clientHeight || 600;
  const { depth, layers } = layoutAgents(proj.agents, proj.edges);
  const layerKeys = Object.keys(layers).map(Number).sort((a, b) => a - b);

  const nodeW = 160, nodeH = 60, vGap = 90, hGap = 30;
  const positions = {};
  const yStart = 70;
  let maxRowW = 0;
  layerKeys.forEach((lvl, li) => {
    const row = layers[lvl];
    const totalW = row.length * nodeW + (row.length - 1) * hGap;
    if (totalW > maxRowW) maxRowW = totalW;
    const xStart = Math.max(40, (W - totalW) / 2);
    row.forEach((id, i) => {
      positions[id] = {
        x: xStart + i * (nodeW + hGap),
        y: yStart + li * (nodeH + vGap),
      };
    });
  });

  // Content bounding box for fit-to-view.
  const allPos = Object.values(positions);
  if (allPos.length) {
    const minX = Math.min(...allPos.map(p => p.x)) - 30;
    const minY = Math.min(...allPos.map(p => p.y)) - 30;
    const maxX = Math.max(...allPos.map(p => p.x + nodeW)) + 30;
    const maxY = Math.max(...allPos.map(p => p.y + nodeH)) + 30;
    state.graphBounds[proj.slug] = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  // Defs / arrow marker
  const ns = "http://www.w3.org/2000/svg";
  const defs = document.createElementNS(ns, "defs");
  defs.innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#5a6280"/></marker>`;
  svg.appendChild(defs);

  // Edges
  proj.edges.forEach((e) => {
    const s = positions[e.source];
    const t = positions[e.target];
    if (!s || !t) return;
    const x1 = s.x + nodeW / 2, y1 = s.y + nodeH;
    const x2 = t.x + nodeW / 2, y2 = t.y;
    const my = (y1 + y2) / 2;
    const path = document.createElementNS(ns, "path");
    const dispatchKey = `${e.source}->${e.target}`;
    let edgeCls = "edge";
    if (state.activeDispatches.has(dispatchKey)) edgeCls += " edge-active";
    path.setAttribute("class", edgeCls);
    path.setAttribute("d", `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`);
    svg.appendChild(path);
  });

  // Nodes
  proj.agents.forEach((a) => {
    const pos = positions[a.id];
    if (!pos) return;
    const isOrchestrator = (a.parents || []).length === 0;
    const isSelected = state.selectedAgent
      && state.selectedAgent.slug === proj.slug
      && state.selectedAgent.agentId === a.id;
    const status = (proj.statuses && proj.statuses[a.id]) || "idle";

    const g = document.createElementNS(ns, "g");
    g.style.cursor = "pointer";
    g.onclick = () => selectAgent(proj.slug, a.id);

    const rect = document.createElementNS(ns, "rect");
    let cls = "node-rect";
    if (isOrchestrator) cls += " orchestrator";
    if (isSelected) cls += " selected";
    if (status === "running") cls += " pulse";
    rect.setAttribute("class", cls);
    rect.setAttribute("data-status", status);
    rect.setAttribute("x", pos.x);
    rect.setAttribute("y", pos.y);
    rect.setAttribute("rx", 8);
    rect.setAttribute("ry", 8);
    rect.setAttribute("width", nodeW);
    rect.setAttribute("height", nodeH);
    g.appendChild(rect);

    const label = document.createElementNS(ns, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("x", pos.x + nodeW / 2);
    label.setAttribute("y", pos.y + 24);
    label.setAttribute("text-anchor", "middle");
    label.textContent = a.id;
    g.appendChild(label);

    const model = document.createElementNS(ns, "text");
    model.setAttribute("class", "node-model");
    model.setAttribute("x", pos.x + nodeW / 2);
    model.setAttribute("y", pos.y + 42);
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

  applyViewBox(svg, proj.slug);
}

function applyViewBox(svg, slug) {
  if (!state.viewBoxes[slug]) {
    fitGraph(slug);
  }
  const vb = state.viewBoxes[slug];
  if (!vb) return;
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  updateZoomLevel();
}

function fitGraph(slug) {
  const b = state.graphBounds[slug];
  if (!b) return;
  state.viewBoxes[slug] = { ...b };
}

function updateZoomLevel() {
  const el = $("zoomLevel");
  if (!el) return;
  const slug = state.activeTab;
  const vb = state.viewBoxes[slug];
  const b = state.graphBounds[slug];
  if (!vb || !b) { el.textContent = ""; return; }
  const pct = Math.round((b.w / vb.w) * 100);
  el.textContent = `${pct}%`;
}

function zoomBy(factor, anchorPx, anchorPy) {
  const slug = state.activeTab;
  const vb = state.viewBoxes[slug];
  if (!vb) return;
  const newW = vb.w * factor;
  const newH = vb.h * factor;
  // anchor: keep the content point under cursor stationary in screen space
  if (anchorPx !== undefined && anchorPy !== undefined) {
    vb.x += (vb.w - newW) * anchorPx;
    vb.y += (vb.h - newH) * anchorPy;
  } else {
    vb.x += (vb.w - newW) / 2;
    vb.y += (vb.h - newH) / 2;
  }
  vb.w = newW;
  vb.h = newH;
  const svg = $("graph");
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  updateZoomLevel();
}

let panState = null;

function onGraphWheel(e) {
  if (!state.viewBoxes[state.activeTab]) return;
  e.preventDefault();
  const svg = e.currentTarget;
  const rect = svg.getBoundingClientRect();
  const px = (e.clientX - rect.left) / rect.width;
  const py = (e.clientY - rect.top) / rect.height;
  const factor = e.deltaY < 0 ? 0.88 : 1.12;
  zoomBy(factor, px, py);
}

function onGraphMouseDown(e) {
  // Only pan when click hits empty canvas, not a node group.
  // Nodes use a <g> element; the wrapper svg or the defs/path get hit on background.
  let t = e.target;
  while (t && t !== e.currentTarget) {
    if (t.tagName === "g") return; // clicked on a node, let node onclick handle
    t = t.parentNode;
  }
  const vb = state.viewBoxes[state.activeTab];
  if (!vb) return;
  panState = { startX: e.clientX, startY: e.clientY, vb: { ...vb } };
  e.currentTarget.classList.add("panning");
  document.addEventListener("mousemove", onGraphMouseMove);
  document.addEventListener("mouseup", onGraphMouseUp);
}

function onGraphMouseMove(e) {
  if (!panState) return;
  const svg = $("graph");
  const rect = svg.getBoundingClientRect();
  const dx = ((e.clientX - panState.startX) / rect.width) * panState.vb.w;
  const dy = ((e.clientY - panState.startY) / rect.height) * panState.vb.h;
  const vb = state.viewBoxes[state.activeTab];
  vb.x = panState.vb.x - dx;
  vb.y = panState.vb.y - dy;
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}

function onGraphMouseUp() {
  panState = null;
  $("graph").classList.remove("panning");
  document.removeEventListener("mousemove", onGraphMouseMove);
  document.removeEventListener("mouseup", onGraphMouseUp);
}

function bindZoomControls() {
  const svg = $("graph");
  svg.addEventListener("wheel", onGraphWheel, { passive: false });
  svg.addEventListener("mousedown", onGraphMouseDown);
  $("zoomIn").onclick = () => zoomBy(0.85);
  $("zoomOut").onclick = () => zoomBy(1.18);
  $("zoomReset").onclick = () => {
    delete state.viewBoxes[state.activeTab];
    fitGraph(state.activeTab);
    const vb = state.viewBoxes[state.activeTab];
    if (vb) {
      svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
      updateZoomLevel();
    }
  };
}

function modelLabel(a) {
  if (a.model === "grok") return "grok";
  const m = a.claude_model || "claude-sonnet-4-6";
  return m.replace(/^claude-/, "").replace(/-\d+$/, (s) => s);
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

// ---------- Right panel ----------

async function selectAgent(slug, agentId) {
  state.selectedAgent = { slug, agentId };
  await refreshSession();
  renderGraph();
  renderRightPanel();
}

async function refreshSession() {
  const { slug, agentId } = state.selectedAgent;
  const r = await fetch(`/api/projects/${slug}/agents/${agentId}/session`);
  const j = await r.json();
  state.currentSession = { ...j.session, messages: j.messages };
}

function renderRightPanel() {
  const header = $("rightHeader");
  const msgRoot = $("messages");
  msgRoot.innerHTML = "";
  if (!state.selectedAgent) {
    header.textContent = "Chưa chọn agent";
    return;
  }
  const proj = state.projectCache[state.selectedAgent.slug];
  const agent = proj.agents.find((a) => a.id === state.selectedAgent.agentId);
  header.innerHTML = `${escapeHtml(agent.id)}
    <span class="sub">${escapeHtml(agent.role || "")} • ${escapeHtml(modelLabel(agent))}</span>`;

  (state.currentSession?.messages || []).forEach((m) => addBubble(m.role, m.content));
}

function addBubble(role, text) {
  const root = $("messages");
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
  const html = renderMessage(text);
  el.innerHTML = html;
}

function renderMessage(text) {
  // Replace complete dispatch tags with placeholder, render markdown, then swap back.
  const cards = [];
  let stripped = text.replace(DISPATCH_TAG_RE, (_m, target, body) => {
    const idx = cards.length;
    cards.push({ target, body, complete: true });
    return `\n\n@@DISPATCH_CARD_${idx}@@\n\n`;
  });
  // Detect unclosed dispatch tag being streamed in.
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
  if (window.DOMPurify) {
    html = DOMPurify.sanitize(html, { ADD_TAGS: ["details", "summary"] });
  }
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

// ---------- Chat dispatch ----------

function bindChat() {
  const form = $("chatForm");
  const input = $("chatInput");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || state.streaming) return;
    if (!state.selectedAgent) {
      setStatus("Hãy chọn một agent trên đồ thị trước.");
      return;
    }
    input.value = "";
    await sendMessage(text);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
}

async function sendMessage(text) {
  const { slug, agentId: rootAgent } = state.selectedAgent;
  const proj = state.projectCache[slug];

  addBubble("user", text);
  const bubbles = {}; // agentId -> {bubble, contentEl, thinkEl, assembled}

  function bubbleFor(agentId) {
    if (bubbles[agentId]) return bubbles[agentId];
    const b = addBubble("assistant", "");
    const roleEl = b.querySelector(".role");
    roleEl.textContent = "assistant • " + agentId;
    const contentEl = b.querySelector(".content");
    const thinkEl = document.createElement("div");
    thinkEl.className = "thinking";
    b.insertBefore(thinkEl, contentEl);
    bubbles[agentId] = { bubble: b, contentEl, thinkEl, assembled: "" };
    return bubbles[agentId];
  }

  state.streaming = true;
  $("chatSend").disabled = true;
  setStatus("đang chạy...");
  proj.statuses[rootAgent] = "running";
  renderGraph();

  try {
    const resp = await fetch(`/api/projects/${slug}/agents/${rootAgent}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!resp.ok || !resp.body) {
      const errText = await resp.text();
      const b = bubbleFor(rootAgent);
      setContent(b.contentEl, `(lỗi backend ${resp.status}) ${errText}`);
      proj.statuses[rootAgent] = "error";
      renderGraph();
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
        handleEvent(slug, evt, bubbleFor, rootAgent);
      }
    }
    setStatus("xong");
  } catch (err) {
    setStatus("network error: " + err.message);
    proj.statuses[rootAgent] = "error";
    renderGraph();
  } finally {
    state.streaming = false;
    $("chatSend").disabled = false;
  }
}

function handleEvent(slug, evt, bubbleFor, rootAgent) {
  const proj = state.projectCache[slug];
  const agent = evt.agent || rootAgent;
  const showHere = state.selectedAgent
    && state.selectedAgent.slug === slug
    && agent === state.selectedAgent.agentId;

  switch (evt.type) {
    case "delta": {
      if (!showHere) break;
      const b = bubbleFor(agent);
      b.assembled += evt.text;
      setContent(b.contentEl, b.assembled);
      if (b.thinkEl.dataset.done !== "1") {
        b.thinkEl.style.display = "none";
        b.thinkEl.dataset.done = "1";
      }
      $("messages").scrollTop = $("messages").scrollHeight;
      break;
    }
    case "thinking": {
      if (!showHere) break;
      const b = bubbleFor(agent);
      b.thinkEl.textContent = "đang suy nghĩ… " + (evt.text || "").slice(-80);
      $("messages").scrollTop = $("messages").scrollHeight;
      break;
    }
    case "status": {
      if (!showHere) break;
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
      renderGraph();
      break;
    }
    case "agent_done": {
      proj.statuses[agent] = evt.status || "ok";
      if (showHere) {
        const b = bubbleFor(agent);
        b.thinkEl.style.display = "none";
      }
      renderGraph();
      break;
    }
    case "dispatch_started": {
      const key = `${evt.source}->${evt.target}`;
      state.activeDispatches.add(key);
      proj.statuses[evt.target] = "running";
      renderGraph();
      const preview = (evt.task || "").replace(/\s+/g, " ").slice(0, 80);
      setStatus(`${evt.source} → ${evt.target}: ${preview}`);
      break;
    }
    case "dispatch_complete": {
      const key = `${evt.source}->${evt.target}`;
      state.activeDispatches.delete(key);
      proj.statuses[evt.target] = evt.status === "ok" ? "ok" : "error";
      renderGraph();
      setStatus(`${evt.source} → ${evt.target} ${evt.status === "ok" ? "xong" : "lỗi"}`);
      break;
    }
    case "dispatch_rejected": {
      setStatus(`dispatch bị từ chối ${evt.target}: ${evt.reason}`);
      break;
    }
    case "error": {
      if (showHere) {
        const b = bubbleFor(agent);
        setContent(b.contentEl, (b.assembled || "") + `\n\n> **[error]** ${evt.message || ""}`);
      }
      proj.statuses[agent] = "error";
      renderGraph();
      break;
    }
    case "meta":
    case "start":
    case "complete":
      break;
  }
}

function setStatus(s) {
  $("chatStatus").textContent = s;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

window.addEventListener("resize", renderGraph);
init();
