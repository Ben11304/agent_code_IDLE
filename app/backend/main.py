from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import pty
import re
import signal
import struct
import termios
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, projects
from . import telegram_bot as tg
from .adapters import get_stream

TREE_EXCLUDE = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".DS_Store", ".vscode", ".idea", ".pytest_cache", ".mypy_cache",
    ".ipynb_checkpoints", "dist", "build", ".cache",
}

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="AgentUI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()
_orphan_count = db.cleanup_stale_running()
if _orphan_count:
    print(f"[startup] reset {_orphan_count} orphan 'running' session(s) to 'cancelled'")

DISPATCH_RE = re.compile(
    r'<dispatch\s+agent="([^"]+)"\s*>([\s\S]*?)</dispatch>',
    re.IGNORECASE,
)

# Scheduler tags, parsed from the stream like <dispatch>. <schedule> registers a
# recurring/deferred/goal-driven re-invocation; <schedule_stop> lets a goal-loop
# (until-mode) self-terminate. The \s+ after "schedule" makes this NOT match
# <schedule_stop ...>. See docs/scheduler-spec.md.
SCHEDULE_RE = re.compile(r'<schedule\s+([^>]*?)>([\s\S]*?)</schedule>', re.IGNORECASE)
SCHEDULE_STOP_RE = re.compile(
    r'<schedule_stop\b([^>]*?)(?:/>|>([\s\S]*?)</schedule_stop>)', re.IGNORECASE)
_SCHED_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

_SCHED_INTERVAL_FLOOR_S = 300      # 5 min — bound subscription-quota burn
_SCHED_UNTIL_CEILING = 48          # hard ceiling for until-loops without <schedule_stop>
_SCHED_ZOMBIE_DAYS = 7

_SCHEDULE_INSTRUCTIONS = (
    "\n\n## ⏰ Recurring / deferred work — emit <schedule>, NEVER narrate a loop (control-plane parsed)\n"
    "You CANNOT run background timers, cron jobs, pollers, heartbeats, or detached processes. A turn ends and "
    "your CLI process EXITS. The ONLY thing that can ever wake you up again is a `<schedule>` tag parsed by the "
    "control plane. No tag = nothing runs = you will NEVER be re-invoked.\n\n"
    "**Trigger — whenever the user asks you to track / monitor / watch / poll / check periodically / keep them "
    "posted / report every N minutes / run until done — or in Vietnamese: 'theo dõi', 'tracking', 'mỗi 30 phút', "
    "'định kỳ', 'báo cáo định kỳ', 'tự chạy tới đích', 'cho đến khi xong' — you MUST emit a `<schedule>` tag in "
    "THAT SAME response.** Pick the form:\n"
    '  <schedule every="30m" until="<the condition that ends it>">Check …; DONE → report + <schedule_stop/>; '
    'FAILED → fix & rerun; still running → note progress to state/progress.md.</schedule>   ← "track until done"\n'
    '  <schedule every="30m" max="8">Check … and report.</schedule>                          ← fixed cadence/count\n'
    '  <schedule in="2h">Do … once.</schedule>                                                ← one-shot\n'
    "Durations: `30m | 2h | 90s | 1d`; minimum 5m for `every`. End a goal loop with "
    '<schedule_stop reason="..."/>.\n\n'
    "**ABSOLUTELY FORBIDDEN: describing tracking that does not exist.** Sentences like 'tracking active', "
    "'loop is running', 'BOSS heartbeat ~30′', 'VLM poller', 'I'll wake myself every 30 min', 'tôi sẽ tự đánh "
    "thức', 'anh cứ nghỉ, tracking lo phần còn lại' — WITHOUT a `<schedule>` tag in the same message — are LIES. "
    "There is no heartbeat, no poller, no loop. The user verifies on the graph: no 🕒 badge ⇒ you lied and they "
    "get silence. If you truly should not schedule, say so in one plain sentence — do not invent a background "
    "process.\n\n"
    "**Emit <schedule_stop> ONLY to actually end a loop you started on an EARLIER turn** — never as an "
    "illustration/quote, and never in the same message where you create a schedule (that would instantly kill "
    "it). To explain the stop tag in prose, describe it in words; do not write the literal tag."
)

# Phrases that mean the user wants recurring / deferred follow-up. If a user turn
# matches this AND the agent's response emitted no <schedule> tag, the driver fires
# one corrective continuation (delivered via the prompt channel, so it reaches the
# model even on a resumed session where --append-system-prompt may not).
_TRACK_INTENT_RE = re.compile(
    r"(tracking|track this|track it|track the|monitor|keep me posted|keep an eye|periodically|"
    r"recurring|every\s+\d+\s*(m|min|minute|mins|minutes|h|hr|hour|hours)\b|"
    r"theo\s*d[õo]i|đ[ịi]nh\s*k[ỳy]|b[áa]o\s*c[áa]o\s*đ[ịi]nh\s*k[ỳy]|m[ỗo]i\s+\d+\s*(ph[úu]t|gi[ờo]|p|h|m)|"
    r"cho\s+đ[ếe]n\s+khi\s+xong|t[ựu]\s+ch[ạa]y|until\s+(it'?s\s+)?(done|finished|complete|over))",
    re.IGNORECASE)

_SCHEDULE_NUDGE = (
    "[CONTROL-PLANE SCHEDULE CHECK] Your previous response described tracking / monitoring / a recurring "
    "check (a 'heartbeat', 'poller', 'loop active', 'I'll wake myself every N min', 'tôi sẽ tự đánh thức', "
    "'tracking lo phần còn lại') — but you emitted NO <schedule> tag. So NOTHING was registered: there is no "
    "timer, no heartbeat, no poller, no loop, and you will NOT be re-invoked. The user will get silence — the "
    "exact failure to avoid. Fix it NOW by choosing ONE:\n"
    '(a) Emit the real recurring tag, e.g. <schedule every="30m" until="<goal that ends it>">Concise check; '
    'DONE → report + <schedule_stop/>; FAILED → fix & continue; else note progress.</schedule>\n'
    '(b) Or a fixed cadence: <schedule every="30m" max="8">…</schedule>.\n'
    "(c) Or, if you genuinely should NOT schedule this, say so plainly in ONE sentence and retract the "
    "tracking claim.\n"
    "Do NOT again describe a background loop without emitting the tag."
)


def _has_schedule_tag(text: str) -> bool:
    return bool(text) and bool(SCHEDULE_RE.search(text) or SCHEDULE_STOP_RE.search(text))


def _looks_like_tracking_intent(text: str) -> bool:
    return bool(text) and bool(_TRACK_INTENT_RE.search(text))


def _should_include_schedule_instructions(slug: str, agent_id: str, message: str) -> bool:
    """Include the heavy schedule instructions (~524 tokens) only when relevant.
    Include when:
    - User message shows tracking/recurring/monitoring intent
    - This is a scheduled fire (wrapped prompt)
    - The agent currently has at least one active schedule (may need <schedule_stop>)
    Never include when the scheduler is globally disabled — nothing can fire, so
    the ~524-token guard is pure overhead (this is what the off switch promises).
    """
    if not _scheduler_enabled():
        return False
    msg = (message or "").lower()
    if _looks_like_tracking_intent(message or ""):
        return True
    if "scheduled check" in msg or "schedule_stop" in msg or "automatic recurring" in msg:
        return True
    try:
        for t in db.list_scheduled_tasks(slug):
            if t.get("agent_id") == agent_id and t.get("active"):
                return True
    except Exception:
        pass
    return False


def _parse_duration(s: str) -> Optional[int]:
    """'30m' / '2h' / '90s' / '1d' → seconds. None if unparseable."""
    if not s:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", s, re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# Size guard when injecting a worker result back into the orchestrator's prompt.
# Larger results are truncated head+tail with a marker; the full output remains in
# the worker's own session db / state/ files.
_LEDGER_MAX_CHARS = 8000
_LEDGER_HEAD_CHARS = _LEDGER_MAX_CHARS - 1200
_LEDGER_TAIL_CHARS = 1000


def _format_results_as_context(results: list) -> str:
    """Render dispatch ledger rows as <dispatch_result> blocks that the
    orchestrator model can read on its next turn. Mirrors the <dispatch> tag
    style the orchestrator already knows from `_dispatch_instructions`.
    """
    blocks = []
    for r in results:
        text = r.get("result_text") or ""
        if len(text) > _LEDGER_MAX_CHARS:
            head = text[:_LEDGER_HEAD_CHARS]
            tail = text[-_LEDGER_TAIL_CHARS:]
            text = (
                f"{head}\n\n[... truncated; full output in {r['target_agent']} "
                f"chat window or its `state/` files ...]\n\n{tail}"
            )
        status_attr = ""
        if r.get("status") and r["status"] != "ok":
            status_attr = f' status="{r["status"]}"'
        task_excerpt = (r.get("task") or "").strip().splitlines()[0] if r.get("task") else ""
        if len(task_excerpt) > 200:
            task_excerpt = task_excerpt[:200] + "…"
        # Summary-only digest (spec §6.9): a one-line, mechanically-extracted header
        # so the orchestrator can scan fast and deep-read ONLY the exceptions.
        ps = _parse_structured(r.get("result_text") or "")
        res, esc = ps.get("result") or {}, ps.get("escalate")
        bits = []
        if res.get("status"):
            bits.append(f"status={res['status']}")
        if res.get("goal_status"):
            bits.append(f"goal={res['goal_status']}")
        if esc:
            bits.append(f"ESCALATE:{esc.get('type', '?')}")
        if res.get("summary"):
            bits.append(res["summary"][:120])
        digest_line = ("[digest] " + " · ".join(bits) + "\n") if bits else ""
        blocks.append(
            f'<dispatch_result from="{r["target_agent"]}"{status_attr}>\n'
            f'{digest_line}'
            f'(task: {task_excerpt})\n\n'
            f'{text}\n'
            f'</dispatch_result>'
        )
    if not blocks:
        return ""
    return (
        "\n\n".join(blocks)
        + "\n\n---\n\nThe `<dispatch_result>` blocks above contain the actual outputs "
        "from workers you dispatched in your previous response(s). Reason over this "
        "real data; **do NOT pretend you are still waiting for results**. Scan the "
        "`[digest]` lines first; **deep-read only the blocks where status=blocked, "
        "goal=missed/partial, or an ESCALATE is present** — the rest you can accept on "
        "the digest. If a result is incomplete or in error/cancelled status, decide "
        "whether to retry, escalate, or report to the user."
    )


@app.get("/api/projects")
def api_projects():
    return {"projects": projects.list_projects()}


@app.get("/api/projects/{slug}")
def api_project(slug: str):
    p = projects.get_project(slug)
    if not p:
        raise HTTPException(404, "project not found")
    out = dict(p)
    statuses = {}
    overrides = db.list_agent_overrides(slug)
    for a in out["agents"]:
        statuses[a["id"]] = db.get_last_status(slug, a["id"]) or "idle"
        ov = overrides.get(a["id"]) or {}
        a["default_claude_model"] = a.get("claude_model") or "claude-sonnet-4-6"
        a["default_grok_model"] = a.get("grok_model") or "grok-build"
        a["default_deepseek_model"] = a.get("deepseek_model") or "deepseek-v4-flash"
        a["default_glm_model"] = a.get("glm_model") or "glm-4.6"  # glm-4.6 | glm-5.2
        a["default_effort"] = a.get("effort")
        if ov.get("claude_model"):
            a["claude_model"] = ov["claude_model"]
        if ov.get("grok_model"):
            a["grok_model"] = ov["grok_model"]
        if ov.get("deepseek_model"):
            a["deepseek_model"] = ov["deepseek_model"]
        if ov.get("glm_model"):
            a["glm_model"] = ov["glm_model"]
        if ov.get("model"):
            a["model"] = ov["model"]  # adapter override wins over project.yaml
        if "effort" in ov:
            a["effort"] = ov["effort"]
    out["statuses"] = statuses
    out["positions"] = db.get_node_positions(slug)
    out["schedules"] = [_schedule_public(t) for t in db.list_scheduled_tasks(slug)]
    # Telegram control channel: stamp the one agent it drives (if any) so the
    # graph UI can show a "connected to <bot>" badge on its node.
    conn = tg.connection()
    if conn and conn["slug"] == slug:
        for a in out["agents"]:
            a["telegram_bot"] = conn["bot"] if a["id"] == conn["agent_id"] else None
    return out


class NodePositions(BaseModel):
    positions: dict[str, dict]


@app.post("/api/projects/{slug}/positions")
def api_save_positions(slug: str, body: NodePositions):
    p = projects.get_project(slug)
    if not p:
        raise HTTPException(404, "project not found")
    valid_ids = {a["id"] for a in p["agents"]}
    clean = {}
    for agent_id, pos in body.positions.items():
        if agent_id not in valid_ids:
            continue
        try:
            clean[agent_id] = {"x": float(pos["x"]), "y": float(pos["y"])}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"invalid position for {agent_id}")
    db.set_node_positions(slug, clean)
    return {"saved": sorted(clean.keys())}


@app.delete("/api/projects/{slug}/positions")
def api_clear_positions(slug: str):
    if not projects.get_project(slug):
        raise HTTPException(404, "project not found")
    db.clear_node_positions(slug)
    return {"cleared": True}


# Approximate context-window sizes per adapter family. Used only to render the
# "context window" gauge in the expandable agent panel — token counts are
# ESTIMATED (chars/4 over the active session + system prompt), not exact, since
# the CLIs do not report usage in a form we persist. Labelled "≈" in the UI.
# Real context window per MODEL — the denominator of the context gauge and the
# auto-compact threshold. The numerator is the CLI's real reported token usage;
# only this denominator is configuration, so it must match the model actually
# being served. Source: Anthropic model catalog (2026-06): Fable 5, Opus
# 4.7/4.8 and Sonnet 4.6 are 1M-context models; Haiku 4.5 is 200K. The user's
# Claude Code subscription serves the 1M window (confirmed via /model:
# "Opus 4.8 (1M context)"). Unknown models fall back to the adapter default —
# deliberately conservative: compacting early is cheap, while a real overflow
# fails the turn loudly (CLI returns a prompt-too-long error; resume guard +
# cold-start preamble recover) — it does NOT silently hallucinate past the
# limit.
_MODEL_CONTEXT_WINDOWS = {
    "claude-fable-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    # DeepSeek V4 (served via DeepSeek's native Anthropic endpoint → claude -p).
    # V4 ships 1M context by default. deepseek-chat/deepseek-reasoner are the
    # legacy aliases (retire 2026-07-24, mapped to v4-flash thinking/non-thinking).
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    # GLM (Zhipu) via its native Anthropic-compatible endpoint → claude -p.
    "glm-4.6": 200_000,
    "glm-5.2": 1_000_000,
    "glm-4.5-air": 128_000,
}
_CONTEXT_WINDOWS = {"claude": 200_000, "grok": 256_000, "deepseek": 1_000_000, "glm": 200_000}  # adapter fallback


def _context_window_for(model_kind: str, model_id: Optional[str]) -> int:
    if model_id and model_id in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[model_id]
    return _CONTEXT_WINDOWS.get(model_kind, 200_000)


def _usage_ctx_tokens(usage: dict, window: int | None = None) -> int:
    """Context-window occupancy from a CLI usage blob = the LARGEST single API
    request in the turn, NOT the top-level sums (cumulative billing across
    agentic rounds — routinely exceeds the window). Shared by the stats endpoint
    and the auto-compact threshold check."""
    def _req_total(d: dict) -> int:
        return (
            (d.get("input_tokens") or 0)
            + (d.get("cache_creation_input_tokens") or 0)
            + (d.get("cache_read_input_tokens") or 0)
            + (d.get("output_tokens") or 0)
        )
    iters = usage.get("iterations") or []
    if iters:
        return max(_req_total(it) for it in iters)
    total = _req_total(usage)
    # Cumulative-usage providers (e.g. DeepSeek via its Anthropic-compatible
    # endpoint) report tokens SUMMED across every internal tool-use round, with
    # NO per-iteration breakdown (iterations == []). The cumulative cache_read
    # alone can reach several million in one agentic turn. A single request can
    # never exceed the model's context window, so when the blob does, it is
    # cumulative billing, not occupancy — fall back to the non-cumulative buckets
    # (fresh input + output) which approximate the final request's footprint.
    # Without this, the gauge pins at 100% and auto-compact fires every turn,
    # thrashing the --resume session. Claude's per-request totals stay < window,
    # so this branch never triggers for Claude.
    if window and total > window:
        return (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    return total


def _session_context_pct(sess: Optional[dict], model_kind: str,
                         model_id: Optional[str] = None) -> float:
    """Context % of a session's last completed turn, 0.0 when unknown (no usage
    recorded yet — fresh session, or a grok node which reports no usage)."""
    if not sess or not sess.get("usage"):
        return 0.0
    try:
        usage = json.loads(sess["usage"])
    except (ValueError, TypeError):
        return 0.0
    window = _context_window_for(model_kind, model_id)
    if not window:
        return 0.0
    return round(_usage_ctx_tokens(usage, window) / window * 100.0, 1)


def _memory_info(project_root: str, agent: dict, cwd_abs: str) -> Optional[dict]:
    """Inspect an agent's persistent memory file (`state/progress.md`): when it
    was last written and the most recent dated headline. Returns None if absent.

    The memory file lives in the agent's own folder, which is the parent of its
    `system_prompt_file` (e.g. `BOSS/AGENT.md` → `BOSS/`). That is NOT always the
    same as `cwd` (an orchestrator may run with `cwd: .`), so we try the prompt
    folder first, then `cwd`, then the `<ID>` convention.
    """
    root = Path(project_root)
    candidates = []
    spf = agent.get("system_prompt_file") or ""
    if spf:
        candidates.append((root / spf).parent)
    if cwd_abs:
        candidates.append(Path(cwd_abs))
    candidates.append(root / agent["id"])

    prog = None
    for base in candidates:
        p = base / "state" / "progress.md"
        if p.exists() and p.is_file():
            prog = p
            break
    if prog is None:
        return None
    try:
        st = prog.stat()
    except OSError:
        return None
    headline = ""
    try:
        with prog.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                if s.startswith("## "):
                    headline = s[3:].strip()
                    break
    except OSError:
        pass
    try:
        rel = str(prog.resolve().relative_to(Path(project_root).resolve()))
    except Exception:
        rel = str(prog)
    return {"path": rel, "mtime": st.st_mtime, "headline": headline}


def _agent_dir(project_root: str, agent: dict, cwd_abs: str) -> Path:
    """The agent's OWN folder (where `state/` lives) — parent of its
    `system_prompt_file`, else `cwd`, else the `<ID>` convention. Mirrors the
    resolution order in `_memory_info` so the rollup lands beside progress.md."""
    root = Path(project_root)
    spf = agent.get("system_prompt_file") or ""
    if spf:
        return (root / spf).parent
    if cwd_abs:
        return Path(cwd_abs)
    return root / agent["id"]


def _iso(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _file_hash(project_root: str, rel_path: Optional[str]) -> Optional[str]:
    """sha256 of a child's progress.md content, so the rollup can detect a real
    content change (not just an mtime touch). Short prefix is enough to compare."""
    if not rel_path:
        return None
    p = Path(project_root) / rel_path
    try:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return "sha256:" + h[:16]
    except OSError:
        return None


# A child that was active much more recently than it last wrote its memory file
# has been working without persisting — the exact "active 6h ago, memory 9 days
# stale" failure the rollup is meant to surface. Threshold: 6 hours.
_STALE_MEMORY_GAP_S = 6 * 3600

# status (raw session state) → coarse job status for the parent rollup. We only
# emit what we can actually observe; we do NOT fabricate "done"/"blocked".
_JOB_STATUS = {"running": "in_progress", "ok": "idle", "error": "failed", "idle": "idle"}


def _write_children_rollups(project_root: str, project: dict, stats: dict) -> list[str]:
    """For every agent that HAS children, write a read-only, AUTO-DERIVED rollup
    of its children's job status to `<parent>/state/children_status.json`.

    This is a *projection* of the per-agent stats we already computed — never a
    hand-written second memory. The parent must never edit it. Writes are atomic
    and skipped when the meaningful payload is unchanged (only `generated_at`
    would differ), so polling `/stats` on every graph refresh does not churn git.
    """
    agents = project["agents"]
    agent_by_id = {a["id"]: a for a in agents}
    written: list[str] = []
    now = time.time()
    for parent in agents:
        pid = parent["id"]
        child_ids = [a["id"] for a in agents if pid in (a.get("parents") or [])]
        if not child_ids:
            continue
        children: dict = {}
        for cid in child_ids:
            st = stats.get(cid) or {}
            mem = st.get("memory") or {}
            mtime = mem.get("mtime")
            last_act = st.get("updated_at")
            stale = bool(last_act and mtime and (last_act - mtime) > _STALE_MEMORY_GAP_S)
            cdir = _agent_dir(project_root, agent_by_id[cid],
                              projects.resolve_cwd(project_root, agent_by_id[cid].get("cwd", "."))) \
                if cid in agent_by_id else None
            children[cid] = {
                "status": _JOB_STATUS.get(st.get("status"), st.get("status") or "idle"),
                "context_pct": st.get("context_pct"),
                "context_tokens": st.get("context_tokens"),
                "message_count": st.get("message_count"),
                "last_activity": last_act,
                "last_activity_iso": _iso(last_act),
                "memory_mtime": mtime,
                "memory_updated_iso": _iso(mtime),
                "memory_headline": mem.get("headline") or None,
                "memory_hash": _file_hash(project_root, mem.get("path")),
                "stale_memory": stale,
                # slim-overview routing fields (spec §3 / §6.3) — let BOSS route off
                # the rollup without opening each child's full manifest.
                "manifest_version": _read_manifest_version(cdir / "outputs" / "manifest.md") if cdir else None,
                "overview_path": f"../{cid}/overview.md",
                "body_incomplete": _read_overview_flag(cdir) if cdir else None,
            }
        digest = hashlib.sha256(
            json.dumps(children, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        state_dir = _agent_dir(project_root, parent, projects.resolve_cwd(project_root, parent.get("cwd", "."))) / "state"
        out = state_dir / "children_status.json"
        # Skip rewrite if the substantive payload is identical to what is on disk.
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            if prev.get("digest") == digest:
                continue
        except (OSError, ValueError):
            pass
        body = {
            "generated_at": _iso(now),
            "generated_by": "agentui control-plane (DERIVED, read-only — do NOT hand-edit)",
            "parent": pid,
            "digest": digest,
            "children": children,
        }
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, out)
            try:
                written.append(str(out.resolve().relative_to(Path(project_root).resolve())))
            except ValueError:
                written.append(str(out))
        except OSError:
            pass
    return written


@app.get("/api/projects/{slug}/stats")
def api_project_stats(slug: str):
    """Per-agent runtime stats for the expandable graph panels: status, effective
    model/effort, last activity, message count, ESTIMATED context-window usage,
    and persistent-memory freshness. Cheap enough to call on every graph refresh.
    """
    project = projects.get_project(slug)
    if not project:
        raise HTTPException(404, "project not found")
    root = project["root"]
    overrides = db.list_agent_overrides(slug)
    stats: dict = {}
    for a in project["agents"]:
        aid = a["id"]
        status = db.get_last_status(slug, aid) or "idle"
        sessions = db.list_sessions(slug, aid)
        sess = sessions[0] if sessions else None
        msg_count = 0
        est_chars = 0
        updated_at = None
        has_session = False
        usage = None
        if sess:
            updated_at = sess.get("updated_at")
            has_session = bool(sess.get("claude_session_id"))
            msgs = db.get_messages(sess["id"])
            msg_count = len(msgs)
            est_chars = sum(len(m.get("content") or "") for m in msgs)
            if sess.get("usage"):
                try:
                    usage = json.loads(sess["usage"])
                except (ValueError, TypeError):
                    usage = None

        model_kind = a.get("model", "claude")
        ov = overrides.get(aid) or {}
        if model_kind == "grok":
            eff_model = ov.get("grok_model") or a.get("grok_model") or "grok-build"
        elif model_kind == "deepseek":
            eff_model = ov.get("deepseek_model") or a.get("deepseek_model") or "deepseek-v4-flash"
        elif model_kind == "glm":
            eff_model = ov.get("glm_model") or a.get("glm_model") or "glm-4.6"
        else:
            eff_model = ov.get("claude_model") or a.get("claude_model") or "claude-sonnet-4-6"
        effort = ov.get("effort") if (ov and "effort" in ov) else a.get("effort")

        window = _context_window_for(model_kind, eff_model)
        # Prefer the CLI's real token usage from the last turn. Context-window
        # occupancy = every input bucket (fresh + cache create + cache read) +
        # output. Fall back to a chars/4 estimate only when no usage is recorded
        # yet (e.g. a session that has never completed a turn, or a grok node).
        if usage:
            ctx_tokens = _usage_ctx_tokens(usage, window)
            token_source = "exact"
        else:
            sys_chars = 0
            try:
                sp = projects.resolve_system_prompt(root, a.get("system_prompt_file", ""))
                sys_chars = len(sp or "")
            except Exception:
                pass
            ctx_tokens = (est_chars + sys_chars) // 4
            token_source = "estimate"
        pct = round(min(100.0, ctx_tokens / window * 100.0), 1) if window else 0.0

        cwd_abs = projects.resolve_cwd(root, a.get("cwd", "."))
        stats[aid] = {
            "status": status,
            "model_kind": model_kind,
            "model": eff_model,
            "effort": effort,
            "updated_at": updated_at,
            "message_count": msg_count,
            "context_tokens": ctx_tokens,
            "token_source": token_source,
            "context_window": window,
            "context_pct": pct,
            "has_session": has_session,
            "num_sessions": len(sessions),
            "memory": _memory_info(root, a, cwd_abs),
        }
    rollups = _write_children_rollups(root, project, stats)
    return {"stats": stats, "rollups_written": rollups}


@app.post("/api/projects/{slug}/rollup")
def api_project_rollup(slug: str):
    """Force-regenerate every parent's `state/children_status.json` from the
    current per-agent stats. Same derivation as the `/stats` side-effect, exposed
    standalone so a parent agent (or the UI) can refresh the rollup on demand."""
    data = api_project_stats(slug)
    return {"rollups_written": data.get("rollups_written", [])}


class AgentSettings(BaseModel):
    claude_model: Optional[str] = None
    grok_model: Optional[str] = None
    deepseek_model: Optional[str] = None
    glm_model: Optional[str] = None
    effort: Optional[str] = None


_AGENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class NewAgent(BaseModel):
    id: str
    role: Optional[str] = ""
    model: str = "claude"
    claude_model: Optional[str] = None
    grok_model: Optional[str] = None
    deepseek_model: Optional[str] = None
    glm_model: Optional[str] = None
    effort: Optional[str] = None
    system_prompt_file: Optional[str] = ""
    cwd: Optional[str] = "."
    parents: list = []
    custom_files: Optional[list] = None  # [{path, content}] from parent-generated preview


def _validate_new_agent(slug: str, body: "NewAgent") -> dict:
    project = projects.get_project(slug)
    if not project:
        raise HTTPException(404, "project not found")
    if not _AGENT_ID_RE.match(body.id):
        raise HTTPException(400, "id must be uppercase letters/digits/underscore starting with a letter")
    if body.model not in ("claude", "grok", "deepseek", "glm"):
        raise HTTPException(400, "model must be 'claude', 'grok', 'deepseek', or 'glm'")
    existing = {a["id"] for a in project["agents"]}
    if body.id in existing:
        raise HTTPException(409, f"agent id already exists: {body.id}")
    for p in body.parents:
        if p not in existing:
            raise HTTPException(400, f"parent agent not found: {p}")
    if body.id in body.parents:
        raise HTTPException(400, "agent cannot be its own parent")
    return project


@app.post("/api/projects/{slug}/agents/preview")
def api_preview_agent(slug: str, body: NewAgent):
    _validate_new_agent(slug, body)
    preview = projects.preview_agent(slug, body.model_dump())
    if "error" in preview:
        raise HTTPException(404, preview["error"])
    return preview


_FILE_BLOCK_RE = re.compile(
    r'<file\s+path="([^"]+)"\s*>([\s\S]*?)</file>',
    re.IGNORECASE,
)


def _bootstrap_prompt(slug: str, body: "NewAgent", parent_id: str) -> str:
    proj = projects.get_project(slug)
    others = [a["id"] for a in proj["agents"] if a["id"] != parent_id]
    return (
        "[CONTROL-PLANE BOOTSTRAP REQUEST — not a normal user task; do not dispatch]\n\n"
        "A new child agent is being added under your orchestration. Generate the bootstrap "
        "files for it based on your knowledge of this project — actual paths, conventions, "
        "downstream consumers. Be specific, not generic.\n\n"
        "## New agent\n"
        f"- ID: `{body.id}`\n"
        f"- Adapter: `{body.model}`\n"
        f"- Role (user description): {body.role or '(unspecified — infer from ID)'}\n"
        f"- Parents in graph: {', '.join(body.parents)}\n\n"
        "## Project\n"
        f"- Name: {proj['name']}\n"
        f"- Other agents: {', '.join(others) or 'none'}\n\n"
        "## Output format — STRICT\n"
        "⚠️ DO NOT use Write, Edit, Bash, or any file-system tools. Do NOT create files or folders on disk. "
        "The control plane parses your TEXT output and creates the files — your only job is to write content into the tags below.\n\n"
        "Emit ONLY the file blocks below, in this exact order, with NO prose before, between, or after. "
        "Each block uses the verbatim envelope:\n\n"
        f'<file path="{body.id}/RELATIVE_PATH">\n'
        "...content...\n"
        "</file>\n\n"
        "Required files (5):\n"
        f"1. `{body.id}/AGENT.md`\n"
        f"2. `{body.id}/inputs/manifest.md`\n"
        f"3. `{body.id}/outputs/manifest.md`\n"
        f"4. `{body.id}/state/progress.md`\n"
        f"5. `{body.id}/context/code_map.md`\n\n"
        "## Content guidelines\n"
        "- `AGENT.md` is the system prompt. Under 80 lines. Required sections in order: "
        "  (a) **NOTICE** (refuse to act on missing info, override all other rules), "
        "  (b) **Role**, "
        "  (c) **Required reads** in strict order — point at REAL files (always include the "
        "  five shared files `../shared/{research_integrity,tool_conventions,handoff_schema,"
        "glossary,scope_decisions}.md`, then this agent's `./inputs/manifest.md`, "
        "`./context/code_map.md`, and `./state/progress.md` LAST), "
        "  (d) **Scope IN/OUT**, "
        "  (e) **Pre-flight checklist**, "
        "  (f) **Deliverables** — list the SPECIFIC files this agent must keep current. "
        "  At minimum this MUST include: prepend a dated entry to `./state/progress.md` "
        "  on every meaningful turn, and bump `./outputs/manifest.md` (with a Bump-log entry) "
        "  on every produced/modified artifact. Also list any domain-specific deliverables "
        "  with REAL paths (e.g. `paper/latex/asce2027_paper.tex`, `results/<run_id>/predictions.parquet`, "
        "  `documentation/METHODOLOGY.md` §X). "
        "  (g) **Output contract** including the 3-statement-type rule (fact / literature claim / design decision), "
        "  (h) **Escalation triggers**. "
        "NO routing tables, NO worker lists, NO 'dispatch this' patterns — the control plane "
        "injects current children at runtime.\n"
        "- `inputs/manifest.md` — YAML frontmatter (`schema_version: 1`, `agent`, "
        "`direction: inputs`, `updated`) + a table with columns: Source agent | Synced "
        "version | Artifact / path | Used for which section. One row per upstream artifact this "
        "agent depends on. If you know specific upstream artifacts this agent will consume "
        "(based on the parent's domain knowledge), fill them in concretely; else `(TBD)`.\n"
        "- `outputs/manifest.md` — YAML frontmatter (`direction: outputs`) + sections in "
        "order: **Version** (start at `0.1.0`), **Bump rule** (major / minor / patch — see "
        "VLM-style: schema change major, new artifact same schema minor, metadata patch), "
        "**Bump log** with the bootstrap entry `0.0.0 → 0.1.0 (date): manifest bootstrapped via "
        "AgentUI`, **Artifacts** table (Artifact path | Consumer agents | Current version | "
        "Updated | Checksum/note). The agent will keep this current per Deliverables.\n"
        "- `state/progress.md` — `# <ID> Progress log (newest on top)` + a Convention block "
        "instructing to PREPEND a dated section every meaningful turn (format: `## YYYY-MM-DD — headline` "
        "followed by 2–5 bullets with evidence). One initial entry dated today recording the bootstrap.\n"
        "- `context/code_map.md` — sections: **Owned** (this agent's files + domain artifacts "
        "with REAL paths), **Read-only references** (`../shared/`, upstream agents), "
        "**Out of scope** (other agents' folders).\n\n"
        "Reference actual folders that exist in this project (e.g. `paper/`, `documentation/`, "
        "`data/`, `analyse/`, `layout/`, etc.) — do not invent fake paths. If you don't know "
        "which artifact the new agent should produce, mark it `(TBD)` rather than guessing.\n\n"
        "**The self-update mechanism is the most important part of this bootstrap.** "
        "The newly-created agent must know — from its own AGENT.md — that it is responsible "
        "for keeping `state/progress.md` and `outputs/manifest.md` current. This is what "
        "makes the existing project agents (VLM, FRAMEWORK, etc.) actually log their work "
        "without external prompting. Be explicit about it.\n"
    )


def _validate_bootstrap_files(files: list, agent_id: str) -> list[str]:
    warnings: list[str] = []
    paths = {f["path"] for f in files}
    for required in [
        f"{agent_id}/AGENT.md",
        f"{agent_id}/inputs/manifest.md",
        f"{agent_id}/outputs/manifest.md",
        f"{agent_id}/state/progress.md",
        f"{agent_id}/context/code_map.md",
    ]:
        if required not in paths:
            warnings.append(f"Missing file `{required}` — the parent did not emit it. You can create it later.")
    for f in files:
        if not f["path"].startswith(f"{agent_id}/"):
            warnings.append(f"`{f['path']}` is not inside the `{agent_id}/` folder — it will be rejected at write time.")
    return warnings


@app.post("/api/projects/{slug}/agents/preview-from-parent")
async def api_preview_from_parent(slug: str, body: NewAgent):
    _validate_new_agent(slug, body)
    if not body.parents:
        raise HTTPException(400, "At least 1 parent must be selected to generate from a parent. Or use the template preview.")
    parent_id = body.parents[0]

    queue: asyncio.Queue = asyncio.Queue()
    tracker: list = []

    async def emit(evt):
        await queue.put(evt)

    bootstrap_msg = _bootstrap_prompt(slug, body, parent_id)

    async def driver():
        try:
            await _run_agent(slug, parent_id, bootstrap_msg, emit, tracker)
            if tracker:
                await asyncio.gather(*tracker, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tracker:
                if not t.done():
                    t.cancel()
            if tracker:
                await asyncio.gather(*tracker, return_exceptions=True)
            raise
        except Exception as e:
            await queue.put({"type": "error", "agent": parent_id, "message": str(e)})
        finally:
            await queue.put(None)

    async def sse():
        yield _sse({"type": "start", "parent": parent_id, "new_agent_id": body.id})
        task = asyncio.create_task(driver())
        assembled = ""
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if evt is None:
                    break
                if evt.get("type") == "delta" and evt.get("agent") == parent_id:
                    assembled += evt.get("text", "")
                yield _sse(evt)
            files = [
                {"path": m.group(1).strip(), "content": m.group(2).strip("\n")}
                for m in _FILE_BLOCK_RE.finditer(assembled)
            ]
            # de-dupe by path, keep first occurrence
            seen = set()
            unique = []
            for f in files:
                if f["path"] in seen:
                    continue
                seen.add(f["path"])
                unique.append(f)
            warnings = _validate_bootstrap_files(unique, body.id)
            root = projects._project_root_for_slug(slug)
            target_folder = str(root / body.id) if root else body.id
            yield _sse({
                "type": "bootstrap_done",
                "files": unique,
                "warnings": warnings,
                "target_folder": target_folder,
            })
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/projects/{slug}/agents")
def api_add_agent(slug: str, body: NewAgent):
    _validate_new_agent(slug, body)
    ok, msg = projects.create_agent(slug, body.model_dump())
    if not ok:
        raise HTTPException(500, msg)
    refreshed = projects.get_project(slug)
    return {"ok": True, "project": refreshed}


# ----- New-project creation (scaffold a fresh agent system) -----

class NewProject(BaseModel):
    root: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = ""
    agents: list = []  # [{id, role, model, claude_model?, grok_model?, effort?, parents?}]


@app.get("/api/fs/validate")
def api_fs_validate(path: str):
    p = Path(path).expanduser()
    return {
        "path": str(p),
        "exists": p.exists(),
        "is_dir": p.is_dir() if p.exists() else None,
        "is_project": (p / ".agentui" / "project.yaml").exists(),
        "non_empty": bool(p.is_dir() and any(p.iterdir())) if p.exists() else False,
        "parent_exists": p.parent.exists(),
    }


def _validate_project_agents(agents: list) -> None:
    ids = [a.get("id") for a in agents]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "duplicate agent id in the list")
    for a in agents:
        if not _AGENT_ID_RE.match(a.get("id") or ""):
            raise HTTPException(400, f"invalid agent id (must be UPPERCASE): {a.get('id')}")
        if a.get("model", "claude") not in ("claude", "grok", "deepseek", "glm"):
            raise HTTPException(400, f"model must be claude|grok|deepseek|glm: {a.get('id')}")
        for p in a.get("parents") or []:
            if p not in ids:
                raise HTTPException(400, f"parent '{p}' is not in the project (agent {a.get('id')})")
        if a.get("id") in (a.get("parents") or []):
            raise HTTPException(400, f"agent cannot be its own parent: {a.get('id')}")


@app.post("/api/projects/preview-create")
def api_preview_create(body: NewProject):
    _validate_project_agents(body.agents or [])
    return projects.preview_project(body.model_dump())


@app.post("/api/projects/create")
def api_create_project(body: NewProject):
    if not body.root or not body.root.strip():
        raise HTTPException(400, "missing project folder path")
    _validate_project_agents(body.agents or [])
    ok, msg, slug = projects.create_project(body.model_dump())
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "slug": slug, "projects": projects.list_projects()}


@app.post("/api/projects/{slug}/agents/{agent_id}/clear")
def api_clear_session(slug: str, agent_id: str):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    sess = db.new_session(slug, agent_id)
    return {"ok": True, "new_session_id": sess["id"]}


@app.post("/api/projects/{slug}/agents/{agent_id}/settings")
def api_set_agent_settings(slug: str, agent_id: str, body: AgentSettings):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    db.set_agent_override(slug, agent_id,
                          claude_model=body.claude_model,
                          grok_model=body.grok_model,
                          deepseek_model=body.deepseek_model,
                          glm_model=body.glm_model,
                          effort=body.effort)
    return {"ok": True}


class AdapterSwitch(BaseModel):
    adapter: str   # claude | grok | deepseek | glm
    model: Optional[str] = None  # specific model id for the new adapter (optional)


# Default model per adapter when the caller omits `model`.
_ADAPTER_DEFAULT_MODEL = {
    "claude": "claude-sonnet-4-6",
    "grok": "grok-build",
    "deepseek": "deepseek-v4-flash",
    "glm": "glm-4.6",
}


@app.post("/api/projects/{slug}/agents/{agent_id}/adapter")
def api_set_agent_adapter(slug: str, agent_id: str, body: AdapterSwitch):
    """Switch an agent's adapter (claude|grok|deepseek|glm) at runtime — stored
    as an override that wins over project.yaml, so no file edit / restart needed.
    Lets a user move a node e.g. opus(claude) -> glm-5.2 from the chat UI."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    if body.adapter not in ("claude", "grok", "deepseek", "glm"):
        raise HTTPException(400, "adapter must be 'claude', 'grok', 'deepseek', or 'glm'")
    db.set_agent_adapter(slug, agent_id, body.adapter, body.model or _ADAPTER_DEFAULT_MODEL[body.adapter])
    return {"ok": True}


@app.get("/api/skills")
def api_skills():
    """List installed global Agent Skills (~/.claude/skills/*/SKILL.md) with name +
    description parsed from each SKILL.md YAML frontmatter. Read-only reference for
    the UI's Skills panel — the user decides when to use them."""
    skills_dir = Path.home() / ".claude" / "skills"
    out = []
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            sk = d / "SKILL.md"
            if not d.is_dir() or not sk.is_file():
                continue
            name, desc = d.name, ""
            try:
                text = sk.read_text(encoding="utf-8", errors="replace")
                if text.lstrip().startswith("---"):
                    fm = text.split("---", 2)[1]
                    cur = None
                    for line in fm.splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip('"\'')
                            cur = None
                        elif line.startswith("description:"):
                            val = line.split(":", 1)[1].strip()
                            # YAML block scalar (">", ">-", "|", "|-", "|+") → body is
                            # the following indented lines; the indicator is not text.
                            if not val or val[0] in ">|":
                                desc = ""
                            else:
                                desc = val.strip('"\'')
                            cur = "description"
                        elif cur == "description" and line.startswith(("  ", "\t")):
                            desc = (desc + " " + line.strip()).strip()  # folded continuation
                        elif line and not line[0].isspace():
                            cur = None
            except Exception:
                pass
            out.append({"name": name, "description": desc, "dir": d.name})
    return {"skills": out}


@app.get("/api/workspace/info")
def api_workspace_info():
    root = projects.get_workspace_root()
    proj_roots = {str(Path(p["root"]).resolve()) for p in projects.list_projects()}
    return {
        "workspace_root": str(root) if root else None,
        "project_roots": sorted(proj_roots),
    }


_MAX_FILE_BYTES = 5_000_000


def _resolve_workspace_file(path: str):
    root = projects.get_workspace_root()
    if not root:
        raise HTTPException(404, "no workspace root configured")
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "path escapes workspace root")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return target


@app.get("/api/workspace/raw")
def api_workspace_raw(path: str):
    """Serve raw file bytes (for PDF, images, etc) with proper Content-Type."""
    import mimetypes
    target = _resolve_workspace_file(path)
    mime, _ = mimetypes.guess_type(str(target))
    if not mime:
        mime = "application/octet-stream"
    return FileResponse(target, media_type=mime)


@app.get("/api/workspace/file")
def api_workspace_file(path: str):
    root = projects.get_workspace_root()
    if not root:
        raise HTTPException(404, "no workspace root configured")
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "path escapes workspace root")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    try:
        size = target.stat().st_size
    except OSError as e:
        raise HTTPException(500, f"stat failed: {e}")
    if size > _MAX_FILE_BYTES:
        raise HTTPException(413, f"file too large: {size} bytes (max {_MAX_FILE_BYTES})")
    try:
        content = target.read_text(encoding="utf-8")
        is_binary = False
    except UnicodeDecodeError:
        content = "(binary file — preview not available)"
        is_binary = True
    return {
        "rel_path": path,
        "abs_path": str(target),
        "content": content,
        "size": size,
        "is_binary": is_binary,
    }


@app.get("/api/workspace/tree")
def api_workspace_tree(path: str = ""):
    root = projects.get_workspace_root()
    if not root:
        raise HTTPException(404, "no workspace root configured")
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "path escapes workspace root")
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, "directory not found")

    project_roots = {Path(p["root"]).resolve() for p in projects.list_projects()}

    items = []
    try:
        children = list(target.iterdir())
    except PermissionError:
        return {"items": [], "rel_path": path, "abs_path": str(target)}
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children:
        if child.name in TREE_EXCLUDE:
            continue
        is_dir = child.is_dir()
        try:
            resolved = child.resolve()
        except OSError:
            resolved = child
        items.append({
            "name": child.name,
            "type": "folder" if is_dir else "file",
            "rel_path": str(child.relative_to(root)),
            "abs_path": str(child),
            "is_project": is_dir and resolved in project_roots,
        })
    return {"items": items, "rel_path": path, "abs_path": str(target)}


@app.get("/api/projects/{slug}/tree")
def api_tree(slug: str, path: str = ""):
    project = projects.get_project(slug)
    if not project:
        raise HTTPException(404, "project not found")
    root = Path(project["root"]).resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "path escapes project root")
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, "directory not found")

    items = []
    try:
        children = list(target.iterdir())
    except PermissionError:
        return {"items": [], "rel_path": path, "abs_path": str(target)}
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children:
        if child.name in TREE_EXCLUDE:
            continue
        is_dir = child.is_dir()
        items.append({
            "name": child.name,
            "type": "folder" if is_dir else "file",
            "rel_path": str(child.relative_to(root)),
            "abs_path": str(child),
        })
    return {"items": items, "rel_path": path, "abs_path": str(target)}


@app.get("/api/projects/{slug}/agents/{agent_id}/session")
def api_session(slug: str, agent_id: str):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    sess = db.get_or_create_active_session(slug, agent_id)
    return {
        "session": sess,
        "messages": db.get_messages(sess["id"]),
    }


class ChatBody(BaseModel):
    message: str
    # grok-only one-shot options consumed by the next turn
    best_of_n: Optional[int] = None
    check_loop: Optional[bool] = None
    memory_mode: Optional[str] = None  # "on" | "off" | None


def _get_children(project_data: dict, agent_id: str) -> list[str]:
    return [a["id"] for a in project_data["agents"] if agent_id in (a.get("parents") or [])]


def _dispatch_instructions(children: list[str]) -> str:
    return (
        "\n\n## Dispatch protocol — MANDATORY (overrides any invoke pattern described in this file's main body)\n"
        "**Current direct workers, from the live project graph** (may differ from any static list in the main body of this file; "
        f"workers can be added or removed via the control plane at any time): {', '.join(children)}.\n\n"
        "**You MUST dispatch to a worker for ANY task involving reading their scope (code, data, files, configs, manifests), "
        "answering questions about their domain, running their pipelines, or producing artifacts. "
        "Reading source files yourself is a violation of your role.** Self-justifying excuses NOT accepted: "
        '"simpler", "faster", "just a quick read", "information query" — these mean DISPATCH anyway.\n\n'
        "Format (verbatim, one tag per worker, exact ID):\n\n"
        '<dispatch agent="WORKER_ID">Concise task statement.</dispatch>\n\n'
        "## How to write the task inside the tag — read carefully\n"
        "The system automatically injects the worker's role context (their AGENT.md is appended as system prompt) "
        "AND resumes their prior session if alive. The worker therefore ALREADY knows:\n"
        "  • who they are and their scope,\n"
        "  • the shared/ files they must read on pre-flight,\n"
        "  • their tool conventions, manifest schema, integrity rules,\n"
        "  • everything from their prior turns in this session.\n\n"
        "Do NOT repeat any of these in the dispatch task. No \"You are agent X\", no reading lists, "
        "no path references to shared/*, no pre-flight reminders. Those are wasted tokens and the worker already has them.\n\n"
        "The dispatch task should be ONE concise statement of what to do this turn, often 1–3 sentences. "
        "If the worker's session is fresh, you may include the agent folder path "
        "(e.g. \".claude/AGENT/<NAME>/\") once as the only orientation hint. Nothing more.\n\n"
        "Examples of correct dispatch task body:\n"
        "  • \"List all references cited in the ASCE 2027 paper, grouped by hazard. Read documentation/REFERENCES.md, paper/REFERENCES.md, paper/latex/references.bib.\"\n"
        "  • \"Verify what BOSS just said about the vulnerability formula 0.40/0.30/0.30 in vulnerability/energy_vulnerability_analyzer.py. Report line evidence.\"\n"
        "  • \"Continue: add more bullets on Cascadia exposure to the report you just produced.\"\n\n"
        "Examples of INCORRECT (do not produce):\n"
        "  • \"You are the DOCS agent. Read in mandatory order: 1. shared/research_integrity.md 2. ...\"\n"
        "  • Reading lists, role briefings, pre-flight blocks.\n\n"
        "The system parses tags in real time and runs the worker; the user verifies your orchestration by watching "
        "the graph light up. Narrating \"I will dispatch\" without emitting the tag is a lie — user sees nothing happen.\n\n"
        "## How you receive worker results\n"
        "On the turn AFTER a dispatch (either an automatic CONTROL-PLANE CONTINUATION or the user's next message), "
        "the prompt will begin with `<dispatch_result from=\"WORKER_ID\">...</dispatch_result>` blocks containing "
        "the full output of each worker you dispatched. Reason over that real data. **Never claim you are still "
        "waiting for results when these blocks are present.** If a result is incomplete or marked status=error/cancelled, "
        "decide whether to retry, escalate, or report to the user.\n"
    )


def _read_capped(p: Path, max_chars: int) -> str:
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    txt = txt.strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rstrip() + "\n[… truncated — read the full file if needed]"
    return txt


def _progress_excerpt(p: Path, max_sections: int = 2, max_chars: int = 2600) -> str:
    """First N dated `## ` sections of progress.md (newest first by convention)."""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    out: list[str] = []
    sections = 0
    for ln in lines:
        if ln.startswith("## "):
            sections += 1
            if sections > max_sections:
                break
        out.append(ln)
        if sum(len(x) + 1 for x in out) > max_chars:
            out.append("[… truncated]")
            break
    return "\n".join(out).strip()


def _session_preamble(project_root: str, agent: dict, cwd_abs: str) -> str:
    """Deterministic cold-start recap injected whenever the CLI session can NOT
    be resumed (brand-new session, post-/clear, or torn last turn). Built ONLY
    from the agent's persistent files — same sources every time — so every
    wake-up starts from the same structured state instead of amnesia.

    Order of precedence elsewhere: a /compact seed (richer, conversation-aware)
    replaces this; the preamble is the fallback floor.
    """
    adir = _agent_dir(project_root, agent, cwd_abs)
    parts: list[str] = []

    prog = adir / "state" / "progress.md"
    if prog.is_file():
        ex = _progress_excerpt(prog)
        if ex:
            parts.append(f"### Your memory (`state/progress.md`, newest first)\n{ex}")

    roll = adir / "state" / "children_status.json"
    if roll.is_file():
        try:
            data = json.loads(roll.read_text(encoding="utf-8"))
            rows = []
            for cid, c in (data.get("children") or {}).items():
                stale = " ⚠ STALE-MEMORY" if c.get("stale_memory") else ""
                rows.append(
                    f"- {cid}: {c.get('status')}, ctx {c.get('context_pct')}%, "
                    f"active {c.get('last_activity_iso') or '—'}, "
                    f"memory {c.get('memory_updated_iso') or '—'}{stale}"
                    + (f" — {c['memory_headline']}" if c.get("memory_headline") else "")
                )
            if rows:
                parts.append("### Status of your workers (derived, read-only)\n" + "\n".join(rows))
        except (OSError, ValueError):
            pass

    # Slim self-overview (preferred): the agent's holistic "where am I" snapshot.
    # Falls back to the input-contract head when absent/empty or body still a
    # placeholder (body_incomplete) — the migration safety net, so behaviour is
    # unchanged for any agent that has no overview yet.
    ov = adir / "overview.md"
    ov_text = _read_capped(ov, 1600) if ov.is_file() else ""
    ov_usable = bool(ov_text) and "body_incomplete: true" not in ov_text.lower()
    if ov_text:
        parts.append(f"### Overview (slim self-snapshot, `overview.md`)\n{ov_text}")

    man = adir / "inputs" / "manifest.md"
    if man.is_file():
        # When a usable overview is present, the upstream contract is secondary —
        # cap it tighter to avoid double-loading; full head otherwise (fallback floor).
        ex = _read_capped(man, 600 if ov_usable else 1200)
        if ex:
            parts.append(f"### Input contract (`inputs/manifest.md`)\n{ex}")

    if not parts:
        return ""
    return (
        "[CONTROL-PLANE COLD-START] New CLI session (the previous session could not be resumed). "
        "Below is your persistent state — read it before acting:\n\n"
        + "\n\n".join(parts)
    )


def _schedule_public(t: dict) -> dict:
    """Trim a scheduled_tasks row to what the UI needs."""
    return {
        "id": t["id"], "agent_id": t["agent_id"], "kind": t["kind"],
        "prompt": t["prompt"], "interval_seconds": t["interval_seconds"],
        "until_goal": t.get("until_goal"), "next_run_at": t["next_run_at"],
        "last_run_at": t.get("last_run_at"), "last_status": t.get("last_status"),
        "runs_done": t["runs_done"], "max_runs": t.get("max_runs"),
        "active": bool(t["active"]), "origin": t["origin"],
    }


def _build_schedule(slug: str, agent_id: str, attrs: dict, body: str,
                    origin: str) -> tuple[Optional[dict], Optional[str]]:
    """Create a scheduled_tasks row from <schedule>-style attrs (every/in/max/until).
    Returns (row, None) on success or (None, error_message). No streaming side
    effects — shared by the live tag parser and the REST endpoint."""
    body = (body or "").strip()
    if not body:
        return None, "schedule: empty task body"
    if not _scheduler_enabled():
        return None, "scheduler is globally disabled"
    until_goal = (attrs.get("until") or "").strip() or None
    every = _parse_duration(attrs.get("every", ""))
    delay = _parse_duration(attrs.get("in", ""))
    max_attr = attrs.get("max")
    try:
        max_runs = int(max_attr) if max_attr not in (None, "") else None
    except (ValueError, TypeError):
        max_runs = None

    now = time.time()
    if every is not None or until_goal:
        interval = max(every if every is not None else _SCHED_INTERVAL_FLOOR_S,
                       _SCHED_INTERVAL_FLOOR_S)
        kind = "until" if until_goal else "interval"
        if kind == "until":
            max_runs = max_runs or _SCHED_UNTIL_CEILING
        t = db.create_scheduled_task(
            slug, agent_id, body, kind, interval, now + interval,
            until_goal=until_goal, max_runs=max_runs, origin=origin)
    elif delay is not None:
        t = db.create_scheduled_task(
            slug, agent_id, body, "once", None, now + delay,
            max_runs=1, origin=origin)
    else:
        return None, "schedule: need every=, in=, or until="
    return t, None


async def _register_schedule(slug: str, agent_id: str, attrs: dict, body: str,
                             origin: str, emit) -> Optional[dict]:
    """Tag-path wrapper: build the row, then emit schedule_created / error."""
    t, err = _build_schedule(slug, agent_id, attrs, body, origin)
    if err:
        await emit({"type": "error", "agent": agent_id, "message": err + " — ignored"})
        return None
    await emit({"type": "schedule_created", "agent": agent_id,
                "schedule": _schedule_public(t)})
    return t


async def _run_agent(
    slug: str,
    agent_id: str,
    message: str,
    emit,
    tracker: list,
    chain: tuple = (),
    grok_options: Optional[dict] = None,
    retry_count: int = 0,
) -> str:
    """Run a single agent chat turn. Emit events via callback.

    Recursive: if agent is an orchestrator (has children), parse dispatch tags
    from streamed text and fire worker dispatches as background tasks.
    `chain` tracks ancestors to prevent infinite loops.
    """
    turn_start = time.time()   # for the schedule_stop same-turn-echo guard
    found = projects.get_agent(slug, agent_id)
    if not found:
        await emit({"type": "error", "agent": agent_id, "message": f"agent {agent_id} not found"})
        return ""
    project, agent = found

    sess = db.get_or_create_active_session(slug, agent_id)
    # Record the ORIGINAL user/synth message in the messages table (for UI
    # fidelity). The string we actually send to the CLI may be enriched below
    # with worker results from prior dispatches.
    db.add_message(sess["id"], "user", message, meta={"chain": list(chain)} if chain else None)
    db.update_session_status(sess["id"], "running")
    await emit({"type": "agent_status", "agent": agent_id, "status": "running"})

    # ----- LEDGER ENRICHMENT -----
    # If this agent dispatched workers in a prior turn and the results have not
    # yet been consumed, prepend them to the message that goes to the CLI so the
    # model can reason over the actual data. The original `message` is still
    # what the user sees in the chat history; only the CLI prompt is enriched.
    pending_results = db.get_unconsumed_results(slug, agent_id)
    consumed_ids: list = []
    if pending_results:
        ledger_block = _format_results_as_context(pending_results)
        message = ledger_block + "\n\n" + message
        consumed_ids = [int(r["id"]) for r in pending_results]
    # ----- END LEDGER ENRICHMENT -----

    # ----- SEED ENRICHMENT (compact recap) -----
    # A fresh session created by /compact carries a one-time recap of the prior
    # (compacted) session. Prepend it so the model continues seamlessly with a
    # small context, then clear it after a clean turn.
    seed_text = sess.get("seed")
    if seed_text:
        message = (
            "[COMPACTED CONTEXT] Recap of the previous session (compacted to reduce context). "
            "Use it as the basis to continue seamlessly:\n\n"
            + seed_text + "\n\n---\n\n" + message
        )
    # ----- END SEED ENRICHMENT -----

    # ----- VERSION-PIN DRIFT (spec §6.5) -----
    # Stale input pins → prepend a high-priority control-plane warning so the agent
    # re-syncs BEFORE acting on stale upstream. A warn+instruct (not a hard return)
    # so the agent can run sync.sh itself this same turn — a hard block would
    # deadlock (only the agent's own turn can fix the pin). It MUST NOT consume the
    # drifted artifact until re-synced.
    drift = _check_version_pins(project["root"], project, agent_id)
    if drift:
        message = (
            "[CONTROL-PLANE DRIFT] Your input pins are STALE vs the producer's current "
            "outputs/manifest.md:\n- " + "\n- ".join(drift) + "\n"
            "Run `sync.sh " + agent_id + "` and re-read inputs BEFORE any work this turn; "
            "do NOT consume the drifted artifact until the pin matches.\n\n" + message
        )
    # ----- END VERSION-PIN DRIFT -----

    # ----- OPEN-DISSENT GATE (spec §15.2 forcing function) -----
    # An orchestrator (agent with children) cannot silently proceed while a worker's
    # dissent is open — prepend it so the model MUST ratify/overrule. The flag stays
    # in db until a [DISSENT_RESOLVE], so this re-surfaces every turn until addressed.
    if _get_children(project, agent_id):
        _dissent_warn = _open_dissent_warning(slug)
        if _dissent_warn:
            message = _dissent_warn + message
    # ----- END OPEN-DISSENT GATE -----

    # ----- VERIFY-DELTA HINT (spec §16.1 incremental verification) -----
    # An auditor that has declared `verified: PROD@ver` before → control-plane
    # computes the deterministic skip-set (producers whose version is unchanged) and
    # injects it, so the auditor re-verifies only the delta. No-op for non-auditors.
    _vd = _verify_delta(slug, project, agent_id)
    if _vd:
        _vd_hint = _verify_delta_hint(_vd)
        if _vd_hint:
            message = _vd_hint + message
    # ----- END VERIFY-DELTA HINT -----

    system_prompt = projects.resolve_system_prompt(project["root"], agent.get("system_prompt_file", ""))
    cwd = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    children = _get_children(project, agent_id)

    if children:
        system_prompt = (system_prompt or "") + _dispatch_instructions(children)

    if _should_include_schedule_instructions(slug, agent_id, message):
        system_prompt = (system_prompt or "") + _SCHEDULE_INSTRUCTIONS

    _adir = _agent_dir(project["root"], agent, cwd)

    override = db.get_agent_override(slug, agent_id) or {}
    model = override.get("model") or agent.get("model", "claude")
    stream_fn = get_stream(model)
    effort = override.get("effort") if "effort" in override else agent.get("effort")

    # Resume guard. Only continue a prior CLI session if its last turn ended
    # cleanly. Sessions left in "running" (orphan from a uvicorn restart, swept
    # to "cancelled" by the startup reaper), "cancelled" (user hit stop or
    # browser disconnected mid-stream — claude server state may be torn), or
    # "error" cannot be safely resumed: claude --resume into a half-finished
    # state often returns empty or hangs silently. Better to start fresh.
    resume_sid = sess.get("claude_session_id") if sess.get("last_status") == "ok" else None

    # ----- COLD-START PREAMBLE -----
    # No resumable CLI session → the model wakes up with amnesia (only AGENT.md).
    # Inject the deterministic recap built from its persistent files so every
    # wake-up starts from the same state. A /compact seed (prepended above) is
    # richer and conversation-aware, so it takes precedence over this floor.
    if resume_sid is None and not seed_text:
        preamble = _session_preamble(project["root"], agent, cwd)
        if preamble:
            message = preamble + "\n\n---\n\n" + message
    # ----- END COLD-START PREAMBLE -----

    if model == "claude":
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("claude_model") or agent.get("claude_model") or "claude-sonnet-4-6",
            effort=effort,
            resume_session_id=resume_sid,
        )
    elif model == "grok":
        gopts = grok_options or {}
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("grok_model") or agent.get("grok_model") or "grok-build",
            effort=effort,
            resume_session_id=resume_sid,
            best_of_n=gopts.get("best_of_n"),
            check_loop=bool(gopts.get("check_loop")),
            memory_mode=gopts.get("memory_mode"),
        )
    elif model == "deepseek":
        # Same harness as claude (driven through the proxy); --resume works, so
        # we keep the normal resume_sid path. Adapter injects the proxy env.
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("deepseek_model") or agent.get("deepseek_model") or "deepseek-v4-flash",
            effort=effort,
            resume_session_id=resume_sid,
        )
    elif model == "glm":
        # Same harness as claude/deepseek; adapter injects GLM's Anthropic
        # endpoint env per-subprocess. --resume works normally.
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("glm_model") or agent.get("glm_model") or "glm-4.6",
            effort=effort,
            resume_session_id=resume_sid,
        )
    else:
        agen = stream_fn(message=message, system_prompt=system_prompt, cwd=cwd)

    assembled: list[str] = []
    buf = ""
    dispatched: set = set()
    sched_seen: set = set()
    sched_stop_seen: set = set()
    final_status = "ok"
    last_usage: dict = {}          # real token usage of this turn (for context_log)
    new_chain = chain + (agent_id,)

    try:
        async for evt in agen:
            etype = evt.get("type")
            if etype == "delta":
                text = evt["text"]
                assembled.append(text)
                buf += text
                # find newly completed dispatch tags
                for m in DISPATCH_RE.finditer(buf):
                    key = (m.start(), m.group(1))
                    if key in dispatched:
                        continue
                    dispatched.add(key)
                    target = m.group(1).strip()
                    task = m.group(2).strip()
                    if target in new_chain:
                        await emit({
                            "type": "dispatch_rejected",
                            "source": agent_id,
                            "target": target,
                            "reason": "would create dispatch loop",
                        })
                        continue
                    if target not in children:
                        await emit({
                            "type": "dispatch_rejected",
                            "source": agent_id,
                            "target": target,
                            "reason": f"{target} is not a worker of {agent_id}",
                        })
                        continue
                    await emit({
                        "type": "dispatch_started",
                        "source": agent_id,
                        "target": target,
                        "task": task,
                    })
                    task_handle = asyncio.create_task(
                        _dispatched_run(slug, agent_id, target, task, emit, tracker, new_chain)
                    )
                    tracker.append(task_handle)
                # schedule tags → register recurring/deferred re-invocation
                for m in SCHEDULE_RE.finditer(buf):
                    if m.start() in sched_seen:
                        continue
                    sched_seen.add(m.start())
                    attrs = {k.lower(): v for k, v in _SCHED_ATTR_RE.findall(m.group(1) or "")}
                    await _register_schedule(slug, agent_id, attrs, m.group(2), "agent", emit)
                # schedule_stop → a goal loop self-terminates its own schedule(s)
                for m in SCHEDULE_STOP_RE.finditer(buf):
                    if m.start() in sched_stop_seen:
                        continue
                    sched_stop_seen.add(m.start())
                    sattrs = {k.lower(): v for k, v in _SCHED_ATTR_RE.findall(m.group(1) or "")}
                    reason = (sattrs.get("reason") or (m.group(2) or "")).strip()
                    # created_before=turn_start: a stop tag only ends loops that
                    # pre-date this turn, so quoting/echoing the example in the same
                    # message that creates a schedule does NOT instantly kill it.
                    stopped = db.deactivate_agent_schedules(
                        slug, agent_id, kind="until", created_before=turn_start)
                    for sid in stopped:
                        await emit({"type": "schedule_done", "agent": agent_id,
                                    "id": sid, "reason": reason or "goal reached"})
                await emit({"type": "delta", "agent": agent_id, "text": text})
            elif etype == "meta":
                data = evt.get("data") or {}
                if data.get("claude_session_id"):
                    db.set_claude_session_id(sess["id"], data["claude_session_id"])
                if data.get("usage"):
                    db.set_session_usage(sess["id"], data["usage"])
                    last_usage = data["usage"]
                await emit({"type": "meta", "agent": agent_id, "data": data})
            elif etype == "thinking":
                await emit({"type": "thinking", "agent": agent_id, "text": evt.get("text", "")})
            elif etype == "tool_use":
                # Adapter surfaced a tool call (Read/Grep/Glob/Edit/Write/Bash/
                # MCP...) with its parsed input. Forward as-is so the UI can
                # render a "files / commands accessed" bubble per agent.
                await emit({"type": "tool_use", "agent": agent_id,
                            "tool": evt.get("tool"), "input": evt.get("input") or {}})
            elif etype == "done":
                pass  # finalize below
            elif etype == "error":
                final_status = "error"
                await emit({"type": "error", "agent": agent_id, "message": evt.get("message", "")})
                break
    except asyncio.CancelledError:
        final_status = "cancelled"
        raise
    finally:
        final_text = "".join(assembled)
        if final_text:
            db.add_message(sess["id"], "assistant", final_text)
        db.update_session_status(sess["id"], final_status)
        # Mark ledger rows consumed only on a clean turn — if we errored or were
        # cancelled, leave the rows so the next attempt can still see them.
        if consumed_ids and final_status == "ok":
            db.consume_results(consumed_ids, sess["id"])
        if seed_text and final_status == "ok":
            db.clear_session_seed(sess["id"])
        await emit({"type": "agent_done", "agent": agent_id, "text": final_text, "status": final_status})

        # ----- PER-TURN CONTEXT LOG (feature C) — passive, ~0 token; toggle context_log_enabled -----
        # One row per real model turn: constructed-context size (system_prompt + the
        # fully-enriched message that was sent) + this turn's real token usage. No
        # prompt change, just measurement. resumed=False marks cold-start turns (the
        # ones that receive the overview injection) for a clean A/B.
        try:
            if db.get_setting("context_log_enabled", "1") == "1":
                _cc = len(system_prompt or "") + len(message or "")
                _u = last_usage or {}
                db.add_context_log(
                    project_slug=slug, agent_id=agent_id, status=final_status,
                    ctx_chars=_cc, est_tokens=round(_cc / 4),
                    input_tokens=_u.get("input_tokens"),
                    cache_read=_u.get("cache_read_input_tokens"),
                    cache_creation=_u.get("cache_creation_input_tokens"),
                    output_tokens=_u.get("output_tokens"),
                    resumed=bool(resume_sid),
                )
        except Exception:
            pass
        # ----- END CONTEXT LOG -----

    # ----- STRUCTURED OUTPUT: parse [RESULT]/[ESCALATE], retry once, stamp overview (§6.4) -----
    # Only on a clean turn with text. Absent blocks are fine (no retry). A single
    # corrective retry on malformed shape; never loops (retry_count guard).
    if final_status == "ok" and final_text:
        parsed = _parse_structured(final_text)
        if parsed["malformed"] and retry_count == 0:
            corrective = (
                "[CONTROL-PLANE] Your structured block was malformed: "
                + (parsed["error"] or "unknown")
                + ". Re-emit correctly per shared/overview_protocol.md (balanced tags; escalate "
                "type ∈ {DATA,BOSS_DECISION,HUMAN,SUBTASK,TOOL,BLOCKED}; [HALT]/[DISSENT] REQUIRE `evidence`)."
            )
            return await _run_agent(slug, agent_id, corrective, emit, tracker,
                                    chain, grok_options, retry_count=1)
        if parsed["result"] and parsed["result"].get("verified"):
            # Incremental-verify watermark (spec §16.1): record what the auditor
            # verified + at which producer version, so next pass can skip unchanged.
            for _prod, _ver in _parse_verified(parsed["result"]["verified"]).items():
                db.set_verify_watermark(slug, agent_id, _prod, _ver)
        if parsed["result"] or parsed["escalate"]:
            adir = _agent_dir(project["root"], agent, cwd)
            version = _read_manifest_version(adir / "outputs" / "manifest.md")
            _stamp_overview(adir, version, parsed["escalate"])
            if parsed["escalate"]:
                # Route to the parent's ledger so the orchestrator picks it up next
                # turn (§6.6). Auto-resolution per type (DATA pull / SUBTASK / TOOL) is
                # deliberately left to BOSS/human — see _handle_escalate docstring.
                routed = _handle_escalate(parsed["escalate"], slug, project, agent_id)
                await emit({"type": "meta", "agent": agent_id,
                            "data": {"escalate": parsed["escalate"], "routed_to": routed}})
        # Agency mechanisms (spec §15.1/§15.2): soft-trigger (model emits) + hard-enforce here.
        if parsed["halt"]:
            routed = _handle_halt(parsed["halt"], slug, project, agent_id)
            await emit({"type": "meta", "agent": agent_id,
                        "data": {"halt": parsed["halt"], "routed_to": routed}})
        if parsed["dissent"]:
            routed = _handle_dissent(parsed["dissent"], slug, project, agent_id)  # opens BLOCKING-FLAG
            await emit({"type": "meta", "agent": agent_id,
                        "data": {"dissent": parsed["dissent"], "routed_to": routed}})
        if parsed["dissent_resolve"]:
            n = _handle_dissent_resolve(parsed["dissent_resolve"], slug, agent_id)  # clears the gate
            await emit({"type": "meta", "agent": agent_id,
                        "data": {"dissent_resolved": parsed["dissent_resolve"], "count": n}})
    # ----- END STRUCTURED OUTPUT -----

    return "".join(assembled)


def _manifest_snapshot(slug: str, agent_id: str) -> Optional[dict]:
    """mtime + version of a worker's outputs/manifest.md, for the contract
    verify around a dispatch. None when the agent has no manifest."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        return None
    project, agent = found
    cwd_abs = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    man = _agent_dir(project["root"], agent, cwd_abs) / "outputs" / "manifest.md"
    try:
        st = man.stat()
    except OSError:
        return None
    version = None
    try:
        lines = man.read_text(encoding="utf-8", errors="replace").splitlines()[:40]
        for i, line in enumerate(lines):
            # frontmatter style: `version: 2.10.0`
            m = re.match(r"\s*version:\s*([\w.\-]+)", line, re.IGNORECASE)
            if m:
                version = m.group(1)
                break
            # heading style: `## Version` then the value on a following line
            if re.match(r"#+\s*version\s*$", line.strip(), re.IGNORECASE):
                for nxt in lines[i + 1:i + 4]:
                    nxt = nxt.strip()
                    if nxt:
                        vm = re.match(r"([\w.\-]+)", nxt)
                        if vm:
                            version = vm.group(1)
                        break
                break
    except OSError:
        pass
    return {"mtime": st.st_mtime, "version": version}


# ----- STRUCTURED OUTPUT: [RESULT] / [ESCALATE] parse + overview stamp (spec §4, §6.4) -----
_RESULT_RE = re.compile(r"\[RESULT\](?P<body>.*?)\[/RESULT\]", re.DOTALL | re.IGNORECASE)
_ESCALATE_RE = re.compile(r"\[ESCALATE\](?P<body>.*?)\[/ESCALATE\]", re.DOTALL | re.IGNORECASE)
_HALT_RE = re.compile(r"\[HALT\](?P<body>.*?)\[/HALT\]", re.DOTALL | re.IGNORECASE)
_DISSENT_RE = re.compile(r"\[DISSENT\](?P<body>.*?)\[/DISSENT\]", re.DOTALL | re.IGNORECASE)
_DISSENT_RESOLVE_RE = re.compile(r"\[DISSENT_RESOLVE\](?P<body>.*?)\[/DISSENT_RESOLVE\]", re.DOTALL | re.IGNORECASE)
_KV_RE = re.compile(r"^\s*(?P<k>\w+)\s*:\s*(?P<v>.+?)\s*$", re.MULTILINE)
_ESC_TYPES = {"DATA", "BOSS_DECISION", "HUMAN", "SUBTASK", "TOOL", "BLOCKED"}
_HALT_REASONS = {"saturated", "dead_end", "false_premise", "diminishing_returns"}
_DISSENT_VERDICTS = {"ratify", "overrule"}


def _kv(body: str) -> dict:
    return {m.group("k").lower(): m.group("v").strip() for m in _KV_RE.finditer(body)}


def _parse_structured(text: str) -> dict:
    """Parse the optional structured blocks an agent emits at end of turn:
    [RESULT] / [ESCALATE] / [HALT] / [DISSENT] / [DISSENT_RESOLVE].

    Malformed = an unbalanced open tag, an [ESCALATE] type not in _ESC_TYPES, or a
    [HALT]/[DISSENT] missing its REQUIRED evidence (the anti-self-certification
    guardrail — a worker may surface a judgment, never on a bare claim).
    Absence of any block is NOT malformed (analysis turns emit none).
    """
    out: dict = {"result": None, "escalate": None, "halt": None, "dissent": None,
                 "dissent_resolve": None, "malformed": False, "error": ""}

    def _flag(msg: str) -> None:
        out["malformed"] = True
        out["error"] = (out["error"] + "; " + msg).strip("; ")

    for tag in ("[RESULT]", "[ESCALATE]", "[HALT]", "[DISSENT]", "[DISSENT_RESOLVE]"):
        close = tag[:1] + "/" + tag[1:]
        if text.count(tag) != text.count(close):
            _flag(f"unbalanced {tag} tags")

    rm = _RESULT_RE.search(text)
    if rm:
        out["result"] = _kv(rm.group("body"))
    em = _ESCALATE_RE.search(text)
    if em:
        esc = _kv(em.group("body"))
        t = (esc.get("type") or "").upper()
        if t not in _ESC_TYPES:
            _flag(f"[ESCALATE] type '{t}' invalid")
        else:
            esc["type"] = t
        out["escalate"] = esc

    # [HALT] (spec §15.1): worker self-halts a futile task. EVIDENCE is required —
    # a halt without quantitative evidence is a lazy claim, rejected.
    hm = _HALT_RE.search(text)
    if hm:
        h = _kv(hm.group("body"))
        if not (h.get("evidence") or "").strip():
            _flag("[HALT] missing required `evidence`")
        out["halt"] = h

    # [DISSENT] (spec §15.2): worker blocks a wrong DIRECTION. evidence + against required.
    dm = _DISSENT_RE.search(text)
    if dm:
        d = _kv(dm.group("body"))
        if not (d.get("evidence") or "").strip():
            _flag("[DISSENT] missing required `evidence`")
        if not (d.get("against") or "").strip():
            _flag("[DISSENT] missing required `against`")
        out["dissent"] = d

    # [DISSENT_RESOLVE] (orchestrator only): ratify|overrule an open dissent.
    drm = _DISSENT_RESOLVE_RE.search(text)
    if drm:
        dr = _kv(drm.group("body"))
        v = (dr.get("verdict") or "").lower()
        if v not in _DISSENT_VERDICTS:
            _flag(f"[DISSENT_RESOLVE] verdict '{v}' invalid")
        else:
            dr["verdict"] = v
        out["dissent_resolve"] = dr
    return out


def _read_manifest_version(out_manifest: Path) -> Optional[str]:
    """Version from a `outputs/manifest.md` — frontmatter `version:` or `## Version`
    heading style (same convention as _manifest_snapshot). None when absent."""
    try:
        lines = out_manifest.read_text(encoding="utf-8", errors="replace").splitlines()[:40]
    except OSError:
        return None
    for i, line in enumerate(lines):
        m = re.match(r"\s*version:\s*([\w.\-]+)", line, re.IGNORECASE)
        if m:
            return m.group(1)
        if re.match(r"#+\s*version\s*$", line.strip(), re.IGNORECASE):
            for nxt in lines[i + 1:i + 4]:
                nxt = nxt.strip()
                if nxt:
                    vm = re.match(r"([\w.\-]+)", nxt)
                    return vm.group(1) if vm else None
    return None


def _stamp_overview(agent_dir: Path, version: Optional[str], escalate: Optional[dict]) -> None:
    """Overwrite the MACHINE fields of overview.md (agent owns the BODY, we own
    the header echo). manifest_version ← real version; last_updated ← today;
    body_incomplete ← heuristic (placeholder / near-empty BODY); open_escalation
    ← escalate summary when present. No-op when the agent has no overview yet."""
    ov = agent_dir / "overview.md"
    if not ov.is_file():
        return
    try:
        txt = ov.read_text(encoding="utf-8")
    except OSError:
        return

    bm = re.search(r"<!-- OVERVIEW:BODY -->(.*?)<!-- /OVERVIEW:BODY -->", txt, re.DOTALL)
    body = bm.group(1) if bm else ""
    incomplete = ("(chờ" in body) or (len(body.strip()) < 40)
    today = time.strftime("%Y-%m-%d")

    def _set(key: str, val: str) -> None:
        nonlocal txt
        txt = re.sub(rf"(?m)^({re.escape(key)}:).*$",
                     lambda m: f"{m.group(1)} {val}", txt, count=1)

    if version:
        _set("manifest_version", version)
    _set("body_incomplete", "true" if incomplete else "false")
    _set("last_updated", today)
    if escalate:
        summary = (f"{escalate.get('type', '?')}→{escalate.get('target', '?')}: "
                   f"{escalate.get('reason', '')}")[:120]
        _set("open_escalation", summary)

    try:
        ov.write_text(txt, encoding="utf-8")
    except OSError:
        pass


def _read_overview_flag(agent_dir: Path) -> Optional[bool]:
    """`body_incomplete` from an agent's overview.md HEADER. None when no overview."""
    ov = agent_dir / "overview.md"
    try:
        for line in ov.read_text(encoding="utf-8", errors="replace").splitlines()[:30]:
            m = re.match(r"\s*body_incomplete:\s*(true|false)", line, re.IGNORECASE)
            if m:
                return m.group(1).lower() == "true"
    except OSError:
        return None
    return None


def _check_version_pins(project_root: str, project: dict, agent_id: str) -> list[str]:
    """Drift report: input pins in `<agent>/inputs/manifest.md` that no longer
    match the producer's current `outputs/manifest.md` version. Empty = clean.

    Best-effort + fail-open: an unparseable inputs file yields no drift (never a
    spurious block). Generic `- <PRODUCER>: <ver>` line shape (all-caps id).
    """
    agents = {a["id"]: a for a in project["agents"]}
    agent = agents.get(agent_id)
    if not agent:
        return []
    adir = _agent_dir(project_root, agent, projects.resolve_cwd(project_root, agent.get("cwd", ".")))
    try:
        lines = (adir / "inputs" / "manifest.md").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    drift: list[str] = []
    for line in lines:
        m = re.match(r"\s*-\s*([A-Z][A-Z0-9_]*)\s*:\s*([\w.\-]+)", line)
        if not m:
            continue
        prod, pinned = m.group(1), m.group(2)
        prod_agent = agents.get(prod)
        if not prod_agent:
            continue
        pdir = _agent_dir(project_root, prod_agent,
                          projects.resolve_cwd(project_root, prod_agent.get("cwd", ".")))
        current = _read_manifest_version(pdir / "outputs" / "manifest.md")
        if current and pinned and current != pinned:
            drift.append(f"{prod} pinned={pinned} current={current}")
    return drift


def _handle_escalate(esc: dict, slug: str, project: dict, agent_id: str) -> Optional[str]:
    """Route a worker's [ESCALATE] to its parent's dispatch ledger so the
    orchestrator picks it up next turn (reuses the ledger — no separate table).

    NOTE: type-specific AUTO-resolution (DATA auto-pull+stamp, SUBTASK auto-dispatch,
    TOOL exec) is deliberately NOT performed here. Those need project-specific
    extraction and carry correctness risk (pulling a wrong/stale section, running an
    arbitrary tool). Surfacing to the orchestrator keeps BOSS/human in the loop.
    Returns the parent agent id routed to, or None for a top-level agent.
    """
    agents = {a["id"]: a for a in project["agents"]}
    agent = agents.get(agent_id)
    if not agent:
        return None
    parents = agent.get("parents") or []
    parent = parents[0] if parents else None
    if not parent:
        return None  # top-level agent → already surfaced via the meta event
    db.record_dispatch_result(
        project_slug=slug, source_agent=parent, target_agent=agent_id,
        task=f"ESCALATE:{esc.get('type', '?')} → {esc.get('target', '?')}",
        result_text="[ESCALATION from worker — route per type, do NOT ignore]\n"
                    + json.dumps(esc, ensure_ascii=False, indent=2),
        status="ok",
    )
    return parent


def _parent_of(project: dict, agent_id: str) -> Optional[str]:
    agent = next((a for a in project["agents"] if a["id"] == agent_id), None)
    parents = (agent or {}).get("parents") or []
    return parents[0] if parents else None


def _handle_halt(halt: dict, slug: str, project: dict, agent_id: str) -> Optional[str]:
    """Worker self-halted a futile task (spec §15.1). Log it (audit halt-rate) and
    surface to the parent's ledger as a recommendation. NO auto-resume — the turn
    already ended; BOSS reads it next turn and may overturn (re-dispatch)."""
    db.add_halt_log(slug, agent_id, reason=halt.get("reason", ""), evidence=halt.get("evidence", ""),
                    recommendation=halt.get("recommendation", ""), confidence=halt.get("confidence", ""))
    parent = _parent_of(project, agent_id)
    if parent:
        db.record_dispatch_result(
            project_slug=slug, source_agent=parent, target_agent=agent_id,
            task=f"HALT:{halt.get('reason', '?')}",
            result_text="[SELF-HALT from worker — judgment, not failure. Ratify or overturn]\n"
                        + json.dumps(halt, ensure_ascii=False, indent=2),
            status="ok",
        )
    return parent


def _handle_dissent(dissent: dict, slug: str, project: dict, agent_id: str) -> Optional[str]:
    """Worker dissents a DIRECTION (spec §15.2). Record a BLOCKING-FLAG (the HARD
    forcing half — gated in _run_agent) + surface to the parent's ledger."""
    db.add_dissent_flag(slug, source_agent=agent_id, against=dissent.get("against", ""),
                        reason=dissent.get("reason", ""), evidence=dissent.get("evidence", ""),
                        severity=(dissent.get("severity") or "blocking").lower())
    parent = _parent_of(project, agent_id)
    if parent:
        db.record_dispatch_result(
            project_slug=slug, source_agent=parent, target_agent=agent_id,
            task=f"DISSENT against: {dissent.get('against', '?')}",
            result_text="[DISSENT from worker — you MUST ratify or overrule before proceeding on this]\n"
                        + json.dumps(dissent, ensure_ascii=False, indent=2),
            status="ok",
        )
    return parent


def _handle_dissent_resolve(dr: dict, slug: str, agent_id: str) -> int:
    """Orchestrator resolves an open dissent (ratify|overrule). Clears the gate."""
    return db.resolve_dissent(slug, against=dr.get("against", ""), verdict=dr.get("verdict", ""),
                              resolved_by=agent_id, resolution_reason=dr.get("reason", ""))


def _open_dissent_warning(slug: str) -> str:
    """Forcing function (spec §15.2): text prepended to an orchestrator's turn while
    any dissent is open, so it CANNOT silently proceed. Empty when none open."""
    flags = db.get_open_dissents(slug)
    if not flags:
        return ""
    lines = [f"- against \"{f['against']}\" (from {f['source_agent']}, {f.get('severity') or 'blocking'}): "
             f"{f.get('reason') or ''} | evidence: {(f.get('evidence') or '')[:200]}" for f in flags]
    return (
        "[CONTROL-PLANE OPEN DISSENT] A worker has blocked one or more directions. You MUST address each "
        "before proceeding on it — emit `[DISSENT_RESOLVE]\\nagainst: <…>\\nverdict: ratify|overrule\\nreason: <…>\\n[/DISSENT_RESOLVE]`:\n"
        + "\n".join(lines) + "\n\n"
    )


def _parse_verified(s: str) -> dict:
    """Parse a [RESULT] `verified:` field — 'PROD@ver, PROD@ver' → {PROD: ver}."""
    out: dict = {}
    for tok in re.split(r"[,\n]", s or ""):
        m = re.match(r"\s*([A-Za-z][\w-]*)\s*@\s*([\w.\-]+)", tok)
        if m:
            out[m.group(1).upper()] = m.group(2)
    return out


def _verify_delta(slug: str, project: dict, agent_id: str) -> Optional[dict]:
    """Incremental verification (spec §16.1): compare an auditor's stored watermarks
    vs producers' CURRENT versions → {skip, reverify}. None when no watermark yet
    (the agent never declared `verified:` → not an incremental auditor)."""
    wm = db.get_verify_watermarks(slug, agent_id)
    if not wm:
        return None
    agents = {a["id"]: a for a in project["agents"]}
    skip, reverify = [], []
    for prod, vver in wm.items():
        pa = agents.get(prod)
        cur = None
        if pa:
            pdir = _agent_dir(project["root"], pa, projects.resolve_cwd(project["root"], pa.get("cwd", ".")))
            cur = _read_manifest_version(pdir / "outputs" / "manifest.md")
        if cur and cur == vver:
            skip.append(f"{prod}@{vver}")
        else:
            reverify.append(f"{prod} (verified@{vver} → now {cur or '?'})")
    return {"skip": skip, "reverify": reverify}


def _verify_delta_hint(delta: Optional[dict]) -> str:
    """Forcing hint prepended to an auditor's turn: the deterministic SKIP set it
    must trust (don't re-audit unchanged), turning O(N) re-audit into O(Δ)."""
    if not delta or not delta.get("skip"):
        return ""
    txt = ("[CONTROL-PLANE VERIFY-DELTA] Incremental verification (spec §16.1). You already verified these "
           "at the SAME producer version — do NOT re-resolve/re-audit, trust your prior verdict:\n  SKIP: "
           + ", ".join(delta["skip"]) + "\n")
    if delta.get("reverify"):
        txt += "  RE-VERIFY (changed/new since your watermark): " + ", ".join(delta["reverify"]) + "\n"
    txt += ("Re-verify ONLY the changed/new set; run cheap mechanical checks (count-reconcile/dedup/drift) "
            "every pass but reserve identifier-resolution/web for the RE-VERIFY set + publish-gate. Advance "
            "your watermark by emitting `verified: PROD@ver, …` in [RESULT].\n\n")
    return txt


def _parse_goal(task: str) -> Optional[dict]:
    """Extract a `goal:` sub-block from an Outcome dispatch body (spec §14.2). The
    control-plane cannot read arbitrary project metrics, so this only captures the
    contract fields — the gate enforces PROCESS (no self-certification), not the value."""
    if not re.search(r"(?mi)^\s*goal:", task) and "mode: outcome" not in task.lower():
        return None
    fields: dict = {}
    for key in ("type", "predicate", "baseline_value", "metric_pinned", "acceptance_by"):
        m = re.search(rf"(?mi)^\s*{key}:\s*(.+)$", task)
        if m:
            fields[key] = m.group(1).strip()
    return fields or None
# ----- END STRUCTURED OUTPUT -----


async def _dispatched_run(slug, source_id, target_id, task, emit, tracker, chain):
    """Run a worker dispatched by source_id. Capture its final text and write
    it to the dispatch_results ledger so source_id can see the output on its
    next prompt (via enrichment in _run_agent).
    """
    status = "ok"
    error_msg = None
    result_text = ""
    manifest_before = _manifest_snapshot(slug, target_id)
    try:
        result_text = await _run_agent(slug, target_id, task, emit, tracker, chain) or ""
        # _run_agent sets final_status internally (e.g. to "error" on
        # adapter error events) and updates the worker session row before
        # returning. Read it back to preserve nuance in the ledger.
        worker_status = db.get_last_status(slug, target_id)
        if worker_status in ("error", "cancelled"):
            status = worker_status
    except asyncio.CancelledError:
        status = "cancelled"
        # On cancellation, recover whatever the worker had assembled before
        # being cut off, so the source agent at least sees a partial result.
        if not result_text:
            try:
                sessions = db.list_sessions(slug, target_id)
                if sessions:
                    msgs = db.get_messages(sessions[0]["id"])
                    if msgs and msgs[-1]["role"] == "assistant":
                        result_text = msgs[-1]["content"] or ""
            except Exception:
                pass
        db.record_dispatch_result(
            project_slug=slug, source_agent=source_id, target_agent=target_id,
            task=task,
            result_text=result_text or "(cancelled before any output)",
            status=status, meta={"chain": list(chain)},
        )
        await emit({
            "type": "dispatch_complete", "source": source_id,
            "target": target_id, "status": status, "message": "cancelled",
        })
        raise
    except Exception as e:
        status = "error"
        error_msg = str(e)

    # Contract verify: did the worker publish via outputs/manifest.md? A soft
    # flag (not a block) — answer-only dispatches legitimately don't bump it.
    # The note rides inside the ledger text so the orchestrator model reacts.
    if status == "ok" and manifest_before is not None:
        after = _manifest_snapshot(slug, target_id)
        if after and after["mtime"] == manifest_before["mtime"]:
            result_text = (result_text or "") + (
                f"\n\n[control-plane verify] outputs/manifest.md of {target_id} did NOT change "
                f"during this dispatch (still version {after.get('version') or '?'}). If the task "
                "created/modified a downstream artifact → the result is NOT yet published per contract; "
                "require the worker to bump the manifest before consuming."
            )
        elif after:
            result_text = (result_text or "") + (
                f"\n\n[control-plane verify] outputs/manifest.md was updated "
                f"(version {manifest_before.get('version') or '?'} → {after.get('version') or '?'})."
            )

    # Goal-gate (spec §6.8/§14.4): an Outcome dispatch carries a goal contract. The
    # control-plane cannot read arbitrary project metrics, so it enforces PROCESS, not
    # the value: a worker's self-reported goal_status is a CLAIM, never acceptance —
    # acceptance is by the external authority. (Budget/loop is bounded by the existing
    # continuation cap; metric plateau detection would need a project-specific reader.)
    if status == "ok":
        goal = _parse_goal(task)
        if goal:
            claimed = (_parse_structured(result_text or "").get("result") or {}).get("goal_status")
            acc = goal.get("acceptance_by", "control_plane")
            note = (f"\n\n[control-plane goal-gate] acceptance_by={acc}. Worker self-reported "
                    f"goal_status={claimed or 'none'} — a CLAIM, not acceptance. Do NOT accept on "
                    "self-report")
            if acc and acc.lower().replace("-", "_") != "control_plane":
                note += f"; route to {acc} to verify before consuming."
            else:
                note += "; verify the pinned predicate/metric yourself before consuming."
            if goal.get("type", "").lower().startswith("direction") and goal.get("baseline_value"):
                note += (f" Directional: require new > baseline {goal['baseline_value']} on "
                         f"{goal.get('metric_pinned', 'the pinned metric')}.")
            result_text = (result_text or "") + note

    db.record_dispatch_result(
        project_slug=slug, source_agent=source_id, target_agent=target_id,
        task=task,
        result_text=result_text or (error_msg or "(empty result)"),
        status=status, meta={"chain": list(chain)},
    )

    await emit({
        "type": "dispatch_complete",
        "source": source_id,
        "target": target_id,
        "status": status,
        "message": error_msg,
    })


# Above this share of the context window, the next user turn triggers an
# automatic compact (summary → fresh seeded session) BEFORE the turn runs.
# 40% (was 80%): lowered 2026-07-01 because reasoning adapters (glm-5.2 etc.)
# over-think on large cached context — observed a BOSS session reach 459k
# cached input tokens (46% of its 1M window) with 80% never firing, driving
# ~38k-token thinking traces and verbose output. 40% fires compaction early
# enough (≈400k on a 1M window) to keep context lean across all adapters.
# 40% still leaves ample headroom for the summary turn itself to complete.
_AUTO_COMPACT_PCT = 40.0

# Continuation budget per user turn: how many times the orchestrator may react
# to completed dispatches (and chain new ones) within one SSE response.
_MAX_CONT_ROUNDS = 3


async def _auto_compact_if_needed(slug: str, agent_id: str, emit) -> bool:
    """If the agent's active session is above the auto-compact threshold, run
    the compact flow (same as /compact) before the user's turn: the agent
    summarises its context, a fresh session is created seeded with the recap.
    Returns True if a compact happened.

    Torn sessions (last_status != ok) are skipped: they cannot be resumed
    anyway, so the next turn starts a fresh CLI session and the cold-start
    preamble covers recovery — compacting would just waste a turn.
    """
    found = projects.get_agent(slug, agent_id)
    if not found:
        return False
    _, agent = found
    sessions = db.list_sessions(slug, agent_id)
    sess = sessions[0] if sessions else None
    if not sess or sess.get("last_status") != "ok" or not sess.get("claude_session_id"):
        return False
    model_kind = agent.get("model", "claude")
    ov = db.get_agent_override(slug, agent_id) or {}
    if model_kind == "grok":
        eff_model = ov.get("grok_model") or agent.get("grok_model") or "grok-build"
    elif model_kind == "deepseek":
        eff_model = ov.get("deepseek_model") or agent.get("deepseek_model") or "deepseek-v4-flash"
    elif model_kind == "glm":
        eff_model = ov.get("glm_model") or agent.get("glm_model") or "glm-4.6"
    else:
        eff_model = ov.get("claude_model") or agent.get("claude_model") or "claude-sonnet-4-6"
    pct = _session_context_pct(sess, model_kind, eff_model)
    if pct < _AUTO_COMPACT_PCT:
        return False

    await emit({"type": "compact_started", "agent": agent_id, "auto": True, "pct": pct})
    tracker: list = []
    summary = (await _run_agent(slug, agent_id, _COMPACT_PROMPT, emit, tracker) or "").strip()
    if tracker:
        await asyncio.gather(*tracker, return_exceptions=True)
    new_sess = db.new_session(slug, agent_id)
    if summary:
        db.set_session_seed(new_sess["id"], summary)
        db.add_message(
            new_sess["id"], "assistant",
            f"📦 **Auto-compact @ {pct}% context** — new session seeded with the recap below. "
            "The next turn continues from this recap.\n\n---\n\n" + summary,
        )
    else:
        # Summary turn failed (likely the old session was too overloaded to
        # answer). Still rotate to a fresh session — staying at >80% is worse.
        # The cold-start preamble (state/progress.md) covers recovery.
        db.add_message(
            new_sess["id"], "assistant",
            f"📦 **Auto-compact @ {pct}% context** — recap empty (old session overloaded?); "
            "the new session will start from the cold-start preamble built from `state/progress.md`.",
        )
    db.update_session_status(new_sess["id"], "ok")
    await emit({"type": "compacted", "agent": agent_id, "new_session_id": new_sess["id"], "auto": True})
    return True


# ---------- Detached runs (turn execution survives browser disconnect) ----------
#
# A chat turn runs as a server-side task that publishes events to an in-memory
# per-run buffer plus live subscriber queues. The SSE response returned by
# /chat is merely the FIRST subscriber: closing the browser only unsubscribes —
# the turn keeps running to completion and persists its results (messages
# table, dispatch ledger) exactly as if the tab had stayed open.
# Reopening the UI re-attaches via GET /stream with full event replay (seq 0).
# Stopping is now an explicit POST /stop, never a side effect of disconnect.

_RUN_KEEP_DONE_S = 600  # finished runs stay replayable this long


class _Run:
    def __init__(self, slug: str, agent_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.slug = slug
        self.agent_id = agent_id
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.events: list[dict] = []          # every event, stamped with "seq"
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.task: Optional[asyncio.Task] = None

    async def publish(self, evt: dict) -> None:
        evt = dict(evt)
        evt["seq"] = len(self.events)
        self.events.append(evt)
        for q in list(self.subscribers):
            q.put_nowait(evt)

    def finish(self) -> None:
        self.done = True
        self.finished_at = time.time()
        for q in list(self.subscribers):
            q.put_nowait(None)


_RUNS: dict[str, _Run] = {}


def _active_run(slug: str, agent_id: str) -> Optional[_Run]:
    for r in _RUNS.values():
        if r.slug == slug and r.agent_id == agent_id and not r.done:
            return r
    return None


def _latest_run(slug: str, agent_id: str) -> Optional[_Run]:
    cands = [r for r in _RUNS.values() if r.slug == slug and r.agent_id == agent_id]
    return max(cands, key=lambda r: r.started_at) if cands else None


def _prune_runs() -> None:
    now = time.time()
    for rid in [rid for rid, r in _RUNS.items()
                if r.done and r.finished_at and now - r.finished_at > _RUN_KEEP_DONE_S]:
        _RUNS.pop(rid, None)


async def _run_subscriber_sse(run: _Run, since: int = 0):
    """SSE generator attached to a run: replay buffered events from `since`,
    then follow live. Subscribe BEFORE replaying so no event is missed; the
    seq filter drops any duplicates that race in during replay. Disconnect
    only removes the queue — the run task is untouched."""
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)
    try:
        yield _sse({"type": "start", "agent": run.agent_id, "run_id": run.id,
                    "replay": max(0, since) < len(run.events)})
        nxt = max(0, since)
        while nxt < len(run.events):
            yield _sse(run.events[nxt])
            nxt += 1
        if run.done:
            yield _sse({"type": "complete"})
            return
        while True:
            try:
                # 15s timeout keeps the socket alive during long quiet phases
                # (Opus extended thinking can sit 10-30s without a byte).
                evt = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if evt is None:
                break
            if evt["seq"] < nxt:
                continue
            nxt = evt["seq"] + 1
            yield _sse(evt)
        yield _sse({"type": "complete"})
    finally:
        run.subscribers.discard(q)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _start_run(slug: str, agent_id: str, message: str, *,
               origin: str = "user", grok_options: Optional[dict] = None) -> _Run:
    """Create + launch a detached run for one agent turn. Shared by POST /chat and
    the scheduler — a scheduled fire is identical to a user turn. Caller is
    responsible for the one-active-run-per-agent policy (api_chat 409s, the
    scheduler skips). Returns the _Run; subscribe to it for the SSE stream."""
    run = _Run(slug, agent_id)
    _RUNS[run.id] = run
    emit = run.publish

    async def driver():
        # Multi-round continuation: after each wave of dispatches lands in the
        # ledger, the orchestrator gets another turn to react — synthesise, or
        # chain follow-up dispatches — inside the SAME run, up to
        # _MAX_CONT_ROUNDS. Dispatches fired on the final round still run to
        # completion (results land in the ledger for the NEXT user turn); they
        # just don't trigger another continuation.
        all_tasks: list = []
        cur: list = []
        saw_sched = False   # did any turn this run emit a <schedule>/<schedule_stop> tag?
        try:
            await _auto_compact_if_needed(slug, agent_id, emit)
            txt = await _run_agent(slug, agent_id, message, emit, cur,
                                   grok_options=grok_options)
            saw_sched = saw_sched or _has_schedule_tag(txt)
            rounds = 0
            while cur and rounds < _MAX_CONT_ROUNDS:
                await asyncio.gather(*cur, return_exceptions=True)
                all_tasks.extend(cur)
                rounds += 1
                last = rounds >= _MAX_CONT_ROUNDS
                # UI renders this as a thin separator between continuation
                # rounds instead of showing the raw control-plane prompt.
                await emit({"type": "continuation_round", "agent": agent_id,
                            "round": rounds, "max": _MAX_CONT_ROUNDS})
                synth = (
                    f"[CONTROL-PLANE CONTINUATION {rounds}/{_MAX_CONT_ROUNDS}] All worker "
                    "dispatches from your previous response have completed. Their outputs are "
                    "provided as <dispatch_result> blocks at the top of this message. Reason "
                    "over the real data and "
                    + (
                        "produce your final answer / summary for the user NOW. Continuation "
                        "budget is EXHAUSTED — do NOT emit further dispatch tags; work with "
                        "what you have and report anything unfinished."
                        if last else
                        "either: produce your final answer / summary for the user, or — only "
                        "if the results require it — emit the next dispatch tag(s). Do NOT "
                        "re-emit the same tasks. Do NOT say you are still waiting."
                    )
                )
                cur = []
                txt = await _run_agent(slug, agent_id, synth, emit, cur)
                saw_sched = saw_sched or _has_schedule_tag(txt)
            if cur:
                await asyncio.gather(*cur, return_exceptions=True)
                all_tasks.extend(cur)

            # Schedule safety-net — STRICTLY one extra turn, only when the user asked
            # to track/monitor but no <schedule> tag was emitted the whole run. Tightly
            # gated (origin user-only, intent regex, not already scheduled) so it can't
            # loop or add steady-state load: it fires at most once per user turn, never
            # on scheduled fires or continuations. Without it, the agent's "tracking is
            # active" narration silently registers nothing — the failure we're fixing.
            if (origin == "user" and not saw_sched
                    and _looks_like_tracking_intent(message)):
                ncur: list = []
                await _run_agent(slug, agent_id, _SCHEDULE_NUDGE, emit, ncur)
                if ncur:
                    await asyncio.gather(*ncur, return_exceptions=True)
                    all_tasks.extend(ncur)
        except asyncio.CancelledError:
            pending = [t for t in all_tasks + cur if not t.done()]
            for t in pending:
                t.cancel()
            if all_tasks or cur:
                await asyncio.gather(*(all_tasks + cur), return_exceptions=True)
            try:
                await emit({"type": "error", "agent": agent_id,
                            "message": "turn stopped by user"})
            except Exception:
                pass
        except Exception as e:
            await emit({"type": "error", "agent": agent_id, "message": str(e)})
        finally:
            run.finish()

    run.task = asyncio.create_task(driver())
    return run


@app.post("/api/projects/{slug}/agents/{agent_id}/chat")
async def api_chat(slug: str, agent_id: str, body: ChatBody):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    _prune_runs()
    existing = _active_run(slug, agent_id)
    if existing:
        raise HTTPException(409, f"a turn is already running for {agent_id} (run {existing.id})")

    grok_options = {
        "best_of_n": body.best_of_n,
        "check_loop": body.check_loop,
        "memory_mode": body.memory_mode,
    } if (body.best_of_n or body.check_loop or body.memory_mode) else None

    run = _start_run(slug, agent_id, body.message, origin="user", grok_options=grok_options)

    return StreamingResponse(
        _run_subscriber_sse(run, since=0),
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
    )


@app.get("/api/projects/{slug}/runs")
def api_runs(slug: str):
    """Active (not yet done) runs for this project — the UI calls this on
    project open to re-attach to turns that kept running while the browser
    was closed."""
    _prune_runs()
    return {"runs": [
        {"run_id": r.id, "agent_id": r.agent_id, "started_at": r.started_at,
         "events": len(r.events)}
        for r in _RUNS.values() if r.slug == slug and not r.done
    ]}


@app.get("/api/projects/{slug}/agents/{agent_id}/stream")
async def api_stream(slug: str, agent_id: str, since: int = 0):
    """Re-attach to this agent's run (active, or finished within the keep
    window) with replay from `since`. 404 when there is nothing to attach."""
    if not projects.get_agent(slug, agent_id):
        raise HTTPException(404, "agent not found")
    run = _active_run(slug, agent_id) or _latest_run(slug, agent_id)
    if not run:
        raise HTTPException(404, "no run to attach")
    return StreamingResponse(
        _run_subscriber_sse(run, since=since),
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
    )


@app.post("/api/projects/{slug}/agents/{agent_id}/stop")
async def api_stop(slug: str, agent_id: str):
    """Explicitly cancel the agent's active run. Since browser disconnect no
    longer cancels anything, this is the ONLY way to stop a turn."""
    run = _active_run(slug, agent_id)
    if not run or not run.task or run.task.done():
        return {"stopped": False, "reason": "no active run"}
    run.task.cancel()
    return {"stopped": True, "run_id": run.id}


class ScheduleBody(BaseModel):
    agent_id: str
    prompt: str
    # one of: every (recurring), in (one-shot delay), until (goal loop, needs every)
    every: Optional[str] = None
    delay: Optional[str] = None      # maps to the `in=` attr
    until: Optional[str] = None
    max: Optional[int] = None


@app.get("/api/projects/{slug}/schedules")
def api_list_schedules(slug: str):
    if not projects.get_project(slug):
        raise HTTPException(404, "project not found")
    return {"schedules": [_schedule_public(t) for t in db.list_scheduled_tasks(slug)]}


@app.post("/api/projects/{slug}/schedules")
def api_create_schedule(slug: str, body: ScheduleBody):
    if not projects.get_project(slug):
        raise HTTPException(404, "project not found")
    if not projects.get_agent(slug, body.agent_id):
        raise HTTPException(404, "agent not found")
    if not _scheduler_enabled():
        raise HTTPException(403, "scheduler is globally disabled")
    attrs = {"every": body.every or "", "in": body.delay or "",
             "until": body.until or "", "max": body.max}
    t, err = _build_schedule(slug, body.agent_id, attrs, body.prompt, "user")
    if err:
        raise HTTPException(400, err)
    return {"schedule": _schedule_public(t)}


@app.patch("/api/projects/{slug}/schedules/{task_id}")
def api_patch_schedule(slug: str, task_id: int, active: bool):
    t = db.get_scheduled_task(task_id)
    if not t or t["project_slug"] != slug:
        raise HTTPException(404, "schedule not found")
    # resuming a recurring schedule whose next_run_at is in the past → fire next tick
    updated = db.set_scheduled_active(task_id, active)
    return {"schedule": _schedule_public(updated)}


@app.delete("/api/projects/{slug}/schedules/{task_id}")
def api_delete_schedule(slug: str, task_id: int):
    t = db.get_scheduled_task(task_id)
    if not t or t["project_slug"] != slug:
        raise HTTPException(404, "schedule not found")
    db.delete_scheduled_task(task_id)
    return {"deleted": True, "id": task_id}


@app.get("/api/scheduler/enabled")
def api_get_scheduler_enabled():
    return {"enabled": _scheduler_enabled()}


@app.post("/api/scheduler/enabled")
def api_set_scheduler_enabled(payload: dict = Body(default={"enabled": True})):
    en = bool(payload.get("enabled", True)) if isinstance(payload, dict) else True
    was = _scheduler_enabled()
    db.set_setting("scheduler_enabled", "1" if en else "0")
    if en and not was:
        # Re-enabling: never replay fires that came due while disabled. Push
        # overdue recurring tasks to their next forward cycle; retire overdue
        # one-shots. Only the 0→1 transition triggers this.
        _skip_overdue_on_resume()
    return {"enabled": _scheduler_enabled()}


# ---------- Per-turn context log (feature C): report + on/off toggle ----------

@app.get("/api/context-log/report")
def api_context_log_report(project: Optional[str] = None):
    return {"rows": db.context_log_report(project)}


@app.get("/api/context-log/enabled")
def api_get_context_log_enabled():
    return {"enabled": db.get_setting("context_log_enabled", "1") == "1"}


@app.post("/api/context-log/enabled")
def api_set_context_log_enabled(payload: dict = Body(default={"enabled": True})):
    en = bool(payload.get("enabled", True)) if isinstance(payload, dict) else True
    db.set_setting("context_log_enabled", "1" if en else "0")
    return {"enabled": db.get_setting("context_log_enabled", "1") == "1"}


# ---------- Scheduler loop (fires due schedules; see docs/scheduler-spec.md) ----------
#
# A fire = _start_run with a wrapped prompt — identical to a /chat turn, so it
# streams + persists and is picked up by the UI's /runs poll. The loop only
# fires; the per-fire task awaits the run and computes the next fire time.

_sched_inflight: set[int] = set()


def _wrap_schedule_prompt(t: dict, n: int) -> str:
    head = f"[SCHEDULED CHECK #{n}" + (f"/{t['max_runs']}" if t.get("max_runs") else "") + "] "
    body = (t["prompt"] or "").strip()
    if t["kind"] == "until":
        return (
            head + body + "\n\n"
            f"(Goal: {t.get('until_goal')}.) This is an automatic recurring check by the control plane. "
            "If the goal is COMPLETE → give the final report and emit "
            '<schedule_stop reason="..."/> to end the loop. If something FAILED → fix it and continue. '
            "If still in progress → record concrete progress to state/progress.md (so a fresh session can "
            "recover) and you will be re-invoked next interval. Do NOT emit a new <schedule> tag."
        )
    return (head + body + "\n\n"
            "(Automatic scheduled run by the control plane. Do NOT emit a new <schedule> tag.)")


async def _run_scheduled_fire(t: dict) -> None:
    tid, slug, agent_id = t["id"], t["project_slug"], t["agent_id"]
    try:
        n = t["runs_done"] + 1
        run = _start_run(slug, agent_id, _wrap_schedule_prompt(t, n), origin=f"schedule:{tid}")
        await run.publish({"type": "schedule_fired", "agent": agent_id, "id": tid,
                           "run": run.id, "n": n, "max": t.get("max_runs")})
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        status = db.get_last_status(slug, agent_id) or "ok"
        cur = db.get_scheduled_task(tid)
        stopped_mid = (cur is None) or (not cur["active"])   # e.g. <schedule_stop> this round
        reached_ceiling = bool(t.get("max_runs")) and n >= t["max_runs"]
        if t["kind"] == "once" or stopped_mid or reached_ceiling:
            db.record_scheduled_fire(tid, None, status)
            if reached_ceiling and t["kind"] == "until" and not stopped_mid:
                await run.publish({"type": "schedule_exhausted", "agent": agent_id,
                                   "id": tid, "n": n})
            elif t["kind"] == "once":
                await run.publish({"type": "schedule_done", "agent": agent_id,
                                   "id": tid, "reason": "one-shot complete"})
        else:
            interval = t["interval_seconds"] or _SCHED_INTERVAL_FLOOR_S
            db.record_scheduled_fire(tid, time.time() + interval, status)
    except Exception as e:  # never let one bad fire kill the loop
        print(f"[scheduler] fire {tid} error: {e}")
    finally:
        _sched_inflight.discard(tid)


def _scheduler_enabled() -> bool:
    return db.get_setting("scheduler_enabled", "1") == "1"


def _skip_overdue_on_resume() -> None:
    """Called on the disabled→enabled transition. Anything that came due while
    the scheduler was off must NOT be replayed: push overdue interval/until
    tasks to `now + interval` (resume the next cycle only), retire overdue
    one-shots (their moment has passed)."""
    now = time.time()
    pushed = retired = 0
    for t in db.get_due_scheduled_tasks(now):   # active AND next_run_at <= now
        if t.get("kind") == "once":
            db.set_scheduled_active(t["id"], False)
            retired += 1
        else:
            interval = t.get("interval_seconds") or _SCHED_INTERVAL_FLOOR_S
            db.defer_scheduled_task(t["id"], now + interval)
            pushed += 1
    if pushed or retired:
        print(f"[scheduler] resume: deferred {pushed} overdue task(s) to next "
              f"cycle, retired {retired} one-shot(s)")


async def _scheduler_tick() -> None:
    if not _scheduler_enabled():
        return
    now = time.time()
    fired_agents: set = set()
    for t in db.get_due_scheduled_tasks(now):
        tid, slug, agent_id = t["id"], t["project_slug"], t["agent_id"]
        if tid in _sched_inflight:
            continue
        # zombie guard: untouched too long → retire
        if now - (t.get("updated_at") or t["created_at"]) > _SCHED_ZOMBIE_DAYS * 86400:
            db.set_scheduled_active(tid, False)
            continue
        # agent removed from the project → retire
        if not projects.get_agent(slug, agent_id):
            db.set_scheduled_active(tid, False)
            continue
        # one active run per agent: skip-on-busy (and ≤1 fire/agent/tick) → short retry
        key = (slug, agent_id)
        if key in fired_agents or _active_run(slug, agent_id):
            interval = t["interval_seconds"] or _SCHED_INTERVAL_FLOOR_S
            db.defer_scheduled_task(tid, now + min(interval, 120))
            continue
        fired_agents.add(key)
        _sched_inflight.add(tid)
        asyncio.create_task(_run_scheduled_fire(t))


async def _scheduler_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await _scheduler_tick()
        except Exception as e:
            print(f"[scheduler] tick error: {e}")


_COMPACT_PROMPT = (
    "[CONTROL-PLANE COMPACT — not a normal task] Summarise all of this session's work & conversation "
    "into one concise RECAP so that YOU YOURSELF can continue in a new session with less "
    "context. Include: current state, decisions locked in + brief reasons, artifacts/versions "
    "in use (path + version), work in progress, open questions / items waiting on the user. Do NOT repeat "
    "verbatim — keep only the minimum information needed to continue seamlessly. Do NOT dispatch. "
    "Output ONLY the recap (markdown), no preamble."
)


@app.post("/api/projects/{slug}/agents/{agent_id}/compact")
async def api_compact(slug: str, agent_id: str):
    """Compact a long session: the agent summarises its own context, then a fresh
    session is created seeded with that recap (prepended to its next turn). The
    old session/history stays in the db; the new one starts with small context."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(evt):
        await queue.put(evt)

    tracker: list = []

    async def driver():
        try:
            summary = await _run_agent(slug, agent_id, _COMPACT_PROMPT, emit, tracker)
            if tracker:
                await asyncio.gather(*tracker, return_exceptions=True)
            summary = (summary or "").strip()
            if not summary:
                await queue.put({"type": "error", "agent": agent_id,
                                 "message": "compact: recap empty — no new session created"})
            else:
                new_sess = db.new_session(slug, agent_id)
                db.set_session_seed(new_sess["id"], summary)
                db.add_message(
                    new_sess["id"], "assistant",
                    "📦 **Context compacted** — new session seeded with the recap below. "
                    "The next turn continues from this recap (smaller context).\n\n---\n\n" + summary,
                )
                db.update_session_status(new_sess["id"], "ok")
                await queue.put({"type": "compacted", "agent": agent_id,
                                 "new_session_id": new_sess["id"]})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await queue.put({"type": "error", "agent": agent_id, "message": str(e)})
        finally:
            await queue.put(None)

    async def sse():
        yield _sse({"type": "start", "agent": agent_id, "mode": "compact"})
        task = asyncio.create_task(driver())
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if evt is None:
                    break
                yield _sse(evt)
            yield _sse({"type": "complete"})
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# UI-only read-only side panels (cluster jobs + subscription usage).
# These are pure projections — they NEVER touch agent sessions, the dispatch
# ledger, or any orchestration state.
# ---------------------------------------------------------------------------

_CLUSTER_NAMES = ["ascend", "cardinal", "pitzer"]


@app.get("/api/cluster/jobs")
async def api_cluster_jobs():
    """SLURM queue across all federated clusters via `squeue --clusters=all`.
    Returns {clusters: {name: [job,...]}, error?}. Read-only."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    fmt = "%i|%P|%j|%t|%M|%D|%R|%C"
    clusters: dict[str, list] = {c: [] for c in _CLUSTER_NAMES}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "squeue", "--clusters=all", "-u", user, "-o", fmt,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=12)
    except FileNotFoundError:
        return {"clusters": clusters, "error": "squeue not found on PATH"}
    except asyncio.TimeoutError:
        if proc is not None:
            try: proc.kill()
            except Exception: pass
        return {"clusters": clusters, "error": "squeue timed out (12s)"}
    except Exception as e:
        return {"clusters": clusters, "error": f"squeue failed: {e}"}

    cur = None
    for line in out.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("CLUSTER:"):
            cur = s.split(":", 1)[1].strip()
            clusters.setdefault(cur, [])
            continue
        if s.startswith("JOBID|") or "|" not in s:
            continue
        p = s.split("|")
        if len(p) < 4:
            continue
        clusters.setdefault(cur or "unknown", []).append({
            "id": p[0], "partition": p[1], "name": p[2], "state": p[3],
            "time": p[4] if len(p) > 4 else "", "nodes": p[5] if len(p) > 5 else "",
            "reason": p[6] if len(p) > 6 else "", "cpus": p[7] if len(p) > 7 else "",
        })

    if proc.returncode and not any(clusters.values()):
        msg = err.decode("utf-8", errors="replace")[:200].strip()
        return {"clusters": clusters, "error": msg or "squeue returned non-zero"}
    return {"clusters": clusters}


@app.get("/api/usage")
async def api_usage():
    """Best-effort Claude subscription usage. Reads the existing OAuth bearer from
    ~/.claude/.credentials.json (subscription auth, NOT an ANTHROPIC_API_KEY) and pulls
    the anthropic-ratelimit-unified-* headers off a cheap authenticated GET. Returns
    {available, five_hour:{pct,reset_at}, weekly:{pct,reset_at}} or {available:false}.
    The token is never logged or returned."""
    def _fetch():
        import urllib.request
        import urllib.error
        cred = Path.home() / ".claude" / ".credentials.json"
        try:
            tok = (json.loads(cred.read_text()).get("claudeAiOauth") or {}).get("accessToken")
        except Exception:
            return {"available": False, "reason": "no credentials"}
        if not tok:
            return {"available": False, "reason": "no token"}
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {tok}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
                "User-Agent": "AgentUI-usage/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"available": False, "reason": str(e)[:120]}

        def _period(block):
            d = body.get(block)
            if not isinstance(d, dict):
                return None
            u = d.get("utilization")
            try: pct = round(float(u), 1)  # already 0..100
            except Exception: pct = None
            reset = None
            ra = d.get("resets_at")
            if ra:
                try:
                    reset = int(datetime.fromisoformat(str(ra)).timestamp())
                except Exception:
                    reset = None
            return {"pct": pct, "reset_at": reset}

        five, week = _period("five_hour"), _period("seven_day")
        if five is None and week is None:
            return {"available": False, "reason": "no usage fields"}
        return {"available": True, "five_hour": five, "weekly": week}

    return await asyncio.to_thread(_fetch)


# ---------------------------------------------------------------------------
# Terminal dock — real PTY shells over WebSocket (xterm.js front-end).
# Isolated subsystem: its own process registry, never touches agent sessions.
# Safeguards: per-server cap, kill-on-disconnect, idle reap, kill-all on
# shutdown. Server binds 127.0.0.1 only (run.sh), so shells are local-only.
# ---------------------------------------------------------------------------

_MAX_TERMINALS = 6
_TERM_IDLE_S = 1800  # reap a shell with no I/O for 30 min
_terminals: dict[str, dict] = {}  # id -> {pid, fd, last_io}


def _term_kill(tid: str):
    t = _terminals.pop(tid, None)
    if not t:
        return
    try:
        os.kill(t["pid"], signal.SIGHUP)
    except Exception:
        pass
    try:
        os.kill(t["pid"], signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(t["fd"])
    except Exception:
        pass
    try:
        os.waitpid(t["pid"], os.WNOHANG)
    except Exception:
        pass


@app.websocket("/api/terminal/ws")
async def api_terminal_ws(ws: WebSocket):
    """One PTY-backed `bash` login shell per connection. Frontend protocol (JSON text):
    {"t":"i","d":"<keystrokes>"} input, {"t":"r","cols":C,"rows":R} resize.
    Server streams raw shell bytes back as binary frames."""
    await ws.accept()
    if len(_terminals) >= _MAX_TERMINALS:
        await ws.send_text("\r\n[terminal limit reached — close another terminal first]\r\n")
        await ws.close()
        return

    tid = uuid.uuid4().hex[:8]
    pid, fd = pty.fork()
    if pid == 0:
        # Child: become the shell. (pty.fork already set up the controlling tty.)
        os.environ["TERM"] = "xterm-256color"
        # Give the user a clean login shell — don't leak AgentUI's own venv
        # (VIRTUAL_ENV + its PATH prefix + its PS1) into their terminal, otherwise
        # the prompt shows just "(.venv) " with no cwd instead of their normal one.
        venv = os.environ.pop("VIRTUAL_ENV", None)
        os.environ.pop("VIRTUAL_ENV_PROMPT", None)
        os.environ.pop("PS1", None)
        if venv:
            kept = [p for p in os.environ.get("PATH", "").split(":")
                    if p and not p.startswith(venv)]
            os.environ["PATH"] = ":".join(kept)
        os.chdir(os.path.expanduser("~"))
        try:
            os.execvp("bash", ["bash", "-l", "-i"])
        except Exception:
            os.execvp("sh", ["sh", "-i"])
        os._exit(127)

    _terminals[tid] = {"pid": pid, "fd": fd, "last_io": time.time()}
    loop = asyncio.get_event_loop()

    async def pump_out():
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
            except OSError:
                break
            if not data:
                break
            _terminals.get(tid, {}).update(last_io=time.time())
            try:
                await ws.send_bytes(data)
            except Exception:
                break
        try:
            await ws.close()
        except Exception:
            pass

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("t")
            if t == "i":
                try:
                    os.write(fd, (msg.get("d") or "").encode("utf-8"))
                    _terminals.get(tid, {}).update(last_io=time.time())
                except OSError:
                    break
            elif t == "r":
                try:
                    cols = int(msg.get("cols") or 80)
                    rows = int(msg.get("rows") or 24)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        out_task.cancel()
        _term_kill(tid)


async def _terminal_reaper():
    while True:
        await asyncio.sleep(120)
        now = time.time()
        for tid, t in list(_terminals.items()):
            if now - t.get("last_io", now) > _TERM_IDLE_S:
                _term_kill(tid)


@app.on_event("startup")
async def _start_terminal_reaper():
    asyncio.create_task(_terminal_reaper())


@app.on_event("startup")
async def _start_scheduler():
    asyncio.create_task(_scheduler_loop())
    # Telegram control channel (BOSS by default). No-op when no bot token is
    # configured — runs inside this loop alongside the scheduler.
    if tg.is_enabled():
        asyncio.create_task(tg.start_telegram())


@app.on_event("shutdown")
async def _kill_all_terminals():
    for tid in list(_terminals.keys()):
        _term_kill(tid)
    if tg.is_enabled():
        await tg.stop_telegram()


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that always sends Cache-Control: no-store so the browser
    never serves a stale app.js/styles.css during development (the recurring
    'I edited app.js but the UI shows the old behavior' failure)."""
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


if FRONTEND_DIR.exists():
    app.mount("/", _NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
