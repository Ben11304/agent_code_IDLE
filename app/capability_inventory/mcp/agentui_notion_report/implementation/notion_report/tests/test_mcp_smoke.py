from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path


try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # Core tests intentionally run without the optional MCP SDK.
    ClientSession = None


@unittest.skipIf(ClientSession is None, "optional MCP SDK is not installed")
class MCPServerSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_exposes_only_destination_bound_tools(self):
        root = Path(__file__).resolve().parents[2]
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(root / "notion_report" / "run_mcp.py")],
            env={
                "AGENTUI_PROJECT_SLUG": "gelsight",
                "AGENTUI_AGENT_ID": "BOSS",
                "NOTION_REPORT_CONFIG": str(root / "notion_report" / "config.example.json"),
            },
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=5),
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        self.assertEqual(names, {
            "notion_report_destination",
            "list_notion_project_tree",
            "read_notion_project_page",
            "get_current_report_schema",
            "create_notion_report",
            "append_notion_child_page",
            "sync_notion_managed_report",
        })
        for tool in tools.tools:
            properties = (tool.inputSchema or {}).get("properties", {})
            self.assertNotIn("parent_page_id", properties)
            self.assertNotIn("parent_url", properties)
            self.assertNotIn("project_slug", properties)
            self.assertNotIn("agent_id", properties)


if __name__ == "__main__":
    unittest.main()
