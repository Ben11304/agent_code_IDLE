from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

try:
    from ruamel.yaml import YAML
    _ruamel = YAML()
    _ruamel.preserve_quotes = True
    _ruamel.indent(mapping=2, sequence=4, offset=2)
except ImportError:
    _ruamel = None

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "registry.yaml"


def _load_registry() -> list[Path]:
    if not REGISTRY_PATH.exists():
        return []
    with REGISTRY_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    return [Path(p).expanduser() for p in data.get("projects", [])]


def _load_project_yaml(project_root: Path) -> dict[str, Any] | None:
    cfg = project_root / ".agentui" / "project.yaml"
    if not cfg.exists():
        return None
    with cfg.open() as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("root", str(project_root))
    if "slug" not in data:
        data["slug"] = project_root.name.lower().replace(" ", "-")
    return data


def get_workspace_root() -> Path | None:
    """Workspace root for the unified folder tree.

    Reads `workspace_root` from registry.yaml if set. Otherwise computes the
    common parent directory of all registered project paths.
    """
    if not REGISTRY_PATH.exists():
        return None
    with REGISTRY_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    explicit = data.get("workspace_root")
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists() and p.is_dir():
            return p.resolve()
    paths = [Path(p).expanduser().resolve() for p in data.get("projects", []) if Path(p).expanduser().exists()]
    if not paths:
        return None
    # common ancestor of all project paths
    common_parts = list(paths[0].parts)
    for p in paths[1:]:
        parts = p.parts
        for i, (a, b) in enumerate(zip(common_parts, parts)):
            if a != b:
                common_parts = common_parts[:i]
                break
        else:
            common_parts = common_parts[:min(len(common_parts), len(parts))]
    if not common_parts:
        return None
    candidate = Path(*common_parts)
    return candidate if candidate.is_dir() else candidate.parent


def list_projects() -> list[dict[str, Any]]:
    out = []
    for root in _load_registry():
        cfg = _load_project_yaml(root)
        if not cfg:
            continue
        out.append({
            "slug": cfg["slug"],
            "name": cfg.get("name", root.name),
            "description": cfg.get("description", ""),
            "root": cfg["root"],
            "agent_count": len(cfg.get("agents", [])),
        })
    return out


def get_project(slug: str) -> dict[str, Any] | None:
    for root in _load_registry():
        cfg = _load_project_yaml(root)
        if not cfg:
            continue
        if cfg["slug"] != slug:
            continue
        agents = cfg.get("agents", []) or []
        edges = []
        for a in agents:
            for parent in a.get("parents", []) or []:
                edges.append({"source": parent, "target": a["id"]})
        return {
            "slug": cfg["slug"],
            "name": cfg.get("name", root.name),
            "description": cfg.get("description", ""),
            "root": cfg["root"],
            "agents": [
                {
                    "id": a["id"],
                    "role": a.get("role", ""),
                    "model": a.get("model", "claude"),
                    "claude_model": a.get("claude_model", "claude-sonnet-4-6"),
                    "grok_model": a.get("grok_model", "grok-build"),
                    "effort": a.get("effort"),
                    "cwd": a.get("cwd", "."),
                    "system_prompt_file": a.get("system_prompt_file", ""),
                    "parents": a.get("parents", []) or [],
                }
                for a in agents
            ],
            "edges": edges,
        }
    return None


def get_agent(slug: str, agent_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    project = get_project(slug)
    if not project:
        return None
    for a in project["agents"]:
        if a["id"] == agent_id:
            return project, a
    return None


def resolve_system_prompt(project_root: str, system_prompt_file: str) -> str:
    if not system_prompt_file:
        return ""
    path = Path(project_root) / system_prompt_file
    if not path.exists():
        return ""
    return path.read_text()


def resolve_cwd(project_root: str, cwd: str) -> str:
    p = Path(project_root) / (cwd or ".")
    if not p.exists():
        return project_root
    return str(p)


def _find_project_yaml(slug: str) -> Path | None:
    for root in _load_registry():
        cfg = root / ".agentui" / "project.yaml"
        if not cfg.exists():
            continue
        with cfg.open() as f:
            data = yaml.safe_load(f) or {}
        s = data.get("slug") or root.name.lower().replace(" ", "-")
        if s == slug:
            return cfg
    return None


def add_agent(slug: str, agent: dict[str, Any]) -> tuple[bool, str]:
    """Append a new agent entry to the project's .agentui/project.yaml.

    Uses ruamel.yaml when available so comments and key order are preserved.
    Falls back to PyYAML safe_dump (lossy on comments) if ruamel missing.

    Returns (ok, message).
    """
    cfg_path = _find_project_yaml(slug)
    if not cfg_path:
        return False, "project.yaml not found"

    if _ruamel is not None:
        with cfg_path.open() as f:
            data = _ruamel.load(f)
        if data is None:
            data = {}
        agents = data.setdefault("agents", [])
        if any((a.get("id") if isinstance(a, dict) else None) == agent["id"] for a in agents):
            return False, f"agent id already exists: {agent['id']}"
        agents.append(_clean_agent(agent))
        with cfg_path.open("w") as f:
            _ruamel.dump(data, f)
    else:
        with cfg_path.open() as f:
            data = yaml.safe_load(f) or {}
        agents = data.setdefault("agents", [])
        if any(a.get("id") == agent["id"] for a in agents):
            return False, f"agent id already exists: {agent['id']}"
        agents.append(_clean_agent(agent))
        with cfg_path.open("w") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return True, "ok"


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _render(template: str, ctx: dict[str, str]) -> str:
    out = template
    for key, val in ctx.items():
        out = out.replace("{{" + key + "}}", str(val))
    return out


def _load_template(relpath: str) -> str:
    p = TEMPLATES_DIR / relpath
    return p.read_text(encoding="utf-8") if p.exists() else ""


# Per-agent files rendered from templates/agent/** — canonical structure derived
# from ConstructionVLM-Eval-AGENT. Same set used by create_agent and the
# "+ agent" template path.
_AGENT_TEMPLATE_FILES = [
    "AGENT.md",
    "inputs/manifest.md",
    "outputs/manifest.md",
    "state/progress.md",
    "context/code_map.md",
]


def _agent_ctx(agent_id: str, role: str, parents: list[str],
               extra: dict[str, Any] | None = None) -> dict[str, str]:
    from datetime import date
    today = date.today().isoformat()
    if parents:
        input_rows = "\n".join(
            f"- {p}: unset  (not synced yet \u2014 run `sync.sh {agent_id}`)" for p in parents
        )
    else:
        input_rows = "- (none \u2014 root/orchestrator agent, no producers)"
    ctx: dict[str, str] = {
        "id": agent_id,
        "role": role or "_(one-line description of this agent's purpose)_",
        "parents_csv": ", ".join(parents) if parents else "none",
        "input_rows": input_rows,
        "date": today,
        "scope_in": "- (TBD: files/paths this agent may read & modify)",
        "scope_out": "- Other agents' folders; `../shared/` (read-only).",
        "deliverables": "",
        "escalation": "",
        "hard_rules": "",
        "owned_extra": "",
        "skills": "",
    }
    if extra:
        ctx.update({k: str(v) for k, v in extra.items() if v is not None})
    return ctx


def render_agent_files(agent_id: str, role: str, parents: list[str],
                       extra: dict[str, Any] | None = None) -> dict[str, str]:
    ctx = _agent_ctx(agent_id, role, parents, extra)
    return {rel: _render(_load_template(f"agent/{rel}"), ctx)
            for rel in _AGENT_TEMPLATE_FILES}


def _project_root_for_slug(slug: str) -> Path | None:
    cfg = _find_project_yaml(slug)
    if not cfg:
        return None
    return cfg.parent.parent  # .agentui/project.yaml → project root


def preview_agent(slug: str, agent: dict[str, Any]) -> dict[str, Any]:
    root = _project_root_for_slug(slug)
    if root is None:
        return {"error": "project not found"}

    folder_name = agent["id"]  # we keep folder name == agent id (existing pattern)
    target = root / folder_name
    files = render_agent_files(
        agent_id=agent["id"],
        role=agent.get("role", ""),
        parents=agent.get("parents") or [],
    )

    warnings: list[str] = []
    if target.exists():
        warnings.append(f"Folder already exists: {target.name}. Creation will be rejected.")
    shared = root / "shared"
    if not shared.exists():
        warnings.append("Project has no `shared/` folder yet. The templates reference `../shared/research_integrity.md` etc. Create `shared/` first or adjust the templates.")

    return {
        "target_folder": str(target),
        "folder_name": folder_name,
        "files": [{"path": f"{folder_name}/{fn}", "content": c}
                  for fn, c in files.items()],
        "warnings": warnings,
    }


def create_agent(slug: str, agent: dict[str, Any]) -> tuple[bool, str]:
    """Atomic: create folder + files + append yaml. Rollback on failure.

    If `agent["custom_files"]` is provided (list of {path, content}), write those
    files verbatim instead of the template. Each path MUST start with the agent's
    folder name. Otherwise the function falls back to the template.
    """
    root = _project_root_for_slug(slug)
    if root is None:
        return False, "project not found"

    folder_name = agent["id"]
    target = root / folder_name
    # folder_pre_exists: parent agent may have created it already during bootstrap
    # (bypassPermissions + Write tool). We tolerate this — just overwrite files and
    # update yaml. Only reject if the agent is already registered in project.yaml.
    folder_pre_exists = target.exists()

    custom = agent.get("custom_files")
    if custom:
        # validate every path stays within the agent folder
        for f in custom:
            p = f.get("path") or ""
            if not p.startswith(folder_name + "/"):
                return False, f"file path outside agent folder: {p}"
            if ".." in Path(p).parts:
                return False, f"path traversal in: {p}"
        files_to_write = {
            (Path(f["path"]).relative_to(folder_name)).as_posix(): f.get("content", "")
            for f in custom
        }
    else:
        files_to_write = render_agent_files(
            agent_id=agent["id"],
            role=agent.get("role", ""),
            parents=agent.get("parents") or [],
        )

    try:
        target.mkdir(parents=True, exist_ok=True)
        for relpath, content in files_to_write.items():
            fp = target / relpath
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        if not agent.get("system_prompt_file"):
            agent["system_prompt_file"] = f"{folder_name}/AGENT.md"
        if not agent.get("cwd"):
            agent["cwd"] = folder_name
        # drop the custom_files key before yaml — it's a transport-only field
        for_yaml = {k: v for k, v in agent.items() if k != "custom_files"}
        ok, msg = add_agent(slug, for_yaml)
        if not ok:
            raise RuntimeError(f"yaml update failed: {msg}")
    except Exception as e:
        import shutil as _shutil
        # Only wipe the folder if WE created it — don't destroy pre-existing work.
        if target.exists() and not folder_pre_exists:
            _shutil.rmtree(target, ignore_errors=True)
        return False, f"rollback: {e}"

    return True, "ok"


def _clean_agent(a: dict[str, Any]) -> dict[str, Any]:
    """Drop empty/None fields so the yaml stays minimal."""
    out: dict[str, Any] = {"id": a["id"]}
    if a.get("role"):
        out["role"] = a["role"]
    out["model"] = a.get("model", "claude")
    if a.get("claude_model"):
        out["claude_model"] = a["claude_model"]
    if a.get("grok_model"):
        out["grok_model"] = a["grok_model"]
    if a.get("effort"):
        out["effort"] = a["effort"]
    if a.get("system_prompt_file"):
        out["system_prompt_file"] = a["system_prompt_file"]
    if a.get("cwd"):
        out["cwd"] = a["cwd"]
    out["parents"] = a.get("parents") or []
    return out


# ---------------------------------------------------------------------------
# Project creation — scaffold a brand-new agent system from templates/
# (canonical structure derived from ConstructionVLM-Eval-AGENT).
# ---------------------------------------------------------------------------

_SHARED_FILES = [
    "research_integrity.md",
    "tool_conventions.md",
    "handoff_schema.md",
    "scope_decisions.md",
    "glossary.md",
]


def _project_ctx(name: str, description: str, agents: list[dict], date_str: str) -> dict[str, str]:
    ids = [a["id"] for a in agents]
    rows = "\n".join(
        f"| {a['id']} | {a.get('role', '')} | {a.get('model', 'claude')} | "
        f"{', '.join(a.get('parents') or []) or '—'} |"
        for a in agents
    ) or "| (no agents yet) | | | |"
    return {
        "project_name": name,
        "project_description": description or "",
        "agent_ids_csv": ", ".join(ids) if ids else "(none yet)",
        "agent_table": "| Agent | Role | Model | Parents |\n|---|---|---|---|\n" + rows,
        "date": date_str,
    }


def render_shared_files(ctx: dict[str, str]) -> dict[str, str]:
    return {f: _render(_load_template(f"shared/{f}"), ctx) for f in _SHARED_FILES}


def generate_sync_sh(agents: list[dict]) -> str:
    """Generate sync.sh from the agent graph. producers_for(<agent>) = its direct
    parents (the manifest edges declared in project.yaml)."""
    cases, valid = [], []
    for a in agents:
        parents = a.get("parents") or []
        if parents:
            cases.append(f'    {a["id"]})   echo "{" ".join(parents)}" ;;')
            valid.append(a["id"])
    ctx = {
        "cases_block": "\n".join(cases) if cases else '    "") echo "" ;;',
        "valid_csv": " / ".join(valid) if valid else "(none)",
    }
    return _render(_load_template("sync.sh.tmpl"), ctx)


def render_readme(ctx: dict[str, str]) -> str:
    return _render(_load_template("README.md"), ctx)


def add_project_to_registry(root: Path) -> None:
    """Append an absolute project root to registry.yaml (dedup)."""
    if _ruamel is not None and REGISTRY_PATH.exists():
        with REGISTRY_PATH.open() as f:
            data = _ruamel.load(f) or {}
        projects_list = data.setdefault("projects", [])
        if str(root) not in [str(p) for p in projects_list]:
            projects_list.append(str(root))
        with REGISTRY_PATH.open("w") as f:
            _ruamel.dump(data, f)
    else:
        data = {}
        if REGISTRY_PATH.exists():
            with REGISTRY_PATH.open() as f:
                data = yaml.safe_load(f) or {}
        projects_list = data.setdefault("projects", [])
        if str(root) not in [str(p) for p in projects_list]:
            projects_list.append(str(root))
        with REGISTRY_PATH.open("w") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def validate_new_project(root_str: str, slug: str) -> tuple[bool, str]:
    root = Path(root_str).expanduser()
    if root.exists() and not root.is_dir():
        return False, "path exists but is not a directory"
    if (root / ".agentui" / "project.yaml").exists():
        return False, "folder is already an AgentUI project (.agentui/project.yaml exists)"
    existing = {p["slug"] for p in list_projects()}
    if slug in existing:
        return False, f"slug already exists: {slug}"
    return True, "ok"


def preview_project(payload: dict[str, Any]) -> dict[str, Any]:
    """Dry-run: what files would be written, plus warnings. No disk writes."""
    from datetime import date
    root = Path(payload["root"]).expanduser()
    name = payload.get("name") or root.name
    slug = payload.get("slug") or name.lower().replace(" ", "-")
    agents = payload.get("agents") or []
    ctx = _project_ctx(name, payload.get("description", ""), agents, date.today().isoformat())

    files: list[str] = ["README.md", "sync.sh", ".agentui/project.yaml"]
    files += [f"shared/{f}" for f in _SHARED_FILES]
    for a in agents:
        files += [f"{a['id']}/{rel}" for rel in _AGENT_TEMPLATE_FILES]

    warnings: list[str] = []
    if root.exists() and any(root.iterdir()):
        warnings.append(f"Folder `{root}` already has content — the scaffold will add to it (existing files are not deleted).")
    ok, msg = validate_new_project(str(root), slug)
    if not ok:
        warnings.append(msg)
    _ = ctx  # ctx used at create time
    return {"root": str(root), "name": name, "slug": slug,
            "files": files, "warnings": warnings, "can_create": ok}


def create_project(payload: dict[str, Any]) -> tuple[bool, str, str]:
    """Atomic scaffold of a new project. Returns (ok, message, slug).

    Writes: shared/ (5 conventions), sync.sh, README.md, .agentui/project.yaml,
    one folder per agent. Appends root to registry.yaml LAST. Rolls back any
    paths WE created on failure (never touches pre-existing user files)."""
    from datetime import date
    root = Path(payload["root"]).expanduser()
    name = payload.get("name") or root.name
    slug = payload.get("slug") or name.lower().replace(" ", "-")
    description = payload.get("description", "")
    agents = payload.get("agents") or []

    ok, msg = validate_new_project(str(root), slug)
    if not ok:
        return False, msg, slug

    ctx = _project_ctx(name, description, agents, date.today().isoformat())
    created: list[Path] = []  # paths we made (for rollback)

    def _write(path: Path, content: str, executable: bool = False):
        new_dir = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if new_dir:
            created.append(path.parent)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        if not existed:
            created.append(path)
        if executable:
            import os as _os
            _os.chmod(path, 0o755)

    try:
        root.mkdir(parents=True, exist_ok=True)
        # shared/
        for fn, content in render_shared_files(ctx).items():
            _write(root / "shared" / fn, content)
        # sync.sh + README
        _write(root / "sync.sh", generate_sync_sh(agents), executable=True)
        _write(root / "README.md", render_readme(ctx))
        # project.yaml
        project_yaml = {
            "name": name,
            "slug": slug,
            "description": description,
            "agents": [_project_agent_entry(a) for a in agents],
        }
        cfg_path = root / ".agentui" / "project.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if not cfg_path.parent.exists():
            created.append(cfg_path.parent)
        with cfg_path.open("w") as f:
            yaml.safe_dump(project_yaml, f, sort_keys=False, allow_unicode=True)
        created.append(cfg_path)
        # per-agent folders
        for a in agents:
            for rel, content in render_agent_files(
                a["id"], a.get("role", ""), a.get("parents") or []
            ).items():
                _write(root / a["id"] / rel, content)
        # registry LAST
        add_project_to_registry(root.resolve())
    except Exception as e:
        import shutil as _shutil
        for p in reversed(created):
            try:
                if p.is_dir():
                    _shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
            except Exception:
                pass
        return False, f"rollback: {e}", slug

    return True, "ok", slug


def _project_agent_entry(a: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": a["id"]}
    if a.get("role"):
        out["role"] = a["role"]
    out["model"] = a.get("model", "claude")
    out["claude_model"] = a.get("claude_model") or "claude-sonnet-4-6"
    if a.get("model") == "grok":
        out["grok_model"] = a.get("grok_model") or "grok-build"
    if a.get("effort"):
        out["effort"] = a["effort"]
    out["system_prompt_file"] = f"{a['id']}/AGENT.md"
    out["cwd"] = a["id"]
    out["parents"] = a.get("parents") or []
    return out
