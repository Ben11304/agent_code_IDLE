from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, projects
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

# Step-by-step protocol tags, parsed from the stream like <dispatch>. <plan>
# declares (or replaces) the agent's step list; <step> marks one step's status.
# Persisted to the agent's own `state/plan.md` so the plan survives sessions.
PLAN_RE = re.compile(r'<plan>([\s\S]*?)</plan>', re.IGNORECASE)
STEP_RE = re.compile(
    r'<step\s+n="(\d+)"\s+status="(pending|doing|done|blocked)"\s*(?:/>|>([\s\S]*?)</step>)',
    re.IGNORECASE,
)

_PLAN_LINE_RE = re.compile(r"^\s*(\d+)\.\s*\[([ x~!])\]\s*(.*)$")
_PLAN_STATUS_MARK = {"pending": " ", "doing": "~", "done": "x", "blocked": "!"}
_MARK_TO_STATUS = {" ": "pending", "~": "doing", "x": "done", "!": "blocked"}

_PLAN_INSTRUCTIONS = (
    "\n\n## Step-by-step protocol (control-plane parsed — MANDATORY for multi-step tasks)\n"
    "If the task has ≥2 distinct steps: emit the plan BEFORE starting work, as a tag:\n"
    "<plan>\n1. step one\n2. step two\n</plan>\n"
    "Then IMMEDIATELY AFTER finishing/starting/getting stuck on each step, emit:\n"
    "<step n=\"1\" status=\"done\">one-line note (optional)</step>\n"
    "Valid status: doing | done | blocked. The control plane parses the tags in real time, persists them to "
    "`state/plan.md` (survives sessions) and shows progress on the graph — narrating steps without emitting "
    "the tag is INVISIBLE to the user. When you wake up in a new session and `state/plan.md` still has an "
    "unfinished step → CONTINUE from that step; do NOT re-plan unless the user asks."
)


def _extract_plan_steps(body: str) -> list[str]:
    steps = []
    for ln in body.splitlines():
        s = re.sub(r"^(?:\d+[.)]|[-*•])\s*", "", ln.strip()).strip()
        if s:
            steps.append(s)
    return steps


def _write_plan_file(path: Path, agent_id: str, steps: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Plan — {agent_id}",
        "_(control-plane managed — updated via `<plan>`/`<step>` tags, do not edit by hand)_",
        "",
    ]
    lines += [f"{i}. [ ] {s}" for i, s in enumerate(steps, 1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_plan_step(path: Path, n: int, status: str, note: str = "") -> bool:
    mark = _PLAN_STATUS_MARK.get(status)
    if mark is None:
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for i, ln in enumerate(lines):
        m = _PLAN_LINE_RE.match(ln)
        if m and int(m.group(1)) == n:
            text = m.group(3)
            if note:
                text = f"{text} — {note}"
            lines[i] = f"{m.group(1)}. [{mark}] {text}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def _read_plan(path: Path) -> Optional[dict]:
    """Parse plan.md → progress summary for the stats panel / preamble."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    steps = []
    for ln in lines:
        m = _PLAN_LINE_RE.match(ln)
        if m:
            steps.append({
                "n": int(m.group(1)),
                "status": _MARK_TO_STATUS.get(m.group(2), "pending"),
                "text": m.group(3),
            })
    if not steps:
        return None
    done = sum(1 for s in steps if s["status"] == "done")
    cur = (next((s for s in steps if s["status"] in ("doing", "blocked")), None)
           or next((s for s in steps if s["status"] == "pending"), None))
    return {
        "total": len(steps),
        "done": done,
        "blocked": any(s["status"] == "blocked" for s in steps),
        "current": f'{cur["n"]}. {cur["text"]}' if cur else None,
    }

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
        blocks.append(
            f'<dispatch_result from="{r["target_agent"]}"{status_attr}>\n'
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
        "real data; **do NOT pretend you are still waiting for results**. If a result "
        "is incomplete or in error/cancelled status, decide whether to retry, escalate, "
        "or report to the user."
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
        a["default_effort"] = a.get("effort")
        if ov.get("claude_model"):
            a["claude_model"] = ov["claude_model"]
        if ov.get("grok_model"):
            a["grok_model"] = ov["grok_model"]
        if "effort" in ov:
            a["effort"] = ov["effort"]
    out["statuses"] = statuses
    out["positions"] = db.get_node_positions(slug)
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
_CONTEXT_WINDOWS = {"claude": 200_000, "grok": 256_000}


def _usage_ctx_tokens(usage: dict) -> int:
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
    return _req_total(usage)


def _session_context_pct(sess: Optional[dict], model_kind: str) -> float:
    """Context % of a session's last completed turn, 0.0 when unknown (no usage
    recorded yet — fresh session, or a grok node which reports no usage)."""
    if not sess or not sess.get("usage"):
        return 0.0
    try:
        usage = json.loads(sess["usage"])
    except (ValueError, TypeError):
        return 0.0
    window = _CONTEXT_WINDOWS.get(model_kind, 200_000)
    if not window:
        return 0.0
    return round(_usage_ctx_tokens(usage) / window * 100.0, 1)


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
        else:
            eff_model = ov.get("claude_model") or a.get("claude_model") or "claude-sonnet-4-6"
        effort = ov.get("effort") if (ov and "effort" in ov) else a.get("effort")

        window = _CONTEXT_WINDOWS.get(model_kind, 200_000)
        # Prefer the CLI's real token usage from the last turn. Context-window
        # occupancy = every input bucket (fresh + cache create + cache read) +
        # output. Fall back to a chars/4 estimate only when no usage is recorded
        # yet (e.g. a session that has never completed a turn, or a grok node).
        if usage:
            ctx_tokens = _usage_ctx_tokens(usage)
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
            "plan": _read_plan(_agent_dir(root, a, cwd_abs) / "state" / "plan.md"),
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
    effort: Optional[str] = None


_AGENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class NewAgent(BaseModel):
    id: str
    role: Optional[str] = ""
    model: str = "claude"
    claude_model: Optional[str] = None
    grok_model: Optional[str] = None
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
    if body.model not in ("claude", "grok"):
        raise HTTPException(400, "model must be 'claude' or 'grok'")
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
        if a.get("model", "claude") not in ("claude", "grok"):
            raise HTTPException(400, f"model must be claude|grok: {a.get('id')}")
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
                          effort=body.effort)
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

    plan_p = adir / "state" / "plan.md"
    if plan_p.is_file() and _read_plan(plan_p):
        ex = _read_capped(plan_p, 1200)
        if ex:
            parts.append(
                "### Unfinished plan (`state/plan.md`) — CONTINUE from the unfinished step, do not re-plan\n" + ex
            )

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

    man = adir / "inputs" / "manifest.md"
    if man.is_file():
        ex = _read_capped(man, 1200)
        if ex:
            parts.append(f"### Input contract (`inputs/manifest.md`)\n{ex}")

    if not parts:
        return ""
    return (
        "[CONTROL-PLANE COLD-START] New CLI session (the previous session could not be resumed). "
        "Below is your persistent state — read it before acting:\n\n"
        + "\n\n".join(parts)
    )


async def _run_agent(
    slug: str,
    agent_id: str,
    message: str,
    emit,
    tracker: list,
    chain: tuple = (),
    grok_options: Optional[dict] = None,
) -> str:
    """Run a single agent chat turn. Emit events via callback.

    Recursive: if agent is an orchestrator (has children), parse dispatch tags
    from streamed text and fire worker dispatches as background tasks.
    `chain` tracks ancestors to prevent infinite loops.
    """
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

    system_prompt = projects.resolve_system_prompt(project["root"], agent.get("system_prompt_file", ""))
    cwd = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    children = _get_children(project, agent_id)
    if children:
        system_prompt = (system_prompt or "") + _dispatch_instructions(children)
    system_prompt = (system_prompt or "") + _PLAN_INSTRUCTIONS
    plan_path = _agent_dir(project["root"], agent, cwd) / "state" / "plan.md"

    model = agent.get("model", "claude")
    stream_fn = get_stream(model)
    override = db.get_agent_override(slug, agent_id) or {}
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
    else:
        agen = stream_fn(message=message, system_prompt=system_prompt, cwd=cwd)

    assembled: list[str] = []
    buf = ""
    dispatched: set = set()
    plan_seen: set = set()
    step_seen: set = set()
    final_status = "ok"
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
                # plan / step tags → persist to state/plan.md + notify graph
                for m in PLAN_RE.finditer(buf):
                    if m.start() in plan_seen:
                        continue
                    plan_seen.add(m.start())
                    steps = _extract_plan_steps(m.group(1))
                    if steps:
                        try:
                            _write_plan_file(plan_path, agent_id, steps)
                            await emit({"type": "plan_updated", "agent": agent_id, "total": len(steps)})
                        except OSError:
                            pass
                for m in STEP_RE.finditer(buf):
                    if m.start() in step_seen:
                        continue
                    step_seen.add(m.start())
                    note = (m.group(3) or "").strip()
                    if len(note) > 200:
                        note = note[:200] + "…"
                    if _set_plan_step(plan_path, int(m.group(1)), m.group(2).lower(), note):
                        await emit({"type": "plan_step", "agent": agent_id,
                                    "n": int(m.group(1)), "status": m.group(2).lower()})
                await emit({"type": "delta", "agent": agent_id, "text": text})
            elif etype == "meta":
                data = evt.get("data") or {}
                if data.get("claude_session_id"):
                    db.set_claude_session_id(sess["id"], data["claude_session_id"])
                if data.get("usage"):
                    db.set_session_usage(sess["id"], data["usage"])
                await emit({"type": "meta", "agent": agent_id, "data": data})
            elif etype == "thinking":
                await emit({"type": "thinking", "agent": agent_id, "text": evt.get("text", "")})
            elif etype == "status":
                await emit({"type": "status", "agent": agent_id, "status": evt.get("status", "")})
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
# 80% leaves enough headroom for the summary turn itself to complete.
_AUTO_COMPACT_PCT = 80.0

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
    pct = _session_context_pct(sess, agent.get("model", "claude"))
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
# table, dispatch ledger, plan.md) exactly as if the tab had stayed open.
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


@app.post("/api/projects/{slug}/agents/{agent_id}/chat")
async def api_chat(slug: str, agent_id: str, body: ChatBody):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    _prune_runs()
    existing = _active_run(slug, agent_id)
    if existing:
        raise HTTPException(409, f"a turn is already running for {agent_id} (run {existing.id})")

    run = _Run(slug, agent_id)
    _RUNS[run.id] = run
    emit = run.publish

    grok_options = {
        "best_of_n": body.best_of_n,
        "check_loop": body.check_loop,
        "memory_mode": body.memory_mode,
    } if (body.best_of_n or body.check_loop or body.memory_mode) else None

    async def driver():
        # Multi-round continuation: after each wave of dispatches lands in the
        # ledger, the orchestrator gets another turn to react — synthesise, or
        # chain follow-up dispatches — inside the SAME run, up to
        # _MAX_CONT_ROUNDS. Dispatches fired on the final round still run to
        # completion (results land in the ledger for the NEXT user turn); they
        # just don't trigger another continuation.
        all_tasks: list = []
        cur: list = []
        try:
            await _auto_compact_if_needed(slug, agent_id, emit)
            await _run_agent(slug, agent_id, body.message, emit, cur,
                             grok_options=grok_options)
            rounds = 0
            while cur and rounds < _MAX_CONT_ROUNDS:
                await asyncio.gather(*cur, return_exceptions=True)
                all_tasks.extend(cur)
                rounds += 1
                last = rounds >= _MAX_CONT_ROUNDS
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
                await _run_agent(slug, agent_id, synth, emit, cur)
            if cur:
                await asyncio.gather(*cur, return_exceptions=True)
                all_tasks.extend(cur)
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


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
