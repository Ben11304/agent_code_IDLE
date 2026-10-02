from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import DestinationStore
from .schema import REPORT_NOTE_POLICY, load_report_schema
from .tool import NotionReportTool, ReportSpec


SERVER_NAME = "AgentUI Notion Report"
SERVER_VERSION = "2.3.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

DESTINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verify": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}

CURRENT_REPORT_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

CREATE_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string", "default": ""},
        "sections_json": {"type": "string", "default": "[]"},
        "subpages_json": {"type": "string", "default": "[]"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["title"],
    "additionalProperties": False,
}

APPEND_CHILD_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_title": {"type": "string"},
        "summary": {"type": "string", "default": ""},
        "sections_json": {"type": "string", "default": "[]"},
        "create_if_missing": {"type": "boolean", "default": False},
    },
    "required": ["page_title"],
    "additionalProperties": False,
}

INVENTORY_TREE_SCHEMA = {
    "type": "object",
    "properties": {
        "max_depth": {"type": "integer", "default": 4, "minimum": 0, "maximum": 8},
        "max_pages": {"type": "integer", "default": 100, "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}

READ_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_id": {"type": "string"},
        "max_blocks": {"type": "integer", "default": 500, "minimum": 1, "maximum": 1000},
    },
    "required": ["page_id"],
    "additionalProperties": False,
}

SYNC_MANAGED_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "page_title": {"type": "string"},
        "report_key": {"type": "string"},
        "source_ref": {"type": "string"},
        "summary": {"type": "string", "default": ""},
        "sections_json": {"type": "string", "default": "[]"},
        "create_if_missing": {"type": "boolean", "default": False},
        "dry_run": {"type": "boolean", "default": True},
    },
    "required": ["page_title", "report_key", "source_ref"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "notion_report_destination",
        "description": (
            "Show and optionally verify this agent's control-plane-bound project Notion destination. "
            "Project and agent identity are injected automatically; never ask the user for them."
        ),
        "inputSchema": DESTINATION_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_notion_project_tree",
        "description": (
            "Inventory pages only inside the control-plane-bound project subtree. "
            "Use this for 'all Notion' requests; it never scans the whole workspace."
        ),
        "inputSchema": INVENTORY_TREE_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read_notion_project_page",
        "description": (
            "Read blocks from one page after proving it belongs to the bound project subtree. "
            "Returns normalized text and a content SHA-256 for audit/staleness checks."
        ),
        "inputSchema": READ_PAGE_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_current_report_schema",
        "description": (
            "Reload and return the current project-owned report schema plus the system-owned "
            "USER NOTE / SCHEMA NOTE authority policy. Call this after an authorized schema "
            "edit and use the returned version, hash, and contract for the successor report."
        ),
        "inputSchema": CURRENT_REPORT_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_notion_report",
        "description": (
            "Create a report below the user's pre-bound parent page. In sections_json, "
            "a section may include tables=[{key,caption,columns,rows}] to create native "
            "Notion tables, diagrams=[{key,format:'mermaid',source,caption}] to create "
            "native Mermaid diagrams, and highlights=[text] for bold change callouts. "
            "The custom writer validates the structured form, compiles it to Notion "
            "enhanced Markdown, and verifies both Markdown and rendered blocks on read-back."
        ),
        "inputSchema": CREATE_REPORT_SCHEMA,
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "append_notion_child_page",
        "description": (
            "Publish report blocks to exactly one direct child page matched by exact title "
            "under the user's pre-bound parent, then read back and verify. Creation is "
            "allowed only when create_if_missing=true; duplicate exact titles always fail."
        ),
        "inputSchema": APPEND_CHILD_PAGE_SCHEMA,
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "sync_notion_managed_report",
        "description": (
            "Dry-run or synchronize an AgentUI-owned report section on an exact direct child page. "
            "Manual page content is preserved; dry_run defaults true and writes require read-back."
        ),
        "inputSchema": SYNC_MANAGED_REPORT_SCHEMA,
        "annotations": {"readOnlyHint": False},
    },
]


def _current_report_schema() -> dict | None:
    project_root = os.environ.get("AGENTUI_PROJECT_ROOT", "").strip()
    schema_file = os.environ.get("AGENTUI_REPORT_SCHEMA_FILE", "").strip()
    if project_root and schema_file:
        schema_path = Path(schema_file).resolve()
        root_path = Path(project_root).resolve()
        try:
            schema_rel = str(schema_path.relative_to(root_path))
        except ValueError as exc:
            raise ValueError("report schema file is outside the scoped project root") from exc
        return load_report_schema(
            root_path,
            {"enabled": True, "schema_file": schema_rel},
        )
    return None


def _tool() -> NotionReportTool:
    path = os.environ.get(
        "NOTION_REPORT_CONFIG",
        str(Path(__file__).with_name("destinations.json")),
    )
    return NotionReportTool(
        DestinationStore(path), report_schema=_current_report_schema())


def _scoped_identity() -> tuple[str, str]:
    """Return the immutable AgentUI identity assigned to this MCP process."""
    scoped_project = os.environ.get("AGENTUI_PROJECT_SLUG", "").strip()
    scoped_agent = os.environ.get("AGENTUI_AGENT_ID", "").strip()
    if not scoped_project or not scoped_agent:
        raise ValueError("AgentUI Notion MCP requires control-plane project and agent scope")
    return scoped_project, scoped_agent


def _call_tool(name: str, arguments: dict[str, Any]) -> dict:
    project_slug, agent_id = _scoped_identity()
    if name == "get_current_report_schema":
        loaded = _current_report_schema()
        if not loaded:
            return {
                "configured": False,
                "note_policy": REPORT_NOTE_POLICY,
            }
        return {
            key: value for key, value in loaded.items()
            if key != "path_abs"
        }
    if name == "notion_report_destination":
        return _tool().status(
            project_slug,
            agent_id,
            verify=bool(arguments.get("verify", False)),
        )
    if name == "list_notion_project_tree":
        return _tool().inventory_project_tree(
            project_slug,
            agent_id,
            max_depth=int(arguments.get("max_depth", 4)),
            max_pages=int(arguments.get("max_pages", 100)),
        )
    if name == "read_notion_project_page":
        return _tool().read_project_page(
            project_slug,
            agent_id,
            str(arguments["page_id"]),
            max_blocks=int(arguments.get("max_blocks", 500)),
        )
    if name == "append_notion_child_page":
        return _tool().append_to_child_page(
            project_slug,
            agent_id,
            str(arguments["page_title"]),
            summary=str(arguments.get("summary", "")),
            sections=json.loads(str(arguments.get("sections_json", "[]"))),
            create_if_missing=bool(arguments.get("create_if_missing", False)),
        )
    if name == "sync_notion_managed_report":
        return _tool().sync_managed_report(
            project_slug,
            agent_id,
            str(arguments["page_title"]),
            str(arguments["report_key"]),
            str(arguments["source_ref"]),
            summary=str(arguments.get("summary", "")),
            sections=json.loads(str(arguments.get("sections_json", "[]"))),
            create_if_missing=bool(arguments.get("create_if_missing", False)),
            dry_run=bool(arguments.get("dry_run", True)),
        )
    if name != "create_notion_report":
        raise ValueError(f"unknown tool: {name}")

    report = ReportSpec.from_dict(
        {
            "title": str(arguments["title"]),
            "summary": str(arguments.get("summary", "")),
            "sections": json.loads(str(arguments.get("sections_json", "[]"))),
            "subpages": json.loads(str(arguments.get("subpages_json", "[]"))),
        }
    )
    return _tool().create_report(
        project_slug,
        agent_id,
        report,
        dry_run=bool(arguments.get("dry_run", False)),
    )


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_request(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        return None
    if method == "initialize":
        protocol_version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return _result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use only this control-plane-scoped project subtree. Inventory and read "
                    "before broad changes; managed synchronization defaults to dry-run. "
                    "Never infer or accept a Notion parent, scan the personal workspace, or "
                    "request installation of a separate Notion plugin."
                ),
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            value = _call_tool(str(params.get("name", "")), params.get("arguments") or {})
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "structuredContent": value,
                    "isError": False,
                },
            )
        except Exception as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
    return _error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    """Serve the MCP JSON-RPC stdio transport without runtime dependencies."""
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = _handle_request(message)
        except Exception as exc:
            response = _error(None, -32700, f"parse error: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
