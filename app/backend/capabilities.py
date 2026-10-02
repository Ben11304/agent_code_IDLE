from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import os
import re
import shutil
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

from openai_codex import CodexConfig
from openai_codex.async_client import AsyncCodexClient
from openai_codex.generated.v2_all import (
    ListMcpServerStatusResponse,
    SkillsExtraRootsSetResponse,
    SkillsListResponse,
)


_CATALOG_TTL_SECONDS = 45
_catalog_cache: dict[str, tuple[float, dict]] = {}
_catalog_lock = asyncio.Lock()
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_IDLE_APP_ROOT = Path(__file__).resolve().parents[1]
_IDLE_REPO_ROOT = _IDLE_APP_ROOT.parent
_IDLE_INVENTORY_ROOT = _IDLE_APP_ROOT / "capability_inventory"
_IDLE_INVENTORY_SKILLS = _IDLE_INVENTORY_ROOT / "skills"
_IDLE_INVENTORY_INDEX = _IDLE_INVENTORY_ROOT / "inventory.json"
_EXPLICIT_SKILL_RE = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Za-z0-9][A-Za-z0-9_.:-]*)")


def _toml_key(value: str) -> str:
    return value if _BARE_TOML_KEY.fullmatch(value) else json.dumps(value)


def normalize_policy(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    plugins = {
        str(plugin): enabled
        for plugin, enabled in (raw.get("plugins") or {}).items()
        if isinstance(plugin, str) and isinstance(enabled, bool)
    }
    skills = {
        str(path): enabled
        for path, enabled in (raw.get("skills") or {}).items()
        if isinstance(path, str) and isinstance(enabled, bool)
    }
    servers: dict[str, dict] = {}
    for name, value in (raw.get("mcp_servers") or {}).items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        entry: dict[str, Any] = {}
        if isinstance(value.get("enabled"), bool):
            entry["enabled"] = value["enabled"]
        tools = {
            str(tool): enabled
            for tool, enabled in (value.get("tools") or {}).items()
            if isinstance(tool, str) and isinstance(enabled, bool)
        }
        if tools:
            entry["tools"] = tools
        if entry:
            servers[name] = entry
    return {"plugins": plugins, "skills": skills, "mcp_servers": servers}


def inherited_capability_config(env: dict[str, str] | None = None) -> dict:
    source_env = env or os.environ
    codex_home = source_env.get("CODEX_HOME")
    path = (
        Path(codex_home).expanduser() / "config.toml"
        if codex_home
        else Path.home() / ".codex" / "config.toml"
    )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, TypeError):
        return {
            "plugins": {}, "skills": {}, "mcp_disabled_tools": {},
            "mcp_server_names": [],
        }
    plugins = {
        str(name): bool(value.get("enabled", False))
        for name, value in (data.get("plugins") or {}).items()
        if isinstance(name, str) and isinstance(value, dict)
    }
    skill_states: dict[str, bool] = {}
    for item in ((data.get("skills") or {}).get("config") or []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            skill_states[item["path"]] = bool(item.get("enabled", True))
    disabled: dict[str, list[str]] = {}
    for name, value in (data.get("mcp_servers") or {}).items():
        if not isinstance(value, dict):
            continue
        tools = value.get("disabled_tools")
        if isinstance(tools, list):
            disabled[str(name)] = [str(tool) for tool in tools if isinstance(tool, str)]
    return {
        "plugins": plugins,
        "skills": skill_states,
        "mcp_disabled_tools": disabled,
        "mcp_server_names": sorted(
            str(name) for name, value in (data.get("mcp_servers") or {}).items()
            if isinstance(name, str) and isinstance(value, dict)
        ),
    }


def _codex_home(env: dict[str, str] | None = None) -> Path:
    source_env = env or os.environ
    configured = source_env.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _version_key(value: str) -> tuple:
    """Natural-ish ordering for cached plugin versions without a packaging dep."""
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"([0-9]+)", value)
        if part
    )


def downloaded_plugins(
    env: dict[str, str] | None = None,
    configured_states: dict[str, bool] | None = None,
) -> list[dict]:
    """Return the newest downloaded version of every plugin in the Codex cache.

    Cache presence is intentionally independent from runtime activation. This is
    the inventory users need in order to discover a plugin before enabling it.
    """
    source_env = env or os.environ
    cache = _codex_home(source_env) / "plugins" / "cache"
    selected: dict[str, dict] = {}
    states = configured_states or {}
    manifests: list[tuple[Path, str, str]] = []
    if cache.is_dir():
        for manifest_path in cache.glob("*/*/*/.codex-plugin/plugin.json"):
            try:
                marketplace = manifest_path.parents[3].name
            except IndexError:
                continue
            manifests.append((manifest_path, marketplace, "codex-cache"))

    for manifest_path, marketplace, source in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict):
            continue
        root = manifest_path.parent.parent
        name = str(manifest.get("name") or root.parent.name).strip()
        version = str(manifest.get("version") or root.name).strip()
        if not name:
            continue
        plugin_id = f"{name}@{marketplace}"
        raw_skill_roots = manifest.get("skills")
        if isinstance(raw_skill_roots, str):
            raw_skill_roots = [raw_skill_roots]
        elif not isinstance(raw_skill_roots, list):
            raw_skill_roots = ["./skills/"] if (root / "skills").is_dir() else []
        skill_roots: list[str] = []
        skill_paths: list[str] = []
        for raw_root in raw_skill_roots:
            if not isinstance(raw_root, str):
                continue
            candidate = (root / raw_root).resolve()
            scan_root = candidate if candidate.is_dir() else candidate.parent
            if not scan_root.is_dir():
                continue
            skill_roots.append(str(scan_root))
            if candidate.is_file() and candidate.name == "SKILL.md":
                skill_paths.append(str(candidate))
            else:
                skill_paths.extend(str(path.resolve()) for path in candidate.rglob("SKILL.md"))
        interface = manifest.get("interface") if isinstance(manifest.get("interface"), dict) else {}
        record = {
            "id": plugin_id,
            "name": name,
            "display_name": str(interface.get("displayName") or name),
            "description": _brief(str(manifest.get("description") or "")),
            "marketplace": marketplace,
            "version": version,
            "path": str(root.resolve()),
            "skill_roots": sorted(set(skill_roots)),
            "skill_paths": sorted(set(skill_paths)),
            "has_apps": bool(manifest.get("apps")),
            "has_mcp_servers": bool(manifest.get("mcpServers")),
            "baseline_enabled": bool(states.get(plugin_id, False)),
            "downloaded": True,
            "source": source,
        }
        old = selected.get(plugin_id)
        if old is None or _version_key(version) > _version_key(old["version"]):
            selected[plugin_id] = record
    return sorted(selected.values(), key=lambda item: item["display_name"].lower())


def idle_inventory_skill_paths() -> list[str]:
    snapshot = load_inventory_snapshot()
    return sorted(
        str((_IDLE_REPO_ROOT / item["path"]).resolve())
        for item in snapshot.get("skills", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    )


def enabled_inventory_skills(policy: dict | None) -> list[dict]:
    """Resolve the exact Idle-owned skill surface enabled for one agent.

    This deliberately reads the canonical snapshot instead of scanning Codex or
    the host filesystem.  The returned absolute SKILL.md paths are suitable for
    SDK ``SkillInput`` values and for matching completed file-read commands.
    """
    normalized = normalize_policy(policy)
    snapshot = load_inventory_snapshot()
    plugin_baselines = {
        item["id"]: bool(item.get("baseline_enabled", False))
        for item in snapshot.get("plugins", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    plugin_states = {
        plugin_id: normalized["plugins"].get(plugin_id, baseline)
        for plugin_id, baseline in plugin_baselines.items()
    }
    result: list[dict] = []
    for item in snapshot.get("skills", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        try:
            path = str(_inventory_path(item["path"]))
        except ValueError:
            continue
        override = normalized["skills"].get(path)
        source_path = item.get("source_path")
        if override is None and isinstance(source_path, str):
            override = normalized["skills"].get(source_path)
        configured = (
            bool(item.get("configured_baseline_enabled", item.get("baseline_enabled", False)))
            if override is None else override
        )
        if not configured or not plugin_states.get(item.get("plugin_id"), True):
            continue
        result.append({
            "id": path,
            "name": str(item.get("name") or item.get("id") or Path(path).parent.name),
            "path": path,
        })
    return sorted(result, key=lambda skill: (skill["name"].lower(), skill["path"]))


def explicit_skill_inputs(message: str, policy: dict | None) -> list[dict]:
    """Resolve explicit ``$skill-name`` mentions to enabled SDK skill inputs.

    Duplicate inventory copies with the same public skill name are collapsed to
    one deterministic path because Codex's explicit invocation is name-based.
    """
    requested_names = list(dict.fromkeys(_EXPLICIT_SKILL_RE.findall(message or "")))
    if not requested_names:
        return []
    by_name: dict[str, dict] = {}
    for skill in enabled_inventory_skills(policy):
        by_name.setdefault(skill["name"], skill)
    return [by_name[name] for name in requested_names if name in by_name]


def load_inventory_snapshot() -> dict:
    try:
        data = json.loads(_IDLE_INVENTORY_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"skills": [], "plugins": [], "mcp_servers": []}
    if not isinstance(data, dict):
        return {"skills": [], "plugins": [], "mcp_servers": []}
    return data


def _write_inventory_snapshot(snapshot: dict) -> None:
    """Atomically replace the canonical index after a validated mutation."""
    temporary = _IDLE_INVENTORY_INDEX.with_name(
        f".{_IDLE_INVENTORY_INDEX.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, _IDLE_INVENTORY_INDEX)


def _inventory_path(relative: str) -> Path:
    path = (_IDLE_REPO_ROOT / relative).resolve()
    if not path.is_relative_to(_IDLE_INVENTORY_ROOT.resolve()):
        raise ValueError(f"inventory path escapes capability_inventory: {relative}")
    return path


def _inventory_mcp_manifests(snapshot: dict | None = None) -> list[dict]:
    """Load only secret-free launch manifests indexed by Idle's inventory."""
    manifests: list[dict] = []
    for item in (snapshot or load_inventory_snapshot()).get("mcp_servers", []):
        if not isinstance(item, dict) or not isinstance(item.get("manifest"), str):
            continue
        try:
            data = json.loads(_inventory_path(item["manifest"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            manifests.append(data)
    return manifests


def _inventory_launch_args(values: Any) -> list[str]:
    """Resolve repo-relative inventory paths without accepting path escapes."""
    if not isinstance(values, list):
        return []
    result: list[str] = []
    inventory_prefix = str(_IDLE_INVENTORY_ROOT.relative_to(_IDLE_REPO_ROOT)) + "/"
    for value in values:
        text = str(value)
        if text.startswith(inventory_prefix):
            text = str(_inventory_path(text))
        result.append(text)
    return result


def delete_inventory_capability(*, kind: str, target: str) -> dict:
    """Permanently remove one indexed skill package or MCP server package.

    The target must resolve from the current index. Files are first renamed to
    an inventory-local staging path, then the index is atomically committed.
    A tombstone prevents the explicit importer from silently resurrecting an
    intentionally removed external capability.
    """
    snapshot = load_inventory_snapshot()
    updated = copy.deepcopy(snapshot)
    staged: Path | None = None
    original: Path | None = None
    removed: dict

    if kind == "skill":
        matched = next(
            (
                item for item in snapshot.get("skills", [])
                if isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and str(_inventory_path(item["path"])) == target
            ),
            None,
        )
        if not matched:
            raise ValueError("skill not found in Agent Idle inventory")
        skill_path = _inventory_path(matched["path"])
        original = skill_path.parent
        removed = {
            "kind": "skill",
            "id": matched.get("id") or matched.get("name"),
            "name": matched.get("name") or skill_path.parent.name,
            "path": matched["path"],
            "source_path": matched.get("source_path"),
            "scope": matched.get("scope"),
            "plugin_id": matched.get("plugin_id"),
            "deleted_at": time.time(),
        }
        updated["skills"] = [
            item for item in snapshot.get("skills", []) if item is not matched
        ]
        tombstones = [
            item for item in snapshot.get("deleted_skills", [])
            if isinstance(item, dict) and item.get("path") != matched["path"]
        ]
        updated["deleted_skills"] = [*tombstones, removed]
    elif kind == "mcp_server":
        matched = next(
            (
                item for item in snapshot.get("mcp_servers", [])
                if isinstance(item, dict) and str(item.get("id")) == target
            ),
            None,
        )
        if not matched or not isinstance(matched.get("manifest"), str):
            raise ValueError("MCP server not found in Agent Idle inventory")
        manifest_path = _inventory_path(matched["manifest"])
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            manifest = {}
        original = manifest_path.parent
        removed = {
            "kind": "mcp_server",
            "id": matched.get("id") or target,
            "name": manifest.get("name") or matched.get("id") or target,
            "manifest": matched["manifest"],
            "deleted_at": time.time(),
        }
        updated["mcp_servers"] = [
            item for item in snapshot.get("mcp_servers", []) if item is not matched
        ]
        tombstones = [
            item for item in snapshot.get("deleted_mcp_servers", [])
            if isinstance(item, dict) and item.get("id") != matched.get("id")
        ]
        updated["deleted_mcp_servers"] = [*tombstones, removed]
    else:
        raise ValueError("only skills and MCP servers can be deleted")

    inventory_root = _IDLE_INVENTORY_ROOT.resolve()
    original = original.resolve()
    protected = {
        inventory_root,
        _IDLE_INVENTORY_SKILLS.resolve(),
        (inventory_root / "plugins").resolve(),
        (inventory_root / "mcp").resolve(),
    }
    if not original.is_relative_to(inventory_root) or original in protected:
        raise ValueError("refusing to delete a protected inventory directory")

    staging_root = inventory_root / ".delete-staging"
    if original.exists():
        staging_root.mkdir(parents=True, exist_ok=True)
        staged = staging_root / f"{uuid.uuid4().hex}-{original.name}"
        original.rename(staged)
    try:
        _write_inventory_snapshot(updated)
    except Exception:
        if staged is not None and staged.exists():
            staged.rename(original)
        raise
    if staged is not None:
        shutil.rmtree(staged)
    try:
        staging_root.rmdir()
    except OSError:
        pass
    _catalog_cache.clear()
    return removed


def plugin_extra_roots(
    policy: dict | None, env: dict[str, str] | None = None
) -> list[str]:
    """Extra roots explicitly enabled for one agent from Idle's inventory."""
    normalized = normalize_policy(policy)
    snapshot = load_inventory_snapshot()
    plugin_baselines = {
        item["id"]: bool(item.get("baseline_enabled", False))
        for item in snapshot.get("plugins", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    plugin_states = {
        plugin_id: normalized["plugins"].get(plugin_id, baseline)
        for plugin_id, baseline in plugin_baselines.items()
    }
    roots: list[str] = []
    for skill in snapshot.get("skills", []):
        if not isinstance(skill, dict) or not isinstance(skill.get("path"), str):
            continue
        path = _inventory_path(skill["path"])
        configured = normalized["skills"].get(
            str(path), bool(skill.get("configured_baseline_enabled", skill.get("baseline_enabled", False)))
        )
        parent_enabled = plugin_states.get(skill.get("plugin_id"), True)
        if configured and parent_enabled:
            roots.append(str(path.parent.parent))
    return sorted(set(roots))


def codex_config_overrides(
    policy: dict | None,
    inherited: dict | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Compile one agent's capability policy to ephemeral Codex `-c` values."""
    normalized = normalize_policy(policy)
    overrides: list[str] = []
    inherited = inherited if isinstance(inherited, dict) else {}
    skill_states = dict(inherited.get("skills") or {})
    snapshot = load_inventory_snapshot()
    plugin_baselines = {
        item["id"]: bool(item.get("baseline_enabled", False))
        for item in snapshot.get("plugins", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    plugin_states = {
        plugin_id: normalized["plugins"].get(plugin_id, baseline)
        for plugin_id, baseline in plugin_baselines.items()
    }

    # Central copies are the runtime source of truth. Explicitly disable every
    # imported source path and configure only its inventory-owned counterpart.
    for skill in snapshot.get("skills", []):
        if not isinstance(skill, dict) or not isinstance(skill.get("path"), str):
            continue
        central_path = str(_inventory_path(skill["path"]))
        source_path = skill.get("source_path")
        if isinstance(source_path, str) and source_path != central_path:
            skill_states[source_path] = False
        override = normalized["skills"].get(central_path)
        if override is None and isinstance(source_path, str):
            override = normalized["skills"].get(source_path)
        configured = (
            bool(skill.get("configured_baseline_enabled", skill.get("baseline_enabled", False)))
            if override is None else override
        )
        skill_states[central_path] = bool(
            configured and plugin_states.get(skill.get("plugin_id"), True)
        )
    for deleted in snapshot.get("deleted_skills", []):
        if not isinstance(deleted, dict):
            continue
        source_path = deleted.get("source_path")
        if isinstance(source_path, str) and source_path:
            skill_states[source_path] = False

    # Native plugin switches still govern app/MCP contributions where Codex
    # supports them; skill content itself always comes from the Idle copy above.
    for plugin_id, enabled in sorted(normalized["plugins"].items()):
        overrides.append(
            f"plugins.{_toml_key(plugin_id)}.enabled={str(enabled).lower()}"
        )
    skill_entries = []
    for path, enabled in sorted(skill_states.items()):
        skill_entries.append(
            "{path=" + json.dumps(path) + ",enabled=" + str(enabled).lower() + "}"
        )
    if skill_entries:
        overrides.append(
            "skills.config=[" + ",".join(skill_entries) + "]"
        )
    # The indexed manifest is also the launch source of truth. This makes an
    # agent's MCP selection reproducible without mutating or depending on the
    # user's ~/.codex/config.toml. Runtime aggregators (for example codex_apps)
    # have no launch definition and remain controlled by Codex itself.
    manifests = _inventory_mcp_manifests(snapshot)
    inventory_server_names = {
        manifest["name"] for manifest in manifests
        if bool(manifest.get("controllable", False))
    }
    for inherited_server in inherited.get("mcp_server_names") or []:
        if inherited_server not in inventory_server_names:
            overrides.append(
                f"mcp_servers.{_toml_key(inherited_server)}.enabled=false"
            )

    for manifest in sorted(manifests, key=lambda item: item["name"]):
        if not bool(manifest.get("controllable", False)):
            continue
        server = manifest["name"]
        entry = normalized["mcp_servers"].get(server, {})
        prefix = f"mcp_servers.{_toml_key(server)}"
        launch = manifest.get("launch") if isinstance(manifest.get("launch"), dict) else {}
        runtime_env = env if env is not None else os.environ
        if launch.get("managed_by") == "agentui" and not (
            runtime_env.get("AGENTUI_PROJECT_SLUG")
            and runtime_env.get("AGENTUI_AGENT_ID")
        ):
            # This server needs immutable project/agent identity arguments that
            # only the AgentUI adapter can supply. A partial mcp_servers entry
            # without a URL or stdio command is invalid Codex configuration.
            continue
        if isinstance(launch.get("url"), str):
            overrides.append(f"{prefix}.url={json.dumps(launch['url'])}")
        elif isinstance(launch.get("command"), str):
            overrides.append(f"{prefix}.command={json.dumps(launch['command'])}")
            overrides.append(
                f"{prefix}.args={json.dumps(_inventory_launch_args(launch.get('args')))}"
            )
        # AgentUI-managed stdio servers are defined earlier by the adapter so
        # that scoped, secret-free identity arguments can be generated per run.
        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            enabled = bool(manifest.get("baseline_enabled", False))
        overrides.append(f"{prefix}.enabled={str(enabled).lower()}")

        tool_policy = entry.get("tools") or {}
        disabled_tools = set(
            (inherited.get("mcp_disabled_tools") or {}).get(server) or []
        )
        for tool in manifest.get("tools") or []:
            if (
                isinstance(tool, dict)
                and isinstance(tool.get("name"), str)
                and not bool(tool.get("baseline_enabled", True))
            ):
                disabled_tools.add(tool["name"])
        for name, enabled in tool_policy.items():
            if enabled:
                disabled_tools.discard(name)
            else:
                disabled_tools.add(name)
        if disabled_tools or tool_policy:
            overrides.append(
                f"{prefix}.disabled_tools={json.dumps(sorted(disabled_tools))}"
            )
    return tuple(overrides)


def _config_file(config: CodexConfig) -> Path:
    env = config.env or os.environ
    codex_home = env.get("CODEX_HOME")
    return Path(codex_home).expanduser() / "config.toml" if codex_home else Path.home() / ".codex" / "config.toml"


def _configured_server_states(config: CodexConfig) -> dict[str, bool]:
    states: dict[str, bool] = {}
    try:
        with _config_file(config).open("rb") as handle:
            data = tomllib.load(handle)
        for name, value in (data.get("mcp_servers") or {}).items():
            if isinstance(value, dict):
                states[str(name)] = bool(value.get("enabled", True))
    except (OSError, ValueError, TypeError):
        pass

    # Runtime overrides win over config.toml. This deliberately parses only the
    # boolean switch we emit; tool inventory itself comes from app-server.
    prefix_pattern = re.compile(
        r'^mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\.'
    )
    enabled_pattern = re.compile(
        r'^mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\.enabled=(true|false)$'
    )
    for item in config.config_overrides:
        prefix_match = prefix_pattern.match(item)
        if prefix_match:
            states.setdefault(prefix_match.group(1) or prefix_match.group(2), True)
        enabled_match = enabled_pattern.match(item)
        if enabled_match:
            states[enabled_match.group(1) or enabled_match.group(2)] = (
                enabled_match.group(3) == "true"
            )
    return states


def _configured_plugin_states(config: CodexConfig) -> dict[str, bool]:
    states: dict[str, bool] = {}
    try:
        with _config_file(config).open("rb") as handle:
            data = tomllib.load(handle)
        for name, value in (data.get("plugins") or {}).items():
            if isinstance(value, dict):
                states[str(name)] = bool(value.get("enabled", False))
    except (OSError, ValueError, TypeError):
        pass
    enabled_pattern = re.compile(
        r'^plugins\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\.enabled=(true|false)$'
    )
    for item in config.config_overrides:
        match = enabled_pattern.match(item)
        if match:
            states[match.group(1) or match.group(2)] = match.group(3) == "true"
    return states


def _absolute_path(value: Any) -> str:
    """Unwrap SDK AbsolutePathBuf RootModels into a usable filesystem path."""
    return str(getattr(value, "root", value))


async def _list_mcp_pages(client: AsyncCodexClient) -> list[Any]:
    pages: list[Any] = []
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"detail": "full", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = await client.request(
            "mcpServerStatus/list",
            params,
            response_model=ListMcpServerStatusResponse,
        )
        pages.extend(page.data)
        cursor = page.next_cursor
        if not cursor:
            break
    return pages


def _brief(value: str | None, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def discover_external_catalog(
    *, config: CodexConfig, cwd: str, cache_key: str, force: bool = False
) -> dict:
    """Ask the same Codex app-server used for turns for its skill/MCP inventory."""
    now = time.monotonic()
    cached = _catalog_cache.get(cache_key)
    if cached and not force and now - cached[0] < _CATALOG_TTL_SECONDS:
        return copy.deepcopy(cached[1])

    async with _catalog_lock:
        cached = _catalog_cache.get(cache_key)
        if cached and not force and time.monotonic() - cached[0] < _CATALOG_TTL_SECONDS:
            return copy.deepcopy(cached[1])

        plugin_states = _configured_plugin_states(config)
        plugins = downloaded_plugins(config.env, plugin_states)
        all_plugin_roots = sorted({
            root for plugin in plugins for root in plugin.get("skill_roots", [])
        })
        inventory_paths = set(idle_inventory_skill_paths())
        if inventory_paths:
            all_plugin_roots.append(str(_IDLE_INVENTORY_SKILLS.resolve()))
            all_plugin_roots = sorted(set(all_plugin_roots))
        async with AsyncCodexClient(config) as client:
            await client.initialize()
            # Inventory must include downloaded-but-inactive plugin skills. Extra
            # roots affect this isolated discovery process only; they do not
            # silently activate a plugin for any agent.
            if all_plugin_roots:
                await client.request(
                    "skills/extraRoots/set",
                    {"extraRoots": all_plugin_roots},
                    response_model=SkillsExtraRootsSetResponse,
                )
            skill_response = await client.request(
                "skills/list",
                {"cwds": [cwd], "forceReload": bool(force)},
                response_model=SkillsListResponse,
            )
            mcp_pages = await _list_mcp_pages(client)

        errors: list[str] = []
        configured_states = _configured_server_states(config)
        inherited = inherited_capability_config(config.env)

        # app-server omits tools hidden by disabled_tools and often exposes no
        # tools for a disabled server. Probe all configured servers in a second,
        # isolated process with those filters cleared, then merge definitions
        # back while retaining their real baseline enabled state.
        restrictions_present = any(not enabled for enabled in configured_states.values()) or any(
            inherited.get("mcp_disabled_tools", {}).values()
        )
        if configured_states and restrictions_present:
            probe_overrides = list(config.config_overrides)
            for server_name in sorted(configured_states):
                prefix = f"mcp_servers.{_toml_key(server_name)}"
                probe_overrides.extend([
                    f"{prefix}.enabled=true",
                    f"{prefix}.disabled_tools=[]",
                ])
            probe_config = replace(config, config_overrides=tuple(probe_overrides))
            try:
                async with AsyncCodexClient(probe_config) as probe_client:
                    await probe_client.initialize()
                    probed_pages = await _list_mcp_pages(probe_client)
                by_name = {server.name: server for server in mcp_pages}
                for server in probed_pages:
                    current = by_name.get(server.name)
                    if current is None or len(server.tools) > len(current.tools):
                        by_name[server.name] = server
                mcp_pages = list(by_name.values())
            except Exception as exc:
                errors.append(f"Full MCP tool probe failed: {_brief(str(exc), 220)}")

        skills = []
        plugin_by_path: list[tuple[Path, dict]] = []
        for plugin in plugins:
            for root in plugin.get("skill_roots", []):
                plugin_by_path.append((Path(root), plugin))
        for entry in skill_response.data:
            for error in entry.errors:
                errors.append(_brief(str(error), 240))
            for skill in entry.skills:
                interface = skill.interface
                path = _absolute_path(skill.path)
                inventory_managed = path in inventory_paths
                plugin = next(
                    (
                        candidate
                        for root, candidate in plugin_by_path
                        if Path(path).is_relative_to(root)
                    ),
                    None,
                )
                default_enabled = False if inventory_managed else bool(skill.enabled)
                baseline_enabled = default_enabled and (
                    bool(plugin["baseline_enabled"]) if plugin else True
                )
                skills.append({
                    "id": path,
                    "name": skill.name,
                    "display_name": (
                        interface.display_name if interface and interface.display_name else skill.name
                    ),
                    "description": _brief(skill.description),
                    "path": path,
                    "scope": (
                        "inventory" if inventory_managed
                        else "plugin" if plugin
                        else getattr(skill.scope, "value", str(skill.scope))
                    ),
                    "plugin_id": plugin["id"] if plugin else None,
                    "plugin_name": plugin["display_name"] if plugin else None,
                    "downloaded": bool(plugin) or inventory_managed,
                    "inventory_managed": inventory_managed,
                    "runtime_active": (
                        False if inventory_managed
                        else bool(plugin["baseline_enabled"]) if plugin
                        else True
                    ),
                    "configured_baseline_enabled": default_enabled,
                    "baseline_enabled": baseline_enabled,
                })

        servers = []
        for server in mcp_pages:
            server_info = server.server_info
            tools = []
            disabled_tools = set(inherited.get("mcp_disabled_tools", {}).get(server.name) or [])
            for name, tool in sorted(server.tools.items()):
                annotations = tool.annotations if isinstance(tool.annotations, dict) else {}
                tools.append({
                    "id": name,
                    "name": name,
                    "title": tool.title or name,
                    "description": _brief(tool.description),
                    "baseline_enabled": name not in disabled_tools,
                    "read_only": bool(annotations.get("readOnlyHint", False)),
                })
            servers.append({
                "id": server.name,
                "name": server.name,
                "description": _brief(server_info.description if server_info else None),
                "auth_status": getattr(server.auth_status, "value", str(server.auth_status)),
                "baseline_enabled": configured_states.get(server.name, True),
                # app-server also reports runtime-owned aggregators such as
                # `codex_apps`. They are inventory, not a valid mcp_servers.*
                # config entry; emitting a partial override would fail startup.
                "controllable": server.name in configured_states,
                "tools": tools,
                "resource_count": len(server.resources) + len(server.resource_templates),
                # An empty server with no resources/templates is indistinguishable
                # from a failed/auth-blocked connection. Be explicit instead of
                # presenting zero as a confidently complete inventory.
                "tool_inventory_complete": bool(tools) or bool(
                    len(server.resources) + len(server.resource_templates)
                ),
            })

        catalog = {
            "plugins": plugins,
            "skills": sorted(skills, key=lambda item: (item["scope"], item["name"].lower())),
            "mcp_servers": sorted(servers, key=lambda item: item["name"].lower()),
            "errors": errors,
            "discovered_at": time.time(),
        }
        _catalog_cache[cache_key] = (time.monotonic(), catalog)
        return copy.deepcopy(catalog)


async def discover_catalog(
    *, config: CodexConfig, cwd: str, cache_key: str, force: bool = False
) -> dict:
    """Read the canonical Agent Idle snapshot; never crawl runtime directories."""
    del config, cwd
    try:
        revision = _IDLE_INVENTORY_INDEX.stat().st_mtime_ns
    except OSError:
        revision = 0
    inventory_cache_key = f"idle-inventory:{cache_key}:{revision}"
    now = time.monotonic()
    cached = _catalog_cache.get(inventory_cache_key)
    if cached and not force and now - cached[0] < _CATALOG_TTL_SECONDS:
        return copy.deepcopy(cached[1])

    snapshot = load_inventory_snapshot()
    errors: list[str] = []
    skills: list[dict] = []
    for item in snapshot.get("skills", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("Invalid skill entry in capability_inventory/inventory.json")
            continue
        try:
            path = _inventory_path(item["path"])
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"Inventory skill is missing: {item['path']}")
            continue
        plugin_id = item.get("plugin_id")
        configured_baseline = bool(
            item.get("configured_baseline_enabled", item.get("baseline_enabled", False))
        )
        skills.append({
            "id": str(path),
            "name": str(item.get("name") or item.get("id") or path.parent.name),
            "display_name": str(item.get("display_name") or item.get("name") or path.parent.name),
            "description": _brief(str(item.get("description") or "")),
            "path": str(path),
            "source_path": item.get("source_path"),
            "scope": "plugin" if plugin_id else "inventory",
            "source_scope": item.get("scope"),
            "plugin_id": plugin_id,
            "plugin_name": None,
            "downloaded": True,
            "inventory_managed": True,
            "runtime_active": bool(item.get("baseline_enabled", False)),
            "configured_baseline_enabled": configured_baseline,
            "baseline_enabled": bool(item.get("baseline_enabled", False)),
        })

    plugins: list[dict] = []
    for item in snapshot.get("plugins", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("Invalid plugin entry in capability_inventory/inventory.json")
            continue
        try:
            path = _inventory_path(item["path"])
        except ValueError as exc:
            errors.append(str(exc))
            continue
        plugin_id = str(item.get("id") or item.get("name") or path.name)
        children = [skill for skill in skills if skill.get("plugin_id") == plugin_id]
        display_name = str(item.get("display_name") or item.get("name") or plugin_id)
        for skill in children:
            skill["plugin_name"] = display_name
        plugins.append({
            "id": plugin_id,
            "name": str(item.get("name") or plugin_id),
            "display_name": display_name,
            "description": _brief(str(item.get("description") or "")),
            "marketplace": str(item.get("marketplace") or "inventory"),
            "version": str(item.get("version") or "snapshot"),
            "path": str(path),
            "skill_roots": sorted({str(Path(skill["path"]).parent.parent) for skill in children}),
            "skill_paths": sorted(skill["path"] for skill in children),
            "has_apps": (path / ".app.json").is_file(),
            "has_mcp_servers": (path / ".mcp.json").is_file(),
            "baseline_enabled": bool(item.get("baseline_enabled", False)),
            "downloaded": True,
            "source": "idle-inventory",
        })

    servers: list[dict] = []
    for item in snapshot.get("mcp_servers", []):
        if not isinstance(item, dict) or not isinstance(item.get("manifest"), str):
            errors.append("Invalid MCP entry in capability_inventory/inventory.json")
            continue
        try:
            manifest_path = _inventory_path(item["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"Invalid MCP manifest {item.get('manifest')}: {_brief(str(exc), 180)}")
            continue
        if not isinstance(manifest, dict):
            errors.append(f"Invalid MCP manifest object: {item['manifest']}")
            continue
        tools = []
        for tool in manifest.get("tools") or []:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            tools.append({
                "id": tool["name"],
                "name": tool["name"],
                "title": tool.get("title") or tool["name"],
                "description": _brief(str(tool.get("description") or "")),
                "baseline_enabled": bool(tool.get("baseline_enabled", True)),
                "read_only": bool(tool.get("read_only", False)),
            })
        servers.append({
            "id": str(manifest.get("id") or manifest.get("name")),
            "name": str(manifest.get("name") or manifest.get("id")),
            "description": _brief(str(manifest.get("description") or "")),
            "auth_status": manifest.get("auth_status"),
            "baseline_enabled": bool(manifest.get("baseline_enabled", False)),
            "controllable": bool(manifest.get("controllable", False)),
            "tools": sorted(tools, key=lambda tool: tool["name"]),
            "resource_count": int(manifest.get("resource_count") or 0),
            "tool_inventory_complete": bool(manifest.get("tool_inventory_complete", False)),
            "inventory_manifest": str(manifest_path),
            "launch": manifest.get("launch") or {},
        })

    catalog = {
        "plugins": sorted(plugins, key=lambda item: item["display_name"].lower()),
        "skills": sorted(skills, key=lambda item: (item["scope"], item["name"].lower(), item["path"])),
        "mcp_servers": sorted(servers, key=lambda item: item["name"].lower()),
        "errors": errors,
        "source": "idle-inventory",
        "inventory_path": str(_IDLE_INVENTORY_INDEX),
        "discovered_at": snapshot.get("created_at") or time.time(),
    }
    _catalog_cache[inventory_cache_key] = (time.monotonic(), catalog)
    return copy.deepcopy(catalog)


def effective_catalog(catalog: dict, policy: dict | None) -> dict:
    normalized = normalize_policy(policy)
    result = copy.deepcopy(catalog)
    plugin_policy = normalized["plugins"]
    plugin_states: dict[str, bool] = {}
    for plugin in result.get("plugins", []):
        override = plugin_policy.get(plugin["id"])
        plugin["enabled"] = (
            plugin["baseline_enabled"] if override is None else override
        )
        plugin["overridden"] = override is not None
        plugin_states[plugin["id"]] = plugin["enabled"]

    skill_policy = normalized["skills"]
    for skill in result.get("skills", []):
        override = skill_policy.get(skill["path"])
        if override is None and isinstance(skill.get("source_path"), str):
            override = skill_policy.get(skill["source_path"])
        configured = (
            skill.get("configured_baseline_enabled", skill["baseline_enabled"])
            if override is None
            else override
        )
        parent_enabled = plugin_states.get(skill.get("plugin_id"), True)
        skill["available"] = parent_enabled
        skill["configured_enabled"] = configured
        skill["enabled"] = bool(parent_enabled and configured)
        skill["overridden"] = override is not None

    for plugin in result.get("plugins", []):
        children = [
            skill for skill in result.get("skills", [])
            if skill.get("plugin_id") == plugin["id"]
        ]
        plugin["skill_count"] = len(children)
        plugin["enabled_skill_count"] = sum(1 for skill in children if skill["enabled"])

    server_policy = normalized["mcp_servers"]
    for server in result.get("mcp_servers", []):
        entry = server_policy.get(server["name"], {})
        server_override = entry.get("enabled")
        server["enabled"] = (
            server["baseline_enabled"] if server_override is None else server_override
        )
        server["overridden"] = server_override is not None
        tool_policy = entry.get("tools") or {}
        for tool in server.get("tools", []):
            override = tool_policy.get(tool["name"])
            configured = tool["baseline_enabled"] if override is None else override
            tool["configured_enabled"] = configured
            tool["enabled"] = bool(server["enabled"] and configured)
            tool["overridden"] = override is not None
        server["enabled_tool_count"] = sum(
            1 for tool in server.get("tools", []) if tool["enabled"]
        )
    return result


def update_policy(
    policy: dict | None,
    catalog: dict,
    *,
    kind: str,
    target: str,
    enabled: bool,
    server_name: str | None = None,
) -> dict:
    """Validate and update one override. Matching the baseline removes the override."""
    normalized = normalize_policy(policy)
    if kind == "plugin":
        plugin = next(
            (item for item in catalog.get("plugins", []) if item["id"] == target),
            None,
        )
        if not plugin:
            raise ValueError("plugin not found in the downloaded Codex inventory")
        if enabled == plugin["baseline_enabled"]:
            normalized["plugins"].pop(plugin["id"], None)
        else:
            normalized["plugins"][plugin["id"]] = enabled
        return normalized

    if kind == "skill":
        skill = next((item for item in catalog.get("skills", []) if item["id"] == target), None)
        if not skill:
            raise ValueError("skill not found in the current Codex inventory")
        if skill.get("plugin_id") and not skill.get("available", False):
            raise ValueError("enable the parent plugin before changing this skill")
        configured_baseline = skill.get(
            "configured_baseline_enabled", skill["baseline_enabled"]
        )
        if enabled == configured_baseline:
            normalized["skills"].pop(skill["path"], None)
            if isinstance(skill.get("source_path"), str):
                normalized["skills"].pop(skill["source_path"], None)
        else:
            if isinstance(skill.get("source_path"), str):
                normalized["skills"].pop(skill["source_path"], None)
            normalized["skills"][skill["path"]] = enabled
        return normalized

    if kind == "mcp_server":
        server = next(
            (item for item in catalog.get("mcp_servers", []) if item["id"] == target),
            None,
        )
        if not server:
            raise ValueError("MCP server not found in the current Codex inventory")
        if not server.get("controllable", False):
            raise ValueError("this MCP server is managed by the Codex runtime")
        entry = normalized["mcp_servers"].setdefault(server["name"], {})
        if enabled == server["baseline_enabled"]:
            entry.pop("enabled", None)
        else:
            entry["enabled"] = enabled
        if not entry:
            normalized["mcp_servers"].pop(server["name"], None)
        return normalized

    if kind == "mcp_tool":
        server = next(
            (item for item in catalog.get("mcp_servers", []) if item["id"] == server_name),
            None,
        )
        if not server:
            raise ValueError("MCP server not found in the current Codex inventory")
        if not server.get("controllable", False):
            raise ValueError("this MCP server is managed by the Codex runtime")
        tool = next((item for item in server.get("tools", []) if item["id"] == target), None)
        if not tool:
            raise ValueError("MCP tool not found in the current Codex inventory")
        entry = normalized["mcp_servers"].setdefault(server["name"], {})
        tools = entry.setdefault("tools", {})
        if enabled == tool["baseline_enabled"]:
            tools.pop(tool["name"], None)
        else:
            tools[tool["name"]] = enabled
        if not tools:
            entry.pop("tools", None)
        if not entry:
            normalized["mcp_servers"].pop(server["name"], None)
        return normalized

    raise ValueError("kind must be plugin, skill, mcp_server, or mcp_tool")
