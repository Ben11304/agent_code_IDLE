from __future__ import annotations

import asyncio
import json
import re
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
    return out


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
        "version | Artifact / path | Dùng cho phần nào. One row per upstream artifact this "
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
            warnings.append(f"Thiếu file `{required}` — parent không emit. Bạn có thể tạo sau.")
    for f in files:
        if not f["path"].startswith(f"{agent_id}/"):
            warnings.append(f"`{f['path']}` không nằm trong folder `{agent_id}/` — bị từ chối lúc ghi.")
    return warnings


@app.post("/api/projects/{slug}/agents/preview-from-parent")
async def api_preview_from_parent(slug: str, body: NewAgent):
    _validate_new_agent(slug, body)
    if not body.parents:
        raise HTTPException(400, "Cần chọn ít nhất 1 parent để sinh từ parent. Hoặc dùng template preview.")
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
        "Do NOT repeat any of these in the dispatch task. No \"Bạn là X agent\", no reading lists, "
        "no path references to shared/*, no pre-flight reminders. Those are wasted tokens and the worker already has them.\n\n"
        "The dispatch task should be ONE concise statement of what to do this turn, often 1–3 sentences. "
        "If the worker's session is fresh, you may include the agent folder path "
        "(e.g. \".claude/AGENT/<NAME>/\") once as the only orientation hint. Nothing more.\n\n"
        "Examples of correct dispatch task body:\n"
        "  • \"List tất cả references cite trong paper ASCE 2027, group theo hazard. Đọc documentation/REFERENCES.md, paper/REFERENCES.md, paper/latex/references.bib.\"\n"
        "  • \"Verify câu BOSS vừa nói về formula vulnerability 0.40/0.30/0.30 trong vulnerability/energy_vulnerability_analyzer.py. Report line evidence.\"\n"
        "  • \"Continue: bổ sung thêm bullet về Cascadia exposure vào bản report bạn vừa làm.\"\n\n"
        "Examples of INCORRECT (do not produce):\n"
        "  • \"Bạn là DOCS agent. Đọc theo thứ tự bắt buộc: 1. shared/research_integrity.md 2. ...\"\n"
        "  • Reading lists, role briefings, pre-flight blocks.\n\n"
        "The system parses tags in real time and runs the worker; the user verifies your orchestration by watching "
        "the graph light up. Narrating \"I will dispatch\" without emitting the tag is a lie — user sees nothing happen.\n"
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
    db.add_message(sess["id"], "user", message, meta={"chain": list(chain)} if chain else None)
    db.update_session_status(sess["id"], "running")
    await emit({"type": "agent_status", "agent": agent_id, "status": "running"})

    system_prompt = projects.resolve_system_prompt(project["root"], agent.get("system_prompt_file", ""))
    cwd = projects.resolve_cwd(project["root"], agent.get("cwd", "."))
    children = _get_children(project, agent_id)
    if children:
        system_prompt = (system_prompt or "") + _dispatch_instructions(children)

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
                await emit({"type": "delta", "agent": agent_id, "text": text})
            elif etype == "meta":
                data = evt.get("data") or {}
                if data.get("claude_session_id"):
                    db.set_claude_session_id(sess["id"], data["claude_session_id"])
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
        await emit({"type": "agent_done", "agent": agent_id, "text": final_text, "status": final_status})

    return "".join(assembled)


async def _dispatched_run(slug, source_id, target_id, task, emit, tracker, chain):
    status = "ok"
    error_msg = None
    try:
        await _run_agent(slug, target_id, task, emit, tracker, chain)
    except Exception as e:
        status = "error"
        error_msg = str(e)
    await emit({
        "type": "dispatch_complete",
        "source": source_id,
        "target": target_id,
        "status": status,
        "message": error_msg,
    })


@app.post("/api/projects/{slug}/agents/{agent_id}/chat")
async def api_chat(slug: str, agent_id: str, body: ChatBody):
    found = projects.get_agent(slug, agent_id)
    if not found:
        raise HTTPException(404, "agent not found")

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(evt):
        await queue.put(evt)

    tracker: list = []

    grok_options = {
        "best_of_n": body.best_of_n,
        "check_loop": body.check_loop,
        "memory_mode": body.memory_mode,
    } if (body.best_of_n or body.check_loop or body.memory_mode) else None

    async def driver():
        try:
            await _run_agent(slug, agent_id, body.message, emit, tracker,
                             grok_options=grok_options)
            if tracker:
                await asyncio.gather(*tracker, return_exceptions=True)
        except asyncio.CancelledError:
            # User aborted (browser disconnect / stop button).
            # Cascade cancel to any still-running dispatched workers so they stop too.
            for t in tracker:
                if not t.done():
                    t.cancel()
            if tracker:
                await asyncio.gather(*tracker, return_exceptions=True)
            raise
        except Exception as e:
            await queue.put({"type": "error", "agent": agent_id, "message": str(e)})
        finally:
            await queue.put(None)

    async def sse():
        yield _sse({"type": "start", "agent": agent_id})
        task = asyncio.create_task(driver())
        try:
            while True:
                try:
                    # 15s timeout keeps the socket alive during long quiet
                    # phases (Opus extended thinking can sit 10-30s without
                    # emitting any byte). Without this, browsers + proxies
                    # silently close the SSE and the chat appears to hang as
                    # "đã dừng" with no response. The comment line is not a
                    # data event, so the frontend ignores it.
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
