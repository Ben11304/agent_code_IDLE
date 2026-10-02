#!/usr/bin/env python3
"""Import the current Codex capability surface into Agent Idle's local inventory.

This is an explicit maintenance command, not a runtime crawler. The dashboard
and agent harness consume only the resulting files under capability_inventory.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path


INVENTORY_ROOT = Path(__file__).resolve().parent
APP_ROOT = INVENTORY_ROOT.parent
REPO_ROOT = APP_ROOT.parent
sys.path.insert(0, str(APP_ROOT))

from backend.adapters import _codex_sdk_settings  # noqa: E402
from backend.capabilities import discover_external_catalog  # noqa: E402


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.").lower()
    return cleaned or "capability"


def _copy_package(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store"),
    )


def _copy_mcp_implementation(
    source: Path, destination: Path, *, exclude_runtime_config: bool = False
) -> None:
    excluded = {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
        "dist", ".DS_Store",
    }

    def ignore(_directory: str, names: list[str]) -> set[str]:
        blocked = {
            name for name in names
            if name in excluded or name.endswith((".egg-info", ".pyc"))
        }
        if exclude_runtime_config:
            blocked.add("destinations.json")
        return blocked

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(REPO_ROOT.resolve()))


def _safe_mcp_config() -> dict:
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    allowed = {
        "url", "command", "args", "cwd", "enabled", "enabled_tools",
        "disabled_tools", "env_vars", "startup_timeout_sec", "tool_timeout_sec",
    }
    result = {}
    for name, value in (raw.get("mcp_servers") or {}).items():
        if isinstance(value, dict):
            result[str(name)] = {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key in allowed
            }
    return result


async def snapshot(cwd: Path) -> dict:
    try:
        previous_snapshot = json.loads(
            (INVENTORY_ROOT / "inventory.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        previous_snapshot = {}
    deleted_skills = [
        item for item in previous_snapshot.get("deleted_skills", [])
        if isinstance(item, dict)
    ]
    deleted_skill_sources = {
        item["source_path"] for item in deleted_skills
        if isinstance(item.get("source_path"), str)
    }
    deleted_skill_keys = {
        (item.get("name"), item.get("scope"), item.get("plugin_id"))
        for item in deleted_skills
    }
    deleted_mcp_servers = [
        item for item in previous_snapshot.get("deleted_mcp_servers", [])
        if isinstance(item, dict)
    ]
    deleted_mcp_ids = {
        str(item.get("id")) for item in deleted_mcp_servers if item.get("id")
    }
    runtime_env = {
        "AGENTUI_PROJECT_SLUG": "aecbench",
        "AGENTUI_AGENT_ID": "CRAFTER",
        "AGENTUI_PROJECT_NAME": "AEC-Bench Review",
        "AGENTUI_PROJECT_ROOT_AGENT_ID": "BOSS",
        "NOTION_REPORT_CONFIG": str(REPO_ROOT / "notion_report" / "destinations.json"),
    }
    config, *_ = _codex_sdk_settings(runtime_env)
    config.cwd = str(cwd)
    catalog = await discover_external_catalog(
        config=config,
        cwd=str(cwd),
        cache_key=f"inventory-import:{cwd}",
        force=True,
    )

    skill_entries: list[dict] = []
    plugin_entries: list[dict] = []
    plugin_destinations: dict[str, Path] = {}

    for plugin in catalog.get("plugins", []):
        source_root = Path(plugin["path"]).resolve()
        destination = INVENTORY_ROOT / "plugins" / _slug(plugin["id"].replace("@", "--"))
        _copy_package(source_root, destination)
        plugin_destinations[plugin["id"]] = destination
        plugin_entries.append({
            "id": plugin["id"],
            "name": plugin["name"],
            "display_name": plugin["display_name"],
            "description": plugin["description"],
            "marketplace": plugin["marketplace"],
            "version": plugin["version"],
            "baseline_enabled": plugin["baseline_enabled"],
            "path": _relative(destination),
            "source_path": str(source_root),
        })

    for skill in catalog.get("skills", []):
        source_file = Path(skill["path"]).resolve()
        # Files produced by an earlier import are visible to the discovery
        # probe, but are outputs, not additional source capabilities. Manually
        # curated inventory skills (directory names without the source-- prefix)
        # remain first-class entries.
        if (
            source_file.is_relative_to(INVENTORY_ROOT / "skills")
            and "--" in source_file.parent.name
        ):
            continue
        plugin_id = skill.get("plugin_id")
        deleted_key = (
            skill.get("name"), skill.get("scope") or "skill", plugin_id
        )
        deleted = str(source_file) in deleted_skill_sources or deleted_key in deleted_skill_keys
        if plugin_id and plugin_id in plugin_destinations:
            plugin = next(item for item in catalog["plugins"] if item["id"] == plugin_id)
            relative_skill = source_file.relative_to(Path(plugin["path"]).resolve())
            destination_file = plugin_destinations[plugin_id] / relative_skill
            if deleted:
                shutil.rmtree(destination_file.parent, ignore_errors=True)
                continue
        elif source_file.is_relative_to(INVENTORY_ROOT):
            destination_file = source_file
        else:
            destination_dir = (
                INVENTORY_ROOT / "skills" /
                f"{_slug(skill.get('scope') or 'skill')}--{_slug(skill['name'])}"
            )
            if deleted:
                shutil.rmtree(destination_dir, ignore_errors=True)
                continue
            _copy_package(source_file.parent, destination_dir)
            destination_file = destination_dir / "SKILL.md"
        skill_entries.append({
            "id": skill["name"],
            "name": skill["name"],
            "display_name": skill.get("display_name") or skill["name"],
            "description": skill.get("description") or "",
            "scope": skill.get("scope") or "skill",
            "plugin_id": plugin_id,
            "baseline_enabled": bool(skill.get("baseline_enabled", False)),
            "configured_baseline_enabled": bool(
                skill.get("configured_baseline_enabled", skill.get("baseline_enabled", False))
            ),
            "path": _relative(destination_file),
            "source_path": str(source_file),
        })

    launch_configs = _safe_mcp_config()
    mcp_entries = []
    mcp_root = INVENTORY_ROOT / "mcp"
    mcp_root.mkdir(parents=True, exist_ok=True)
    for server in catalog.get("mcp_servers", []):
        if str(server.get("id")) in deleted_mcp_ids:
            continue
        manifest = {
            "schema_version": 1,
            "id": server["id"],
            "name": server["name"],
            "description": server.get("description") or "",
            "auth_status": server.get("auth_status"),
            "baseline_enabled": bool(server.get("baseline_enabled", False)),
            "controllable": bool(server.get("controllable", False)),
            "runtime_managed": not bool(server.get("controllable", False)),
            "tool_inventory_complete": bool(server.get("tool_inventory_complete", False)),
            "resource_count": int(server.get("resource_count") or 0),
            "tools": server.get("tools") or [],
            "launch": launch_configs.get(server["name"], {}),
        }
        if server["name"] == "agentui_notion_report":
            implementation = (
                mcp_root / _slug(server["name"]) / "implementation" / "notion_report"
            )
            _copy_mcp_implementation(
                REPO_ROOT / "notion_report",
                implementation,
                exclude_runtime_config=True,
            )
            manifest["launch"] = {
                "managed_by": "agentui",
                "transport": "stdio",
                "implementation": _relative(implementation / "run_mcp.py"),
                "source_path": str((REPO_ROOT / "notion_report").resolve()),
                "runtime_config_external": "notion_report/destinations.json",
            }
        elif server["name"] == "codex_apps":
            manifest["launch"] = {
                "managed_by": "codex-runtime",
                "transport": "runtime-aggregate",
            }
        elif server["name"] == "openconstruction":
            source = Path("/users/PGS0407/binben14/VietHuy/OpenConstruction/OC-mcp")
            implementation = mcp_root / _slug(server["name"]) / "implementation"
            _copy_mcp_implementation(source, implementation)
            manifest["launch"] = {
                "command": "uv",
                "args": [
                    "--directory", _relative(implementation),
                    "run", "--frozen", "openconstruction-mcp",
                ],
                "source_path": str(source.resolve()),
            }
        destination = mcp_root / _slug(server["name"]) / "manifest.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        mcp_entries.append({
            "id": server["id"],
            "manifest": _relative(destination),
            "tool_count": len(manifest["tools"]),
        })

    snapshot_data = {
        "schema_version": 1,
        "created_at": time.time(),
        "source_cwd": str(cwd),
        "skills": sorted(skill_entries, key=lambda item: (item["name"], item["path"])),
        "plugins": sorted(plugin_entries, key=lambda item: item["id"]),
        "mcp_servers": sorted(mcp_entries, key=lambda item: item["id"]),
        "deleted_skills": deleted_skills,
        "deleted_mcp_servers": deleted_mcp_servers,
    }
    (INVENTORY_ROOT / "inventory.json").write_text(
        json.dumps(snapshot_data, indent=2, ensure_ascii=False) + "\n"
    )
    return snapshot_data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path("/users/PGS0407/binben14/VietHuy/AECPlayGround-AGENT/CRAFTER"),
        help="Representative agent cwd used only during this explicit import.",
    )
    args = parser.parse_args()
    result = asyncio.run(snapshot(args.cwd.resolve()))
    print(json.dumps({
        "skills": len(result["skills"]),
        "plugins": len(result["plugins"]),
        "mcp_servers": len(result["mcp_servers"]),
        "mcp_tools": sum(item["tool_count"] for item in result["mcp_servers"]),
    }, indent=2))


if __name__ == "__main__":
    main()
