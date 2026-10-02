from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from notion_report.config import (  # noqa: E402
    DestinationError,
    DestinationStore,
    SYSTEM_AGENT_ID,
    SYSTEM_PROJECT_SLUG,
)
from notion_report.tool import NotionReportTool  # noqa: E402


def _default_token_env(project_slug: str, environ: dict[str, str]) -> str:
    project_key = "".join(ch if ch.isalnum() else "_" for ch in project_slug.upper())
    candidates = [f"NOTION_{project_key}_TOKEN", "NOTION_REPORT_TOKEN", "NOTION_TOKEN"]
    return next((name for name in candidates if environ.get(name, "").strip()), candidates[0])


def get_notion_settings(
    config_path: str | Path,
    project_slug: str,
    agent_id: str,
    *,
    environ: dict[str, str] | None = None,
    editable: bool | None = None,
) -> dict:
    env = environ if environ is not None else os.environ
    editable = agent_id == "BOSS" if editable is None else editable
    result = {
        "project_slug": project_slug,
        "agent_id": agent_id,
        "editable": editable,
        "configured": False,
        "notion_url": "",
        "parent_title": "",
        "workspace_name": "",
        "token_env": "",
        "token_available": False,
        "verified": False,
        "auto_provision_available": False,
    }
    store = DestinationStore(config_path)
    try:
        destination = store.resolve(project_slug, agent_id)
    except DestinationError as exc:
        if str(exc).startswith("No Notion destination bound"):
            try:
                hub = store.get(SYSTEM_PROJECT_SLUG, SYSTEM_AGENT_ID)
            except DestinationError:
                hub = None
            token_env = hub.token_env if hub else _default_token_env(project_slug, env)
            result.update(
                token_env=token_env,
                token_available=bool(env.get(token_env, "").strip()),
                auto_provision_available=bool(hub),
            )
            return result
        raise

    result.update(
        configured=True,
        notion_url=destination.parent_url,
        parent_title=destination.parent_title,
        workspace_name=destination.workspace_name,
        token_env=destination.token_env,
        token_available=bool(env.get(destination.token_env, "").strip()),
        # A destination can only be written by bind_destination after live
        # workspace and page verification. Live writes verify it again.
        verified=True,
        destination_owner_agent_id=destination.agent_id,
        inherited=destination.agent_id != agent_id,
    )
    return result


def bind_notion_settings(
    config_path: str | Path,
    project_slug: str,
    agent_id: str,
    notion_url: str,
    *,
    environ: dict[str, str] | None = None,
    editable: bool | None = None,
    tool_factory: Callable[..., NotionReportTool] = NotionReportTool,
) -> dict:
    editable = agent_id == "BOSS" if editable is None else editable
    if not editable:
        raise DestinationError("Notion report settings are currently enabled only for root/BOSS agents")
    parent_url = str(notion_url or "").strip()
    if not parent_url:
        raise DestinationError("Notion URL is required")

    env = environ if environ is not None else os.environ
    store = DestinationStore(config_path)
    try:
        existing_token_env = store.get(project_slug, agent_id).token_env
    except DestinationError as exc:
        if str(exc).startswith("No Notion destination bound"):
            existing_token_env = ""
        else:
            raise
    token_env = existing_token_env if env.get(existing_token_env, "").strip() else _default_token_env(project_slug, env)
    if not env.get(token_env, "").strip():
        raise DestinationError(
            f"Missing Notion credential in server environment variable {token_env}"
        )

    tool = tool_factory(store, environ=env)
    status = tool.bind_destination(project_slug, agent_id, parent_url, token_env)
    return {
        "project_slug": project_slug,
        "agent_id": agent_id,
        "editable": True,
        "configured": True,
        "notion_url": status["parent_url"],
        "parent_title": status.get("parent_title", ""),
        "workspace_name": status.get("workspace_name", ""),
        "token_env": token_env,
        "token_available": True,
        "verified": bool(status.get("verified")),
    }
