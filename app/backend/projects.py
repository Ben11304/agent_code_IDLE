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


_AGENT_MD_TEMPLATE = """# {{id}} Agent

## Role
{{role}}

## Required reads (in this exact order, before any substantive work)
1. `../shared/research_integrity.md`
2. `../shared/tool_conventions.md`
3. `../shared/handoff_schema.md`
4. `./inputs/manifest.md`
5. `./state/progress.md`

## Scope

**IN** — you handle:
- Whatever is declared in `./inputs/manifest.md` and `./outputs/manifest.md`.

**OUT** — escalate, do not do:
- Any artifact, file, or decision outside the manifests above.
- Modifications to other agents' folders or to `../shared/`.

## Pre-flight checklist
- [ ] All Required reads loaded.
- [ ] Inputs declared in `./inputs/manifest.md` are present at the pinned version.
- [ ] `./state/progress.md` read to know where last turn ended.

## Output contract
- Produce only the artifacts declared in `./outputs/manifest.md`.
- Handoff format: follow `../shared/handoff_schema.md`.
- After meaningful change, update `./state/progress.md` with a one-line entry: date, action, outcome.

## Escalation triggers (stop and return control)
- A Required read is missing or version mismatched.
- An input is missing, stale, or ambiguous.
- The request falls outside the IN scope above.
- The output cannot be produced from available inputs.

When escalating, return: **Problem** (one sentence), **Evidence** (file paths or excerpts), **Options** (numbered), **Recommendation** (which option, why). Do not guess.
"""

_INPUTS_MANIFEST_TEMPLATE = """---
schema_version: 1
agent: {{id}}
direction: inputs
updated: {{date}}
---

# {{id}} — Inputs manifest

This agent consumes the following upstream artifacts. Pin a version when sync occurs; refuse work if pin is unset or stale.

| Source agent | Artifact | Pinned version | Last synced | Status |
|---|---|---|---|---|
{{input_rows}}

> **How to use**: when an upstream changes, bump the version pin and re-run pre-flight. If pin can't be satisfied → escalate.
"""

_OUTPUTS_MANIFEST_TEMPLATE = """---
schema_version: 1
agent: {{id}}
direction: outputs
updated: {{date}}
---

# {{id}} — Outputs manifest

Artifacts this agent produces for downstream consumers.

| Artifact | Consumers | Current version | Updated | Notes |
|---|---|---|---|---|
| (TBD: first artifact) | (TBD: which downstream agents) | unset | — | |

> **Pattern**: bump version on schema change, update timestamp on content change, notify listed consumers.
"""

_PROGRESS_TEMPLATE = """# {{id}} Progress log

## Current state
Initialized {{date}}. Awaiting first dispatch.

## History
- {{date}} — agent bootstrapped via AgentUI (parents: {{parents_csv}}).
"""

_CODE_MAP_TEMPLATE = """# {{id}} Code map

Files and modules this agent owns or references.

## Owned (this agent is the author / maintainer)
- `./AGENT.md` — role and contract.
- `./inputs/manifest.md`
- `./outputs/manifest.md`
- `./state/progress.md`
- (Add owned artifacts here.)

## Read-only references
- `../shared/` — project-wide conventions.
- Upstream agents listed in `./inputs/manifest.md`.
"""

_AGENT_FILE_TEMPLATES: dict[str, str] = {
    "AGENT.md": _AGENT_MD_TEMPLATE,
    "inputs/manifest.md": _INPUTS_MANIFEST_TEMPLATE,
    "outputs/manifest.md": _OUTPUTS_MANIFEST_TEMPLATE,
    "state/progress.md": _PROGRESS_TEMPLATE,
    "context/code_map.md": _CODE_MAP_TEMPLATE,
}


def _render(template: str, ctx: dict[str, str]) -> str:
    out = template
    for key, val in ctx.items():
        out = out.replace("{{" + key + "}}", str(val))
    return out


def render_agent_files(agent_id: str, role: str, parents: list[str]) -> dict[str, str]:
    from datetime import date
    today = date.today().isoformat()
    if parents:
        input_rows = "\n".join(
            f"| {p} | (TBD: artifact name) | unset | — | pending |"
            for p in parents
        )
    else:
        input_rows = "| (none — root agent) | | | | |"
    parents_csv = ", ".join(parents) if parents else "none"

    ctx = {
        "id": agent_id,
        "role": role or "_(short one-line description of this agent's purpose)_",
        "input_rows": input_rows,
        "parents_csv": parents_csv,
        "date": today,
    }
    return {fn: _render(tmpl, ctx) for fn, tmpl in _AGENT_FILE_TEMPLATES.items()}


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
        warnings.append(f"Thư mục đã tồn tại: {target.name}. Tạo sẽ bị từ chối.")
    shared = root / "shared"
    if not shared.exists():
        warnings.append("Project chưa có thư mục `shared/`. Các template tham chiếu `../shared/research_integrity.md` v.v. Bạn nên tạo `shared/` trước hoặc sửa lại template.")

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
    if target.exists():
        return False, f"folder already exists: {target.name}"

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
        target.mkdir(parents=True, exist_ok=False)
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
        if target.exists():
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
