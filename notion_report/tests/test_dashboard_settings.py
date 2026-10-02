from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.backend.notion_settings import bind_notion_settings, get_notion_settings
from notion_report.config import Destination, DestinationError, extract_page_id


PARENT_URL = "https://www.notion.so/Reports-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class FakeBindingTool:
    def __init__(self, store, *, environ):
        self.store = store
        self.environ = environ

    def bind_destination(self, project_slug, agent_id, parent_url, token_env):
        page_id = extract_page_id(parent_url)
        self.store.upsert(Destination(
            project_slug=project_slug,
            agent_id=agent_id,
            parent_url=parent_url,
            parent_page_id=page_id,
            token_env=token_env,
            workspace_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            parent_title="Reports",
            workspace_name="Research",
        ))
        return {
            "parent_url": parent_url,
            "parent_title": "Reports",
            "workspace_name": "Research",
            "verified": True,
        }


class DashboardNotionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "destinations.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_unbound_worker_gets_empty_placeholder(self):
        result = get_notion_settings(
            self.path,
            "gelsight",
            "RESEARCHER",
            environ={},
            editable=False,
        )
        self.assertFalse(result["editable"])
        self.assertEqual(result["notion_url"], "")

    def test_worker_inherits_project_binding_but_cannot_rebind(self):
        bind_notion_settings(
            self.path,
            "gelsight",
            "BOSS",
            PARENT_URL,
            environ={"NOTION_REPORT_TOKEN": "secret-value"},
            editable=True,
            tool_factory=FakeBindingTool,
        )
        result = get_notion_settings(
            self.path,
            "gelsight",
            "MODEL",
            environ={"NOTION_REPORT_TOKEN": "secret-value"},
            editable=False,
        )
        self.assertFalse(result["editable"])
        self.assertTrue(result["configured"])
        self.assertTrue(result["inherited"])
        self.assertEqual(result["destination_owner_agent_id"], "BOSS")
        self.assertEqual(result["notion_url"], PARENT_URL)

    def test_boss_requires_server_side_token(self):
        with self.assertRaisesRegex(DestinationError, "NOTION_GELSIGHT_TOKEN"):
            bind_notion_settings(self.path, "gelsight", "BOSS", PARENT_URL, environ={})

    def test_boss_binding_infers_project_token_without_exposing_it(self):
        result = bind_notion_settings(
            self.path,
            "gelsight",
            "BOSS",
            PARENT_URL,
            environ={"NOTION_GELSIGHT_TOKEN": "secret-value"},
            tool_factory=FakeBindingTool,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["token_env"], "NOTION_GELSIGHT_TOKEN")
        self.assertNotIn("secret-value", str(result))

    def test_non_boss_cannot_bind(self):
        with self.assertRaisesRegex(DestinationError, "root/BOSS"):
            bind_notion_settings(
                self.path,
                "gelsight",
                "RESEARCHER",
                PARENT_URL,
                environ={"NOTION_GELSIGHT_TOKEN": "secret-value"},
                editable=False,
            )

    def test_hyphenated_root_agent_is_editable_and_uses_shared_token(self):
        status = get_notion_settings(
            self.path,
            "openconstruction-meta",
            "OC-META",
            environ={"NOTION_REPORT_TOKEN": "secret-value"},
            editable=True,
        )
        self.assertTrue(status["editable"])
        self.assertTrue(status["token_available"])
        self.assertEqual(status["token_env"], "NOTION_REPORT_TOKEN")

        result = bind_notion_settings(
            self.path,
            "openconstruction-meta",
            "OC-META",
            PARENT_URL,
            environ={"NOTION_REPORT_TOKEN": "secret-value"},
            editable=True,
            tool_factory=FakeBindingTool,
        )
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
