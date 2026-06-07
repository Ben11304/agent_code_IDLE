from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

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
