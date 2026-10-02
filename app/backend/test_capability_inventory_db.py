from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import db


class CapabilityInventoryDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_path = db.DB_PATH
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "inventory.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_global_delete_purges_policy_and_finds_codex_runtime(self) -> None:
        db.set_agent_capability_policy("demo", "worker", {
            "skills": {"/idle/sample/SKILL.md": True},
            "mcp_servers": {"docs": {"enabled": False}},
        })
        session = db.get_or_create_active_session("demo", "worker")
        with db._conn() as connection:
            connection.execute(
                "UPDATE sessions SET claude_session_id=?, cli_adapter=?, updated_at=? WHERE id=?",
                ("codex-thread", "codex", time.time(), session["id"]),
            )

        changed = db.purge_deleted_capability_policy(
            kind="skill", skill_paths=["/idle/sample/SKILL.md"]
        )
        policy = db.get_agent_capability_policy("demo", "worker")
        runtimes = db.list_latest_codex_runtimes()

        self.assertEqual(changed, 1)
        self.assertEqual(policy["skills"], {})
        self.assertEqual(runtimes[0]["claude_session_id"], "codex-thread")

        changed = db.purge_deleted_capability_policy(
            kind="mcp_server", mcp_server="docs"
        )
        self.assertEqual(changed, 1)
        self.assertEqual(
            db.get_agent_capability_policy("demo", "worker")["mcp_servers"], {}
        )

    def test_skill_usage_counts_once_per_turn_and_starts_without_backfill(self) -> None:
        self.assertIsNotNone(db.skill_usage_tracking_started_at())
        self.assertEqual(db.get_agent_skill_usage("demo", "worker"), {})

        first = db.record_agent_skill_use(
            "demo",
            "worker",
            skill_path="/idle/figure/SKILL.md",
            skill_name="nice-figures",
            provider_thread_id="thread-1",
            turn_id="turn-1",
            source="explicit_input",
        )
        duplicate = db.record_agent_skill_use(
            "demo",
            "worker",
            skill_path="/idle/figure/SKILL.md",
            skill_name="nice-figures",
            provider_thread_id="thread-1",
            turn_id="turn-1",
            source="skill_file_read",
        )
        second = db.record_agent_skill_use(
            "demo",
            "worker",
            skill_path="/idle/figure/SKILL.md",
            skill_name="nice-figures",
            provider_thread_id="thread-1",
            turn_id="turn-2",
            source="skill_file_read",
        )

        self.assertTrue(first["recorded"])
        self.assertFalse(duplicate["recorded"])
        self.assertTrue(second["recorded"])
        self.assertEqual(second["usage_count"], 2)
        self.assertEqual(
            db.get_agent_skill_usage("demo", "worker")
            ["/idle/figure/SKILL.md"]["usage_count"],
            2,
        )
        self.assertEqual(
            db.purge_deleted_skill_usage(["/idle/figure/SKILL.md"]), 2
        )
        self.assertEqual(db.get_agent_skill_usage("demo", "worker"), {})


if __name__ == "__main__":
    unittest.main()
