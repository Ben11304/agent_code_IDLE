from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from notion_report.config import Destination, DestinationError, DestinationStore, extract_page_id


class PageIdTests(unittest.TestCase):
    def test_extracts_standard_and_app_links(self):
        expected = "3acac7da-1fcb-8145-b078-c6f476c32425"
        self.assertEqual(
            extract_page_id("https://www.notion.so/GelSight-3acac7da1fcb8145b078c6f476c32425?pvs=4"),
            expected,
        )
        self.assertEqual(
            extract_page_id("https://app.notion.com/p/3acac7da1fcb8145b078c6f476c32425"),
            expected,
        )

    def test_rejects_workspace_root_and_foreign_host(self):
        with self.assertRaises(DestinationError):
            extract_page_id("https://www.notion.so/")
        with self.assertRaises(DestinationError):
            extract_page_id("https://example.com/3acac7da1fcb8145b078c6f476c32425")


class DestinationStoreTests(unittest.TestCase):
    def test_hyphenated_agent_id_is_supported(self):
        destination = Destination(
            project_slug="openconstruction-meta",
            agent_id="OC-META",
            parent_url="https://www.notion.so/parent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            parent_page_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            token_env="NOTION_REPORT_TOKEN",
            workspace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.assertEqual(destination.agent_id, "OC-META")

    def test_round_trip_does_not_store_token_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "destinations.json"
            store = DestinationStore(path)
            destination = Destination(
                project_slug="gelsight",
                agent_id="BOSS",
                parent_url="https://www.notion.so/parent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                parent_page_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                token_env="NOTION_GELSIGHT_TOKEN",
                workspace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )
            store.upsert(destination)
            self.assertEqual(store.get("gelsight", "BOSS"), destination)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["destinations"]["gelsight:BOSS"]["token_env"], "NOTION_GELSIGHT_TOKEN")
            self.assertNotIn("secret_", path.read_text(encoding="utf-8"))

    def test_worker_inherits_the_projects_single_root_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DestinationStore(Path(tmp) / "destinations.json")
            root = Destination(
                project_slug="gelsight",
                agent_id="BOSS",
                parent_url="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                parent_page_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                token_env="NOTION_REPORT_TOKEN",
                workspace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )
            store.upsert(root)
            self.assertEqual(store.resolve("gelsight", "MODEL"), root)

    def test_worker_never_guesses_between_multiple_project_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DestinationStore(Path(tmp) / "destinations.json")
            for agent_id, parent in (
                ("BOSS", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                ("ALT", "cccccccccccccccccccccccccccccccc"),
            ):
                store.upsert(Destination(
                    project_slug="gelsight",
                    agent_id=agent_id,
                    parent_url=parent,
                    parent_page_id=parent,
                    token_env="NOTION_REPORT_TOKEN",
                    workspace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                ))
            with self.assertRaisesRegex(DestinationError, "Ambiguous"):
                store.resolve("gelsight", "MODEL")


if __name__ == "__main__":
    unittest.main()
