from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import db


class EscalationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._old_path
        self._tmp.cleanup()

    def test_open_deduplicate_and_explicit_resolution(self) -> None:
        first = db.open_escalation(
            "demo", "MODEL", "HUMAN", "user", "choose seed count", "manifest#Gate")
        duplicate = db.open_escalation(
            "demo", "MODEL", "HUMAN", "user", "choose seed count", "manifest#Gate")
        self.assertEqual(first["escalation_id"], duplicate["escalation_id"])
        self.assertEqual(1, len(db.get_open_escalations("demo", "MODEL")))

        resolved, rejected = db.resolve_escalations(
            "demo", [first["escalation_id"], "esc-does-not-exist"],
            "BOSS", "user chose five seeds", "session-2")
        self.assertEqual([first["escalation_id"]], resolved)
        self.assertEqual(["esc-does-not-exist"], rejected)
        self.assertEqual([], db.get_open_escalations("demo", "MODEL"))

    def test_same_reason_can_reopen_after_resolution(self) -> None:
        first = db.open_escalation("demo", "BOSS", "HUMAN", "user", "authenticate")
        db.resolve_escalations("demo", [first["escalation_id"]], "BOSS")
        reopened = db.open_escalation("demo", "BOSS", "HUMAN", "user", "authenticate")
        self.assertNotEqual(first["escalation_id"], reopened["escalation_id"])


if __name__ == "__main__":
    unittest.main()
