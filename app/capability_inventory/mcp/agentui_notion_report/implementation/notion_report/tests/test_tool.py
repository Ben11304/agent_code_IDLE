from __future__ import annotations

import tempfile
import unittest
import re
from pathlib import Path

from notion_report.config import (
    Destination,
    DestinationError,
    DestinationStore,
    SYSTEM_AGENT_ID,
    SYSTEM_PROJECT_SLUG,
)
from notion_report.tool import NotionReportTool, ReportSpec
from notion_report.schema import ReportSchemaError, load_report_schema


WORKSPACE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PARENT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _rich(text: str, *, bold: bool = False) -> list[dict]:
    item = {"type": "text", "text": {"content": text}}
    if bold:
        item["annotations"] = {"bold": True}
    return [item]


def _unescape_inline(text: str) -> str:
    text = text.replace("<br>", "\n")
    return re.sub(r"\\([\\*~`$\[\]<>{}|^])", r"\1", text)


def _table_cells(line: str) -> list[str]:
    return [
        _unescape_inline(cell.strip())
        for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])
    ]


def markdown_blocks(markdown: str) -> list[dict]:
    """Small parser for the writer's deterministic test fixture format."""
    lines = markdown.splitlines()
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if line.startswith("<callout "):
            index += 1
            content: list[str] = []
            while index < len(lines) and lines[index] != "</callout>":
                content.append(lines[index].removeprefix("\t"))
                index += 1
            text = _unescape_inline("\n".join(content))
            blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": _rich(text),
                    "icon": {"type": "emoji", "emoji": "📌"},
                },
            })
            index += 1
            continue
        if line.startswith("### "):
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": _rich(_unescape_inline(line[4:]))},
            })
            index += 1
            continue
        if line.startswith("## "):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _rich(_unescape_inline(line[3:]))},
            })
            index += 1
            continue
        if line == "```mermaid":
            index += 1
            source: list[str] = []
            while index < len(lines) and lines[index] != "```":
                source.append(lines[index])
                index += 1
            blocks.append({
                "object": "block", "type": "code",
                "code": {
                    "rich_text": _rich("\n".join(source)),
                    "language": "mermaid",
                    "caption": [],
                },
            })
            index += 1
            continue
        if line.startswith("| "):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].startswith("| "):
                table_lines.append(lines[index])
                index += 1
            data = [_table_cells(table_lines[0])]
            data.extend(_table_cells(row) for row in table_lines[2:])
            table_rows = [
                {
                    "object": "block", "type": "table_row",
                    "table_row": {"cells": [_rich(cell) for cell in row]},
                }
                for row in data
            ]
            blocks.append({
                "object": "block", "type": "table",
                "table": {
                    "table_width": len(data[0]),
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": table_rows,
                },
            })
            continue
        if line.startswith("- "):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": _rich(_unescape_inline(line[2:]))
                },
            })
            index += 1
            continue
        bold = line.startswith("**") and line.endswith("**")
        text = line[2:-2] if bold else line
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(_unescape_inline(text), bold=bold)},
        })
        index += 1
    return blocks


def page_payload(page_id: str, title: str) -> dict:
    return {
        "id": page_id,
        "url": f"https://www.notion.so/{page_id.replace('-', '')}",
        "properties": {
            "title": {
                "type": "title",
                "title": [{"type": "text", "plain_text": title, "text": {"content": title}}],
            }
        },
    }


class FakeClient:
    def __init__(self, workspace_id: str = WORKSPACE_ID):
        self.workspace_id = workspace_id
        self.created: list[dict] = []
        self.appended: list[dict] = []
        self.archived: list[str] = []
        self.markdown_by_page: dict[str, str] = {}
        self.child_blocks: dict[str, list[dict]] = {
            PARENT_ID: [{
                "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "type": "child_page",
                "child_page": {"title": "propose"},
            }],
        }

    def get_self(self) -> dict:
        return {
            "type": "bot",
            "bot": {
                "workspace_id": self.workspace_id,
                "workspace_name": "Research Lab",
            },
        }

    def retrieve_page(self, page_id: str) -> dict:
        if page_id == PARENT_ID:
            return page_payload(page_id, "Agent Reports")
        return page_payload(page_id, "Child")

    def create_page(self, parent_page_id: str, title: str, children: list[dict]) -> dict:
        if not parent_page_id:
            raise AssertionError("workspace-root creation must never occur")
        page_id = f"00000000-0000-0000-0000-{len(self.created) + 1:012d}"
        self.created.append(
            {"parent_page_id": parent_page_id, "title": title, "children": children, "id": page_id}
        )
        self.child_blocks.setdefault(parent_page_id, []).append({
            "id": page_id,
            "type": "child_page",
            "child_page": {"title": title},
        })
        materialized = []
        for index, block in enumerate(children):
            item = dict(block)
            kind = str(item.get("type") or "")
            payload = dict(item.get(kind) or {})
            nested = list(payload.pop("children", []) or [])
            if nested:
                block_id = f"99999999-9999-9999-9999-{len(self.created):06d}{index:06d}"
                item["id"] = block_id
                item["has_children"] = True
                item[kind] = payload
                self.child_blocks[block_id] = nested
            materialized.append(item)
        self.child_blocks[page_id] = materialized
        return page_payload(page_id, title)

    def create_page_markdown(
        self, parent_page_id: str, title: str, markdown: str
    ) -> dict:
        page = self.create_page(
            parent_page_id,
            title,
            markdown_blocks(markdown),
        )
        self.created[-1]["markdown"] = markdown
        self.markdown_by_page[page["id"]] = markdown
        return page

    def retrieve_page_markdown(self, page_id: str) -> dict:
        return {
            "object": "page_markdown",
            "id": page_id,
            "markdown": self.markdown_by_page.get(page_id, ""),
            "truncated": False,
            "unknown_block_ids": [],
        }

    def list_block_children(self, block_id: str) -> list[dict]:
        return list(self.child_blocks.get(block_id, []))

    def append_children(self, block_id: str, children: list[dict]) -> None:
        copied = []
        for index, block in enumerate(children):
            item = dict(block)
            item.setdefault("id", f"eeeeeeee-eeee-eeee-eeee-{len(self.appended):06d}{index:06d}")
            copied.append(item)
        self.appended.append({"block_id": block_id, "children": copied})
        self.child_blocks.setdefault(block_id, []).extend(copied)

    def archive_block(self, block_id: str) -> dict:
        self.archived.append(block_id)
        for parent_id, blocks in self.child_blocks.items():
            self.child_blocks[parent_id] = [block for block in blocks if block.get("id") != block_id]
        return {"id": block_id, "archived": True}


class NotionReportToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DestinationStore(Path(self.tmp.name) / "destinations.json")
        self.client = FakeClient()
        self.tool = NotionReportTool(
            self.store,
            environ={"NOTION_RESEARCH_TOKEN": "secret-test-value"},
            client_factory=lambda _token: self.client,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_bind_verifies_and_locks_workspace(self):
        status = self.tool.bind_destination(
            "gelsight",
            "BOSS",
            f"https://www.notion.so/Agent-Reports-{PARENT_ID.replace('-', '')}",
            "NOTION_RESEARCH_TOKEN",
        )
        self.assertTrue(status["verified"])
        self.assertEqual(status["workspace_id"], WORKSPACE_ID)
        self.assertEqual(status["parent_page_id"], PARENT_ID)

    def test_create_uses_bound_parent_and_nests_subpages_under_main(self):
        self.tool.bind_destination(
            "gelsight",
            "BOSS",
            PARENT_ID,
            "NOTION_RESEARCH_TOKEN",
        )
        report = ReportSpec.from_dict(
            {
                "title": "Project report",
                "summary": "Verified snapshot",
                "sections": [{"heading": "Status", "bullets": ["Ready"]}],
                "subpages": [{
                    "title": "Evidence",
                    "sections": [{"heading": "Artifacts", "content": "run.json"}],
                }],
            }
        )
        result = self.tool.create_report("gelsight", "BOSS", report)
        self.assertEqual(self.client.created[0]["parent_page_id"], PARENT_ID)
        self.assertEqual(self.client.created[1]["parent_page_id"], self.client.created[0]["id"])
        self.assertEqual(result["parent_page_id"], PARENT_ID)
        self.assertEqual(len(result["created_subpages"]), 1)
        self.assertEqual(result["writer"], "notion_enhanced_markdown")
        self.assertIn("## Status", self.client.created[0]["markdown"])
        self.assertTrue(result["read_back_verified"])
        self.assertTrue(result["created_subpages"][0]["read_back_verified"])

    def test_section_content_is_serialized_instead_of_silently_dropped(self):
        self.tool.bind_destination(
            "gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        report = ReportSpec.from_dict({
            "title": "Report v0002",
            "sections": [{"heading": "Status", "content": "Full report body"}],
            "subpages": [{
                "title": "DATA",
                "sections": [{
                    "heading": "Cohort",
                    "content": ["1,399 trials", "20-fold LOSO"],
                }],
            }],
        })

        result = self.tool.create_report("gelsight", "BOSS", report)

        self.assertTrue(result["read_back_verified"])
        self.assertEqual(
            [_block.get(_block["type"], {}).get("rich_text", [{}])[0]
             .get("text", {}).get("content", "")
             for _block in self.client.created[0]["children"]],
            ["Status", "Full report body"],
        )
        self.assertEqual(result["created_subpages"][0]["block_count"], 3)

    def test_native_table_is_created_and_rows_are_read_back(self):
        self.tool.bind_destination(
            "gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        report = ReportSpec.from_dict({
            "title": "Report v0003",
            "sections": [{
                "heading": "Results",
                "tables": [{
                    "key": "experiment_results",
                    "caption": "Verified experiments",
                    "columns": ["Arm", "Accuracy", "Boundary"],
                    "rows": [
                        ["CONTROL", "73.8267%", "validation-only"],
                        ["QH_FORCE", "73.8094%", "gate FAIL"],
                    ],
                }],
            }],
        })

        result = self.tool.create_report("gelsight", "BOSS", report)

        self.assertTrue(result["read_back_verified"])
        table = next(
            block for block in self.client.created[0]["children"]
            if block["type"] == "table"
        )
        self.assertEqual(table["table"]["table_width"], 3)
        self.assertTrue(table["table"]["has_column_header"])
        self.assertEqual(len(table["table"]["children"]), 3)
        self.assertEqual(result["main_block_count"], 6)

    def test_native_table_rejects_non_rectangular_rows(self):
        with self.assertRaisesRegex(ValueError, "expected 2"):
            ReportSpec.from_dict({
                "title": "Broken table",
                "sections": [{
                    "heading": "Results",
                    "tables": [{
                        "key": "results",
                        "columns": ["Arm", "Accuracy"],
                        "rows": [["CONTROL"]],
                    }],
                }],
            })

    def test_mermaid_diagram_uses_markdown_api_and_is_read_back_as_code(self):
        self.tool.bind_destination(
            "gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        report = ReportSpec.from_dict({
            "title": "Report v0004",
            "sections": [{
                "heading": "Changes",
                "highlights": ["Added a verified architecture diagram"],
                "diagrams": [{
                    "key": "model_architecture",
                    "format": "mermaid",
                    "caption": "Recorded architecture",
                    "source": "flowchart LR\nA[12ch inputs] --> B[876D descriptor]\nB -. unknown .-> C[heads]",
                }],
            }],
        })

        result = self.tool.create_report("gelsight", "BOSS", report)

        self.assertTrue(result["read_back_verified"])
        self.assertEqual(result["writer"], "notion_enhanced_markdown")
        markdown = self.client.created[0]["markdown"]
        self.assertIn("**Added a verified architecture diagram**", markdown)
        self.assertIn("```mermaid", markdown)
        code = next(
            block for block in self.client.created[0]["children"]
            if block["type"] == "code"
        )
        self.assertEqual(code["code"]["language"], "mermaid")

    def test_diagram_rejects_non_mermaid_format(self):
        with self.assertRaisesRegex(ValueError, "must be 'mermaid'"):
            ReportSpec.from_dict({
                "title": "Broken diagram",
                "sections": [{
                    "heading": "Architecture",
                    "diagrams": [{
                        "key": "architecture",
                        "format": "plantuml",
                        "source": "A -> B",
                    }],
                }],
            })

    def test_unknown_section_fields_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported section field"):
            ReportSpec.from_dict({
                "title": "Broken",
                "sections": [{"heading": "Status", "body_typo": "lost"}],
            })

    def test_schema_validation_happens_before_notion_write(self):
        schema_root = Path(self.tmp.name) / "project"
        (schema_root / ".agentui").mkdir(parents=True)
        (schema_root / ".agentui" / "report_schema.yaml").write_text(
            """schema_version: 1
name: strict
revision_title_pattern: '^Report v\\d{4}$'
agents: [BOSS]
main_page:
  owner: BOSS
  required_sections:
    - {key: summary, title: Summary}
subpages:
  - key: evidence
    title: Evidence
    owner: BOSS
    required_sections:
      - {key: artifacts, title: Artifacts}
""",
            encoding="utf-8",
        )
        loaded = load_report_schema(schema_root, {
            "enabled": True, "schema_file": ".agentui/report_schema.yaml",
        })
        strict_tool = NotionReportTool(
            self.store,
            environ={"NOTION_RESEARCH_TOKEN": "secret-test-value"},
            client_factory=lambda _token: self.client,
            report_schema=loaded,
        )
        with self.assertRaisesRegex(ReportSchemaError, "missing required section"):
            strict_tool.create_report("gelsight", "BOSS", {
                "title": "Report v0002",
                "sections": [],
                "subpages": [],
            })
        self.assertEqual(self.client.created, [])

    def test_workspace_change_is_blocked_before_write(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        self.client.workspace_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        with self.assertRaisesRegex(DestinationError, "Workspace mismatch"):
            self.tool.create_report("gelsight", "BOSS", {
                "title": "Must not exist",
                "sections": [{"heading": "Status", "content": "Unsafe"}],
            })
        self.assertEqual(self.client.created, [])

    def test_dry_run_does_not_require_token_or_write(self):
        self.store.upsert(
            Destination(
                project_slug="gelsight",
                agent_id="BOSS",
                parent_url=PARENT_ID,
                parent_page_id=PARENT_ID,
                token_env="NOTION_MISSING_TOKEN",
                workspace_id=WORKSPACE_ID,
            )
        )
        result = self.tool.create_report(
            "gelsight", "BOSS", {
                "title": "Preview",
                "sections": [{"heading": "Status", "content": "Ready"}],
                "subpages": [{
                    "title": "A",
                    "sections": [{"heading": "Evidence", "content": "run.json"}],
                }],
            },
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["subpages"], ["A"])
        self.assertEqual(self.client.created, [])

    def test_append_requires_one_exact_direct_child_and_reads_back(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        result = self.tool.append_to_child_page(
            "gelsight", "BOSS", "propose",
            summary="Verified Route B",
            sections=[{"heading": "Decision", "bullets": ["Use K48_QH"]}],
        )
        self.assertEqual(
            self.client.appended[0]["block_id"],
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
        )
        self.assertTrue(result["read_back_verified"])
        self.assertEqual(result["read_back_text"], ["Verified Route B", "Decision", "Use K48_QH"])

    def test_append_fails_closed_without_exact_title(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        with self.assertRaisesRegex(DestinationError, "found 0"):
            self.tool.append_to_child_page(
                "gelsight", "BOSS", "Propose", summary="Must not append")
        self.assertEqual(self.client.appended, [])

    def test_authorized_create_is_read_back_and_retry_is_idempotent(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        self.client.child_blocks[PARENT_ID] = []
        first = self.tool.append_to_child_page(
            "gelsight", "BOSS", "propose",
            summary="Verified Route B",
            create_if_missing=True,
        )
        self.assertEqual(first["action"], "created")
        self.assertTrue(first["read_back_verified"])

        second = self.tool.append_to_child_page(
            "gelsight", "BOSS", "propose",
            summary="Verified Route B",
            create_if_missing=True,
        )
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(second["written_block_count"], 0)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.client.appended, [])

    def test_inventory_and_read_are_confined_to_project_subtree(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        propose_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        self.client.child_blocks[propose_id] = [{
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "Current report"}]},
        }]
        inventory = self.tool.inventory_project_tree("gelsight", "MODEL")
        self.assertEqual(inventory["page_count"], 2)
        self.assertEqual(inventory["scope"], "bound_project_subtree")
        report = self.tool.read_project_page("gelsight", "MODEL", propose_id)
        self.assertEqual(report["blocks"][0]["text"], "Current report")
        self.assertEqual(len(report["content_sha256"]), 64)
        with self.assertRaisesRegex(DestinationError, "outside"):
            self.tool.read_project_page(
                "gelsight", "MODEL", "ffffffff-ffff-ffff-ffff-ffffffffffff")

    def test_managed_report_dry_run_replace_and_read_back(self):
        self.tool.bind_destination("gelsight", "BOSS", PARENT_ID, "NOTION_RESEARCH_TOKEN")
        propose_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        self.client.child_blocks[propose_id] = [
            {"id": "10000000-0000-0000-0000-000000000001", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "Manual note"}]}},
            {"id": "10000000-0000-0000-0000-000000000002", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "AGENTUI_REPORT_START key=status source=old"}]}},
            {"id": "10000000-0000-0000-0000-000000000003", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "Old report"}]}},
            {"id": "10000000-0000-0000-0000-000000000004", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "AGENTUI_REPORT_END key=status"}]}},
        ]
        preview = self.tool.sync_managed_report(
            "gelsight", "BOSS", "propose", "status", "manifest@2",
            summary="New report", dry_run=True)
        self.assertEqual(preview["action"], "replace_managed_section")
        self.assertEqual(self.client.archived, [])
        result = self.tool.sync_managed_report(
            "gelsight", "BOSS", "propose", "status", "manifest@2",
            summary="New report", dry_run=False)
        self.assertTrue(result["read_back_verified"])
        self.assertEqual(len(self.client.archived), 3)
        remaining = [_block.get("paragraph", {}).get("rich_text", [{}])[0].get("plain_text")
                     for _block in self.client.child_blocks[propose_id]
                     if _block.get("type") == "paragraph"]
        self.assertIn("Manual note", remaining)

    def test_system_hub_auto_provisions_once_under_canonical_root(self):
        self.store.upsert(Destination(
            project_slug=SYSTEM_PROJECT_SLUG,
            agent_id=SYSTEM_AGENT_ID,
            parent_url=PARENT_ID,
            parent_page_id=PARENT_ID,
            token_env="NOTION_RESEARCH_TOKEN",
            workspace_id=WORKSPACE_ID,
            workspace_name="Research Lab",
            parent_title="AgentUI System Hub",
        ))
        self.client.child_blocks[PARENT_ID] = []
        tool = NotionReportTool(
            self.store,
            environ={
                "NOTION_RESEARCH_TOKEN": "secret-test-value",
                "AGENTUI_PROJECT_NAME": "New Research Project",
                "AGENTUI_PROJECT_ROOT_AGENT_ID": "BOSS",
            },
            client_factory=lambda _token: self.client,
        )

        first = tool.inventory_project_tree("new-project", "WORKER")
        second = tool.inventory_project_tree("new-project", "WORKER")

        self.assertEqual(first["page_count"], 1)
        self.assertEqual(second["root_page_id"], first["root_page_id"])
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.client.created[0]["parent_page_id"], PARENT_ID)
        self.assertEqual(self.client.created[0]["title"], "New Research Project [new-project]")
        destination = self.store.resolve("new-project", "WORKER")
        self.assertEqual(destination.agent_id, "BOSS")
        self.assertEqual(destination.parent_page_id, first["root_page_id"])

    def test_dry_run_does_not_auto_provision_project(self):
        self.store.upsert(Destination(
            project_slug=SYSTEM_PROJECT_SLUG,
            agent_id=SYSTEM_AGENT_ID,
            parent_url=PARENT_ID,
            parent_page_id=PARENT_ID,
            token_env="NOTION_RESEARCH_TOKEN",
            workspace_id=WORKSPACE_ID,
        ))
        tool = NotionReportTool(
            self.store,
            environ={
                "NOTION_RESEARCH_TOKEN": "secret-test-value",
                "AGENTUI_PROJECT_NAME": "Dry Run Project",
                "AGENTUI_PROJECT_ROOT_AGENT_ID": "BOSS",
            },
            client_factory=lambda _token: self.client,
        )
        with self.assertRaisesRegex(DestinationError, "No Notion destination bound"):
            tool.create_report(
                "dry-run-project", "BOSS", {"title": "Preview"}, dry_run=True)
        self.assertEqual(self.client.created, [])


if __name__ == "__main__":
    unittest.main()
