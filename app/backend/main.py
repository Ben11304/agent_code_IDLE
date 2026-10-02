from __future__ import annotations

import asyncio
import fnmatch
import glob as globlib
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import capabilities, db, projects, tokens, progress_store, agent_log, fixed_cost, notion_settings
from . import terminal_service
from . import telegram_bot as tg
from .adapters import _codex_sdk_settings, get_stream
from .langsmith_tracing import trace_agent_turn, trace_dispatch, trace_orchestration
from notion_report.config import SYSTEM_AGENT_ID, SYSTEM_PROJECT_SLUG
# Project-owned contracts carry structural requirements, native-table contracts,
# and reviewer-facing scientific guidance; backend reload refreshes the loader.
from notion_report.schema import ReportSchemaError, load_report_schema

TREE_EXCLUDE = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".DS_Store", ".vscode", ".idea", ".pytest_cache", ".mypy_cache",
    ".ipynb_checkpoints", "dist", "build", ".cache",
}

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# The user-provided asset lives at the repository root (`agent_code_IDLE/logo`),
# one level above `app/`.  Keep the API URL stable so the graph never needs to
# know the on-disk layout.
NOTION_LOGO_PATH = FRONTEND_DIR.parent.parent / "logo" / "Notion_app_logo.png"
NOTION_REPORT_CONFIG_PATH = FRONTEND_DIR.parent.parent / "notion_report" / "destinations.json"

_NOTION_AGENT_INSTRUCTIONS = """

## AgentUI Notion contract — control-plane enforced
- Use only the `agentui_notion_report` MCP tools. Never search for, suggest,
  install, or ask the user to connect a separate Notion plugin from an AgentUI turn.
- Project and agent identity are injected by the control plane. Never infer or
  supply a project slug or agent id to a Notion tool.
- "All Notion" means the complete subtree below this project's bound parent,
  not the user's whole workspace. Use `list_notion_project_tree`, then
  `read_notion_project_page` for each relevant page.
- For broad rewrites, complete a read-only inventory/audit first. Use
  `sync_notion_managed_report` with `dry_run=true` to show the plan, and write
  only after user authorization. Never overwrite manual content outside the
  AgentUI-managed section.
- Report success only after `read_back_verified=true`. If a project has no binding,
  the connector auto-creates its subtree below the verified AgentUI System Hub.
  If that hub is absent, request the one System Hub setup action; never request a
  separate plugin or credential setup for each project/agent.
"""

app = FastAPI(title="AgentUI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/ui-assets/notion-logo")
def api_notion_logo():
    """Serve the user-provided Notion logo used by graph telemetry badges."""
    if not NOTION_LOGO_PATH.is_file():
        raise HTTPException(404, "Notion logo not found")
    return FileResponse(NOTION_LOGO_PATH, media_type="image/png")

db.init_db()
# Snapshot the sessions the previous process left mid-turn BEFORE the reaper flips
# them to 'cancelled'. Their turn's finally-booking never ran, so their transcript
# tail is unbooked — the crash leg of the rotation-loss fix. The actual booking runs
# at the BOTTOM of this module (_startup_token_sweep) because _book_session_tokens
# is not defined yet at this point.
_orphan_sessions = db.list_running_sessions()
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

# This contract is intentionally injected into EVERY primary agent turn. A
# scheduler submission is already asynchronous; the recurring production bug was
# the model starting a foreground `while squeue ...; sleep ...` tool immediately
# afterwards. That shell keeps the provider CLI alive, which in turn keeps the
# AgentUI run active and trips the one-active-run-per-agent 409 guard. Keep this
# compact and unconditional: a request that merely says "run the experiment" can
# still cause the model to submit a batch job and decide to wait for it.
_SLURM_HANDOFF_INSTRUCTIONS = (
    "\n\n## SLURM handoff — batch jobs must never occupy an AgentUI turn\n"
    "`sbatch` is asynchronous. As soon as it succeeds, capture the cluster and job ID, "
    "report them, and release the current turn. The SLURM job continues independently "
    "after the turn, browser, terminal, or AgentUI closes.\n"
    "- NEVER wait for a submitted job inside a tool call: no `while`/`until`/`for` polling "
    "with `squeue` or `sacct`, no `watch`, no `tail -f`/`tail -F`, no sleep-and-check loop, "
    "and no foreground wait for job completion. Do not hide such a poller behind `nohup`, "
    "`&`, tmux, or another detached shell.\n"
    "- A one-shot `squeue`/`sacct` query is allowed when checking the status NOW. Return "
    "after that single snapshot.\n"
    "- If ongoing monitoring was requested and the AgentUI scheduling contract is present, "
    "emit a real `<schedule>` goal loop whose individual turns perform one status snapshot. "
    "If scheduling is unavailable, say that plainly; the user can inspect Process running or "
    "ask for status later. Never keep this turn open as the monitoring mechanism.\n"
)

_OFFLINE_EVALUATION_INSTRUCTIONS = (
    "\n\n## Offline evaluation safety boundary\n"
    "This turn is running in a disposable evaluation workspace. Do not access "
    "network services, Notion, SSH, SLURM (`sbatch`, `srun`, `scancel`), or paths "
    "outside the current project clone. Do not create background jobs or schedules. "
    "Perform only the requested evaluation behavior and keep every file write inside "
    "the disposable clone.\n"
)

_SCHEDULE_INSTRUCTIONS = (
    "\n\n## ⏰ Recurring / deferred work — emit <schedule>, NEVER narrate a loop (control-plane parsed)\n"
    "You CANNOT run background timers, cron jobs, pollers, heartbeats, or detached processes. A turn ends and "
    "your CLI process EXITS. The ONLY thing that can ever wake you up again is a `<schedule>` tag parsed by the "
    "control plane. No tag = nothing runs = you will NEVER be re-invoked.\n\n"
    "**DO NOT invoke the `schedule` Agent Skill (or any cron/routine skill) to satisfy this.** That skill creates "
    "a cloud routine that runs on Anthropic's servers with NO access to this machine (no SLURM/`sacct`/`ssh`, no "
    "local files) and cannot be shown in this UI's 🕒 Schedules dropdown. For recurring work HERE, the ONLY correct "
    "mechanism is emitting the `<schedule>` tag below — it runs on THIS host (full shell, SLURM, files) and shows "
    "up in the UI. Ignore the skill's matching name; it is the wrong tool for tracking work on this machine.\n\n"
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
    # EVERY alternative here must require scheduling CONTEXT, never a bare noun. A single
    # loose word turns ordinary prose into a phantom schedule request: the word "recurring"
    # in a paper's "six recurring failure patterns" fired this, so the driver sent BOSS a
    # SCHEDULE_NUDGE, BOSS spent its final synthesis turn retracting a promise it never made,
    # and the user's actual question went unanswered. Bare "schedule" / "cron" / "<n> p" were
    # the same trap ("the project schedule", "15 p"). Keep the verbs and the cadences; the
    # cost of a miss (user re-asks) is far below the cost of a false fire (answer destroyed).
_TRACK_INTENT_RE = re.compile(
    r"(track this|track it|track the|keep me posted|keep an eye|"
    r"monitor\s+(this|it|the|a|my|job|jobs|run|queue|training|experiment)\b|"
    r"(run|check|report|update|ping|poll)\s+(me\s+)?periodically|"
    r"recurring\s+(run|check|task|job|report|schedule|turn|reminder)|"
    r"(set|create|add|make|start)\s+(up\s+)?(a\s+)?(schedule|cron)\b|cron\s+job|"
    r"every\s+\d+\s*(m|min|minute|mins|minutes|h|hr|hour|hours)\b|"
    # explicit "schedule" verbs (EN + VI): "set a schedule", "lên lịch", "lên schedule",
    # "tạo/đặt lịch", "lập lịch" — the phrasings that previously slipped through and made
    # the agent reach for the cloud `schedule` skill instead of emitting a <schedule> tag.
    r"l[êe]n\s+(l[ịi]ch|schedule)|l[ậa]p\s+l[ịi]ch|t[ạa]o\s+(l[ịi]ch|schedule)|đ[ặa]t\s+l[ịi]ch|"
    r"theo\s*d[õo]i|đ[ịi]nh\s*k[ỳy]|b[áa]o\s*c[áa]o\s*đ[ịi]nh\s*k[ỳy]|"
    # cadence: "mỗi 30 phút" / "mỗi 30p" — anchored on "mỗi", so a bare "15 p" in prose
    # (which used to match) no longer fires.
    r"m[ỗo]i\s+\d+\s*(ph[úu]t|gi[ờo]|p|h|m)\b|"
    r"cho\s+đ[ếe]n\s+khi\s+xong|t[ựu]\s+ch[ạa]y|until\s+(it'?s\s+)?(done|finished|complete|over))",
    re.IGNORECASE)

# A submission request should receive the scheduler syntax even when it does not
# literally contain "monitor". The handoff contract above still forbids waiting;
# this merely gives the agent the correct control-plane alternative if the task
# naturally needs a later completion check.
_SLURM_SUBMIT_INTENT_RE = re.compile(
    r"(\bsbatch\b|\bsubmit(?:ting)?\s+(?:a\s+|the\s+)?(?:slurm\s+|batch\s+)?job\b|"
    r"\bsubmit(?:ting)?\s+(?:the\s+)?(?:run|training|experiment)\b|"
    r"(?:ch[ạa]y|g[ửu]i|submit)\s+(?:m[ộo]t\s+)?job\b)",
    re.IGNORECASE,
)

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
    - User message shows tracking/recurring/monitoring or SLURM-submit intent
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
    if _SLURM_SUBMIT_INTENT_RE.search(message or ""):
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
    # Reload the project-owned contract on every project fetch. This lets an
    # authorized SCHEMA NOTE become visible without restarting AgentUI.
    try:
        loaded_report_schema = load_report_schema(
            p["root"], p.get("notion_report"))
    except ReportSchemaError as exc:
        loaded_report_schema = {"valid": False, "error": str(exc)}
    if loaded_report_schema:
        out["report_schema"] = {
            key: value for key, value in loaded_report_schema.items()
            if key != "path_abs"
        }
    statuses = {}
    overrides = db.list_agent_overrides(slug)
    for a in out["agents"]:
        statuses[a["id"]] = db.get_last_status(slug, a["id"]) or "idle"
        ov = overrides.get(a["id"]) or {}
        a["default_claude_model"] = a.get("claude_model") or "claude-sonnet-4-6"
        a["default_grok_model"] = a.get("grok_model") or "grok-build"
        a["default_deepseek_model"] = a.get("deepseek_model") or "deepseek-v4-flash"
        a["default_glm_model"] = a.get("glm_model") or "glm-4.6"  # glm-4.6 | glm-5.2
        a["default_codex_model"] = a.get("codex_model") or "gpt-5.6-terra"
        a["default_effort"] = a.get("effort")
        if ov.get("claude_model"):
            a["claude_model"] = ov["claude_model"]
        if ov.get("grok_model"):
            a["grok_model"] = ov["grok_model"]
        if ov.get("deepseek_model"):
            a["deepseek_model"] = ov["deepseek_model"]
        if ov.get("glm_model"):
            a["glm_model"] = ov["glm_model"]
        if ov.get("codex_model"):
            a["codex_model"] = ov["codex_model"]
        if ov.get("model"):
            a["model"] = ov["model"]  # adapter override wins over project.yaml
        if "effort" in ov:
            a["effort"] = ov["effort"]
    # The destination registry is the routing source of truth. Mirror its URL
    # into the project payload so the graph's Notion badge opens the exact bound
    # (or auto-provisioned) project page instead of a stale YAML/default link.
    root_agent = next(
        (item for item in out["agents"] if not (item.get("parents") or [])),
        None,
    )
    if root_agent:
        try:
            notion = notion_settings.get_notion_settings(
                NOTION_REPORT_CONFIG_PATH, slug, root_agent["id"])
        except ValueError:
            notion = {}
        notion_url = str(notion.get("notion_url") or "").strip()
        if notion.get("configured") and notion_url:
            root_agent["notion_url"] = notion_url
            resources = dict(out.get("resources") or {})
            current = resources.get("notion")
            notion_resource = dict(current) if isinstance(current, dict) else {}
            notion_resource["url"] = notion_url
            resources["notion"] = notion_resource
            out["resources"] = resources
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


_PROJECT_PAPER_LIMIT = 500
_MODEL_EVIDENCE_LIMIT = 20


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, Path)) and str(v).strip()]
    return []


def _resource_path_allowed(path: Path) -> bool:
    """Resource-panel files must remain inside the configured workspace tree.

    This mirrors the file viewer's boundary so every item returned by this API
    is safe to open with `/api/workspace/file` or `/api/workspace/raw`.
    """
    workspace = projects.get_workspace_root()
    if not workspace:
        return False
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except (OSError, ValueError):
        return False


def _workspace_resource_item(path: Path) -> dict | None:
    workspace = projects.get_workspace_root()
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(workspace.resolve()) if workspace else None
    except (OSError, ValueError):
        return None
    if rel is None or not resolved.is_file():
        return None
    return {
        "id": hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:16],
        "name": resolved.stem.replace("_", " ").replace("-", " "),
        "filename": resolved.name,
        "abs_path": str(resolved),
        "rel_path": str(rel),
        "availability": "local",
    }


_PAPER_CATALOG_ENTRY_RE = re.compile(r"^\s*-\s+\*\*([^*]+)\*\*\s+[—-]\s+(.+?)\s*$")
_PAPER_URL_RE = re.compile(r"https?://[^\s)>]+")


def _paper_match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _catalog_papers(project: dict, catalog_paths) -> list[dict]:
    """Read the canonical Markdown catalog without pretending metadata is a PDF.

    Catalog rows use ``- **ShortID** — citation``.  The stable ShortID also lets
    telemetry associate a DOI/arXiv/web access with the corresponding panel row.
    """
    project_root = Path(project["root"]).resolve()
    workspace = projects.get_workspace_root()
    out: list[dict] = []
    seen: set[str] = set()
    for raw in _string_list(catalog_paths):
        catalog = Path(raw).expanduser()
        if not catalog.is_absolute():
            catalog = project_root / catalog
        try:
            catalog = catalog.resolve()
        except OSError:
            continue
        if not catalog.is_file() or not _resource_path_allowed(catalog):
            continue
        try:
            lines = catalog.read_text(encoding="utf-8").splitlines()
            source_rel = str(catalog.relative_to(workspace.resolve())) if workspace else ""
        except (OSError, UnicodeError, ValueError):
            continue
        for line_no, line in enumerate(lines, 1):
            match = _PAPER_CATALOG_ENTRY_RE.match(line)
            if not match:
                continue
            key, citation = match.group(1).strip(), match.group(2).strip()
            normalized = _paper_match_key(key)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            quoted_title = re.search(r'["“](.+?)["”]', citation)
            urls = [u.rstrip(".,;") for u in _PAPER_URL_RE.findall(citation)]
            aliases = [key, *urls]
            aliases += re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", citation, re.IGNORECASE)
            aliases += re.findall(r"\barXiv\s*\d{4}\.\d{4,5}(?:v\d+)?\b", citation, re.IGNORECASE)
            out.append({
                "id": hashlib.sha1(f'{project.get("slug", "")}:{normalized}'.encode("utf-8")).hexdigest()[:16],
                "key": key,
                "name": quoted_title.group(1) if quoted_title else key,
                "citation": citation,
                "filename": "",
                "abs_path": "",
                "rel_path": "",
                "source_abs_path": str(catalog),
                "source_rel_path": source_rel,
                "source_line": line_no,
                "url": urls[0] if urls else "",
                "aliases": list(dict.fromkeys(aliases)),
                "availability": "metadata",
            })
            if len(out) >= _PROJECT_PAPER_LIMIT:
                return out
    return out


def _configured_papers(project: dict) -> list[dict]:
    resources = project.get("resources") or {}
    paper_cfg = resources.get("papers") or {}
    roots = _string_list(paper_cfg.get("roots") if isinstance(paper_cfg, dict) else paper_cfg)
    extensions = _string_list(paper_cfg.get("extensions", ["pdf"]) if isinstance(paper_cfg, dict) else ["pdf"])
    suffixes = {"." + ext.lower().lstrip(".") for ext in extensions} or {".pdf"}
    project_root = Path(project["root"]).resolve()
    local_files: dict[str, dict] = {}
    for raw in roots:
        root = Path(raw).expanduser()
        if not root.is_absolute():
            root = project_root / root
        try:
            root = root.resolve()
        except OSError:
            continue
        if not _resource_path_allowed(root):
            continue
        candidates = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
        for path in candidates:
            if len(local_files) >= _PROJECT_PAPER_LIMIT:
                break
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            item = _workspace_resource_item(path)
            if item:
                local_files[item["abs_path"]] = item

    catalogs = paper_cfg.get("catalogs", []) if isinstance(paper_cfg, dict) else []
    catalog_items = _catalog_papers(project, catalogs)
    unmatched = dict(local_files)
    # A local filename starts with (or contains) its catalog ShortID, e.g.
    # ``Suresh2022_ShapeMap3D_ICRA.pdf``. Prefer the longest match.
    for paper in catalog_items:
        key = _paper_match_key(paper.get("key", ""))
        matches = [
            (path, item) for path, item in unmatched.items()
            if key and key in _paper_match_key(item.get("filename", ""))
        ]
        if not matches:
            continue
        path, local = max(matches, key=lambda pair: len(_paper_match_key(pair[1]["filename"])))
        paper.update({
            "filename": local["filename"],
            "abs_path": local["abs_path"],
            "rel_path": local["rel_path"],
            "availability": "local",
            "aliases": list(dict.fromkeys([*(paper.get("aliases") or []), local["filename"], local["rel_path"], local["abs_path"]])),
        })
        unmatched.pop(path, None)

    # PDFs without a catalog row remain visible so dropping a new file into the
    # collection never makes it disappear from the UI.
    for item in unmatched.values():
        item["aliases"] = [item["filename"], item["rel_path"], item["abs_path"]]
        catalog_items.append(item)
    return sorted(catalog_items, key=lambda p: ((p.get("key") or p["name"]).lower(), p.get("filename", "").lower()))


def _resource_glob(project_root: Path, patterns) -> list[Path]:
    found: dict[str, Path] = {}
    for raw in _string_list(patterns):
        pattern = Path(raw).expanduser()
        full = pattern if pattern.is_absolute() else project_root / pattern
        # `glob` supports absolute recursive patterns, unlike Path.glob.
        for match in globlib.glob(str(full), recursive=True):
            try:
                path = Path(match).resolve()
            except OSError:
                continue
            if path.is_file() and _resource_path_allowed(path):
                found[str(path)] = path
                if len(found) >= _MODEL_EVIDENCE_LIMIT:
                    break
    return list(found.values())


def _job_matches_model(job_name: str, patterns) -> bool:
    name = str(job_name or "").lower()
    for raw in _string_list(patterns):
        pattern = raw.lower()
        matched = (fnmatch.fnmatch(name, pattern)
                   if any(c in pattern for c in "*?[") else pattern in name)
        if matched:
            return True
    return False


def _configured_models(project: dict, jobs: list[dict]) -> list[dict]:
    resources = project.get("resources") or {}
    configs = resources.get("models") or []
    if not isinstance(configs, list):
        return []
    root = Path(project["root"]).resolve()
    out = []
    for index, cfg in enumerate(configs):
        if not isinstance(cfg, dict):
            continue
        architecture = _resource_glob(root, cfg.get("architecture"))
        checkpoints = _resource_glob(root, cfg.get("checkpoints"))
        results = _resource_glob(root, cfg.get("results"))
        active_jobs = [j for j in jobs if _job_matches_model(j.get("name", ""), cfg.get("job_patterns"))]
        if active_jobs:
            status, detail = "training", f"{len(active_jobs)} active SLURM job(s)"
        elif checkpoints and results:
            status, detail = "ready", f"{len(checkpoints)} checkpoint(s) + {len(results)} result file(s)"
        else:
            status = "declared"
            missing = []
            if not checkpoints:
                missing.append("checkpoint")
            if not results:
                missing.append("result")
            detail = "missing " + " + ".join(missing) if missing else "architecture declared"
        out.append({
            "id": str(cfg.get("id") or f"model-{index + 1}"),
            "name": str(cfg.get("name") or cfg.get("id") or f"Model {index + 1}"),
            "status": status,
            "detail": detail,
            "architecture_count": len(architecture),
            "checkpoint_count": len(checkpoints),
            "result_count": len(results),
            "active_jobs": [{"id": j.get("id"), "name": j.get("name"), "state": j.get("state")}
                            for j in active_jobs],
        })
    return out


@app.get("/api/projects/{slug}/resources")
async def api_project_resources(slug: str):
    """Read-only project inventory for the graph's Papers / Models monitor."""
    project = projects.get_project(slug)
    if not project:
        raise HTTPException(404, "project not found")
    resources = project.get("resources") or {}
    if not resources:
        return {"enabled": False, "papers": [], "models": []}
    jobs_data = await _cluster_jobs_snapshot() if resources.get("models") else {"clusters": {}}
    jobs = [job for cluster in (jobs_data.get("clusters") or {}).values() for job in cluster]
    return {
        "enabled": True,
        "papers": _configured_papers(project),
        "models": _configured_models(project, jobs),
        "jobs_error": jobs_data.get("error"),
    }


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
    "claude-sonnet-5": 1_000_000,
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
    "gpt-5.6-terra": 400_000,
    "gpt-5.6-sol": 400_000,
}
_CONTEXT_WINDOWS = {"claude": 200_000, "grok": 256_000, "deepseek": 1_000_000, "glm": 200_000, "codex": 400_000}  # adapter fallback


def _context_window_for(model_kind: str, model_id: Optional[str]) -> int:
    if model_id and model_id in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[model_id]
    return _CONTEXT_WINDOWS.get(model_kind, 200_000)


def _usage_ctx_tokens(usage: dict, window: int | None = None) -> Optional[int]:
    """Context-window occupancy from a CLI usage blob = the LARGEST single API
    request in the turn, NOT the top-level sums (cumulative billing across
    agentic rounds — routinely exceeds the window). Shared by the stats endpoint
    and the auto-compact threshold check.

    Returns None when the blob cannot express occupancy at all; the caller must
    then fall back to a chars/4 estimate (_ctx_estimate_tokens).
    """
    def _req_total(d: dict) -> int:
        return (
            (d.get("input_tokens") or 0)
            + (d.get("cache_creation_input_tokens") or 0)
            + (d.get("cache_read_input_tokens") or 0)
            + (d.get("output_tokens") or 0)
        )
    iters = usage.get("iterations") or []
    if iters:
        # Per-request breakdown available → the largest request IS the occupancy.
        return max(_req_total(it) for it in iters)
    total = _req_total(usage)
    if window and total > window:
        # No per-iteration breakdown AND the top-level sum exceeds the window, so
        # the blob is cumulative across tool-use rounds (billing, not occupancy).
        # This is NOT DeepSeek-only: Claude emits it too whenever `iterations` comes
        # back empty — observed on cveval/VLM (2.5M summed) and dfu-pipeline/INTEGRITY
        # (1.3M summed), both claude-sonnet-4-6.
        #
        # Occupancy is genuinely UNRECOVERABLE from this blob. The old code returned
        # `input + output` here, which measures the size of the NEW turn, not the
        # resumed session — the bulk of a --resume context is `cache_read`, which that
        # expression drops. It read INTEGRITY (1.3M summed) as 6,278 tokens = 0.6%, so
        # auto-compact could never fire on precisely the heaviest, most tool-happy
        # agents. Return None and let the caller estimate instead of reporting a
        # confidently-wrong low number.
        return None
    return total


def _ctx_estimate_tokens(root: str, agent: dict, est_chars: int) -> int:
    """Chars/4 context estimate: every persisted message of the session plus the
    agent's system prompt. The fallback numerator whenever the CLI's usage blob is
    missing (fresh session, grok node) or unusable (cumulative — see above). Coarse,
    but it grows monotonically with the session, so a threshold built on it actually
    trips; a 0.0 default would silently disable auto-compact."""
    sys_chars = 0
    try:
        sp = projects.resolve_system_prompt(root, agent.get("system_prompt_file", ""))
        sys_chars = len(sp or "")
    except Exception:
        pass
    return (est_chars + sys_chars) // 4


def _session_context_pct(sess: Optional[dict], model_kind: str,
                         model_id: Optional[str] = None,
                         est_tokens: int = 0) -> float:
    """Context % of a session's last completed turn.

    Numerator prefers the CLI's real usage. When that blob is absent (fresh session,
    or a grok node which reports none) or unusable (cumulative — _usage_ctx_tokens
    returns None), it falls back to `est_tokens`, the caller's chars/4 estimate. The
    fallback is what keeps the auto-compact threshold alive on heavy agents; without
    it those sessions read ~0% forever and never rotate.
    """
    window = _context_window_for(model_kind, model_id)
    if not window:
        return 0.0
    tokens: Optional[int] = None
    if sess and sess.get("usage"):
        try:
            tokens = _usage_ctx_tokens(json.loads(sess["usage"]), window)
        except (ValueError, TypeError):
            tokens = None
    if tokens is None:
        tokens = est_tokens
    return round(min(100.0, tokens / window * 100.0), 1)


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
# Overview BODY staleness — the agent owns its overview BODY (control-plane only
# stamps HEADER/FOOTER machine fields), so a full-but-wrong body passes
# body_incomplete:false and rots silently. This is the floor: a body older than
# this is flagged regardless of activity (catches the BOSS-2026-06-27 case where
# the agent read the live rollup every turn but never mirrored it back).
_STALE_BODY_ABS_S = 14 * 24 * 3600
_STALE_BODY_GRACE_S = 60  # ignore sub-minute mtime skew between sibling files
_OVERVIEW_BODY_RE = re.compile(
    r"<!-- OVERVIEW:BODY -->(.*?)<!-- /OVERVIEW:BODY -->", re.DOTALL)
_MEMORY_RECONCILED_RE = re.compile(
    r"\[MEMORY_RECONCILED\](?P<body>[\s\S]*?)\[/MEMORY_RECONCILED\]",
    re.IGNORECASE,
)

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
    token_totals = db.agent_token_totals(slug)
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

        ov = overrides.get(aid) or {}
        # Adapter override wins, same as _run_agent — otherwise the panel reports the
        # yaml adapter while the agent runs on another (BOSS: yaml glm, /adapter claude),
        # picking the wrong context window and mislabelling the node.
        model_kind = ov.get("model") or a.get("model", "claude")
        if model_kind == "grok":
            eff_model = ov.get("grok_model") or a.get("grok_model") or "grok-build"
        elif model_kind == "deepseek":
            eff_model = ov.get("deepseek_model") or a.get("deepseek_model") or "deepseek-v4-flash"
        elif model_kind == "glm":
            eff_model = ov.get("glm_model") or a.get("glm_model") or "glm-4.6"
        elif model_kind == "codex":
            eff_model = ov.get("codex_model") or a.get("codex_model") or "gpt-5.6-terra"
        else:
            eff_model = ov.get("claude_model") or a.get("claude_model") or "claude-sonnet-4-6"
        effort = ov.get("effort") if (ov and "effort" in ov) else a.get("effort")

        window = _context_window_for(model_kind, eff_model)
        # Prefer the CLI's real token usage from the last turn. Context-window
        # occupancy = every input bucket (fresh + cache create + cache read) +
        # output. Fall back to a chars/4 estimate when no usage is recorded yet
        # (a session that has never completed a turn, or a grok node) OR when the
        # blob is cumulative and cannot express occupancy (_usage_ctx_tokens → None).
        # Preferred source: the CLI transcript. It reports the usage the SERVER returned
        # per request, so max(input+cache_creation+cache_read) IS the context occupancy —
        # no cumulative-vs-occupancy ambiguity and nothing the CLI injected is missed.
        codex_ctx = agent_log.codex_context(sess["claude_session_id"]) \
            if sess and sess.get("claude_session_id") and model_kind == "codex" else None
        if codex_ctx and codex_ctx.get("window"):
            window = codex_ctx["window"]
        scanned = tokens.scan(sess["claude_session_id"]) \
            if sess and sess.get("claude_session_id") and model_kind != "codex" else None
        ctx_tokens = codex_ctx["occupancy"] if codex_ctx else (scanned["occupancy"] if scanned else None)
        token_source = "codex" if codex_ctx else "transcript"
        if ctx_tokens is None:
            ctx_tokens = _usage_ctx_tokens(usage, window) if usage else None
            token_source = "exact"
        if ctx_tokens is None:
            ctx_tokens = _ctx_estimate_tokens(root, a, est_chars)
            token_source = "estimate"
        pct = round(min(100.0, ctx_tokens / window * 100.0), 1) if window else 0.0

        init_meta = None
        if sess and sess.get("init_meta"):
            try:
                init_meta = json.loads(sess["init_meta"])
            except (ValueError, TypeError):
                init_meta = None

        cwd_abs = projects.resolve_cwd(root, a.get("cwd", "."))
        stats[aid] = {
            "status": status,
            "init": init_meta,
            "model_kind": model_kind,
            "model": eff_model,
            "effort": effort,
            "updated_at": updated_at,
            "message_count": msg_count,
            "context_tokens": ctx_tokens,
            "token_source": token_source,
            "context_window": window,
            "context_pct": pct,
            # Lifetime billing for this agent (sum of booked turns) + this session's
            # scan. Billing != occupancy: cache_read is re-charged every request.
            "tokens": token_totals.get(aid),
            "session_tokens": {k: scanned[k] for k in
                               ("requests", "input_tokens", "cache_creation",
                                "cache_read", "output_tokens", "billed_total")} if scanned else None,
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
    codex_model: Optional[str] = None
    effort: Optional[str] = None


class NotionSettings(BaseModel):
    notion_url: str


_AGENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class NewAgent(BaseModel):
    id: str
    role: Optional[str] = ""
    model: str = "claude"
    claude_model: Optional[str] = None
    grok_model: Optional[str] = None
    deepseek_model: Optional[str] = None
    glm_model: Optional[str] = None
    codex_model: Optional[str] = None
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
    if body.model not in ("claude", "grok", "deepseek", "glm", "codex"):
        raise HTTPException(400, "model must be claude, grok, deepseek, glm, or codex")
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
        "Required files (6):\n"
        f"1. `{body.id}/AGENT.md`\n"
        f"2. `{body.id}/overview.md`\n"
        f"3. `{body.id}/inputs/manifest.md`\n"
        f"4. `{body.id}/outputs/manifest.md`\n"
        f"5. `{body.id}/state/progress.md`\n"
        f"6. `{body.id}/context/code_map.md`\n\n"
        "## Content guidelines\n"
        "- `AGENT.md` is the system prompt. Under 80 lines. Required sections in order: "
        "  (a) **NOTICE** (refuse to act on missing info, override all other rules), "
        "  (b) **Boot** — ONE ordered read-list (NO second 'Required reads' section). It must "
        "  read ONLY slim things: the shared rules it needs (`../shared/{research_integrity,"
        "scope_decisions}.md` + others on-demand), this file, its own `./overview.md`, and the "
        "  tiny `./inputs/manifest.md` (pinned versions). A PARENT reads `./state/children_status.json` "
        "  and every direct child's slim `overview.md` before routing. Then an 'On-demand only (NOT at boot)' note: open the "
        "  full `./inputs/<PRODUCER>.md` blob + the artifacts it points to ONLY when consuming a "
        "  specific artifact — never for routing. **Do NOT read `./state/progress.md` at boot** — "
        "  the control-plane auto-injects the recent slice (`progress.json`, ~2 days) into the "
        "  cold-start preamble; the agent only APPENDS to it. "
        "  (c) **Role**, "
        "  (d) **Boundaries** (three tiers: Always / Ask first→ESCALATION / Never), "
        "  (e) **Deliverables** — list the SPECIFIC files this agent must keep current. "
        "  At minimum: prepend a `## YYYY-MM-DD HH:MM — headline` entry to `./state/progress.md` "
        "  on every meaningful turn, and bump `./outputs/manifest.md` (with a Bump-log entry) "
        "  on every produced/modified artifact. Also list domain-specific deliverables "
        "  with REAL paths (e.g. `paper/latex/asce2027_paper.tex`, `results/<run_id>/predictions.parquet`). "
        "  (f) **Handoff** (input = inputs/manifest.md pinned; output = outputs/manifest.md pointing to PATHS, not data), "
        "  (g) **Hard rules** including the 3-statement-type rule (fact / literature claim / design decision) "
        "  and no-fabrication (→ `[VERIFY]`). "
        "NO routing tables, NO worker lists, NO 'dispatch this' patterns — the control plane "
        "injects current children at runtime.\n"
        "- `overview.md` — the slim state pane the parent reads to route. Three comment-delimited "
        "sections: `<!-- OVERVIEW:HEADER -->` (machine fields `status`, `manifest_version: 0.1.0`, "
        "`ready_for_parent`, `body_incomplete: true` — the control-plane stamps these), "
        "`<!-- OVERVIEW:BODY -->` (holistic current state, overwritten each turn, ≤200 words, one "
        "`label:` per line), `<!-- OVERVIEW:FOOTER -->` (`last_artifact`, `manifest_ref`, "
        "`open_escalation`, `last_updated`). Bootstrap the BODY as `- (chờ first task)`.\n"
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
        "instructing to PREPEND a dated section every meaningful turn (format: `## YYYY-MM-DD HH:MM — headline` "
        "— the time is REQUIRED, the control-plane reads it for freshness — followed by 2–5 bullets "
        "with evidence). Note that the control-plane rotates this into `progress.json` and auto-injects "
        "the recent slice, so the agent APPENDS but never reads the full log at boot. One initial entry "
        "dated today recording the bootstrap.\n"
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
        f"{agent_id}/overview.md",
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
        if a.get("model", "claude") not in ("claude", "grok", "deepseek", "glm", "codex"):
            raise HTTPException(400, f"model must be claude|grok|deepseek|glm|codex: {a.get('id')}")
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
    # Rotation point: last chance to book the outgoing session's unbooked token usage —
    # after new_session() every scan reads the NEW transcript only.
    _book_current_session_tokens(slug, agent_id)
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
                          codex_model=body.codex_model,
                          effort=body.effort)
    return {"ok": True}


@app.get("/api/projects/{slug}/agents/{agent_id}/notion-settings")
def api_get_notion_settings(slug: str, agent_id: str):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    is_root_agent = not (found[1].get("parents") or [])
    try:
        return notion_settings.get_notion_settings(
            NOTION_REPORT_CONFIG_PATH,
            slug,
            agent_id,
            editable=is_root_agent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{slug}/agents/{agent_id}/notion-settings")
def api_set_notion_settings(slug: str, agent_id: str, body: NotionSettings):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    is_root_agent = not (found[1].get("parents") or [])
    try:
        return notion_settings.bind_notion_settings(
            NOTION_REPORT_CONFIG_PATH,
            slug,
            agent_id,
            body.notion_url,
            editable=is_root_agent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/notion-system-settings")
def api_get_notion_system_settings():
    """Return the one workspace hub used to provision project subtrees."""
    try:
        return notion_settings.get_notion_settings(
            NOTION_REPORT_CONFIG_PATH,
            SYSTEM_PROJECT_SLUG,
            SYSTEM_AGENT_ID,
            editable=True,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/notion-system-settings")
def api_set_notion_system_settings(body: NotionSettings):
    """Verify and bind the System Hub; this is the only global setup action."""
    try:
        return notion_settings.bind_notion_settings(
            NOTION_REPORT_CONFIG_PATH,
            SYSTEM_PROJECT_SLUG,
            SYSTEM_AGENT_ID,
            body.notion_url,
            editable=True,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


class AdapterSwitch(BaseModel):
    adapter: str   # claude | grok | deepseek | glm
    model: Optional[str] = None  # specific model id for the new adapter (optional)


class CapabilityToggle(BaseModel):
    kind: str  # plugin | skill | mcp_server | mcp_tool
    target: str
    enabled: bool
    server: Optional[str] = None


class CapabilityDelete(BaseModel):
    kind: str  # skill | mcp_server
    target: str


# Default model per adapter when the caller omits `model`.
_ADAPTER_DEFAULT_MODEL = {
    "claude": "claude-sonnet-4-6",
    "grok": "grok-build",
    "deepseek": "deepseek-v4-flash",
    "glm": "glm-4.6",
    "codex": "gpt-5.6-terra",
}


@app.post("/api/projects/{slug}/agents/{agent_id}/adapter")
def api_set_agent_adapter(slug: str, agent_id: str, body: AdapterSwitch):
    """Switch an agent's adapter (claude|grok|deepseek|glm) at runtime — stored
    as an override that wins over project.yaml, so no file edit / restart needed.
    Lets a user move a node e.g. opus(claude) -> glm-5.2 from the chat UI."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    if body.adapter not in ("claude", "grok", "deepseek", "glm", "codex"):
        raise HTTPException(400, "adapter must be claude, grok, deepseek, glm, or codex")
    current = (db.get_agent_override(slug, agent_id) or {}).get("model") or found[1].get("model", "claude")
    rotated = current != body.adapter
    if rotated:
        _book_current_session_tokens(slug, agent_id)
        db.new_session(slug, agent_id)
    db.set_agent_adapter(slug, agent_id, body.adapter, body.model or _ADAPTER_DEFAULT_MODEL[body.adapter])
    # Return the canonical, override-enriched project in the same response. This
    # removes the frontend's second-fetch race and guarantees every node/header
    # repaints from exactly the state that was committed above.
    return {"ok": True, "adapter": body.adapter,
            "model": body.model or _ADAPTER_DEFAULT_MODEL[body.adapter],
            "session_rotated": rotated, "project": api_project(slug)}


def _agent_runtime_env(project: dict, slug: str, agent_id: str) -> dict[str, str]:
    """Build the non-secret identity/config environment shared by Codex + inventory."""
    runtime_env = {
        "AGENTUI_PROJECT_SLUG": slug,
        "AGENTUI_AGENT_ID": agent_id,
        "AGENTUI_PROJECT_NAME": str(project.get("name") or slug),
        "AGENTUI_PROJECT_ROOT_AGENT_ID": next(
            (str(item.get("id")) for item in project.get("agents", [])
             if item.get("id") and not (item.get("parents") or [])),
            agent_id,
        ),
        "NOTION_REPORT_CONFIG": str(NOTION_REPORT_CONFIG_PATH),
    }
    try:
        loaded_report_schema = load_report_schema(
            project["root"], project.get("notion_report"))
    except ReportSchemaError:
        loaded_report_schema = None
    if loaded_report_schema:
        runtime_env["AGENTUI_PROJECT_ROOT"] = str(project["root"])
        runtime_env["AGENTUI_REPORT_SCHEMA_FILE"] = str(
            loaded_report_schema["path_abs"])
    try:
        notion_destination = notion_settings.get_notion_settings(
            NOTION_REPORT_CONFIG_PATH, slug, agent_id)
        token_env_name = str(notion_destination.get("token_env") or "").strip()
        if token_env_name:
            runtime_env["AGENTUI_NOTION_TOKEN_ENV"] = token_env_name
    except ValueError:
        pass
    return runtime_env


async def _agent_capability_payload(
    slug: str, agent_id: str, *, force: bool = False
) -> dict:
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    project, agent = found
    override = db.get_agent_override(slug, agent_id) or {}
    adapter = override.get("model") or agent.get("model", "claude")
    cwd = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    runtime_env = _agent_runtime_env(project, slug, agent_id)
    # Inventory is always the inherited baseline. Applying the saved policy here
    # would hide disabled tools and make it impossible to turn them back on.
    config, _, _, _ = _codex_sdk_settings(runtime_env)
    config.cwd = cwd
    try:
        catalog = await capabilities.discover_catalog(
            config=config,
            cwd=cwd,
            cache_key=f"{slug}:{agent_id}:{cwd}",
            force=force,
        )
    except Exception as exc:
        raise HTTPException(502, f"Codex capability inventory failed: {exc}") from exc
    policy = db.get_agent_capability_policy(slug, agent_id)
    effective = capabilities.effective_catalog(catalog, policy)
    skill_usage = db.get_agent_skill_usage(slug, agent_id)
    for skill in effective.get("skills", []):
        usage = skill_usage.get(skill["path"], {})
        skill["usage_count"] = int(usage.get("usage_count") or 0)
        skill["first_used_at"] = usage.get("first_used_at")
        skill["last_used_at"] = usage.get("last_used_at")
    override_count = len(policy.get("plugins") or {}) + len(policy.get("skills") or {})
    for entry in (policy.get("mcp_servers") or {}).values():
        if not isinstance(entry, dict):
            continue
        override_count += int(isinstance(entry.get("enabled"), bool))
        override_count += len(entry.get("tools") or {})
    return {
        "project_slug": slug,
        "agent_id": agent_id,
        "agent_role": agent.get("role") or "",
        "adapter": adapter,
        "applies_now": adapter == "codex",
        "revision": policy.get("revision", 0),
        "override_count": override_count,
        "updated_at": policy.get("updated_at"),
        "plugins": effective.get("plugins", []),
        "skills": effective.get("skills", []),
        "mcp_servers": effective.get("mcp_servers", []),
        "errors": effective.get("errors", []),
        "inventory_source": effective.get("source"),
        "inventory_path": effective.get("inventory_path"),
        "inventory_delete_enabled": True,
        "skill_usage_tracking_started_at": db.skill_usage_tracking_started_at(),
        "discovered_at": effective.get("discovered_at"),
    }


@app.get("/api/projects/{slug}/agents/{agent_id}/capabilities")
async def api_agent_capabilities(slug: str, agent_id: str, refresh: bool = False):
    return await _agent_capability_payload(slug, agent_id, force=refresh)


@app.post("/api/projects/{slug}/agents/{agent_id}/capabilities/toggle")
async def api_toggle_agent_capability(
    slug: str, agent_id: str, body: CapabilityToggle
):
    if _active_run(slug, agent_id):
        raise HTTPException(409, "agent is running; stop or wait before changing capabilities")
    payload = await _agent_capability_payload(slug, agent_id)
    catalog = {
        "plugins": payload["plugins"],
        "skills": payload["skills"],
        "mcp_servers": payload["mcp_servers"],
    }
    current = db.get_agent_capability_policy(slug, agent_id)
    try:
        updated = capabilities.update_policy(
            current,
            catalog,
            kind=body.kind,
            target=body.target,
            enabled=body.enabled,
            server_name=body.server,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.set_agent_capability_policy(slug, agent_id, updated)

    runtime_reset = False
    if payload["adapter"] == "codex":
        # Book the completed SDK turn before detaching its provider thread. UI
        # messages stay in place; the next send receives the normal cold-start recap.
        _book_current_session_tokens(slug, agent_id)
        runtime_reset = db.reset_agent_runtime(slug, agent_id)
    result = await _agent_capability_payload(slug, agent_id)
    result.update({"ok": True, "runtime_reset": runtime_reset})
    return result


@app.post("/api/projects/{slug}/agents/{agent_id}/capabilities/reset")
async def api_reset_agent_capabilities(slug: str, agent_id: str):
    if _active_run(slug, agent_id):
        raise HTTPException(409, "agent is running; stop or wait before changing capabilities")
    payload = await _agent_capability_payload(slug, agent_id)
    had_overrides = bool(payload.get("override_count"))
    if had_overrides:
        db.set_agent_capability_policy(
            slug, agent_id, {"plugins": {}, "skills": {}, "mcp_servers": {}}
        )

    runtime_reset = False
    if had_overrides and payload["adapter"] == "codex":
        _book_current_session_tokens(slug, agent_id)
        runtime_reset = db.reset_agent_runtime(slug, agent_id)
    result = await _agent_capability_payload(slug, agent_id)
    result.update({"ok": True, "runtime_reset": runtime_reset})
    return result


@app.post("/api/projects/{slug}/agents/{agent_id}/capabilities/delete")
async def api_delete_inventory_capability(
    slug: str, agent_id: str, body: CapabilityDelete
):
    """Delete one capability from Idle's global inventory for every agent."""
    payload = await _agent_capability_payload(slug, agent_id)
    if any(not run.done for run in _RUNS.values()) or db.list_running_sessions():
        raise HTTPException(
            409, "an agent is running; wait or stop all runs before deleting inventory"
        )

    skill = None
    server = None
    if body.kind == "skill":
        skill = next(
            (item for item in payload["skills"] if item["id"] == body.target), None
        )
        if not skill:
            raise HTTPException(404, "skill not found in Agent Idle inventory")
    elif body.kind == "mcp_server":
        server = next(
            (item for item in payload["mcp_servers"] if item["id"] == body.target),
            None,
        )
        if not server:
            raise HTTPException(404, "MCP server not found in Agent Idle inventory")
        if not server.get("controllable", False):
            raise HTTPException(
                400, "runtime-managed MCP cannot be deleted from Idle inventory"
            )
    else:
        raise HTTPException(400, "only skills and MCP servers can be deleted")

    try:
        removed = capabilities.delete_inventory_capability(
            kind=body.kind, target=body.target
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.kind == "skill":
        deleted_skill_paths = [
            value for value in (body.target, removed.get("source_path"))
            if isinstance(value, str)
        ]
        db.purge_deleted_capability_policy(
            kind="skill",
            skill_paths=deleted_skill_paths,
        )
        db.purge_deleted_skill_usage(deleted_skill_paths)
    else:
        db.purge_deleted_capability_policy(
            kind="mcp_server", mcp_server=str(removed["name"])
        )

    reset_count = 0
    reset_errors: list[str] = []
    for runtime in db.list_latest_codex_runtimes():
        try:
            _book_current_session_tokens(runtime["project_slug"], runtime["agent_id"])
        except Exception as exc:
            reset_errors.append(
                f"token booking failed for {runtime['project_slug']}/{runtime['agent_id']}: {exc}"
            )
        try:
            reset_count += int(db.reset_agent_runtime(
                runtime["project_slug"], runtime["agent_id"]
            ))
        except Exception as exc:
            reset_errors.append(
                f"runtime reset failed for {runtime['project_slug']}/{runtime['agent_id']}: {exc}"
            )

    result = await _agent_capability_payload(slug, agent_id, force=True)
    result.update({
        "ok": True,
        "deleted": removed,
        "global_inventory_change": True,
        "runtime_reset_count": reset_count,
        "runtime_reset_errors": reset_errors,
    })
    return result


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


class ContextPolicyBody(BaseModel):
    mode: str
    threshold_tokens: int = 100_000


@app.post("/api/projects/{slug}/agents/{agent_id}/context-policy")
def api_set_context_policy(slug: str, agent_id: str, body: ContextPolicyBody):
    if not projects.get_agent(slug, agent_id):
        raise HTTPException(404, "agent not found")
    mode = (body.mode or "").strip().lower()
    if mode == "default":
        db.delete_context_policy(slug, agent_id)
        return {"mode": "default", "threshold_tokens": None}
    if mode not in {"off", "compact", "clear"}:
        raise HTTPException(400, "mode must be default, off, compact, or clear")
    threshold = int(body.threshold_tokens or 0)
    if threshold < 1_000 or threshold > 10_000_000:
        raise HTTPException(400, "threshold_tokens must be between 1,000 and 10,000,000")
    return db.set_context_policy(slug, agent_id, mode, threshold)


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
        "### Direct-read exception — slim routing state\n"
        "`overview.md` and `state/children_status.json` are control-plane routing state, not worker source files. "
        "You MUST personally read the routing-state files required by your main-body PRE-FLIGHT or routing workflow "
        "before deciding or dispatching; do NOT dispatch a worker merely to read its own overview for you. "
        "This exception is read-only for another agent's overview and applies only to these exact slim-state files; "
        "code, data, configs, manifests, artifacts, and all other worker-domain files still require dispatch.\n\n"
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


def _read_overview_projection(p: Path, max_body_chars: int = 6000) -> str:
    """Read an overview without ever cutting its machine footer in half.

    Legacy callers capped the whole file by character count.  A slightly long
    BODY therefore hid ``open_escalation`` and provenance from the parent.  Cap
    only the agent-authored BODY and always preserve complete HEADER/FOOTER.
    """
    try:
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    match = _OVERVIEW_BODY_RE.search(txt)
    if not match:
        return _read_capped(p, max_body_chars)
    body = match.group(1).strip()
    if len(body) <= max_body_chars:
        return txt
    clipped = body[:max_body_chars].rstrip()
    replacement = (
        "<!-- OVERVIEW:BODY -->\n"
        + clipped
        + "\n[… BODY truncated in prompt; open the overview for the complete summary]\n"
        + "<!-- /OVERVIEW:BODY -->"
    )
    return txt[:match.start()] + replacement + txt[match.end():]


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

    # Structured progress log: newest KEEP_DAYS days, always newest-first regardless of
    # how the file is ordered on disk (the old markdown excerpt guessed the ordering and
    # loaded the OLDEST day for agents that append oldest-first). Falls back to parsing
    # legacy progress.md for agents not yet migrated to progress.json.
    prog_txt = progress_store.recent_text(adir, keep_days=_PROGRESS_KEEP_DAYS)
    if prog_txt:
        parts.append(f"### Your memory (`state/progress.json`, newest first)\n{prog_txt}")

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
    ov_text = _read_overview_projection(ov, 4000) if ov.is_file() else ""
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


def _children_overview_context(project: dict, parent_id: str) -> tuple[str, list[dict]]:
    """Load every direct child's slim overview for the parent's current turn.

    This is an actual prompt enrichment, not just UI theatre: the parent model
    receives the current text before it routes or decides. The access records
    are emitted separately so the owning child nodes pulse yellow.
    """
    agents = {a["id"]: a for a in project.get("agents", [])}
    sections: list[str] = []
    accesses: list[dict] = []
    project_root = project["root"]
    for child_id in _get_children(project, parent_id):
        child = agents.get(child_id)
        if not child:
            continue
        child_cwd = projects.resolve_cwd(project_root, child.get("cwd", "."))
        overview = _agent_dir(project_root, child, child_cwd) / "overview.md"
        text = _read_overview_projection(overview, 6000) if overview.is_file() else ""
        if not text:
            continue
        stale = _check_overview_staleness(project_root, project, child_id)
        if stale:
            text = (
                "⚠ CONTROL-PLANE: this overview is not attested to the current manifest: "
                + stale + ". Treat it as routing-only and ask the owner to reconcile before acceptance.\n\n"
                + text
            )
        try:
            rel_path = str(overview.resolve().relative_to(Path(project_root).resolve()))
        except (OSError, ValueError):
            rel_path = str(overview)
        sections.append(f"### {child_id} (`{rel_path}`)\n{text}")
        accesses.append({"owner": child_id, "path": str(overview), "rel_path": rel_path})
    if not sections:
        return "", []
    return (
        "[CONTROL-PLANE CHILD-OVERVIEW PRE-FLIGHT] Current direct-worker snapshots. "
        "You have read these before routing or deciding this turn:\n\n"
        + "\n\n".join(sections),
        accesses,
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


def _memory_reconciliation_config(project: dict) -> dict:
    """Return the opt-in reconciliation policy for one project.

    Projects without ``memory.reconciliation.enabled`` retain the legacy
    single-turn behaviour.  This makes the GelSight rollout isolated and
    reversible while the policy is being evaluated.
    """
    memory = project.get("memory") or {}
    cfg = memory.get("reconciliation") or {}
    return cfg if isinstance(cfg, dict) and cfg.get("enabled") is True else {}


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _memory_snapshot(agent_dir: Path) -> dict:
    """Content snapshot used to validate a reconciliation receipt for the whole turn."""
    overview = agent_dir / "overview.md"
    try:
        overview_text = overview.read_text(encoding="utf-8", errors="replace")
    except OSError:
        overview_text = ""
    return {
        "progress": _sha256_file(agent_dir / "state" / "progress.md"),
        "manifest": _sha256_file(agent_dir / "outputs" / "manifest.md"),
        "overview": hashlib.sha256(_overview_body(overview).encode("utf-8")).hexdigest(),
        "overview_text": overview_text,
    }


def _receipt_list(value: Optional[str]) -> list[str]:
    raw = (value or "").strip()
    if not raw or raw.lower() in {"none", "n/a", "unchanged"}:
        return []
    return [part.strip() for part in re.split(r"\s*[,|]\s*", raw) if part.strip()]


def _memory_ref_exists(path: Path, ref: Optional[str]) -> bool:
    """Best-effort validation that a receipt pointer names text in its owner file."""
    value = (ref or "").strip()
    if not value or value.lower() in {"none", "n/a"}:
        return False
    # Accept ``path#Heading`` / ``#Heading`` / a dated heading fragment.
    needle = value.rsplit("#", 1)[-1].replace("-", " ").strip().lower()
    if not needle:
        return False
    try:
        hay = path.read_text(encoding="utf-8", errors="replace").replace("-", " ").lower()
    except OSError:
        return False
    return needle in hay


def _memory_semantic_lint(agent_dir: Path, cfg: dict) -> list[str]:
    """Non-destructive hygiene checks; warnings never truncate owner content."""
    warnings: list[str] = []
    body = _overview_body(agent_dir / "overview.md")
    advisory_words = int(cfg.get("overview_advisory_words") or 240)
    words = len(re.findall(r"\b\w+[\w'-]*\b", body, re.UNICODE))
    if words > advisory_words:
        warnings.append(
            f"overview BODY is {words} words (advisory {advisory_words}); rewrite semantically, never truncate"
        )
    manifest_path = agent_dir / "outputs" / "manifest.md"
    try:
        manifest = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        manifest = ""
    paragraphs = [re.sub(r"\s+", " ", p).strip().lower()
                  for p in re.split(r"\n\s*\n", manifest)]
    seen: set[str] = set()
    duplicates = 0
    for paragraph in paragraphs:
        if len(paragraph) < 80 or paragraph.startswith("|"):
            continue
        if paragraph in seen:
            duplicates += 1
        seen.add(paragraph)
    if duplicates:
        warnings.append(f"manifest contains {duplicates} repeated paragraph(s)")
    chronology = len(re.findall(r"(?m)^##\s+20\d{2}-\d{2}-\d{2}\b", manifest))
    if chronology >= 2:
        warnings.append(
            f"manifest contains {chronology} dated top-level sections; chronology belongs in progress"
        )
    return warnings


def _overview_body(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _OVERVIEW_BODY_RE.search(text)
    return match.group(1).strip() if match else ""


def _memory_reconciliation_prompt(project: dict, agent: dict, agent_dir: Path,
                                  final_text: str, turn_before: dict) -> str:
    cfg = _memory_reconciliation_config(project)
    protocol = Path(project["root"]) / str(
        cfg.get("protocol_file") or "shared/memory_protocol.md")
    outcome = final_text.strip()
    if len(outcome) > 2400:
        outcome = outcome[:2400].rstrip() + "\n[… primary response excerpt truncated]"
    before_phase = _memory_snapshot(agent_dir)
    observed = {
        key: "updated" if before_phase.get(key) != turn_before.get(key) else "unchanged"
        for key in ("progress", "manifest", "overview")
    }
    open_escalations = db.get_open_escalations(project["slug"], agent["id"])
    escalation_lines = [
        f"- {row['escalation_id']}: {row['type']}→{row.get('target') or '?'} — {row.get('reason') or ''}"
        for row in open_escalations
    ] or ["- none"]
    return (
        "[CONTROL-PLANE MEMORY_RECONCILIATION — INTERNAL FINALIZATION]\n"
        "The primary task has finished. Do not redo it, dispatch another agent, "
        "schedule work, browse, train, or change project artifacts. You are still the "
        "same owner agent; reconcile only your own persistent memory.\n\n"
        f"Read the semantic contract at `{protocol}` and inspect these owner files:\n"
        f"- recent delta: `{agent_dir / 'state' / 'progress.md'}`\n"
        f"- current technical source of truth: `{agent_dir / 'outputs' / 'manifest.md'}`\n"
        f"- parent-facing summary: `{agent_dir / 'overview.md'}`\n\n"
        "Perform the reconciliation in this exact semantic order:\n"
        "1. Ensure progress contains the meaningful delta from the completed task; do not duplicate an entry already written.\n"
        "2. Reconcile manifest in place if the current technical truth changed. Organize by subject, never append turn history.\n"
        "3. Derive OVERVIEW:BODY only from the finalized manifest. Keep only the compact facts that affect parent routing, acceptance, blocking, or stopping. Do not introduce a technical claim absent from the manifest.\n"
        "4. Do not edit OVERVIEW:HEADER or OVERVIEW:FOOTER; the control plane owns provenance and runtime fields.\n"
        "A file may remain unchanged when its semantic content did not change. Never truncate information mechanically.\n\n"
        "Control-plane observation from the start of the PRIMARY turn to this phase:\n"
        f"- progress: {observed['progress']}\n"
        f"- manifest: {observed['manifest']}\n"
        f"- overview BODY: {observed['overview']}\n"
        "Your receipt must describe the final whole-turn hashes, including edits already made during the primary task.\n\n"
        "Currently open escalation IDs owned by you:\n"
        + "\n".join(escalation_lines) + "\n"
        "Resolve an ID only when this completed task actually removed that blocker.\n\n"
        "Primary response for orientation only:\n"
        f"{outcome or '(no textual outcome)'}\n\n"
        "Finish with exactly this audit block. `durable_delta` is technical, coordination, or none. "
        "Pointers must name real headings/entries; use `none` only when allowed:\n"
        "[MEMORY_RECONCILED]\n"
        "status: ok\n"
        "durable_delta: <technical|coordination|none>\n"
        "progress: <updated|unchanged>\n"
        "progress_ref: <dated heading fragment or none>\n"
        "manifest: <updated|unchanged>\n"
        "manifest_ref: <manifest section or none>\n"
        "overview: <updated|unchanged>\n"
        "overview_manifest_refs: <manifest section(s), separated by |, or none>\n"
        "overview_reason: <why parent routing changed or remained unchanged>\n"
        "resolved_escalations: <explicit esc-ID(s) or none>\n"
        "[/MEMORY_RECONCILED]"
    )


async def _record_skill_usage_event(
    *, project_slug: str, agent_id: str, session_id: str, event: dict, emit
) -> None:
    """Persist one verified skill use and surface the exact aggregate to the UI."""
    try:
        stats = db.record_agent_skill_use(
            project_slug,
            agent_id,
            skill_path=str(event.get("path") or ""),
            skill_name=str(event.get("skill") or ""),
            provider_thread_id=str(event.get("thread_id") or ""),
            turn_id=str(event.get("turn_id") or ""),
            source=str(event.get("source") or "unknown"),
        )
    except (TypeError, ValueError):
        return
    if not stats["recorded"]:
        return
    try:
        db.add_cli_event(
            session_id,
            "skill",
            str(event.get("skill") or "skill"),
            str(event.get("path") or ""),
            "ok",
            event_key=f"{event.get('thread_id')}:{event.get('turn_id')}:{event.get('path')}",
        )
    except Exception:
        pass
    await emit({
        "type": "skill_use",
        "agent": agent_id,
        "skill": event.get("skill"),
        "path": event.get("path"),
        "source": event.get("source"),
        **stats,
    })


async def _run_memory_reconciliation(
    *, project: dict, agent: dict, agent_dir: Path, final_text: str,
    turn_before: dict,
    stream_fn, model: str, override: dict, effort: Optional[str],
    resume_session_id: Optional[str], cwd: str, runtime_env: dict,
    emit, session_id: str,
    capability_policy: Optional[dict] = None,
) -> dict:
    """Run the owner agent's private, non-dispatching memory-finalization turn.

    The model performs semantic reconciliation; this function only orchestrates
    and validates the acknowledgement.  Its prose is intentionally not copied
    into the user's chat, while tool activity remains visible/auditable.
    """
    prompt = _memory_reconciliation_prompt(
        project, agent, agent_dir, final_text, turn_before)
    memory_system = (
        "You are in an internal MEMORY_RECONCILIATION phase initiated by AgentUI. "
        "Follow the supplied memory protocol. Work only inside your own agent directory. "
        "Do not dispatch, schedule, or continue the primary task."
    )
    if model == "claude":
        agen = stream_fn(
            message=prompt, system_prompt=memory_system, cwd=cwd,
            model=override.get("claude_model") or agent.get("claude_model") or "claude-sonnet-4-6",
            effort=effort, resume_session_id=resume_session_id,
            extra_env=runtime_env,
        )
    elif model == "grok":
        agen = stream_fn(
            message=prompt, system_prompt=memory_system, cwd=cwd,
            model=override.get("grok_model") or agent.get("grok_model") or "grok-build",
            effort=effort, resume_session_id=resume_session_id,
        )
    elif model == "deepseek":
        agen = stream_fn(
            message=prompt, system_prompt=memory_system, cwd=cwd,
            model=override.get("deepseek_model") or agent.get("deepseek_model") or "deepseek-v4-flash",
            effort=effort, resume_session_id=resume_session_id,
            runtime_env=runtime_env,
        )
    elif model == "glm":
        agen = stream_fn(
            message=prompt, system_prompt=memory_system, cwd=cwd,
            model=override.get("glm_model") or agent.get("glm_model") or "glm-4.6",
            effort=effort, resume_session_id=resume_session_id,
            runtime_env=runtime_env,
        )
    elif model == "codex":
        codex_effort = effort if effort in (
            None, "default", "low", "medium", "high", "xhigh") else None
        agen = stream_fn(
            message=prompt, system_prompt=memory_system, cwd=cwd,
            model=override.get("codex_model") or agent.get("codex_model") or "gpt-5.6-terra",
            effort=codex_effort, resume_session_id=resume_session_id,
            extra_env=runtime_env,
            capability_policy=capability_policy,
        )
    else:
        agen = stream_fn(message=prompt, system_prompt=memory_system, cwd=cwd)

    await emit({"type": "status", "agent": agent["id"],
                "status": "reconciling_memory"})
    db.add_cli_event(session_id, "memory", "reconciliation", "started", "…")
    assembled: list[str] = []
    error = ""
    new_sid = resume_session_id
    async for evt in agen:
        etype = evt.get("type")
        if etype == "delta":
            assembled.append(evt.get("text") or "")
        elif etype == "tool_use":
            tool_name = evt.get("tool") or "?"
            tool_input = evt.get("input") or {}
            try:
                target = agent_log._target(tool_name, tool_input)
                db.add_cli_event(session_id, "memory_tool", tool_name, target, "…")
            except Exception:
                pass
            await emit({"type": "tool_use", "agent": agent["id"],
                        "tool": tool_name, "input": tool_input,
                        "phase": "memory_reconciliation"})
        elif etype == "skill_use":
            await _record_skill_usage_event(
                project_slug=project["slug"],
                agent_id=agent["id"],
                session_id=session_id,
                event=evt,
                emit=emit,
            )
        elif etype == "meta":
            data = evt.get("data") or {}
            if data.get("claude_session_id"):
                new_sid = data["claude_session_id"]
                db.set_cli_session_id(session_id, new_sid, data.get("cli_adapter") or model)
        elif etype == "error":
            error = evt.get("message") or "memory adapter error"
            break

    text = "".join(assembled)
    marker = _MEMORY_RECONCILED_RE.search(text)
    fields = _kv(marker.group("body")) if marker else {}
    valid_values = {"updated", "unchanged"}
    errors: list[str] = []
    if error:
        errors.append(error)
    if fields.get("status", "").lower() != "ok":
        errors.append("receipt status is not ok")
    delta_kind = fields.get("durable_delta", "").lower()
    if delta_kind not in {"technical", "coordination", "none"}:
        errors.append("durable_delta must be technical, coordination, or none")

    after = _memory_snapshot(agent_dir)
    actual = {
        key: "updated" if after.get(key) != turn_before.get(key) else "unchanged"
        for key in ("progress", "manifest", "overview")
    }
    for key in ("progress", "manifest", "overview"):
        declared = fields.get(key, "").lower()
        if declared not in valid_values:
            errors.append(f"{key} must be updated or unchanged")
        elif declared != actual[key]:
            errors.append(f"{key} receipt says {declared}, hash says {actual[key]}")

    progress_path = agent_dir / "state" / "progress.md"
    manifest_path = agent_dir / "outputs" / "manifest.md"
    if actual["progress"] == "updated" and not _memory_ref_exists(
            progress_path, fields.get("progress_ref")):
        errors.append("updated progress requires a real progress_ref")
    if actual["manifest"] == "updated" and not _memory_ref_exists(
            manifest_path, fields.get("manifest_ref")):
        errors.append("updated manifest requires a real manifest_ref")
    if delta_kind in {"technical", "coordination"} and actual["manifest"] == "unchanged":
        if not _memory_ref_exists(manifest_path, fields.get("manifest_ref")):
            errors.append("durable delta with unchanged manifest requires an existing manifest_ref")
    if delta_kind == "none" and any(value == "updated" for value in actual.values()):
        errors.append("durable_delta none contradicts changed memory hashes")
    # Long technical/chat synthesis may legitimately be derived from existing
    # knowledge, but it must name where that durable knowledge already lives.
    if delta_kind == "none" and len(final_text.strip()) > 800:
        if not _memory_ref_exists(manifest_path, fields.get("manifest_ref")):
            errors.append("long unchanged synthesis requires an existing manifest_ref")

    if actual["overview"] == "updated":
        refs = _receipt_list(fields.get("overview_manifest_refs"))
        if not refs:
            errors.append("updated overview requires overview_manifest_refs")
        else:
            missing_refs = [ref for ref in refs if not _memory_ref_exists(manifest_path, ref)]
            if missing_refs:
                errors.append("overview references missing manifest section(s): "
                              + ", ".join(missing_refs))
    if not (fields.get("overview_reason") or "").strip():
        errors.append("overview_reason is required")

    requested_resolutions = _receipt_list(fields.get("resolved_escalations"))
    open_ids = {
        row["escalation_id"]
        for row in db.get_open_escalations(project["slug"], agent["id"])
    }
    invalid_resolutions = [esc_id for esc_id in requested_resolutions if esc_id not in open_ids]
    if invalid_resolutions:
        errors.append("resolved_escalations not open/owned: " + ", ".join(invalid_resolutions))
    if not manifest_path.is_file():
        errors.append("owner manifest is missing")
    if len(_overview_body(agent_dir / "overview.md")) < 40:
        errors.append("overview BODY is missing or incomplete")

    warnings = _memory_semantic_lint(
        agent_dir, _memory_reconciliation_config(project))
    ok = not errors
    status = "ok" if ok else "error"
    detail = "reconciled" if ok else "; ".join(errors or [
        "missing/invalid MEMORY_RECONCILED audit block"])
    db.add_cli_event(session_id, "memory", "reconciliation", detail[:240], status)
    await emit({"type": "meta", "agent": agent["id"], "data": {
        "memory_reconciliation": {
            "status": status,
            "progress": fields.get("progress"),
            "manifest": fields.get("manifest"),
            "overview": fields.get("overview"),
            "durable_delta": fields.get("durable_delta"),
            "warnings": warnings,
            "message": None if ok else detail[:240],
        }
    }})
    return {
        "ok": ok,
        "text": text,
        "session_id": new_sid,
        "error": detail if not ok else "",
        "warnings": warnings,
        "fields": fields,
        "actual": actual,
        "resolved_escalations": requested_resolutions if ok else [],
    }


@trace_agent_turn
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
    user_message = message

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

    # ----- OVERVIEW BODY STALENESS (spec §6.5 sibling) -----
    # The agent owns its overview BODY; control-plane stamps only HEADER/FOOTER.
    # A full-but-stale body is invisible to body_incomplete, so surface it here.
    body_stale = _check_overview_staleness(project["root"], project, agent_id)
    if body_stale:
        if _memory_reconciliation_config(project):
            message = (
                "[CONTROL-PLANE MEMORY NOTICE] Your overview provenance is stale: "
                + body_stale + ". Complete the user's primary task first; the mandatory "
                "internal MEMORY_RECONCILIATION phase will reconcile manifest and derive "
                "overview afterward. Do not replace the primary task with a memory-only response.\n\n"
                + message
            )
        else:
            message = (
                "[CONTROL-PLANE STALE-BODY] Your overview.md BODY is stale: "
                + body_stale + ".\n\n" + message
            )
    # ----- END OVERVIEW BODY STALENESS -----

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
    system_prompt = (system_prompt or "") + _NOTION_AGENT_INSTRUCTIONS + _SLURM_HANDOFF_INSTRUCTIONS
    if os.environ.get("AGENTUI_EVALUATION_MODE", "").lower() in {"1", "true"}:
        system_prompt += _OFFLINE_EVALUATION_INSTRUCTIONS
    cwd = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    children = _get_children(project, agent_id)

    if children:
        system_prompt = (system_prompt or "") + _dispatch_instructions(children)

    if _should_include_schedule_instructions(slug, agent_id, message):
        system_prompt = (system_prompt or "") + _SCHEDULE_INSTRUCTIONS

    _adir = _agent_dir(project["root"], agent, cwd)
    # Whole-turn baseline: the owner may update memory during the primary task
    # before the dedicated reconciliation phase starts.  Validate the receipt
    # against this snapshot, not merely against the start of finalization.
    turn_memory_before = _memory_snapshot(_adir)

    override = db.get_agent_override(slug, agent_id) or {}
    capability_policy = db.get_agent_capability_policy(slug, agent_id)
    model = override.get("model") or agent.get("model", "claude")
    requested_skills = (
        capabilities.explicit_skill_inputs(user_message, capability_policy)
        if model == "codex" else []
    )
    stream_fn = get_stream(model)
    effort = override.get("effort") if "effort" in override else agent.get("effort")
    notion_runtime_env = _agent_runtime_env(project, slug, agent_id)

    # Resume guard. Only continue a prior CLI session if its last turn ended
    # cleanly. Sessions left in "running" (orphan from a uvicorn restart, swept
    # to "cancelled" by the startup reaper), "cancelled" (user hit stop or
    # browser disconnected mid-stream — claude server state may be torn), or
    # "error" cannot be safely resumed: claude --resume into a half-finished
    # state often returns empty or hangs silently. Better to start fresh.
    resume_sid = (sess.get("claude_session_id")
                  if sess.get("last_status") == "ok" and sess.get("cli_adapter") in (None, model)
                  else None)

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

    # ----- CHILD-OVERVIEW PRE-FLIGHT -----
    # Unlike the cold-start recap, this runs on EVERY parent turn, including a
    # resumed CLI session. That guarantees routing uses the children's current
    # slim state rather than an old conversation snapshot or only the derived
    # children_status rollup.
    child_overviews, overview_accesses = _children_overview_context(project, agent_id)
    if child_overviews:
        message = child_overviews + "\n\n---\n\n" + message
        for access in overview_accesses:
            await emit({
                "type": "resource_access",
                "agent": agent_id,
                "owner": access["owner"],
                "kind": "overview",
                "operation": "reading",
                "tool": "control-plane pre-flight",
                "path": access["path"],
            })
    # ----- END CHILD-OVERVIEW PRE-FLIGHT -----

    if model == "claude":
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("claude_model") or agent.get("claude_model") or "claude-sonnet-4-6",
            effort=effort,
            resume_session_id=resume_sid,
            extra_env=notion_runtime_env,
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
            runtime_env=notion_runtime_env,
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
            runtime_env=notion_runtime_env,
        )
    elif model == "codex":
        codex_effort = effort if effort in (None, "default", "low", "medium", "high", "xhigh") else None
        agen = stream_fn(
            message=message,
            system_prompt=system_prompt,
            cwd=cwd,
            model=override.get("codex_model") or agent.get("codex_model") or "gpt-5.6-terra",
            effort=codex_effort,
            resume_session_id=resume_sid,
            extra_env=notion_runtime_env,
            capability_policy=capability_policy,
            requested_skills=requested_skills,
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
    claude_sid: Optional[str] = None   # CLI session id — keys the transcript token scan
    reconciliation_result: dict = {}
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
                    claude_sid = data["claude_session_id"]
                    db.set_cli_session_id(sess["id"], claude_sid, data.get("cli_adapter") or model)
                if data.get("init"):
                    # The init card and the node panel's tools list must be visible on
                    # an idle agent and survive a browser reload, so the payload is
                    # persisted here rather than living only in the SSE stream.
                    db.set_session_init(sess["id"], data)
                if data.get("usage"):
                    db.set_session_usage(sess["id"], data["usage"])
                    last_usage = data["usage"]
                await emit({"type": "meta", "agent": agent_id, "data": data})
            elif etype == "thinking":
                await emit({"type": "thinking", "agent": agent_id, "text": evt.get("text", "")})
            elif etype == "status":
                await emit({"type": "status", "agent": agent_id,
                            "status": evt.get("status", "responding")})
            elif etype == "tool_use":
                # Adapter surfaced a tool call (Read/Grep/Glob/Edit/Write/Bash/
                # MCP...) with its parsed input. Forward as-is so the UI can
                # render a "files / commands accessed" bubble per agent.
                tool_name = evt.get("tool") or "?"
                tool_input = evt.get("input") or {}
                if model == "codex":
                    try:
                        target = agent_log._target(tool_name, tool_input)
                        raw_status = str(tool_input.get("status") or "").lower()
                        log_status = ("err" if raw_status in ("failed", "error") else
                                      "ok" if raw_status in ("completed", "success", "ok") else "…")
                        db.add_cli_event(sess["id"], "tool", tool_name, target, log_status)
                    except Exception:
                        pass
                await emit({"type": "tool_use", "agent": agent_id,
                            "tool": tool_name, "input": tool_input})
            elif etype == "skill_use":
                await _record_skill_usage_event(
                    project_slug=slug,
                    agent_id=agent_id,
                    session_id=sess["id"],
                    event=evt,
                    emit=emit,
                )
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
        # GelSight pilot: after the primary answer, invoke the SAME owner agent in
        # a narrow internal phase to reconcile progress → manifest → overview.
        # Other projects are unaffected unless they explicitly opt in via
        # .agentui/project.yaml. A memory failure never erases a valid task answer;
        # it remains visible as an auditable stale-provenance warning next turn.
        if final_status == "ok" and final_text and _memory_reconciliation_config(project):
            try:
                reconciliation_result = await _run_memory_reconciliation(
                    project=project,
                    agent=agent,
                    agent_dir=_adir,
                    final_text=final_text,
                    turn_before=turn_memory_before,
                    stream_fn=stream_fn,
                    model=model,
                    override=override,
                    effort=effort,
                    resume_session_id=claude_sid or resume_sid,
                    cwd=cwd,
                    runtime_env=notion_runtime_env,
                    capability_policy=capability_policy,
                    emit=emit,
                    session_id=sess["id"],
                )
                if reconciliation_result.get("session_id"):
                    claude_sid = reconciliation_result["session_id"]
            except asyncio.CancelledError:
                final_status = "cancelled"
                raise
            except Exception as exc:
                reconciliation_result = {"ok": False, "error": str(exc)[:240]}
                db.add_cli_event(sess["id"], "memory", "reconciliation",
                                 str(exc)[:240], "error")
                await emit({"type": "meta", "agent": agent_id, "data": {
                    "memory_reconciliation": {
                        "status": "error", "message": str(exc)[:240]
                    }
                }})
            if not reconciliation_result.get("ok"):
                # Publish gate: keep progress/manifest edits for audit and review,
                # but never expose an unverified overview BODY as current truth.
                # Restore the exact pre-turn overview (including its old provenance);
                # structured finalization below marks it needs_review.
                previous_overview = turn_memory_before.get("overview_text") or ""
                if previous_overview:
                    try:
                        (_adir / "overview.md").write_text(
                            previous_overview, encoding="utf-8")
                    except OSError:
                        pass
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

        # ----- EXACT TOKEN ACCOUNTING (toggle: token_count_enabled) -----
        # Harvest the CLI's own transcript, which records the usage the SERVER returned
        # for every API request of this turn — including everything the CLI injected on
        # its own (CLAUDE.md, skills, tool schemas, tool results). Booked as a delta
        # against what earlier turns of this claude session already recorded.
        try:
            # to_thread — same event-loop-starvation reason as the compact-path scan.
            if model == "codex" and claude_sid and last_usage and final_status == "ok":
                fresh = int(last_usage.get("input_tokens") or 0)
                cached = int(last_usage.get("cache_read_input_tokens") or 0)
                cache_write = int(last_usage.get("cache_creation_input_tokens") or 0)
                output = int(last_usage.get("output_tokens") or 0)
                occupancy = fresh + cached + cache_write + output
                eff_codex_model = override.get("codex_model") or agent.get("codex_model") or "gpt-5.6-terra"
                db.add_token_turn(slug, agent_id, claude_sid, 1, fresh, cache_write,
                                  cached, output, occupancy, model=eff_codex_model)
                delta = {"requests": 1, "input_tokens": fresh, "cache_creation": cache_write,
                         "cache_read": cached, "output_tokens": output, "occupancy": occupancy}
            else:
                delta = await asyncio.to_thread(_book_session_tokens, slug, agent_id, claude_sid)
            if delta:
                await emit({"type": "token_turn", "agent": agent_id, "usage": {
                    "requests": delta["requests"],
                    "output_tokens": delta["output_tokens"],
                    "occupancy": delta["occupancy"],
                }})
        except Exception:
            pass
        # ----- END EXACT TOKEN ACCOUNTING -----

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
        adir = _agent_dir(project["root"], agent, cwd)
        memory_enabled = bool(_memory_reconciliation_config(project))

        # Escalations are durable state, not sticky prose in overview.md.  The
        # control plane assigns an ID, routes that same record, and later clears
        # the projection only when an explicit ID is resolved.
        if parsed["escalate"]:
            opened = db.open_escalation(
                slug, agent_id, parsed["escalate"].get("type") or "BLOCKED",
                parsed["escalate"].get("target") or "",
                parsed["escalate"].get("reason") or "",
                parsed["escalate"].get("evidence") or "",
                sess["id"],
            )
            parsed["escalate"]["id"] = opened["escalation_id"]
            routed = _handle_escalate(parsed["escalate"], slug, project, agent_id)
            await emit({"type": "meta", "agent": agent_id,
                        "data": {"escalate": parsed["escalate"], "routed_to": routed}})

        result_resolutions = _receipt_list(
            (parsed.get("result") or {}).get("resolves"))
        receipt_resolutions = reconciliation_result.get("resolved_escalations") or []
        requested_resolutions = list(dict.fromkeys(
            result_resolutions + receipt_resolutions))
        if requested_resolutions:
            resolution_reason = (
                (parsed.get("result") or {}).get("outcome")
                or (reconciliation_result.get("fields") or {}).get("overview_reason")
                or "explicit end-of-turn resolution"
            )
            resolved_ids, rejected_ids = db.resolve_escalations(
                slug, requested_resolutions, agent_id, resolution_reason, sess["id"])
            await emit({"type": "meta", "agent": agent_id, "data": {
                "escalations_resolved": resolved_ids,
                "escalations_rejected": rejected_ids,
            }})

        # Legacy projects retain their original stamping behaviour.  An opted-in
        # project publishes a new manifest hash only after a valid receipt; an
        # invalid receipt keeps the restored old overview and marks it needs_review.
        if parsed["result"] or parsed["escalate"] or memory_enabled:
            reconciled = bool(reconciliation_result.get("ok"))
            version = _read_manifest_version(adir / "outputs" / "manifest.md")
            manifest_sha = (
                _sha256_file(adir / "outputs" / "manifest.md") if reconciled else None
            )
            _stamp_overview(
                adir,
                version if (reconciled or not memory_enabled) else None,
                parsed["escalate"],
                project_slug=slug if memory_enabled else None,
                agent_id=agent_id if memory_enabled else None,
                memory_status=("verified" if reconciled else "needs_review")
                if memory_enabled else None,
                source_manifest_sha256=manifest_sha,
                derived_at=(datetime.now(timezone.utc).isoformat(timespec="seconds")
                            if manifest_sha else None),
            )
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


def _stamp_overview(
    agent_dir: Path,
    version: Optional[str],
    escalate: Optional[dict],
    *,
    project_slug: Optional[str] = None,
    agent_id: Optional[str] = None,
    memory_status: Optional[str] = None,
    source_manifest_sha256: Optional[str] = None,
    derived_at: Optional[str] = None,
) -> None:
    """Overwrite the MACHINE fields of overview.md (agent owns the BODY, we own
    the header/footer). ``source_manifest_sha256`` is stamped only after the
    owner model has completed semantic reconciliation; it is therefore a
    provenance assertion, not merely a hash observed by the backend.

    manifest_version ← real version; last_updated ← today; body_incomplete ←
    heuristic. Opted-in projects project open escalation rows from the DB and
    always clear stale footer text when no rows remain. No-op when the agent has
    no overview yet.
    """
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
        pattern = rf"(?m)^({re.escape(key)}:).*$"
        if re.search(pattern, txt):
            txt = re.sub(pattern, lambda m: f"{m.group(1)} {val}", txt, count=1)
            return
        # New provenance fields live in the machine-owned footer.  Existing
        # overviews migrate in place without requiring an agent-authored rewrite.
        footer_close = "<!-- /OVERVIEW:FOOTER -->"
        if footer_close in txt:
            txt = txt.replace(footer_close, f"{key}: {val}\n{footer_close}", 1)

    if version:
        _set("manifest_version", version)
    _set("body_incomplete", "true" if incomplete else "false")
    _set("last_updated", today)
    if project_slug and agent_id:
        open_rows = db.get_open_escalations(project_slug, agent_id)
        escalation_ids = [row["escalation_id"] for row in open_rows]
        if open_rows:
            summaries = [
                f"{row['escalation_id']} {row.get('type') or '?'}→"
                f"{row.get('target') or '?'}: {row.get('reason') or ''}"
                for row in open_rows
            ]
            _set("open_escalation", " | ".join(summaries)[:480])
            _set("open_escalation_ids", ",".join(escalation_ids))
        else:
            _set("open_escalation", "none")
            _set("open_escalation_ids", "none")
    elif escalate:
        summary = (f"{escalate.get('type', '?')}→{escalate.get('target', '?')}: "
                   f"{escalate.get('reason', '')}")[:120]
        _set("open_escalation", summary)
    if memory_status:
        _set("memory_status", memory_status)
    if source_manifest_sha256:
        _set("source_manifest_sha256", source_manifest_sha256)
    if derived_at:
        _set("derived_at", derived_at)

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


def _read_overview_field(agent_dir: Path, key: str) -> Optional[str]:
    """Read one machine field from overview HEADER/FOOTER."""
    ov = agent_dir / "overview.md"
    try:
        for line in ov.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(rf"\s*{re.escape(key)}:\s*(.*?)\s*$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip() or None
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


def _check_overview_staleness(project_root: str, project: dict, agent_id: str) -> Optional[str]:
    """Overview BODY staleness — the agent owns its overview BODY (control-plane
    only stamps HEADER/FOOTER), so a full-but-wrong body passes body_incomplete:
    false and rots silently. Mirror the stale_memory idea onto the BODY.

    Two signals, either fires:
      (1) RELATIVE — for an orchestrator, BODY mtime older than its
          state/children_status.json (the live rollup it is supposed to summarize);
          for a leaf, BODY mtime older than its own outputs/manifest.md (producer
          bumped but body not refreshed). Catches "read live data, route off it,
          never wrote it back".
      (2) ABSOLUTE — BODY older than _STALE_BODY_ABS_S regardless of activity.
    Returns a human warning string, or None when fresh / no overview / body incomplete.
    """
    agents = {a["id"]: a for a in project["agents"]}
    agent = agents.get(agent_id)
    if not agent:
        return None
    adir = _agent_dir(project_root, agent, projects.resolve_cwd(project_root, agent.get("cwd", ".")))
    ov = adir / "overview.md"
    if not ov.is_file():
        return None
    # Skip if BODY is already flagged incomplete — that is surfaced separately.
    if _read_overview_flag(adir) is True:
        return None


    # New policy: overview is a compact projection of the owner's own manifest.
    # Compare the attested source hash, not filesystem mtimes.  A matching hash
    # remains fresh indefinitely; a manifest rewrite becomes stale immediately.
    # This also removes the migration bug where writing manifest one minute after
    # overview forced the next task to abandon its real work and rewrite memory.
    if _memory_reconciliation_config(project):
        memory_status = _read_overview_field(adir, "memory_status")
        if memory_status != "verified":
            return (
                "overview memory_status is "
                f"{memory_status or 'missing'}; last reconciliation needs review"
            )
        manifest = adir / "outputs" / "manifest.md"
        current_sha = _sha256_file(manifest)
        recorded_sha = _read_overview_field(adir, "source_manifest_sha256")
        if not current_sha:
            return "owner manifest is missing or unreadable; provenance cannot be verified"
        if not recorded_sha:
            return "overview has no source_manifest_sha256 provenance yet"
        if recorded_sha != current_sha:
            return (
                "overview summarizes an older manifest "
                f"(source {recorded_sha[:12]}…, current {current_sha[:12]}…)"
            )
        return None

    try:
        ov_mtime = ov.stat().st_mtime
    except OSError:
        return None
    now = time.time()

    has_children = any(agent_id in (a.get("parents") or []) for a in project["agents"])
    ref_path = adir / "state" / "children_status.json" if has_children \
        else adir / "outputs" / "manifest.md"
    ref_label = "state/children_status.json (live rollup you must summarize)" if has_children \
        else "outputs/manifest.md (your own producer bump)"
    try:
        ref_mtime = ref_path.stat().st_mtime
    except OSError:
        ref_mtime = None

    reasons: list[str] = []
    if ref_mtime and ov_mtime + _STALE_BODY_GRACE_S < ref_mtime:
        age = int(ref_mtime - ov_mtime)
        reasons.append(f"BODY is {age//3600}h older than {ref_label}")
    if now - ov_mtime > _STALE_BODY_ABS_S:
        reasons.append(f"BODY untouched for {(now-ov_mtime)//86400}d (floor {_STALE_BODY_ABS_S//86400}d)")
    if not reasons:
        return None
    return ("; ".join(reasons)) + " — rewrite the OVERVIEW:BODY block from the live "
    "rollup this turn (control-plane only stamps HEADER/FOOTER; the BODY is yours)"


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


@trace_dispatch
async def _dispatched_run(slug, source_id, target_id, task, emit, tracker, chain):
    """Run a worker dispatched by source_id. Capture its final text and write
    it to the dispatch_results ledger so source_id can see the output on its
    next prompt (via enrichment in _run_agent).
    """
    status = "ok"
    error_msg = None
    result_text = ""
    manifest_before = _manifest_snapshot(slug, target_id)
    manifest_after = None
    manifest_changed = None
    goal_claimed = None
    try:
        # Auto-compact the WORKER before it runs its dispatched task. Without this,
        # a worker only ever reaches _run_agent via dispatch (never _start_run), so
        # the auto-compact checkpoint in _start_run.driver() never fires for it and
        # its context grows unbounded across successive dispatches (observed:
        # RESEARCHER in the aecbench project past 65% with no rotation). Same
        # pre-turn semantics as the top-level path: it uses the last completed
        # turn's usage, skips torn sessions, and no-ops below the threshold.
        await _auto_compact_if_needed(slug, target_id, emit)
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
        manifest_after = _manifest_snapshot(slug, target_id)
        if manifest_after:
            manifest_changed = manifest_after["mtime"] != manifest_before["mtime"]
        if manifest_after and not manifest_changed:
            result_text = (result_text or "") + (
                f"\n\n[control-plane verify] outputs/manifest.md of {target_id} did NOT change "
                f"during this dispatch (still version {manifest_after.get('version') or '?'}). If the task "
                "created/modified a downstream artifact → the result is NOT yet published per contract; "
                "require the worker to bump the manifest before consuming."
            )
        elif manifest_after:
            result_text = (result_text or "") + (
                f"\n\n[control-plane verify] outputs/manifest.md was updated "
                f"(version {manifest_before.get('version') or '?'} → {manifest_after.get('version') or '?'})."
            )

    # Goal-gate (spec §6.8/§14.4): an Outcome dispatch carries a goal contract. The
    # control-plane cannot read arbitrary project metrics, so it enforces PROCESS, not
    # the value: a worker's self-reported goal_status is a CLAIM, never acceptance —
    # acceptance is by the external authority. (Budget/loop is bounded by the existing
    # continuation cap; metric plateau detection would need a project-specific reader.)
    if status == "ok":
        goal = _parse_goal(task)
        if goal:
            goal_claimed = (_parse_structured(result_text or "").get("result") or {}).get("goal_status")
            acc = goal.get("acceptance_by", "control_plane")
            note = (f"\n\n[control-plane goal-gate] acceptance_by={acc}. Worker self-reported "
                    f"goal_status={goal_claimed or 'none'} — a CLAIM, not acceptance. Do NOT accept on "
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
    return {
        "status": status,
        "result": result_text,
        "error": error_msg,
        "manifest_before": manifest_before,
        "manifest_after": manifest_after,
        "manifest_changed": manifest_changed,
        "goal_claimed": goal_claimed,
    }


# Above this share of the context window, the next user turn triggers an
# automatic compact (summary → fresh seeded session) BEFORE the turn runs.
# 40% (was 80%): lowered 2026-07-01 because reasoning adapters (glm-5.2 etc.)
# over-think on large cached context — observed a BOSS session reach 459k
# cached input tokens (46% of its 1M window) with 80% never firing, driving
# ~38k-token thinking traces and verbose output. 40% fires compaction early
# enough (≈400k on a 1M window) to keep context lean across all adapters.
# 40% still leaves ample headroom for the summary turn itself to complete.
#
# Per-adapter since 2026-07-11: 40% was tuned for the WORST adapter and then applied
# to every node. Claude does not over-think on large cached context, so compacting it
# at 400k of a 1M window doubles the compaction rate for no benefit — and each compact
# costs a full extra turn on the largest session plus a lossy recap. Claude keeps the
# original 70%; the reasoning adapters keep the 40% that was measured for them.
_AUTO_COMPACT_PCT = 40.0                        # default: glm / deepseek / grok
_AUTO_COMPACT_PCT_BY_KIND = {"claude": 70.0, "codex": 70.0}


def _auto_compact_pct(model_kind: str) -> float:
    return _AUTO_COMPACT_PCT_BY_KIND.get(model_kind, _AUTO_COMPACT_PCT)

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
    project, agent = found
    root = project["root"]
    sessions = db.list_sessions(slug, agent_id)
    sess = sessions[0] if sessions else None
    if not sess or sess.get("last_status") != "ok" or not sess.get("claude_session_id"):
        return False
    ov = db.get_agent_override(slug, agent_id) or {}
    # The ADAPTER override must win here exactly as it does in _run_agent (`override
    # .get("model") or agent.get("model")`). Reading only project.yaml made this the
    # one place that disagreed with the process actually running: aecbench/BOSS is
    # `model: glm` in yaml but was switched to claude-opus-4-8 via /adapter, so the
    # turn ran on Claude while the threshold was computed for glm — 40% instead of
    # 70%. It auto-compacted at 40.4/41.6/40.7%, rotating BOSS's session ~2x more
    # often than intended and dropping context it still needed.
    model_kind = ov.get("model") or agent.get("model", "claude")
    if model_kind == "grok":
        eff_model = ov.get("grok_model") or agent.get("grok_model") or "grok-build"
    elif model_kind == "deepseek":
        eff_model = ov.get("deepseek_model") or agent.get("deepseek_model") or "deepseek-v4-flash"
    elif model_kind == "glm":
        eff_model = ov.get("glm_model") or agent.get("glm_model") or "glm-4.6"
    elif model_kind == "codex":
        eff_model = ov.get("codex_model") or agent.get("codex_model") or "gpt-5.6-terra"
    else:
        eff_model = ov.get("claude_model") or agent.get("claude_model") or "claude-sonnet-4-6"
    # Numerator, best source first: the CLI transcript gives real per-request occupancy,
    # so the chars/4 estimate (which cannot see tool output the CLI read internally, and
    # therefore under-counts exactly the tool-heavy agents that need compacting) is now
    # only a last resort for sessions with no transcript at all (grok).
    window = _context_window_for(model_kind, eff_model)
    # to_thread: the scan parses a transcript that can reach tens of MB over NFS.
    # Inline it would block the event loop — which also runs every PTY reader, so a
    # slow scan here froze a LIVE claude child mid-turn (observed 2026-07-16).
    codex_ctx = (await asyncio.to_thread(agent_log.codex_context, sess["claude_session_id"])) \
        if model_kind == "codex" else None
    if codex_ctx and codex_ctx.get("window"):
        window = codex_ctx["window"]
    scanned = (await asyncio.to_thread(tokens.scan, sess["claude_session_id"])) \
        if model_kind != "codex" else None
    if (codex_ctx or scanned) and window:
        occupancy = codex_ctx["occupancy"] if codex_ctx else scanned["occupancy"]
        pct = round(min(100.0, occupancy / window * 100.0), 1)
    else:
        est_chars = sum(len(m.get("content") or "") for m in db.get_messages(sess["id"]))
        occupancy = _ctx_estimate_tokens(root, agent, est_chars)
        pct = _session_context_pct(sess, model_kind, eff_model, occupancy)

    policy = db.get_context_policy(slug, agent_id)
    if policy and policy["mode"] == "off":
        return False
    action = policy["mode"] if policy else "compact"
    threshold = int(policy["threshold_tokens"]) if policy else None
    if threshold is not None:
        should_rotate = occupancy >= threshold
    else:
        should_rotate = pct >= _auto_compact_pct(model_kind)
    if not should_rotate:
        return False

    event = {"type": "compact_started", "agent": agent_id, "auto": True,
             "action": action, "pct": pct, "tokens": occupancy, "threshold_tokens": threshold}
    await emit(event)

    # Clear intentionally creates no recap: it books the outgoing turn and starts
    # cold from AGENT.md/state on the pending user turn.
    if action == "clear":
        await asyncio.to_thread(_book_current_session_tokens, slug, agent_id)
        new_sess = db.new_session(slug, agent_id)
        db.add_message(
            new_sess["id"], "assistant",
            f"🧹 **Auto-clear @ {occupancy:,} context tokens** — threshold "
            f"{threshold:,}; the next turn starts a fresh CLI session.",
        )
        db.update_session_status(new_sess["id"], "ok")
        await emit({"type": "compacted", "agent": agent_id, "new_session_id": new_sess["id"],
                    "auto": True, "action": "clear"})
        return True

    tracker: list = []
    summary = (await _run_agent(slug, agent_id, _COMPACT_PROMPT, emit, tracker) or "").strip()
    if tracker:
        await asyncio.gather(*tracker, return_exceptions=True)
    # Rotation point: book the outgoing session's remaining usage (idempotent — the
    # compact turn's own finally-booking normally leaves a zero delta here).
    await asyncio.to_thread(_book_current_session_tokens, slug, agent_id)
    new_sess = db.new_session(slug, agent_id)
    if summary:
        db.set_session_seed(new_sess["id"], summary)
        db.add_message(
            new_sess["id"], "assistant",
            f"📦 **Auto-compact @ {occupancy:,} tokens ({pct}%)** — new session seeded with the recap below. "
            "The next turn continues from this recap.\n\n---\n\n" + summary,
        )
    else:
        # Summary turn failed (likely the old session was too overloaded to
        # answer). Still rotate to a fresh session — staying above the threshold
        # is worse. The cold-start preamble (state/progress.md) covers recovery.
        db.add_message(
            new_sess["id"], "assistant",
            f"📦 **Auto-compact @ {occupancy:,} tokens ({pct}%)** — recap empty (old session overloaded?); "
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

    @trace_orchestration
    async def driver(*, project_slug: str, root_agent_id: str,
                     user_message: str, run_origin: str):
        # Multi-round continuation: after each wave of dispatches lands in the
        # ledger, the orchestrator gets another turn to react — synthesise, or
        # chain follow-up dispatches — inside the SAME run, up to
        # _MAX_CONT_ROUNDS. Dispatches fired on the final round still run to
        # completion (results land in the ledger for the NEXT user turn); they
        # just don't trigger another continuation.
        all_tasks: list = []
        cur: list = []
        saw_sched = False   # did any turn this run emit a <schedule>/<schedule_stop> tag?
        rounds = 0
        root_turn_count = 0
        final_response = ""
        caught_error = None
        cancelled = False
        try:
            await _auto_compact_if_needed(slug, agent_id, emit)
            txt = await _run_agent(slug, agent_id, message, emit, cur,
                                   grok_options=grok_options)
            root_turn_count += 1
            final_response = txt or ""
            saw_sched = saw_sched or _has_schedule_tag(txt)
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
                root_turn_count += 1
                final_response = txt or ""
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
                    and _scheduler_enabled()
                    and _looks_like_tracking_intent(message)):
                ncur: list = []
                txt = await _run_agent(slug, agent_id, _SCHEDULE_NUDGE, emit, ncur)
                root_turn_count += 1
                final_response = txt or ""
                if ncur:
                    await asyncio.gather(*ncur, return_exceptions=True)
                    all_tasks.extend(ncur)
        except asyncio.CancelledError:
            cancelled = True
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
            caught_error = str(e)
            await emit({"type": "error", "agent": agent_id, "message": str(e)})
        finally:
            run.finish()

        dispatches = [
            {
                "source": event.get("source"),
                "target": event.get("target"),
                "task": event.get("task", ""),
            }
            for event in run.events
            if event.get("type") == "dispatch_started"
        ]
        rejected_dispatches = [
            {
                "source": event.get("source"),
                "target": event.get("target"),
                "reason": event.get("reason", ""),
            }
            for event in run.events
            if event.get("type") == "dispatch_rejected"
        ]
        dispatch_failures = [
            {
                "source": event.get("source"),
                "target": event.get("target"),
                "status": event.get("status"),
                "message": event.get("message"),
            }
            for event in run.events
            if (event.get("type") == "dispatch_complete"
                and event.get("status") not in (None, "ok"))
        ]
        errors = [
            {
                "agent": event.get("agent"),
                "message": event.get("message", ""),
            }
            for event in run.events
            if event.get("type") == "error"
        ]
        if cancelled:
            status = "cancelled"
        elif caught_error or any(error.get("agent") == agent_id for error in errors):
            status = "error"
        elif dispatch_failures or rejected_dispatches:
            status = "partial"
        else:
            status = "ok"
        return {
            "status": status,
            "final_response": final_response,
            "trajectory": [item["target"] for item in dispatches],
            "dispatches": dispatches,
            "dispatch_count": len(dispatches),
            "rejected_dispatches": rejected_dispatches,
            "dispatch_failures": dispatch_failures,
            "continuation_rounds": rounds,
            "root_turn_count": root_turn_count,
            "errors": errors,
            "error": caught_error,
        }

    run.task = asyncio.create_task(driver(
        project_slug=slug,
        root_agent_id=agent_id,
        user_message=message,
        run_origin=origin,
    ))
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


def _token_counting_enabled() -> bool:
    """Global switch (same pattern as the scheduler toggle). Off by default: when on,
    every turn that finishes gets its transcript scanned and booked into token_turns."""
    return db.get_setting("token_count_enabled", "0") == "1"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _book_session_tokens(slug: str, agent_id: str, csid: Optional[str]) -> Optional[dict]:
    """Scan a claude session's transcript and book the yet-unbooked remainder into
    token_turns. Idempotent: the booking is a delta against everything already recorded
    for this csid, so calling it twice books nothing the second time.

    Called from three places, which together close every leak:
      1. _run_agent finally — the normal per-turn booking.
      2. Session ROTATION (/clear, /compact, auto-compact) — the last chance to read the
         old transcript. After rotation every later scan points at the NEW session's
         file, so an unbooked remainder on the old one would be lost forever (the case:
         a turn whose finally-booking failed, or a crash before it ran).
      3. Startup sweep over sessions left 'running' — a killed server never reached (1).
    """
    if not (csid and _token_counting_enabled()):
        return None
    try:
        # force=True: booking is final (especially at rotation — the old csid is never
        # scanned again), so it must not accept a TTL-stale scan missing the turn's tail.
        scanned = tokens.scan(csid, force=True)
        if not scanned:
            return None
        booked = db.session_token_booked(csid)
        d_req = scanned["requests"] - (booked.get("requests") or 0)
        if d_req <= 0:
            return None
        delta = {
            "requests": d_req,
            "input_tokens": scanned["input_tokens"] - (booked.get("input_tokens") or 0),
            "cache_creation": scanned["cache_creation"] - (booked.get("cache_creation") or 0),
            "cache_read": scanned["cache_read"] - (booked.get("cache_read") or 0),
            "output_tokens": scanned["output_tokens"] - (booked.get("output_tokens") or 0),
            "occupancy": scanned["occupancy"],
        }
        db.add_token_turn(project_slug=slug, agent_id=agent_id,
                          claude_session_id=csid, model=scanned.get("model"), **delta)
        return delta
    except Exception:
        return None


def _book_current_session_tokens(slug: str, agent_id: str) -> None:
    """Book the agent's CURRENT session before rotating it away. Call sites: the three
    new_session() rotation points (/clear, /compact, auto-compact)."""
    sessions = db.list_sessions(slug, agent_id)
    if sessions:
        _book_session_tokens(slug, agent_id, sessions[0].get("claude_session_id"))


@app.get("/api/projects/{slug}/agents/{agent_id}/log")
async def api_agent_log(slug: str, agent_id: str, since: int = 0):
    """Tool-activity tail for one agent (dashboard "Agent log" tab). Returns tool_use /
    result / say events appended to the transcript after byte `since`, plus the new
    offset to pass next poll. The tail parse runs in a thread — multi-MB file over NFS."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    sessions = db.list_sessions(slug, agent_id)
    sess = sessions[0] if sessions else None
    csid = sess.get("claude_session_id") if sess else None
    if not csid:
        return {"events": [], "offset": 0, "session": None, "reset": False, "status": "no session"}
    if sess.get("cli_adapter") == "codex":
        persisted = await asyncio.to_thread(agent_log.codex_tail, csid, since)
        if persisted.get("events") or persisted.get("offset", since) != since:
            persisted["status"] = sess.get("last_status")
            persisted["agent_running"] = any(
                (not run.done) and run.slug == slug and run.agent_id == agent_id
                for run in _RUNS.values())
            return persisted
        rows = db.list_cli_events(sess["id"], since)
        events = [{
            "ts": datetime.fromtimestamp(r["created_at"], timezone.utc).isoformat(),
            "kind": r["kind"], "tool": r.get("tool") or "",
            "target": r.get("target") or "", "status": r.get("status") or "",
            "id": r.get("event_key"), "out_tokens": None, "round_tokens": None,
        } for r in rows]
        return {"events": events, "offset": rows[-1]["id"] if rows else since,
                "session": csid, "reset": False, "seeded": False,
                "status": sess.get("last_status"),
                "agent_running": any((not run.done) and run.slug == slug and run.agent_id == agent_id
                                     for run in _RUNS.values())}
    # The frontend passes back the session id it last polled; if the current session id
    # differs (a /clear created a new one), its byte offset is meaningless — tail from 0.
    res = await asyncio.to_thread(agent_log.tail, csid, since)
    res["status"] = sess.get("last_status") if sess else None
    res["agent_running"] = any((not r.done) and r.slug == slug and r.agent_id == agent_id
                               for r in _RUNS.values())
    return res


@app.get("/api/dashboard")
async def api_dashboard(bucket: str = "hour", hours: int = 48):
    """Everything the dashboard page renders, in ONE call: per-agent live state across
    every project, lifetime token totals, and a bucketed time series.

    Deliberately one endpoint, not N: the page polls, and fanning out to /stats per
    project would re-scan every transcript on every tick. The transcript scan runs in a
    thread (it parses multi-MB JSONL over NFS — inline it would stall the event loop and
    with it every live agent's PTY reader).
    """
    if bucket not in ("15m", "hour", "day", "week"):
        bucket = "hour"
    hours = max(1, min(int(hours or 48), 24 * 365))

    totals = {(t["project_slug"], t["agent_id"]): t for t in db.token_totals_all()}
    series = await asyncio.to_thread(db.token_series, bucket, hours)

    agents: list[dict] = []
    for project in projects.list_projects():
        slug = project["slug"]
        full = projects.get_project(slug)
        if not full:
            continue
        overrides = db.list_agent_overrides(slug)
        context_policies = db.list_context_policies(slug)
        for a in full["agents"]:
            aid = a["id"]
            ov = overrides.get(aid) or {}
            context_policy = context_policies.get(aid)
            model_kind = ov.get("model") or a.get("model", "claude")
            if model_kind == "grok":
                eff_model = ov.get("grok_model") or a.get("grok_model") or "grok-build"
            elif model_kind == "deepseek":
                eff_model = ov.get("deepseek_model") or a.get("deepseek_model") or "deepseek-v4-flash"
            elif model_kind == "glm":
                eff_model = ov.get("glm_model") or a.get("glm_model") or "glm-4.6"
            elif model_kind == "codex":
                eff_model = ov.get("codex_model") or a.get("codex_model") or "gpt-5.6-terra"
            else:
                eff_model = ov.get("claude_model") or a.get("claude_model") or "claude-sonnet-4-6"

            sessions = db.list_sessions(slug, aid)
            sess = sessions[0] if sessions else None
            window = _context_window_for(model_kind, eff_model)
            ctx_tokens = None
            if sess and sess.get("claude_session_id") and model_kind == "codex":
                codex_ctx = await asyncio.to_thread(agent_log.codex_context, sess["claude_session_id"])
                if codex_ctx:
                    ctx_tokens = codex_ctx["occupancy"]
                    window = codex_ctx.get("window") or window
            if sess and sess.get("claude_session_id") and model_kind != "codex":
                scanned = await asyncio.to_thread(tokens.scan, sess["claude_session_id"])
                if scanned:
                    ctx_tokens = scanned["occupancy"]
            t = totals.get((slug, aid)) or {}
            billed = ((t.get("input_tokens") or 0) + (t.get("cache_creation") or 0)
                      + (t.get("cache_read") or 0) + (t.get("output_tokens") or 0))
            # Fixed context cost — the task-independent bytes this agent (re)loads every
            # session (system prompt + its pre-flight file list). Cheap (reads a few small
            # files, cached by mtime), so it's fine on the polled dashboard endpoint.
            adir = _agent_dir(project["root"],
                              {"id": aid, "system_prompt_file": a.get("system_prompt_file", ""),
                               "cwd": a.get("cwd", ".")},
                              projects.resolve_cwd(project["root"], a.get("cwd", ".")))
            fc = await asyncio.to_thread(fixed_cost.estimate, project["root"], adir,
                                         a.get("system_prompt_file", ""))
            agents.append({
                "project": slug,
                "project_name": project.get("name") or slug,
                "agent": aid,
                "role": a.get("role"),
                "model_kind": model_kind,
                "model": eff_model,
                "status": db.get_last_status(slug, aid) or "idle",
                "context_tokens": ctx_tokens,
                "context_window": window,
                "context_pct": round(min(100.0, (ctx_tokens or 0) / window * 100.0), 1) if window else 0.0,
                "compact_at_pct": _auto_compact_pct(model_kind),
                "context_policy": context_policy or {"mode": "default", "threshold_tokens": None},
                "compact_at_tokens": (context_policy.get("threshold_tokens") if context_policy
                                      and context_policy.get("mode") in {"compact", "clear"}
                                      else round(window * _auto_compact_pct(model_kind) / 100)),
                "updated_at": sess.get("updated_at") if sess else None,
                "turns": t.get("turns") or 0,
                "requests": t.get("requests") or 0,
                "output_tokens": t.get("output_tokens") or 0,
                "cache_read": t.get("cache_read") or 0,
                "billed_total": billed,
                "last_at": t.get("last_at"),
                "fixed_cost": fc["tokens"],
                "fixed_cost_sys": fc["system_prompt"],
                "fixed_cost_preflight": fc["preflight"],
                "fixed_cost_files": fc["files"],
            })
            # Daily snapshot (idempotent per day) — the fixed cost drifts as state files
            # grow/shrink, so one row per agent per day builds the trend.
            try:
                db.upsert_fixed_cost(slug, aid, _today_str(), fc["tokens"],
                                     fc["system_prompt"], fc["preflight"])
            except Exception:
                pass

    return {
        "agents": agents,
        "series": series,
        "bucket": bucket,
        "hours": hours,
        "counting_enabled": _token_counting_enabled(),
        "active_runs": [{"project": r.slug, "agent": r.agent_id} for r in _RUNS.values() if not r.done],
        "server_time": time.time(),
        "fixed_cost_series": db.fixed_cost_series(30),
    }


@app.post("/api/tokens/backfill")
async def api_tokens_backfill():
    """REBUILD the whole token ledger from transcripts, bucketed by the REAL date of
    each request. The transcript is the source of truth, so this wipes token_turns and
    re-derives every row with scan_by_day — fixing the earlier backfill that stamped all
    historical turns with the run date (collapsing the whole time chart onto one day).

    One row per (session, day). Auto-book keeps running for live turns afterwards, on
    today's date, which is correct. Safe to re-run: it always rebuilds from scratch."""
    # Refuse while any turn is in flight: the rebuild wipes the ledger, and a turn
    # finishing mid-rebuild would book its delta against the freshly-wiped table (full
    # session total) right before the rebuild re-adds the same session — double count.
    live = [f"{r.slug}/{r.agent_id}" for r in _RUNS.values() if not r.done]
    if live:
        raise HTTPException(409, f"backfill refused — runs in flight: {', '.join(live)}")
    db.delete_all_token_turns()
    rows_written = 0
    per_agent = {}
    for project in projects.list_projects():
        slug = project["slug"]
        full = projects.get_project(slug)
        if not full:
            continue
        for a in full["agents"]:
            aid = a["id"]
            seen_csid = set()
            for sess in db.list_sessions(slug, aid):
                csid = sess.get("claude_session_id")
                if not csid or csid in seen_csid:
                    continue
                seen_csid.add(csid)
                try:
                    for day in await asyncio.to_thread(tokens.scan_by_day, csid):
                        db.add_token_turn(
                            project_slug=slug, agent_id=aid, claude_session_id=csid,
                            requests=day["requests"], input_tokens=day["input_tokens"],
                            cache_creation=day["cache_creation"], cache_read=day["cache_read"],
                            output_tokens=day["output_tokens"], occupancy=day["occupancy"],
                            model=day["model"], created_at=day["epoch"])
                        rows_written += 1
                        per_agent[f"{slug}/{aid}"] = per_agent.get(f"{slug}/{aid}", 0) + day["output_tokens"]
                except Exception:
                    continue
    return {"rows_written": rows_written, "agents": len(per_agent)}


@app.get("/api/tokens/enabled")
def api_tokens_enabled():
    return {"enabled": _token_counting_enabled()}


@app.post("/api/tokens/enabled")
def api_set_tokens_enabled(body: dict):
    db.set_setting("token_count_enabled", "1" if body.get("enabled") else "0")
    return {"enabled": _token_counting_enabled()}


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


# How many recent days the hot progress.json keeps; older days rotate to the archive.
_PROGRESS_KEEP_DAYS = 2


def _progress_rotate_enabled() -> bool:
    """Global toggle (same pattern as scheduler/token). OFF by default — rotating an
    agent's own state file is intrusive, so it stays opt-in until trusted."""
    return db.get_setting("progress_rotate_enabled", "0") == "1"


async def _progress_rotate_tick() -> None:
    """Move stale days out of every agent's progress.json into its archive. Skips agents
    that are mid-turn (an active run may be about to Edit the file) and torn/never-run
    sessions — the same safety envelope as auto-compact. File I/O runs in a thread; the
    rotator must never block the event loop that also drives every PTY reader."""
    if not _progress_rotate_enabled():
        return
    for project in projects.list_projects():
        slug = project["slug"]
        full = projects.get_project(slug)
        if not full:
            continue
        for a in full["agents"]:
            aid = a["id"]
            # An active run for this agent → leave the file alone this tick.
            if any((not r.done) and r.slug == slug and r.agent_id == aid for r in _RUNS.values()):
                continue
            found = projects.get_agent(slug, aid)
            if not found:
                continue
            _proj, agent = found
            cwd_abs = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
            adir = _agent_dir(project["root"], agent, cwd_abs)
            try:
                if await asyncio.to_thread(progress_store.needs_rotation, adir, _PROGRESS_KEEP_DAYS):
                    res = await asyncio.to_thread(progress_store.rotate, adir, aid, _PROGRESS_KEEP_DAYS)
                    if res:
                        print(f"[progress-rotate] {slug}/{aid}: archived {res['archived_days']}")
                # findings.md gets the same treatment (user ruling 2026-07-18: past-error
                # log, not load-bearing — keep newest days, archive the rest).
                fres = await asyncio.to_thread(progress_store.rotate_findings, adir, aid, _PROGRESS_KEEP_DAYS)
                if fres:
                    print(f"[progress-rotate] {slug}/{aid} findings: archived {fres['archived_days']}")
            except Exception as e:
                print(f"[progress-rotate] {slug}/{aid} error: {e}")


async def _progress_rotate_loop() -> None:
    # Hourly is plenty: days cross the boundary once a day, and the rotate is idempotent.
    while True:
        await asyncio.sleep(3600)
        try:
            await _progress_rotate_tick()
        except Exception as e:
            print(f"[progress-rotate] tick error: {e}")


@app.get("/api/progress/rotate/enabled")
def api_progress_rotate_enabled():
    return {"enabled": _progress_rotate_enabled(), "keep_days": _PROGRESS_KEEP_DAYS}


@app.post("/api/progress/rotate/enabled")
async def api_set_progress_rotate_enabled(body: dict):
    db.set_setting("progress_rotate_enabled", "1" if body.get("enabled") else "0")
    # Run one pass immediately so the effect is visible without waiting for the hourly tick.
    if _progress_rotate_enabled():
        await _progress_rotate_tick()
    return {"enabled": _progress_rotate_enabled()}


@app.post("/api/projects/{slug}/agents/{agent_id}/progress/migrate")
def api_progress_migrate(slug: str, agent_id: str):
    """Build progress.json from the legacy progress.md for one agent (idempotent; the
    .md stays as a backup). Returns days migrated, or 0 if already migrated / no md."""
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")
    project, agent = found
    cwd_abs = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    adir = _agent_dir(project["root"], agent, cwd_abs)
    n = progress_store.migrate_md_to_json(adir)
    return {"migrated_days": n or 0}


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
                # Rotation point — same last-chance booking as the auto-compact path.
                await asyncio.to_thread(_book_current_session_tokens, slug, agent_id)
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


async def _cluster_jobs_snapshot():
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


@app.get("/api/cluster/jobs")
async def api_cluster_jobs():
    return await _cluster_jobs_snapshot()


@app.get("/api/usage")
async def api_usage(provider: str = "claude"):
    """Best-effort Claude or Codex subscription usage from the corresponding
    CLI's saved OAuth session. Credentials stay server-side and are never logged
    or returned; the response contains only normalized limit windows."""
    provider = (provider or "claude").strip().lower()
    if provider not in {"claude", "codex"}:
        raise HTTPException(400, "provider must be claude or codex")

    def _fetch_codex():
        """Read the same ChatGPT-backed limit snapshot used by Codex. The saved
        CLI OAuth token/account id stay server-side and are never returned."""
        import urllib.request
        auth = Path.home() / ".codex" / "auth.json"
        try:
            tokens_data = (json.loads(auth.read_text()).get("tokens") or {})
            access = tokens_data.get("access_token")
            account = tokens_data.get("account_id")
        except Exception:
            return {"available": False, "provider": "codex", "reason": "no Codex credentials"}
        if not access or not account:
            return {"available": False, "provider": "codex", "reason": "Codex not logged in"}
        req = urllib.request.Request(
            "https://chatgpt.com/backend-api/wham/usage",
            headers={"Authorization": f"Bearer {access}", "ChatGPT-Account-Id": account,
                     "User-Agent": "AgentUI-usage/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"available": False, "provider": "codex", "reason": str(e)[:120]}

        def _window(d):
            if not isinstance(d, dict):
                return None
            return {"pct": d.get("used_percent"), "reset_at": d.get("reset_at"),
                    "duration_seconds": d.get("limit_window_seconds")}

        rl = body.get("rate_limit") or {}
        periods = []
        for name, raw in (("primary", rl.get("primary_window")),
                          ("secondary", rl.get("secondary_window"))):
            window = _window(raw)
            if not window:
                continue
            seconds = int(window.get("duration_seconds") or 0)
            label = "weekly" if seconds >= 6 * 86400 else (
                f"{round(seconds / 3600)} hrs" if seconds else name)
            window["label"] = label
            periods.append(window)
        return {"available": bool(periods), "provider": "codex",
                "plan": body.get("plan_type"), "periods": periods,
                "limit_reached": bool(rl.get("limit_reached")),
                "credits": body.get("credits")}

    def _fetch_claude():
        import urllib.request
        import urllib.error
        cred = Path.home() / ".claude" / ".credentials.json"
        try:
            tok = (json.loads(cred.read_text()).get("claudeAiOauth") or {}).get("accessToken")
        except Exception:
            return {"available": False, "provider": "claude", "reason": "no credentials"}
        if not tok:
            return {"available": False, "provider": "claude", "reason": "no token"}
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
            return {"available": False, "provider": "claude", "reason": str(e)[:120]}

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
            return {"available": False, "provider": "claude", "reason": "no usage fields"}
        periods = []
        if five: periods.append({**five, "label": "5 hrs"})
        if week: periods.append({**week, "label": "weekly"})
        return {"available": True, "provider": "claude", "periods": periods,
                "five_hour": five, "weekly": week}

    return await asyncio.to_thread(_fetch_codex if provider == "codex" else _fetch_claude)


terminal_service.register_terminal_routes(app)


@app.on_event("startup")
async def _start_progress_rotator():
    asyncio.create_task(_progress_rotate_loop())


@app.on_event("startup")
async def _start_scheduler():
    asyncio.create_task(_scheduler_loop())
    # Telegram control channel (BOSS by default). No-op when no bot token is
    # configured — runs inside this loop alongside the scheduler.
    if tg.is_enabled():
        asyncio.create_task(tg.start_telegram())


@app.on_event("shutdown")
async def _stop_control_channels():
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


# Crash leg of the rotation-loss fix: book the transcript tail of every session the
# previous process left mid-turn (snapshot taken at the top of this module, before the
# reaper flipped them to 'cancelled'). Runs here — after _book_session_tokens exists.
for _rs in _orphan_sessions:
    _book_session_tokens(_rs["project_slug"], _rs["agent_id"], _rs["claude_session_id"])
