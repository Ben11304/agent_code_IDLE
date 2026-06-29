const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fs = require("fs");
const path = require("path");

// Icons
const { 
  FaRobot, FaProjectDiagram, FaExchangeAlt, FaClock, FaDesktop, 
  FaCode, FaRocket, FaCheckCircle, FaExclamationTriangle,
  FaLayerGroup, FaChartLine, FaCogs, FaUsers, FaLightbulb
} = require("react-icons/fa");
const { HiOutlineLightBulb, HiOutlineChip } = require("react-icons/hi");
const { BiNetworkChart } = require("react-icons/bi");

// Color palette - Modern AI/Tech (Midnight + Cyan)
const COLORS = {
  darkBg: "0B1120",
  darkBgAlt: "0F172A",
  cardBg: "1E293B",
  accent: "22D3EE",       // Cyan
  accentDark: "06B6D4",
  white: "FFFFFF",
  lightBg: "F8FAFC",
  lightCard: "FFFFFF",
  textDark: "0F172A",
  textMuted: "64748B",
  textLight: "E2E8F0",
  success: "10B981",
  warning: "F59E0B",
  danger: "EF4444",
  border: "334155"
};

function renderIconSvg(IconComponent, color = "#FFFFFF", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}

async function createPresentation() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "VietHuy";
  pres.title = "AgentUI - Multi-Agent Control Plane";
  pres.subject = "Localhost UI for multi-agent AI workflows";

  // Pre-render icons
  const iconRobot = await iconToBase64Png(FaRobot, "#22D3EE", 256);
  const iconGraph = await iconToBase64Png(FaProjectDiagram, "#22D3EE", 256);
  const iconDispatch = await iconToBase64Png(FaExchangeAlt, "#22D3EE", 256);
  const iconClock = await iconToBase64Png(FaClock, "#22D3EE", 256);
  const iconDesktop = await iconToBase64Png(FaDesktop, "#22D3EE", 256);
  const iconCode = await iconToBase64Png(FaCode, "#22D3EE", 256);
  const iconRocket = await iconToBase64Png(FaRocket, "#22D3EE", 256);
  const iconCheck = await iconToBase64Png(FaCheckCircle, "#10B981", 256);
  const iconWarning = await iconToBase64Png(FaExclamationTriangle, "#F59E0B", 256);
  const iconLayers = await iconToBase64Png(FaLayerGroup, "#22D3EE", 256);
  const iconChart = await iconToBase64Png(FaChartLine, "#22D3EE", 256);
  const iconCogs = await iconToBase64Png(FaCogs, "#22D3EE", 256);
  const iconUsers = await iconToBase64Png(FaUsers, "#22D3EE", 256);
  const iconLightbulb = await iconToBase64Png(FaLightbulb, "#22D3EE", 256);

  // ========== SLIDE 1: Title ==========
  let slide = pres.addSlide();
  slide.background = { color: COLORS.darkBg };
  
  // Accent bar top
  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0, y: 0, w: 10, h: 0.08, fill: { color: COLORS.accent }
  });

  slide.addImage({ data: iconRobot, x: 4.5, y: 0.8, w: 1, h: 1 });
  
  slide.addText("AgentUI", {
    x: 0.5, y: 1.9, w: 9, h: 1,
    fontSize: 56, fontFace: "Arial", bold: true,
    color: COLORS.white, align: "center"
  });
  
  slide.addText("Localhost Control Plane cho Multi-Agent AI Workflows", {
    x: 0.5, y: 3.0, w: 9, h: 0.6,
    fontSize: 20, fontFace: "Arial",
    color: COLORS.accent, align: "center"
  });

  slide.addText("Sử dụng subscription Claude & Grok — Không cần API Key", {
    x: 0.5, y: 3.65, w: 9, h: 0.4,
    fontSize: 15, fontFace: "Arial",
    color: COLORS.textLight, align: "center"
  });

  slide.addText("VietHuy  •  2026", {
    x: 0.5, y: 5.1, w: 9, h: 0.35,
    fontSize: 13, fontFace: "Arial",
    color: COLORS.textMuted, align: "center"
  });

  // ========== SLIDE 2: The Problem ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Vấn đề", {
    x: 0.5, y: 0.3, w: 9, h: 0.7,
    fontSize: 36, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const problems = [
    { icon: iconWarning, title: "Orchestrator 'mù'", desc: "Agent chính không biết worker đã làm gì. Phải tin tưởng hoặc hỏi lại thủ công." },
    { icon: iconWarning, title: "Khó verify Dispatch", desc: "Model nói 'tôi sẽ dispatch' nhưng thực tế không dispatch. Không có cách nào kiểm tra trực quan." },
    { icon: iconWarning, title: "Scheduling gần như không thể", desc: "claude -p không thể tự wake up. Lời hứa 'mỗi 30 phút' chỉ là lời nói suông." },
    { icon: iconWarning, title: "Debug địa ngục", desc: "Nhiều agent chạy song song, output trộn lẫn, context mất mát, rất khó theo dõi." }
  ];

  problems.forEach((p, i) => {
    const y = 1.2 + i * 1.05;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: 0.5, y: y, w: 9, h: 0.95,
      fill: { color: COLORS.lightCard },
      shadow: { type: "outer", color: "000000", blur: 4, offset: 1, angle: 135, opacity: 0.08 },
      rectRadius: 0.08
    });
    slide.addImage({ data: p.icon, x: 0.7, y: y + 0.2, w: 0.5, h: 0.5 });
    slide.addText(p.title, {
      x: 1.4, y: y + 0.12, w: 7.8, h: 0.35,
      fontSize: 16, fontFace: "Arial", bold: true, color: COLORS.textDark
    });
    slide.addText(p.desc, {
      x: 1.4, y: y + 0.48, w: 7.8, h: 0.4,
      fontSize: 13, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  // ========== SLIDE 3: Solution ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Giải pháp: AgentUI", {
    x: 0.5, y: 0.3, w: 9, h: 0.7,
    fontSize: 36, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  slide.addText("Một Localhost Control Plane thực thụ cho hệ thống multi-agent", {
    x: 0.5, y: 0.95, w: 9, h: 0.4,
    fontSize: 16, fontFace: "Arial", color: COLORS.textMuted
  });

  const solutions = [
    { icon: iconGraph, title: "Graph trực quan", desc: "Mỗi agent là 1 node. Click để mở chat. Drag để sắp xếp. Live status (idle / running / ok / error)." },
    { icon: iconDispatch, title: "Auto-Dispatch có feedback", desc: "Orchestrator emit <dispatch>. Backend parse live, chạy worker, trả kết quả qua ledger + auto-continuation." },
    { icon: iconClock, title: "Scheduler thực sự", desc: "Hỗ trợ interval, one-shot, goal-loop. Agent tự quyết định khi nào dừng với <schedule_stop>." },
    { icon: iconDesktop, title: "Floating Windows", desc: "Nhiều agent chat cùng lúc. Draggable, resizable. Giống VS Code nhưng cho AI agents." }
  ];

  solutions.forEach((s, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = 0.5 + col * 4.75;
    const y = 1.6 + row * 1.85;

    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: x, y: y, w: 4.5, h: 1.7,
      fill: { color: COLORS.lightCard },
      shadow: { type: "outer", color: "000000", blur: 6, offset: 2, angle: 135, opacity: 0.1 },
      rectRadius: 0.1
    });
    slide.addImage({ data: s.icon, x: x + 0.2, y: y + 0.2, w: 0.45, h: 0.45 });
    slide.addText(s.title, {
      x: x + 0.8, y: y + 0.2, w: 3.5, h: 0.4,
      fontSize: 15, fontFace: "Arial", bold: true, color: COLORS.textDark
    });
    slide.addText(s.desc, {
      x: x + 0.2, y: y + 0.7, w: 4.1, h: 0.9,
      fontSize: 12, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  // ========== SLIDE 4: Key Features ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Tính năng nổi bật", {
    x: 0.5, y: 0.3, w: 9, h: 0.6,
    fontSize: 32, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const features = [
    "Auto-dispatch với Dispatch Ledger & Auto-continuation",
    "Thêm agent qua UI (BOSS tự bootstrap file)",
    "Mix Claude + Grok trong cùng graph",
    "Slash commands (/model, /dispatch, /schedule, /track...)",
    "Workspace file tree + floating file viewer",
    "Per-agent model & effort overrides",
    "PTY streaming (tokens đến real-time)",
    "Startup reaper + resume guard + SSE heartbeat",
    "Scheduler on/off global toggle",
    "Hoàn toàn local, dùng subscription có sẵn"
  ];

  features.forEach((f, i) => {
    const col = i < 5 ? 0 : 1;
    const row = i % 5;
    const x = 0.5 + col * 4.8;
    const y = 1.1 + row * 0.85;

    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: x, y: y, w: 4.5, h: 0.75,
      fill: { color: COLORS.lightCard },
      rectRadius: 0.08
    });
    slide.addImage({ data: iconCheck, x: x + 0.15, y: y + 0.15, w: 0.42, h: 0.42 });
    slide.addText(f, {
      x: x + 0.7, y: y + 0.15, w: 3.6, h: 0.5,
      fontSize: 13, fontFace: "Arial", color: COLORS.textDark, valign: "middle"
    });
  });

  // ========== SLIDE 5: Dispatch Magic ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Cơ chế Dispatch & Ledger (Điểm khác biệt cốt lõi)", {
    x: 0.5, y: 0.3, w: 9, h: 0.6,
    fontSize: 26, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const dispatchSteps = [
    { num: "1", title: "Orchestrator emit tag", desc: "<dispatch agent=\"DATA\">Tóm tắt dữ liệu từ 2021-2024.</dispatch>" },
    { num: "2", title: "Backend parse & fire", desc: "Tách task, spawn worker qua cùng cơ chế _run_agent, emit dispatch_started." },
    { num: "3", title: "Worker hoàn thành", desc: "Kết quả ghi vào dispatch_results (ledger) + stream về UI." },
    { num: "4", title: "Auto-continuation", desc: "Orchestrator được enrich <dispatch_result> vào prompt — thấy được output thật." }
  ];

  dispatchSteps.forEach((step, i) => {
    const y = 1.05 + i * 1.1;
    slide.addShape(pres.shapes.OVAL, {
      x: 0.6, y: y + 0.1, w: 0.6, h: 0.6,
      fill: { color: COLORS.accent }
    });
    slide.addText(step.num, {
      x: 0.6, y: y + 0.1, w: 0.6, h: 0.6,
      fontSize: 20, fontFace: "Arial", bold: true, color: COLORS.darkBg,
      align: "center", valign: "middle"
    });
    slide.addText(step.title, {
      x: 1.4, y: y + 0.08, w: 8, h: 0.35,
      fontSize: 15, fontFace: "Arial", bold: true, color: COLORS.textDark
    });
    slide.addText(step.desc, {
      x: 1.4, y: y + 0.45, w: 8, h: 0.5,
      fontSize: 13, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  // ========== SLIDE 6: Scheduler ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Scheduler — Làm việc định kỳ thực sự", {
    x: 0.5, y: 0.3, w: 9, h: 0.6,
    fontSize: 28, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const schedModes = [
    { mode: "Interval", example: 'every="30m" max="8"', use: "Chạy định kỳ cố định số lần" },
    { mode: "One-shot", example: 'in="2h"', use: "Chạy 1 lần sau khoảng thời gian" },
    { mode: "Until (Goal Loop)", example: 'every="30m" until="..."', use: "Lặp đến khi agent tự <schedule_stop>" }
  ];

  schedModes.forEach((m, i) => {
    const x = 0.5 + i * 3.1;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: x, y: 1.1, w: 2.95, h: 2.0,
      fill: { color: COLORS.lightCard },
      shadow: { type: "outer", color: "000000", blur: 5, offset: 1, angle: 135, opacity: 0.08 },
      rectRadius: 0.1
    });
    slide.addText(m.mode, {
      x: x + 0.15, y: 1.25, w: 2.65, h: 0.4,
      fontSize: 15, fontFace: "Arial", bold: true, color: COLORS.accentDark
    });
    slide.addText(m.example, {
      x: x + 0.15, y: 1.7, w: 2.65, h: 0.5,
      fontSize: 12, fontFace: "Consolas", color: COLORS.textDark
    });
    slide.addText(m.use, {
      x: x + 0.15, y: 2.3, w: 2.65, h: 0.65,
      fontSize: 12, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: 0.5, y: 3.4, w: 9, h: 1.9,
    fill: { color: "FEF3C7" },
    rectRadius: 0.1
  });
  slide.addText("Tại sao quan trọng?", {
    x: 0.7, y: 3.55, w: 8.6, h: 0.35,
    fontSize: 14, fontFace: "Arial", bold: true, color: COLORS.textDark
  });
  slide.addText("Claude Code CLI (và hầu hết các CLI khác) không có cơ chế tự thức dậy. AgentUI cung cấp lớp control plane để thực hiện đúng những gì agent hứa hẹn — theo dõi job, polling, báo cáo định kỳ — mà không cần cron hay background process thủ công.", {
    x: 0.7, y: 3.95, w: 8.6, h: 1.2,
    fontSize: 13, fontFace: "Arial", color: COLORS.textDark
  });

  // ========== SLIDE 7: UI Screenshot ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.darkBg };

  slide.addText("Giao diện thực tế", {
    x: 0.5, y: 0.2, w: 9, h: 0.45,
    fontSize: 22, fontFace: "Arial", bold: true, color: COLORS.white
  });

  slide.addImage({
    path: path.join(__dirname, "screenshot.png"),
    x: 0.15, y: 0.7, w: 9.7, h: 4.75,
    sizing: { type: "contain", w: 9.7, h: 4.75 }
  });

  slide.addText("Graph canvas + Floating windows + Live dispatch animation + Workspace tree", {
    x: 0.5, y: 5.35, w: 9, h: 0.25,
    fontSize: 11, fontFace: "Arial", color: "94A3B8", align: "center"
  });

  // ========== SLIDE 8: Architecture ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Kiến trúc", {
    x: 0.5, y: 0.3, w: 9, h: 0.55,
    fontSize: 32, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const arch = [
    { layer: "Frontend", tech: "Vanilla JS + SVG Graph", desc: "Canvas đồ thị, floating windows, SSE consumer, window manager" },
    { layer: "Backend", tech: "FastAPI + SQLite", desc: "REST + SSE, dispatch parser, scheduler loop, PTY adapters" },
    { layer: "Adapter Layer", tech: "claude_stream / grok_stream", desc: "PTY + termios raw mode → streaming real-time, --resume support" },
    { layer: "Agent Projects", tech: ".agentui/project.yaml + AGENT.md", desc: "Graph được định nghĩa bởi user, bootstrap tự động từ BOSS" }
  ];

  arch.forEach((a, i) => {
    const y = 1.0 + i * 1.1;
    slide.addShape(pres.shapes.RECTANGLE, {
      x: 0.5, y: y, w: 0.12, h: 0.95,
      fill: { color: COLORS.accent }
    });
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: 0.62, y: y, w: 8.88, h: 0.95,
      fill: { color: COLORS.lightCard },
      rectRadius: 0.06
    });
    slide.addText(a.layer, {
      x: 0.85, y: y + 0.1, w: 2.2, h: 0.35,
      fontSize: 14, fontFace: "Arial", bold: true, color: COLORS.accentDark
    });
    slide.addText(a.tech, {
      x: 3.1, y: y + 0.1, w: 6.1, h: 0.35,
      fontSize: 14, fontFace: "Arial", bold: true, color: COLORS.textDark
    });
    slide.addText(a.desc, {
      x: 0.85, y: y + 0.5, w: 8.3, h: 0.4,
      fontSize: 12, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  // ========== SLIDE 9: Tech Stack ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Công nghệ & Triển khai", {
    x: 0.5, y: 0.3, w: 9, h: 0.55,
    fontSize: 30, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const stack = [
    { title: "Backend", items: "FastAPI, Uvicorn, SQLite, asyncio, pty + termios" },
    { title: "Frontend", items: "Vanilla JS, SVG, marked.js + DOMPurify, xterm.js (terminal)" },
    { title: "Streaming", items: "Server-Sent Events (SSE), PTY raw mode, no buffering" },
    { title: "Agent Models", items: "Claude Opus 4.8/4.7, Sonnet 4.6, Haiku 4.5 qua claude -p\nGrok qua aas (tùy chọn)" },
    { title: "Cài đặt", items: "./run.sh → tạo .venv → uvicorn → http://127.0.0.1:5174" },
    { title: "Deploy", items: "SSH tunnel + systemd (xem DEPLOY.md). Dùng cho remote server cá nhân." }
  ];

  stack.forEach((s, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = 0.5 + col * 4.75;
    const y = 1.0 + row * 1.45;

    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: x, y: y, w: 4.5, h: 1.3,
      fill: { color: COLORS.lightCard },
      rectRadius: 0.08
    });
    slide.addText(s.title, {
      x: x + 0.2, y: y + 0.12, w: 4.1, h: 0.35,
      fontSize: 14, fontFace: "Arial", bold: true, color: COLORS.accentDark
    });
    slide.addText(s.items, {
      x: x + 0.2, y: y + 0.5, w: 4.1, h: 0.7,
      fontSize: 12, fontFace: "Arial", color: COLORS.textDark
    });
  });

  // ========== SLIDE 10: Differentiators ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Điểm khác biệt so với các công cụ khác", {
    x: 0.5, y: 0.3, w: 9, h: 0.55,
    fontSize: 26, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const diffs = [
    { title: "Không tốn API key riêng", desc: "Dùng subscription Claude/Grok bạn đã trả tiền. Không thêm billing." },
    { title: "Observability cao nhất", desc: "Graph sáng lên khi dispatch xảy ra. Bạn thấy sự thật, không phải lời kể." },
    { title: "Feedback loop thật", desc: "Ledger + auto-continuation giải quyết vấn đề 'orchestrator không biết worker kết quả'." },
    { title: "Scheduling built-in", desc: "Goal-driven loop, recurring tasks hoạt động mà không cần cron hay daemon riêng." },
    { title: "UX desktop-class", desc: "Floating windows, multi-chat, drag node, file tree — không phải chat log đơn giản." },
    { title: "Bootstrap bằng AI", desc: "BOSS tự viết AGENT.md + manifests cho agent mới dựa trên hiểu biết project." }
  ];

  diffs.forEach((d, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = 0.5 + col * 4.75;
    const y = 1.0 + row * 1.45;

    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: x, y: y, w: 4.5, h: 1.3,
      fill: { color: COLORS.lightCard },
      rectRadius: 0.08
    });
    slide.addImage({ data: iconCheck, x: x + 0.15, y: y + 0.15, w: 0.38, h: 0.38 });
    slide.addText(d.title, {
      x: x + 0.65, y: y + 0.15, w: 3.65, h: 0.35,
      fontSize: 13, fontFace: "Arial", bold: true, color: COLORS.textDark
    });
    slide.addText(d.desc, {
      x: x + 0.15, y: y + 0.6, w: 4.2, h: 0.6,
      fontSize: 12, fontFace: "Arial", color: COLORS.textMuted
    });
  });

  // ========== SLIDE 11: Roadmap & Potential ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.lightBg };

  slide.addText("Tiềm năng & Roadmap (Startup angle)", {
    x: 0.5, y: 0.3, w: 9, h: 0.55,
    fontSize: 26, fontFace: "Arial", bold: true, color: COLORS.textDark
  });

  const roadmap = [
    { phase: "Hiện tại (MVP mạnh)", items: "• Đã dùng thực tế trên nhiều project phức tạp (ConstructionVLM, ConSynth-X...)\n• Dispatch + Scheduler + UI hoàn chỉnh\n• Có tài liệu kỹ thuật rất chi tiết (CLAUDE.md)" },
    { phase: "Gần (1-3 tháng)", items: "• Multi-user (auth đơn giản)\n• Template marketplace cho agent\n• Export / Import graph\n• Hỗ trợ thêm adapter (OpenAI, Gemini...)" },
    { phase: "Trung hạn (Startup)", items: "• Local-first + optional sync cloud\n• Team workspace\n• Usage analytics & token optimization\n• Commercial license / hosted version cho lab & công ty" }
  ];

  roadmap.forEach((r, i) => {
    const y = 1.0 + i * 1.45;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: 0.5, y: y, w: 9, h: 1.35,
      fill: { color: i === 2 ? "E0F2FE" : COLORS.lightCard },
      rectRadius: 0.08
    });
    slide.addText(r.phase, {
      x: 0.7, y: y + 0.12, w: 8.6, h: 0.35,
      fontSize: 14, fontFace: "Arial", bold: true, color: i === 2 ? COLORS.accentDark : COLORS.accentDark
    });
    slide.addText(r.items, {
      x: 0.7, y: y + 0.5, w: 8.6, h: 0.8,
      fontSize: 12, fontFace: "Arial", color: COLORS.textDark
    });
  });

  // ========== SLIDE 12: Conclusion ==========
  slide = pres.addSlide();
  slide.background = { color: COLORS.darkBg };

  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0, y: 0, w: 10, h: 0.08, fill: { color: COLORS.accent }
  });

  slide.addText("Kết luận", {
    x: 0.5, y: 0.6, w: 9, h: 0.6,
    fontSize: 32, fontFace: "Arial", bold: true, color: COLORS.white
  });

  slide.addText("AgentUI không chỉ là một giao diện chat.\nNó là lớp control plane thực sự cho kỷ nguyên multi-agent.", {
    x: 0.5, y: 1.4, w: 9, h: 0.9,
    fontSize: 18, fontFace: "Arial", color: COLORS.textLight
  });

  const conclusions = [
    "Giải quyết đau đớn thực tế: observability, dispatch verification, scheduling",
    "Chi phí thấp (dùng subscription sẵn có)",
    "Đã được battle-tested trên các project nghiên cứu phức tạp",
    "Kiến trúc sạch, có tài liệu sâu, dễ mở rộng"
  ];

  conclusions.forEach((c, i) => {
    slide.addImage({ data: iconCheck, x: 0.6, y: 2.5 + i * 0.55, w: 0.35, h: 0.35 });
    slide.addText(c, {
      x: 1.1, y: 2.5 + i * 0.55, w: 8.3, h: 0.45,
      fontSize: 15, fontFace: "Arial", color: COLORS.white
    });
  });

  slide.addText("Sẵn sàng để scale thành sản phẩm startup nhỏ.", {
    x: 0.5, y: 4.9, w: 9, h: 0.4,
    fontSize: 16, fontFace: "Arial", bold: true, color: COLORS.accent
  });

  // Save
  const outputPath = path.join(__dirname, "AgentUI_Pitch_Deck.pptx");
  await pres.writeFile({ fileName: outputPath });
  console.log("✅ Presentation created:", outputPath);
}

createPresentation().catch(err => {
  console.error("Error creating presentation:", err);
  process.exit(1);
});