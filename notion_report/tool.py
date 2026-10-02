from __future__ import annotations

import os
import hashlib
import fcntl
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import NotionClient
from .config import (
    Destination,
    DestinationError,
    DestinationStore,
    SYSTEM_AGENT_ID,
    SYSTEM_PROJECT_SLUG,
    extract_page_id,
)
from .schema import validate_report_spec


MAX_RICH_TEXT = 2000
MAX_TREE_DEPTH = 8
MAX_TREE_PAGES = 200
MAX_READ_BLOCKS = 1000
MAX_TABLE_COLUMNS = 20
# The generated header is itself one table_row; keep the nested children array
# within Notion's 100-block request limit.
MAX_TABLE_ROWS = 99
MAX_TABLE_CELL_TEXT = 4000
MAX_DIAGRAM_SOURCE = 40000
_REPORT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\*~`$\[\]<>{}|^])")


def _plain_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    return value.strip()


def _rich_text(text: str, *, bold: bool = False) -> list[dict]:
    if not text:
        return []
    parts = []
    for i in range(0, len(text), MAX_RICH_TEXT):
        part = {"type": "text", "text": {"content": text[i : i + MAX_RICH_TEXT]}}
        if bold:
            part["annotations"] = {"bold": True}
        parts.append(part)
    return parts


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(text)}}


def _bold_paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text, bold=True)},
    }


def _markdown_inline(text: str) -> str:
    """Escape one structured text value without allowing it to add blocks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = _MARKDOWN_SPECIAL_RE.sub(r"\\\1", normalized)
    return escaped.replace("\n", "<br>")


def _cell_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{field_name} must be scalar text or a number")
    text = str(value).strip()
    if len(text) > MAX_TABLE_CELL_TEXT:
        raise ValueError(
            f"{field_name} exceeds the {MAX_TABLE_CELL_TEXT}-character limit"
        )
    return text


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    key: str = ""
    caption: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "TableSpec":
        if not isinstance(value, dict):
            raise ValueError("each table must be an object")
        unknown = set(value) - {"key", "caption", "columns", "rows"}
        if unknown:
            raise ValueError(
                "unsupported table field(s): " + ", ".join(sorted(unknown))
            )
        columns = value.get("columns") or []
        rows = value.get("rows") or []
        if not isinstance(columns, list) or not columns:
            raise ValueError("table.columns must be a non-empty array")
        if len(columns) > MAX_TABLE_COLUMNS:
            raise ValueError(f"table cannot exceed {MAX_TABLE_COLUMNS} columns")
        normalized_columns = tuple(
            _cell_text(item, f"table.columns[{index}]")
            for index, item in enumerate(columns)
        )
        if any(not item for item in normalized_columns):
            raise ValueError("table column titles must be non-empty")
        if len(set(normalized_columns)) != len(normalized_columns):
            raise ValueError("table column titles must be unique")
        if not isinstance(rows, list):
            raise ValueError("table.rows must be an array")
        if len(rows) > MAX_TABLE_ROWS:
            raise ValueError(f"table cannot exceed {MAX_TABLE_ROWS} data rows")
        normalized_rows: list[tuple[str, ...]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list):
                raise ValueError(f"table.rows[{row_index}] must be an array")
            if len(row) != len(normalized_columns):
                raise ValueError(
                    f"table.rows[{row_index}] has {len(row)} cells; "
                    f"expected {len(normalized_columns)}"
                )
            normalized_rows.append(tuple(
                _cell_text(cell, f"table.rows[{row_index}][{cell_index}]")
                for cell_index, cell in enumerate(row)
            ))
        return cls(
            key=_plain_text(value.get("key", ""), "table.key"),
            caption=_plain_text(value.get("caption", ""), "table.caption"),
            columns=normalized_columns,
            rows=tuple(normalized_rows),
        )

    def notion_block(self) -> dict:
        def row(values: tuple[str, ...]) -> dict:
            return {
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": [_rich_text(value) for value in values]},
            }

        return {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": len(self.columns),
                "has_column_header": True,
                "has_row_header": False,
                "children": [row(self.columns), *(row(values) for values in self.rows)],
            },
        }

    def notion_markdown(self) -> str:
        rows = [self.columns, *self.rows]
        lines = [
            "| " + " | ".join(_markdown_inline(value) for value in row) + " |"
            for row in rows
        ]
        lines.insert(1, "| " + " | ".join("---" for _ in self.columns) + " |")
        return "\n".join(lines)


@dataclass(frozen=True)
class DiagramSpec:
    key: str
    source: str
    format: str = "mermaid"
    caption: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "DiagramSpec":
        if not isinstance(value, dict):
            raise ValueError("each diagram must be an object")
        unknown = set(value) - {"key", "format", "source", "caption"}
        if unknown:
            raise ValueError(
                "unsupported diagram field(s): " + ", ".join(sorted(unknown))
            )
        key = _plain_text(value.get("key", ""), "diagram.key")
        if not key:
            raise ValueError("diagram.key is required")
        format_name = _plain_text(value.get("format", "mermaid"), "diagram.format")
        if format_name != "mermaid":
            raise ValueError("diagram.format must be 'mermaid'")
        source = _plain_text(value.get("source", ""), "diagram.source")
        if not source:
            raise ValueError("diagram.source is required")
        if len(source) > MAX_DIAGRAM_SOURCE:
            raise ValueError(
                f"diagram.source exceeds the {MAX_DIAGRAM_SOURCE}-character limit"
            )
        if "```" in source:
            raise ValueError("diagram.source must not contain a fenced-code delimiter")
        return cls(
            key=key,
            format=format_name,
            source=source,
            caption=_plain_text(value.get("caption", ""), "diagram.caption"),
        )

    def notion_block(self) -> dict:
        return {
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": _rich_text(self.source),
                "language": "mermaid",
                "caption": [],
            },
        }

    def notion_markdown(self) -> str:
        return f"```mermaid\n{self.source}\n```"


@dataclass(frozen=True)
class SectionSpec:
    heading: str
    key: str = ""
    paragraphs: tuple[str, ...] = ()
    highlights: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    tables: tuple[TableSpec, ...] = ()
    diagrams: tuple[DiagramSpec, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> "SectionSpec":
        if not isinstance(value, dict):
            raise ValueError("each section must be an object")
        unknown = set(value) - {
            "key", "heading", "paragraphs", "highlights", "bullets", "content",
            "tables", "diagrams",
        }
        if unknown:
            raise ValueError(
                "unsupported section field(s): " + ", ".join(sorted(unknown))
            )
        heading = _plain_text(value.get("heading", ""), "section.heading")
        if not heading:
            raise ValueError("section.heading is required")
        paragraphs = value.get("paragraphs") or []
        highlights = value.get("highlights") or []
        bullets = value.get("bullets") or []
        tables = value.get("tables") or []
        diagrams = value.get("diagrams") or []
        # ``content`` was used by agents as the intuitive singular/body field.
        # Older code silently ignored it and created heading-only reports while
        # still returning success.  Accept it as paragraph content, but keep all
        # other unknown fields fail-closed so report bodies can never disappear
        # without an explicit schema error again.
        content = value.get("content") or []
        if isinstance(content, str):
            content = [content]
        if (
            not isinstance(paragraphs, list)
            or not isinstance(highlights, list)
            or not isinstance(bullets, list)
        ):
            raise ValueError(
                "section paragraphs, highlights, and bullets must be arrays"
            )
        if not isinstance(tables, list):
            raise ValueError("section.tables must be an array")
        if not isinstance(diagrams, list):
            raise ValueError("section.diagrams must be an array")
        if not isinstance(content, list):
            raise ValueError("section.content must be text or an array of text")
        return cls(
            key=_plain_text(value.get("key", ""), "section.key"),
            heading=heading,
            paragraphs=tuple(
                _plain_text(v, "section.paragraph")
                for v in [*paragraphs, *content]
                if str(v).strip()
            ),
            highlights=tuple(
                _plain_text(v, "section.highlight")
                for v in highlights
                if str(v).strip()
            ),
            bullets=tuple(_plain_text(v, "section.bullet") for v in bullets if str(v).strip()),
            tables=tuple(TableSpec.from_dict(v) for v in tables),
            diagrams=tuple(DiagramSpec.from_dict(v) for v in diagrams),
        )


@dataclass(frozen=True)
class SubpageSpec:
    title: str
    key: str = ""
    summary: str = ""
    sections: tuple[SectionSpec, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> "SubpageSpec":
        if not isinstance(value, dict):
            raise ValueError("each subpage must be an object")
        unknown = set(value) - {"key", "title", "summary", "sections"}
        if unknown:
            raise ValueError(
                "unsupported subpage field(s): " + ", ".join(sorted(unknown))
            )
        title = _plain_text(value.get("title", ""), "subpage.title")
        if not title:
            raise ValueError("subpage.title is required")
        sections = value.get("sections") or []
        if not isinstance(sections, list):
            raise ValueError("subpage.sections must be an array")
        return cls(
            key=_plain_text(value.get("key", ""), "subpage.key"),
            title=title,
            summary=_plain_text(value.get("summary", ""), "subpage.summary"),
            sections=tuple(SectionSpec.from_dict(v) for v in sections),
        )


@dataclass(frozen=True)
class ReportSpec:
    title: str
    summary: str = ""
    sections: tuple[SectionSpec, ...] = field(default_factory=tuple)
    subpages: tuple[SubpageSpec, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: dict) -> "ReportSpec":
        if not isinstance(value, dict):
            raise ValueError("report must be an object")
        unknown = set(value) - {"title", "summary", "sections", "subpages"}
        if unknown:
            raise ValueError(
                "unsupported report field(s): " + ", ".join(sorted(unknown))
            )
        title = _plain_text(value.get("title", ""), "report.title")
        if not title:
            raise ValueError("report.title is required")
        sections = value.get("sections") or []
        subpages = value.get("subpages") or []
        if not isinstance(sections, list) or not isinstance(subpages, list):
            raise ValueError("report.sections and report.subpages must be arrays")
        return cls(
            title=title,
            summary=_plain_text(value.get("summary", ""), "report.summary"),
            sections=tuple(SectionSpec.from_dict(v) for v in sections),
            subpages=tuple(SubpageSpec.from_dict(v) for v in subpages),
        )


def report_blocks(summary: str, sections: tuple[SectionSpec, ...]) -> list[dict]:
    blocks: list[dict] = []
    if summary:
        blocks.append(_block("callout", summary) | {"callout": {"rich_text": _rich_text(summary), "icon": {"type": "emoji", "emoji": "📌"}}})
    for section in sections:
        blocks.append(_block("heading_2", section.heading))
        blocks.extend(_block("paragraph", text) for text in section.paragraphs)
        blocks.extend(_bold_paragraph(text) for text in section.highlights)
        blocks.extend(_block("bulleted_list_item", text) for text in section.bullets)
        for table in section.tables:
            if table.caption:
                blocks.append(_block("heading_3", table.caption))
            blocks.append(table.notion_block())
        for diagram in section.diagrams:
            if diagram.caption:
                blocks.append(_block("heading_3", diagram.caption))
            blocks.append(diagram.notion_block())
    return blocks


def report_markdown(summary: str, sections: tuple[SectionSpec, ...]) -> str:
    """Compile validated report JSON into Notion-flavored Markdown."""
    chunks: list[str] = []
    if summary:
        chunks.append(
            '<callout icon="📌">\n\t'
            + _markdown_inline(summary)
            + "\n</callout>"
        )
    for section in sections:
        chunks.append(f"## {_markdown_inline(section.heading)}")
        chunks.extend(_markdown_inline(text) for text in section.paragraphs)
        chunks.extend(f"**{_markdown_inline(text)}**" for text in section.highlights)
        if section.bullets:
            chunks.append("\n".join(
                f"- {_markdown_inline(text)}" for text in section.bullets
            ))
        for table in section.tables:
            if table.caption:
                chunks.append(f"### {_markdown_inline(table.caption)}")
            chunks.append(table.notion_markdown())
        for diagram in section.diagrams:
            if diagram.caption:
                chunks.append(f"### {_markdown_inline(diagram.caption)}")
            chunks.append(diagram.notion_markdown())
    return "\n\n".join(chunks).strip()


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in prop.get("title") or [])
    return ""


def _block_text(block: dict) -> str:
    payload = block.get(block.get("type", "")) or {}
    if block.get("type") == "child_page":
        return str(payload.get("title") or "")
    if block.get("type") == "table_row":
        return " | ".join(
            "".join(
                part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in cell
            )
            for cell in payload.get("cells") or []
        )
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "")
        for part in payload.get("rich_text") or []
    )


def _content_hash(items: list[dict]) -> str:
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _block_readback_item(block: dict, depth: int) -> dict:
    kind = str(block.get("type") or "")
    payload = block.get(kind) or {}
    if kind == "table":
        return {
            "type": kind,
            "depth": depth,
            "table_width": int(payload.get("table_width") or 0),
            "has_column_header": bool(payload.get("has_column_header")),
            "has_row_header": bool(payload.get("has_row_header")),
        }
    if kind == "table_row":
        cells = []
        for cell in payload.get("cells") or []:
            cells.append("".join(
                part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in cell
            ))
        return {"type": kind, "depth": depth, "cells": cells}
    if kind == "code":
        caption = "".join(
            part.get("plain_text") or part.get("text", {}).get("content", "")
            for part in payload.get("caption") or []
        )
        return {
            "type": kind,
            "depth": depth,
            "text": _block_text(block),
            "language": str(payload.get("language") or ""),
            "caption": caption,
        }
    return {"type": kind, "depth": depth, "text": _block_text(block)}


def _flatten_blocks(
    blocks: list[dict],
    *,
    client: Any | None = None,
    depth: int = 0,
) -> list[dict]:
    """Canonicalize top-level blocks and nested table rows for exact read-back."""
    items: list[dict] = []
    for block in blocks:
        kind = str(block.get("type") or "")
        items.append(_block_readback_item(block, depth))
        payload = block.get(kind) or {}
        nested = payload.get("children") or []
        block_id = str(block.get("id") or "")
        if not nested and client is not None and block_id and bool(block.get("has_children")):
            nested = client.list_block_children(block_id)
        if nested and kind != "child_page":
            items.extend(_flatten_blocks(list(nested), client=client, depth=depth + 1))
    return items


def _verify_created_page(client: Any, page_id: str, expected_blocks: list[dict]) -> list[dict]:
    expected = _flatten_blocks(expected_blocks)
    actual_top = [
        block for block in client.list_block_children(page_id)
        if block.get("type") != "child_page"
    ]
    actual = _flatten_blocks(actual_top, client=client)
    if actual != expected:
        raise DestinationError("Notion page read-back did not match desired content")
    return actual


def _verify_created_markdown_page(
    client: Any,
    page_id: str,
    expected_blocks: list[dict],
) -> tuple[list[dict], str]:
    """Verify both rendered blocks and the Markdown API read surface."""
    actual = _verify_created_page(client, page_id, expected_blocks)
    read_back = client.retrieve_page_markdown(page_id)
    markdown = read_back.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise DestinationError("Notion Markdown read-back was empty")
    if bool(read_back.get("truncated")) or list(read_back.get("unknown_block_ids") or []):
        raise DestinationError(
            "Notion Markdown read-back was truncated or contained unknown blocks"
        )
    return actual, markdown


class NotionReportTool:
    """Create reports only below a pre-bound and workspace-locked parent page."""

    def __init__(
        self,
        store: DestinationStore,
        *,
        environ: dict[str, str] | None = None,
        client_factory: Callable[[str], Any] = NotionClient,
        report_schema: dict | None = None,
    ):
        self.store = store
        self.environ = environ if environ is not None else os.environ
        self.client_factory = client_factory
        self.report_schema = report_schema

    def _client(self, token_env: str):
        token = self.environ.get(token_env, "").strip()
        if not token:
            raise DestinationError(f"Missing Notion credential in environment variable {token_env}")
        return self.client_factory(token)

    def _destination(
        self,
        project_slug: str,
        agent_id: str,
        *,
        allow_provision: bool = True,
    ) -> Destination:
        """Resolve a project lock, optionally provisioning it below System Hub.

        The hub credential is used only while establishing the project child.
        The resulting destination is persisted under the canonical project root,
        after which all agents inherit the narrower project subtree as before.
        """
        try:
            return self.store.resolve(project_slug, agent_id)
        except DestinationError as exc:
            if not str(exc).startswith("No Notion destination bound"):
                raise
            if not allow_provision or (
                project_slug == SYSTEM_PROJECT_SLUG and agent_id == SYSTEM_AGENT_ID
            ):
                raise
        return self._provision_project_destination(project_slug, agent_id)

    def _provision_project_destination(
        self,
        project_slug: str,
        requesting_agent_id: str,
    ) -> Destination:
        project_name = " ".join(
            str(self.environ.get("AGENTUI_PROJECT_NAME") or project_slug).split()
        ).strip()
        root_agent_id = str(
            self.environ.get("AGENTUI_PROJECT_ROOT_AGENT_ID") or ""
        ).strip()
        if not root_agent_id:
            raise DestinationError(
                f"No Notion destination bound for project {project_slug}; "
                "AgentUI did not supply the canonical project root agent"
            )
        title = f"{project_name[:1800]} [{project_slug}]"

        # Serialize auto-provisioning across backend/CLI processes. This prevents
        # two simultaneous first turns from creating duplicate project children.
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.store.path.with_name(f".{self.store.path.name}.provision.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    return self.store.resolve(project_slug, requesting_agent_id)
                except DestinationError as exc:
                    if not str(exc).startswith("No Notion destination bound"):
                        raise

                try:
                    hub = self.store.get(SYSTEM_PROJECT_SLUG, SYSTEM_AGENT_ID)
                except DestinationError as exc:
                    raise DestinationError(
                        f"No Notion destination bound for project {project_slug}; "
                        "bind the AgentUI System Hub once in system settings. "
                        "A separate Notion plugin is not required"
                    ) from exc

                client = self._client(hub.token_env)
                me = client.get_self()
                workspace_id = extract_page_id(
                    (me.get("bot") or {}).get("workspace_id", "")
                )
                if workspace_id != hub.workspace_id:
                    raise DestinationError(
                        f"Workspace mismatch: System Hub is locked to {hub.workspace_id}, "
                        f"but {hub.token_env} belongs to {workspace_id}"
                    )
                client.retrieve_page(hub.parent_page_id)
                direct_children = client.list_block_children(hub.parent_page_id)
                matches = [
                    child for child in direct_children
                    if child.get("type") == "child_page"
                    and str((child.get("child_page") or {}).get("title") or "") == title
                ]
                if len(matches) > 1:
                    raise DestinationError(
                        f"Multiple System Hub children titled {title!r}; "
                        "merge or rename duplicates before provisioning"
                    )
                if matches:
                    page_id = str(matches[0].get("id") or "")
                    page = client.retrieve_page(page_id)
                else:
                    page = client.create_page(
                        hub.parent_page_id,
                        title,
                        [_block(
                            "paragraph",
                            f"AgentUI-managed project root for {project_slug}. "
                            "Agent reports are confined to this subtree.",
                        )],
                    )
                    page_id = str(page.get("id") or "")
                if not page_id:
                    raise DestinationError("Provisioned Notion project page has no page id")
                parent_url = str(page.get("url") or "").strip()
                if not parent_url:
                    parent_url = f"https://www.notion.so/{page_id.replace('-', '')}"
                destination = Destination(
                    project_slug=project_slug,
                    agent_id=root_agent_id,
                    parent_url=parent_url,
                    parent_page_id=page_id,
                    token_env=hub.token_env,
                    workspace_id=hub.workspace_id,
                    workspace_name=hub.workspace_name,
                    parent_title=_page_title(page) or title,
                )
                self.store.upsert(destination)
                return self.store.resolve(project_slug, requesting_agent_id)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _bounded(value: int, *, minimum: int, maximum: int, field: str) -> int:
        number = int(value)
        if number < minimum or number > maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        return number

    def _tree(
        self,
        client: Any,
        destination: Destination,
        *,
        max_depth: int,
        max_pages: int,
    ) -> tuple[list[dict], bool]:
        max_depth = self._bounded(max_depth, minimum=0, maximum=MAX_TREE_DEPTH, field="max_depth")
        max_pages = self._bounded(max_pages, minimum=1, maximum=MAX_TREE_PAGES, field="max_pages")
        root = client.retrieve_page(destination.parent_page_id)
        pages = [{
            "page_id": destination.parent_page_id,
            "title": _page_title(root) or destination.parent_title,
            "parent_page_id": None,
            "depth": 0,
            "last_edited_time": root.get("last_edited_time"),
            "url": root.get("url") or destination.parent_url,
        }]
        queue: list[tuple[str, int]] = [(destination.parent_page_id, 0)]
        truncated = False
        while queue:
            parent_id, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for block in client.list_block_children(parent_id):
                if block.get("type") != "child_page":
                    continue
                if len(pages) >= max_pages:
                    truncated = True
                    queue.clear()
                    break
                child_id = str(block.get("id") or "")
                if not child_id:
                    continue
                child = block.get("child_page") or {}
                pages.append({
                    "page_id": child_id,
                    "title": str(child.get("title") or ""),
                    "parent_page_id": parent_id,
                    "depth": depth + 1,
                    "last_edited_time": block.get("last_edited_time"),
                    "url": block.get("url"),
                })
                queue.append((child_id, depth + 1))
        return pages, truncated

    def inventory_project_tree(
        self,
        project_slug: str,
        agent_id: str,
        *,
        max_depth: int = 4,
        max_pages: int = 100,
    ) -> dict:
        destination = self._destination(project_slug, agent_id)
        self.status(project_slug, agent_id, verify=True)
        client = self._client(destination.token_env)
        pages, truncated = self._tree(
            client, destination, max_depth=max_depth, max_pages=max_pages)
        return {
            "project_slug": project_slug,
            "agent_id": agent_id,
            "scope": "bound_project_subtree",
            "root_page_id": destination.parent_page_id,
            "page_count": len(pages),
            "truncated": truncated,
            "pages": pages,
        }

    def read_project_page(
        self,
        project_slug: str,
        agent_id: str,
        page_id: str,
        *,
        max_blocks: int = 500,
    ) -> dict:
        destination = self._destination(project_slug, agent_id)
        self.status(project_slug, agent_id, verify=True)
        client = self._client(destination.token_env)
        pages, tree_truncated = self._tree(
            client, destination, max_depth=MAX_TREE_DEPTH, max_pages=MAX_TREE_PAGES)
        page_map = {page["page_id"]: page for page in pages}
        normalized_id = extract_page_id(page_id)
        if normalized_id not in page_map:
            suffix = " (tree inventory was truncated)" if tree_truncated else ""
            raise DestinationError(
                f"Page {normalized_id} is outside the bound project subtree{suffix}"
            )
        max_blocks = self._bounded(
            max_blocks, minimum=1, maximum=MAX_READ_BLOCKS, field="max_blocks")
        blocks: list[dict] = []
        queue: list[tuple[str, int]] = [(normalized_id, 0)]
        truncated = False
        while queue:
            block_parent, depth = queue.pop(0)
            for block in client.list_block_children(block_parent):
                if len(blocks) >= max_blocks:
                    truncated = True
                    queue.clear()
                    break
                kind = str(block.get("type") or "")
                item = {
                    "block_id": str(block.get("id") or ""),
                    "type": kind,
                    "text": _block_text(block),
                    "depth": depth,
                    "has_children": bool(block.get("has_children")),
                    "last_edited_time": block.get("last_edited_time"),
                }
                blocks.append(item)
                if item["has_children"] and kind != "child_page" and item["block_id"]:
                    queue.append((item["block_id"], depth + 1))
        hash_items = [
            {"type": block["type"], "text": block["text"], "depth": block["depth"]}
            for block in blocks
        ]
        return {
            "project_slug": project_slug,
            "agent_id": agent_id,
            "scope": "bound_project_subtree",
            "page": page_map[normalized_id],
            "block_count": len(blocks),
            "truncated": truncated,
            "content_sha256": _content_hash(hash_items),
            "blocks": blocks,
        }

    def sync_managed_report(
        self,
        project_slug: str,
        agent_id: str,
        page_title: str,
        report_key: str,
        source_ref: str,
        *,
        summary: str = "",
        sections: tuple[SectionSpec, ...] | list[dict] = (),
        create_if_missing: bool = False,
        dry_run: bool = True,
    ) -> dict:
        title = _plain_text(page_title, "page_title")
        key = _plain_text(report_key, "report_key")
        source = _plain_text(source_ref, "source_ref")
        if not _REPORT_KEY_RE.fullmatch(key):
            raise ValueError("report_key must use lowercase letters, digits, _, . or -")
        if not source or len(source) > 200:
            raise ValueError("source_ref must contain 1-200 characters")
        normalized_sections = tuple(
            item if isinstance(item, SectionSpec) else SectionSpec.from_dict(item)
            for item in sections
        )
        if any(section.tables for section in normalized_sections):
            raise ValueError(
                "native tables are supported only by create_notion_report"
            )
        content = report_blocks(_plain_text(summary, "summary"), normalized_sections)
        if not content:
            raise ValueError("summary or sections content is required")
        start_text = f"AGENTUI_REPORT_START key={key} source={source}"
        end_text = f"AGENTUI_REPORT_END key={key}"
        desired = [_block("paragraph", start_text), *content, _block("paragraph", end_text)]
        expected = [_block_text(block) for block in desired]

        destination = self._destination(
            project_slug, agent_id, allow_provision=not dry_run)
        self.status(project_slug, agent_id, verify=True)
        client = self._client(destination.token_env)
        direct_children = client.list_block_children(destination.parent_page_id)
        matches = [
            child for child in direct_children
            if child.get("type") == "child_page"
            and str((child.get("child_page") or {}).get("title") or "") == title
        ]
        if len(matches) > 1:
            raise DestinationError(f"Multiple direct child pages titled {title!r}")
        if not matches and not create_if_missing:
            raise DestinationError(f"No direct child page titled {title!r}")

        page_id = str(matches[0].get("id") or "") if matches else ""
        existing = client.list_block_children(page_id) if page_id else []
        start_indexes = [
            index for index, block in enumerate(existing)
            if _block_text(block).startswith(f"AGENTUI_REPORT_START key={key} ")
        ]
        end_indexes = [
            index for index, block in enumerate(existing)
            if _block_text(block) == end_text
        ]
        if len(start_indexes) > 1 or len(end_indexes) > 1:
            raise DestinationError(f"Multiple managed sections found for report_key {key}")
        managed: list[dict] = []
        if start_indexes or end_indexes:
            if len(start_indexes) != 1 or len(end_indexes) != 1 or end_indexes[0] < start_indexes[0]:
                raise DestinationError(f"Malformed managed section for report_key {key}")
            managed = existing[start_indexes[0] : end_indexes[0] + 1]
        current_text = [_block_text(block) for block in managed]
        if current_text == expected:
            action = "unchanged"
        elif not matches:
            action = "create"
        elif managed:
            action = "replace_managed_section"
        else:
            action = "append_managed_section"
        plan = {
            "project_slug": project_slug,
            "agent_id": agent_id,
            "page_title": title,
            "page_id": page_id or None,
            "report_key": key,
            "source_ref": source,
            "action": action,
            "existing_managed_text": current_text,
            "desired_text": expected,
        }
        if dry_run:
            return {"dry_run": True, "read_back_verified": False, **plan}
        if action == "create":
            created = client.create_page(destination.parent_page_id, title, desired)
            page_id = str(created.get("id") or "")
        elif action == "replace_managed_section":
            for block in managed:
                block_id = str(block.get("id") or "")
                if not block_id:
                    raise DestinationError("Managed Notion block has no id")
                client.archive_block(block_id)
            client.append_children(page_id, desired)
        elif action == "append_managed_section":
            client.append_children(page_id, desired)
        if not page_id:
            raise DestinationError(f"Notion child page {title!r} has no page id")
        read_back = client.list_block_children(page_id)
        actual = [_block_text(block) for block in read_back[-len(desired):]]
        if actual != expected:
            raise DestinationError("Managed report read-back did not match desired content")
        return {
            "dry_run": False,
            **plan,
            "page_id": page_id,
            "read_back_verified": True,
            "content_sha256": _content_hash([
                {"text": text} for text in actual
            ]),
        }

    def bind_destination(
        self,
        project_slug: str,
        agent_id: str,
        parent_url: str,
        token_env: str,
    ) -> dict:
        """Verify a parent using its token, then atomically save the workspace lock."""
        parent_page_id = extract_page_id(parent_url)
        client = self._client(token_env)
        me = client.get_self()
        page = client.retrieve_page(parent_page_id)
        bot = me.get("bot") or {}
        workspace_id = bot.get("workspace_id")
        if not workspace_id:
            raise DestinationError("Notion token did not report a workspace_id; destination was not saved")
        destination = Destination(
            project_slug=project_slug,
            agent_id=agent_id,
            parent_url=parent_url,
            parent_page_id=parent_page_id,
            token_env=token_env,
            workspace_id=workspace_id,
            workspace_name=str(bot.get("workspace_name") or ""),
            parent_title=_page_title(page),
        )
        self.store.upsert(destination)
        return self.status(project_slug, agent_id, verify=True)

    def status(self, project_slug: str, agent_id: str, *, verify: bool = False) -> dict:
        destination = self._destination(project_slug, agent_id)
        result = {
            "configured": True,
            "project_slug": project_slug,
            "agent_id": agent_id,
            "destination_owner_agent_id": destination.agent_id,
            "inherited": destination.agent_id != agent_id,
            "parent_url": destination.parent_url,
            "parent_page_id": destination.parent_page_id,
            "parent_title": destination.parent_title,
            "workspace_id": destination.workspace_id,
            "workspace_name": destination.workspace_name,
            "token_env": destination.token_env,
            "verified": False,
        }
        if verify:
            client = self._client(destination.token_env)
            me = client.get_self()
            current_workspace = extract_page_id((me.get("bot") or {}).get("workspace_id", ""))
            if current_workspace != destination.workspace_id:
                raise DestinationError(
                    f"Workspace mismatch: destination is locked to {destination.workspace_id}, "
                    f"but {destination.token_env} belongs to {current_workspace}"
                )
            page = client.retrieve_page(destination.parent_page_id)
            result.update(
                verified=True,
                workspace_name=str((me.get("bot") or {}).get("workspace_name") or ""),
                parent_title=_page_title(page) or destination.parent_title,
            )
        return result

    def create_report(
        self,
        project_slug: str,
        agent_id: str,
        report: ReportSpec | dict,
        *,
        dry_run: bool = False,
    ) -> dict:
        spec = report if isinstance(report, ReportSpec) else ReportSpec.from_dict(report)
        if self.report_schema:
            validate_report_spec(spec, self.report_schema)
        destination = self._destination(
            project_slug, agent_id, allow_provision=not dry_run)
        main_blocks = report_blocks(spec.summary, spec.sections)
        main_markdown = report_markdown(spec.summary, spec.sections)
        if not main_markdown:
            raise ValueError("report summary or section body is required")
        subpage_payloads = []
        for subpage in spec.subpages:
            subpage_blocks = report_blocks(subpage.summary, subpage.sections)
            subpage_markdown = report_markdown(subpage.summary, subpage.sections)
            if not subpage_markdown:
                raise ValueError(
                    f"subpage {subpage.title!r} requires summary or section body"
                )
            subpage_payloads.append((subpage, subpage_blocks, subpage_markdown))
        plan = {
            "project_slug": project_slug,
            "agent_id": agent_id,
            "workspace_id": destination.workspace_id,
            "parent_page_id": destination.parent_page_id,
            "title": spec.title,
            "block_count": len(main_blocks),
            "subpages": [sub.title for sub in spec.subpages],
        }
        if dry_run:
            return {"dry_run": True, **plan}

        # Verify immediately before every write. This catches token/workspace changes
        # and revoked parent access without ever falling back to workspace-root writes.
        self.status(project_slug, agent_id, verify=True)
        client = self._client(destination.token_env)
        main = client.create_page_markdown(
            destination.parent_page_id,
            spec.title,
            main_markdown,
        )
        main_id = str(main.get("id") or "")
        if not main_id:
            raise DestinationError("Created Notion report has no page id")
        created_subpages: list[dict] = []
        for subpage, subpage_blocks, subpage_markdown in subpage_payloads:
            child = client.create_page_markdown(
                main_id,
                subpage.title,
                subpage_markdown,
            )
            child_id = str(child.get("id") or "")
            if not child_id:
                raise DestinationError(
                    f"Created Notion subpage {subpage.title!r} has no page id"
                )
            try:
                actual, markdown_read_back = _verify_created_markdown_page(
                    client,
                    child_id,
                    subpage_blocks,
                )
            except DestinationError as exc:
                raise DestinationError(
                    f"Notion subpage {subpage.title!r} read-back did not match desired content"
                ) from exc
            created_subpages.append({
                "id": child_id,
                "url": child.get("url"),
                "title": subpage.title,
                "block_count": len(actual),
                "read_back_verified": True,
                "content_sha256": _content_hash(actual),
                "markdown_sha256": hashlib.sha256(
                    markdown_read_back.encode("utf-8")
                ).hexdigest(),
            })
        try:
            main_actual, main_markdown_read_back = _verify_created_markdown_page(
                client,
                main_id,
                main_blocks,
            )
        except DestinationError as exc:
            raise DestinationError(
                "Notion main report read-back did not match desired content"
            ) from exc
        return {
            "dry_run": False,
            **plan,
            "page": {"id": main.get("id"), "url": main.get("url"), "title": spec.title},
            "created_subpages": created_subpages,
            "read_back_verified": True,
            "main_block_count": len(main_actual),
            "content_sha256": _content_hash(main_actual),
            "markdown_sha256": hashlib.sha256(
                main_markdown_read_back.encode("utf-8")
            ).hexdigest(),
            "writer": "notion_enhanced_markdown",
        }

    def append_to_child_page(
        self,
        project_slug: str,
        agent_id: str,
        page_title: str,
        *,
        summary: str = "",
        sections: tuple[SectionSpec, ...] | list[dict] = (),
        create_if_missing: bool = False,
    ) -> dict:
        """Publish to one exact direct child of the bound parent and read back.

        Creation remains opt-in. Multiple exact matches always fail closed, and
        retrying identical content is idempotent when it is already the page tail.
        """
        title = _plain_text(page_title, "page_title")
        if not title:
            raise ValueError("page_title is required")
        normalized_sections = tuple(
            item if isinstance(item, SectionSpec) else SectionSpec.from_dict(item)
            for item in sections
        )
        if any(section.tables for section in normalized_sections):
            raise ValueError(
                "native tables are supported only by create_notion_report"
            )
        blocks = report_blocks(_plain_text(summary, "summary"), normalized_sections)
        if not blocks:
            raise ValueError("summary or sections content is required")

        destination = self._destination(project_slug, agent_id)
        self.status(project_slug, agent_id, verify=True)
        client = self._client(destination.token_env)
        direct_children = client.list_block_children(destination.parent_page_id)
        matches = [
            child for child in direct_children
            if child.get("type") == "child_page"
            and str((child.get("child_page") or {}).get("title") or "") == title
        ]
        if len(matches) > 1:
            raise DestinationError(
                f"Expected exactly one direct child page titled {title!r} under the bound parent; "
                f"found {len(matches)}"
            )
        if not matches and not create_if_missing:
            raise DestinationError(
                f"Expected exactly one direct child page titled {title!r} under the bound parent; "
                "found 0"
            )

        expected = [_block_text(block) for block in blocks]
        if matches:
            page_id = str(matches[0].get("id") or "")
            action = "unchanged"
            existing = client.list_block_children(page_id)
            existing_tail = existing[-len(blocks):]
            if len(existing_tail) != len(blocks) or [
                _block_text(block) for block in existing_tail
            ] != expected:
                client.append_children(page_id, blocks)
                action = "appended"
        else:
            created = client.create_page(destination.parent_page_id, title, blocks)
            page_id = str(created.get("id") or "")
            action = "created"
        if not page_id:
            raise DestinationError(f"Notion child page {title!r} has no page id")

        read_back = client.list_block_children(page_id)
        tail = read_back[-len(blocks):]
        if len(tail) != len(blocks):
            raise DestinationError("Notion read-back returned fewer blocks than were appended")
        actual = [_block_text(block) for block in tail]
        if actual != expected:
            raise DestinationError("Notion read-back did not match the appended content")
        return {
            "project_slug": project_slug,
            "agent_id": agent_id,
            "parent_page_id": destination.parent_page_id,
            "page": {"id": page_id, "title": title},
            "action": action,
            "written_block_count": 0 if action == "unchanged" else len(blocks),
            "read_back_verified": True,
            "read_back_text": actual,
        }
