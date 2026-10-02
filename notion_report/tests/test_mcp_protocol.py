from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notion_report.mcp_server import _call_tool


class MCPProtocolTests(unittest.TestCase):
    def test_current_schema_tool_reloads_an_authorized_same_turn_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = root / ".agentui" / "report_schema.yaml"
            schema.parent.mkdir()
            source = """\
schema_version: {version}
name: demo-report
revision_title_pattern: '^Report v\\d{{4}}$'
agents: [BOSS]
main_page:
  owner: BOSS
  required_sections:
    - {{key: summary, title: Summary}}
subpages:
  - key: evidence
    title: Evidence
    owner: BOSS
    required_sections:
      - {{key: artifacts, title: Artifacts}}
"""
            schema.write_text(source.format(version=1), encoding="utf-8")
            scoped = {
                "AGENTUI_PROJECT_SLUG": "demo",
                "AGENTUI_AGENT_ID": "BOSS",
                "AGENTUI_PROJECT_ROOT": str(root),
                "AGENTUI_REPORT_SCHEMA_FILE": str(schema),
            }
            with patch.dict(os.environ, scoped, clear=False):
                first = _call_tool("get_current_report_schema", {})
                schema.write_text(source.format(version=2), encoding="utf-8")
                second = _call_tool("get_current_report_schema", {})

            self.assertEqual(first["schema_version"], 1)
            self.assertEqual(second["schema_version"], 2)
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertTrue(second["note_policy"]["schema_note"]["may_change_schema"])
            self.assertNotIn("path_abs", second)

    def test_stdio_handshake_and_destination_bound_tool_schemas(self):
        root = Path(__file__).resolve().parents[2]
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "notion_report_destination",
                    "arguments": {},
                },
            },
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        env = {
            **os.environ,
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "MODEL",
            "NOTION_REPORT_CONFIG": str(root / "notion_report" / "config.example.json"),
        }
        completed = subprocess.run(
            [sys.executable, str(root / "notion_report" / "run_mcp.py")],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "AgentUI Notion Report")
        tools = responses[1]["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "notion_report_destination",
                "list_notion_project_tree",
                "read_notion_project_page",
                "get_current_report_schema",
                "create_notion_report",
                "append_notion_child_page",
                "sync_notion_managed_report",
            },
        )
        for tool in tools:
            properties = tool["inputSchema"].get("properties", {})
            self.assertNotIn("parent_page_id", properties)
            self.assertNotIn("parent_url", properties)
            self.assertNotIn("project_slug", properties)
            self.assertNotIn("agent_id", properties)
        inherited = responses[2]["result"]["structuredContent"]
        self.assertEqual(inherited["project_slug"], "gelsight")
        self.assertEqual(inherited["agent_id"], "MODEL")
        self.assertEqual(inherited["destination_owner_agent_id"], "BOSS")
        self.assertTrue(inherited["inherited"])


if __name__ == "__main__":
    unittest.main()
