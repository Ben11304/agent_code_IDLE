from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notion_report.config import (
    Destination,
    DestinationStore,
    SYSTEM_AGENT_ID,
    SYSTEM_PROJECT_SLUG,
)
from notion_report.run_mcp import _apply_cli_scope, _load_scoped_destination_token


PARENT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKSPACE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class ScopedTokenFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_path = root / "destinations.json"
        self.env_path = root / ".env.local"
        DestinationStore(self.config_path).upsert(Destination(
            project_slug="gelsight",
            agent_id="BOSS",
            parent_url=PARENT_ID,
            parent_page_id=PARENT_ID,
            token_env="NOTION_REPORT_TOKEN",
            workspace_id=WORKSPACE_ID,
        ))
        self.env_path.write_text(
            "UNRELATED_SECRET=must-not-load\nNOTION_REPORT_TOKEN='existing-token'\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_scoped_bound_agent_loads_only_selected_token(self) -> None:
        env = {
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": str(self.config_path),
        }
        self.assertTrue(_load_scoped_destination_token(env, self.env_path))
        self.assertEqual(env["NOTION_REPORT_TOKEN"], "existing-token")
        self.assertNotIn("UNRELATED_SECRET", env)

    def test_unscoped_launch_receives_no_token(self) -> None:
        env = {"NOTION_REPORT_CONFIG": str(self.config_path)}
        self.assertFalse(_load_scoped_destination_token(env, self.env_path))
        self.assertNotIn("NOTION_REPORT_TOKEN", env)

    def test_project_worker_inherits_the_bound_token(self) -> None:
        env = {
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "MODEL",
            "NOTION_REPORT_CONFIG": str(self.config_path),
        }
        self.assertTrue(_load_scoped_destination_token(env, self.env_path))
        self.assertEqual(env["NOTION_REPORT_TOKEN"], "existing-token")

    def test_cli_scope_overrides_stale_resumed_environment(self) -> None:
        env = {
            "AGENTUI_PROJECT_SLUG": "stale-project",
            "AGENTUI_AGENT_ID": "STALE_AGENT",
            "NOTION_REPORT_CONFIG": "/tmp/stale.json",
        }
        _apply_cli_scope([
            "--project", "gelsight",
            "--agent", "BOSS",
            "--config", str(self.config_path),
            "--project-name", "GelSight Research",
            "--project-root-agent", "BOSS",
        ], env)
        self.assertEqual(env["AGENTUI_PROJECT_SLUG"], "gelsight")
        self.assertEqual(env["AGENTUI_AGENT_ID"], "BOSS")
        self.assertEqual(env["NOTION_REPORT_CONFIG"], str(self.config_path))
        self.assertEqual(env["AGENTUI_PROJECT_NAME"], "GelSight Research")
        self.assertEqual(env["AGENTUI_PROJECT_ROOT_AGENT_ID"], "BOSS")

    def test_unbound_project_loads_only_system_hub_token(self) -> None:
        hub_path = Path(self.tmp.name) / "hub-destinations.json"
        DestinationStore(hub_path).upsert(Destination(
            project_slug=SYSTEM_PROJECT_SLUG,
            agent_id=SYSTEM_AGENT_ID,
            parent_url=PARENT_ID,
            parent_page_id=PARENT_ID,
            token_env="NOTION_REPORT_TOKEN",
            workspace_id=WORKSPACE_ID,
        ))
        env = {
            "AGENTUI_PROJECT_SLUG": "new-project",
            "AGENTUI_AGENT_ID": "WORKER",
            "NOTION_REPORT_CONFIG": str(hub_path),
        }
        self.assertTrue(_load_scoped_destination_token(env, self.env_path))
        self.assertEqual(env["NOTION_REPORT_TOKEN"], "existing-token")
        self.assertNotIn("UNRELATED_SECRET", env)


if __name__ == "__main__":
    unittest.main()
