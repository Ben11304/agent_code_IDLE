from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


# This authority boundary is owned by AgentUI rather than by an individual
# project schema.  A project may shape its report, but it cannot silently turn
# ordinary review feedback into permission to rewrite the form itself.
REPORT_NOTE_POLICY = {
    "user_note": {
        "heading_pattern": r"^USER NOTE R-\d{4}$",
        "scope": "content_and_work_only",
        "may_change_schema": False,
    },
    "schema_note": {
        "heading_pattern": r"^SCHEMA NOTE S-\d{4}$",
        "scope": "report_schema_change",
        "may_change_schema": True,
        "version_rule": "apply all open schema notes in one transition and increment schema_version by exactly 1",
    },
    "rules": [
        "Only an exact heading in the latest source revision is actionable.",
        "Never infer schema authority from ordinary prose, formatting edits, deletions, or comments.",
        "Never edit or delete the source revision.",
        "Carry each processed note into the successor with a terminal status and acceptance evidence.",
    ],
}


class ReportSchemaError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportSchemaError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportSchemaError(f"{field} must be text")
    return value.strip()


def _text_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportSchemaError(f"{field} must be a list of text")
    return [_text(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _table_defs(value: Any, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportSchemaError(f"{field} must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ReportSchemaError(f"{field}[{index}] must be an object")
        key = _text(raw.get("key"), f"{field}[{index}].key")
        if not _KEY_RE.fullmatch(key):
            raise ReportSchemaError(f"{field}[{index}].key is invalid: {key!r}")
        if key in seen:
            raise ReportSchemaError(f"duplicate table key {key!r} in {field}")
        seen.add(key)
        title = _text(raw.get("title"), f"{field}[{index}].title")
        min_rows = raw.get("min_rows", 1)
        if not isinstance(min_rows, int) or isinstance(min_rows, bool) or not 1 <= min_rows <= 99:
            raise ReportSchemaError(
                f"{field}[{index}].min_rows must be an integer from 1 to 99"
            )
        raw_columns = raw.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise ReportSchemaError(f"{field}[{index}].columns must be a non-empty list")
        columns: list[dict[str, str]] = []
        column_keys: set[str] = set()
        column_titles: set[str] = set()
        for column_index, column in enumerate(raw_columns):
            if not isinstance(column, dict):
                raise ReportSchemaError(
                    f"{field}[{index}].columns[{column_index}] must be an object"
                )
            column_key = _text(
                column.get("key"),
                f"{field}[{index}].columns[{column_index}].key",
            )
            if not _KEY_RE.fullmatch(column_key):
                raise ReportSchemaError(
                    f"{field}[{index}].columns[{column_index}].key is invalid: "
                    f"{column_key!r}"
                )
            column_title = _text(
                column.get("title"),
                f"{field}[{index}].columns[{column_index}].title",
            )
            if column_key in column_keys or column_title in column_titles:
                raise ReportSchemaError(
                    f"duplicate table column key/title in {field}[{index}]"
                )
            column_keys.add(column_key)
            column_titles.add(column_title)
            columns.append({"key": column_key, "title": column_title})
        out.append({
            "key": key,
            "title": title,
            "columns": columns,
            "min_rows": min_rows,
            "guidance": _optional_text(
                raw.get("guidance"), f"{field}[{index}].guidance"),
        })
    return out


def _diagram_defs(value: Any, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportSchemaError(f"{field} must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ReportSchemaError(f"{field}[{index}] must be an object")
        key = _text(raw.get("key"), f"{field}[{index}].key")
        if not _KEY_RE.fullmatch(key):
            raise ReportSchemaError(f"{field}[{index}].key is invalid: {key!r}")
        if key in seen:
            raise ReportSchemaError(f"duplicate diagram key {key!r} in {field}")
        seen.add(key)
        format_name = _optional_text(
            raw.get("format", "mermaid"), f"{field}[{index}].format"
        )
        if format_name != "mermaid":
            raise ReportSchemaError(
                f"{field}[{index}].format must be 'mermaid'"
            )
        out.append({
            "key": key,
            "title": _text(raw.get("title"), f"{field}[{index}].title"),
            "format": format_name,
            "guidance": _optional_text(
                raw.get("guidance"), f"{field}[{index}].guidance"
            ),
        })
    return out


def _section_defs(value: Any, field: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ReportSchemaError(f"{field} must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            key = title = raw.strip()
            guidance = ""
            required_tables = []
            required_diagrams = []
        elif isinstance(raw, dict):
            key = _text(raw.get("key"), f"{field}[{index}].key")
            title = _text(raw.get("title"), f"{field}[{index}].title")
            guidance = _optional_text(
                raw.get("guidance"), f"{field}[{index}].guidance")
            required_tables = _table_defs(
                raw.get("required_tables"), f"{field}[{index}].required_tables")
            required_diagrams = _diagram_defs(
                raw.get("required_diagrams"),
                f"{field}[{index}].required_diagrams",
            )
        else:
            raise ReportSchemaError(f"{field}[{index}] must be text or an object")
        if not _KEY_RE.fullmatch(key):
            raise ReportSchemaError(f"{field}[{index}].key is invalid: {key!r}")
        if key in seen:
            raise ReportSchemaError(f"duplicate section key {key!r} in {field}")
        seen.add(key)
        section = {"key": key, "title": title}
        if guidance:
            section["guidance"] = guidance
        if required_tables:
            section["required_tables"] = required_tables
        if required_diagrams:
            section["required_diagrams"] = required_diagrams
        out.append(section)
    return out


def load_report_schema(project_root: str | Path, config: Any) -> dict | None:
    """Load and validate one project-owned report contract.

    The schema is deliberately kept inside the project tree so it is versioned
    with the agents that produce it.  A path escape is rejected before reading.
    """
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return None
    root = Path(project_root).resolve()
    rel = _text(config.get("schema_file"), "notion_report.schema_file")
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReportSchemaError("report schema must remain inside the project root") from exc
    if not path.is_file():
        raise ReportSchemaError(f"report schema does not exist: {rel}")
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ReportSchemaError(f"cannot read report schema: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportSchemaError("report schema root must be an object")

    version = data.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise ReportSchemaError("schema_version must be a positive integer")
    name = _text(data.get("name"), "name")
    title_pattern = _text(data.get("revision_title_pattern"), "revision_title_pattern")
    try:
        re.compile(title_pattern)
    except re.error as exc:
        raise ReportSchemaError(f"invalid revision_title_pattern: {exc}") from exc

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents or not all(isinstance(v, str) and v for v in agents):
        raise ReportSchemaError("agents must be a non-empty list of agent IDs")
    agent_ids = set(agents)

    main = data.get("main_page")
    if not isinstance(main, dict):
        raise ReportSchemaError("main_page must be an object")
    main_owner = _text(main.get("owner"), "main_page.owner")
    if main_owner not in agent_ids:
        raise ReportSchemaError(f"main_page.owner {main_owner!r} is not listed in agents")
    main_sections = _section_defs(main.get("required_sections"), "main_page.required_sections")

    raw_subpages = data.get("subpages")
    if not isinstance(raw_subpages, list) or not raw_subpages:
        raise ReportSchemaError("subpages must be a non-empty list")
    subpages: list[dict] = []
    page_keys: set[str] = set()
    page_titles: set[str] = set()
    for index, raw in enumerate(raw_subpages):
        if not isinstance(raw, dict):
            raise ReportSchemaError(f"subpages[{index}] must be an object")
        key = _text(raw.get("key"), f"subpages[{index}].key")
        if not _KEY_RE.fullmatch(key):
            raise ReportSchemaError(f"subpages[{index}].key is invalid: {key!r}")
        title = _text(raw.get("title"), f"subpages[{index}].title")
        owner = _text(raw.get("owner"), f"subpages[{index}].owner")
        if owner not in agent_ids:
            raise ReportSchemaError(f"subpage owner {owner!r} is not listed in agents")
        if key in page_keys or title in page_titles:
            raise ReportSchemaError(f"duplicate subpage key/title: {key!r} / {title!r}")
        page_keys.add(key)
        page_titles.add(title)
        subpages.append({
            "key": key,
            "title": title,
            "owner": owner,
            "guidance": _optional_text(
                raw.get("guidance"), f"subpages[{index}].guidance"),
            "required_sections": _section_defs(
                raw.get("required_sections"), f"subpages[{index}].required_sections"),
        })

    normalized = {
        "schema_version": version,
        "name": name,
        "revision_title_pattern": title_pattern,
        "agents": list(agents),
        "instructions": _text_list(data.get("instructions"), "instructions"),
        "allow_additional_sections": bool(data.get("allow_additional_sections", True)),
        "allow_additional_subpages": bool(data.get("allow_additional_subpages", False)),
        "main_page": {
            "owner": main_owner,
            "guidance": _optional_text(main.get("guidance"), "main_page.guidance"),
            "required_sections": main_sections,
        },
        "subpages": subpages,
    }
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "valid": True,
        "schema_version": version,
        "name": name,
        "path": str(path.relative_to(root)),
        "path_abs": str(path),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "note_policy": REPORT_NOTE_POLICY,
        "contract": normalized,
    }


def _validate_sections(sections: Any, required: list[dict], field: str, allow_extra: bool) -> None:
    by_key: dict[str, Any] = {}
    for section in sections:
        key = str(getattr(section, "key", "") or "").strip()
        if not key:
            raise ReportSchemaError(f"{field} contains a section without key")
        if key in by_key:
            raise ReportSchemaError(f"{field} contains duplicate section key {key!r}")
        by_key[key] = section
    required_by_key = {item["key"]: item for item in required}
    required_keys = set(required_by_key)
    missing = sorted(required_keys - set(by_key))
    if missing:
        raise ReportSchemaError(f"{field} is missing required section(s): {', '.join(missing)}")
    extra = sorted(set(by_key) - required_keys)
    if extra and not allow_extra:
        raise ReportSchemaError(f"{field} contains unsupported section(s): {', '.join(extra)}")
    for key, section in by_key.items():
        paragraphs = tuple(getattr(section, "paragraphs", ()) or ())
        highlights = tuple(getattr(section, "highlights", ()) or ())
        bullets = tuple(getattr(section, "bullets", ()) or ())
        tables = tuple(getattr(section, "tables", ()) or ())
        diagrams = tuple(getattr(section, "diagrams", ()) or ())
        table_by_key: dict[str, Any] = {}
        for table in tables:
            table_key = str(getattr(table, "key", "") or "").strip()
            if not table_key:
                raise ReportSchemaError(f"{field}.{key} contains a table without key")
            if table_key in table_by_key:
                raise ReportSchemaError(
                    f"{field}.{key} contains duplicate table key {table_key!r}"
                )
            table_by_key[table_key] = table
        required_tables = required_by_key.get(key, {}).get("required_tables") or []
        for table_def in required_tables:
            table_key = table_def["key"]
            if table_key not in table_by_key:
                raise ReportSchemaError(
                    f"{field}.{key} is missing required table {table_key!r}"
                )
            table = table_by_key[table_key]
            expected_columns = tuple(
                column["title"] for column in table_def["columns"]
            )
            actual_columns = tuple(getattr(table, "columns", ()) or ())
            if actual_columns != expected_columns:
                raise ReportSchemaError(
                    f"{field}.{key}.{table_key} columns must exactly match "
                    f"{list(expected_columns)!r}"
                )
            row_count = len(tuple(getattr(table, "rows", ()) or ()))
            min_rows = int(table_def.get("min_rows", 1))
            if row_count < min_rows:
                raise ReportSchemaError(
                    f"{field}.{key}.{table_key} must contain at least "
                    f"{min_rows} data row(s); found {row_count}"
                )
        diagram_by_key: dict[str, Any] = {}
        for diagram in diagrams:
            diagram_key = str(getattr(diagram, "key", "") or "").strip()
            if not diagram_key:
                raise ReportSchemaError(
                    f"{field}.{key} contains a diagram without key"
                )
            if diagram_key in diagram_by_key:
                raise ReportSchemaError(
                    f"{field}.{key} contains duplicate diagram key {diagram_key!r}"
                )
            diagram_by_key[diagram_key] = diagram
        required_diagrams = (
            required_by_key.get(key, {}).get("required_diagrams") or []
        )
        for diagram_def in required_diagrams:
            diagram_key = diagram_def["key"]
            if diagram_key not in diagram_by_key:
                raise ReportSchemaError(
                    f"{field}.{key} is missing required diagram {diagram_key!r}"
                )
            diagram = diagram_by_key[diagram_key]
            actual_format = str(getattr(diagram, "format", "") or "")
            if actual_format != diagram_def["format"]:
                raise ReportSchemaError(
                    f"{field}.{key}.{diagram_key} format must be "
                    f"{diagram_def['format']!r}"
                )
        if not paragraphs and not highlights and not bullets and not tables and not diagrams:
            raise ReportSchemaError(
                f"{field}.{key} has no body; add a paragraph, highlight, bullet, "
                "table, or diagram"
            )


def validate_report_spec(report: Any, loaded_schema: dict) -> None:
    """Fail before any Notion write when a report does not satisfy its form."""
    contract = loaded_schema.get("contract") or loaded_schema
    pattern = re.compile(contract["revision_title_pattern"])
    if not pattern.fullmatch(str(getattr(report, "title", ""))):
        raise ReportSchemaError(
            f"report title must match {contract['revision_title_pattern']!r}"
        )
    allow_sections = bool(contract.get("allow_additional_sections", True))
    _validate_sections(
        report.sections,
        contract["main_page"]["required_sections"],
        "main_page",
        allow_sections,
    )

    schema_pages = {item["key"]: item for item in contract["subpages"]}
    actual_pages: dict[str, Any] = {}
    for page in report.subpages:
        key = str(getattr(page, "key", "") or "").strip()
        if not key:
            raise ReportSchemaError("subpage is missing key")
        if key in actual_pages:
            raise ReportSchemaError(f"duplicate subpage key {key!r}")
        actual_pages[key] = page
    missing_pages = sorted(set(schema_pages) - set(actual_pages))
    if missing_pages:
        raise ReportSchemaError(
            "report is missing required subpage(s): " + ", ".join(missing_pages)
        )
    extra_pages = sorted(set(actual_pages) - set(schema_pages))
    if extra_pages and not contract.get("allow_additional_subpages", False):
        raise ReportSchemaError(
            "report contains unsupported subpage(s): " + ", ".join(extra_pages)
        )
    for key, schema_page in schema_pages.items():
        page = actual_pages[key]
        if page.title != schema_page["title"]:
            raise ReportSchemaError(
                f"subpage {key!r} title must be {schema_page['title']!r}"
            )
        _validate_sections(
            page.sections,
            schema_page["required_sections"],
            f"subpages.{key}",
            allow_sections,
        )
