from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# main.py initializes its database at import time. Point it at an isolated DB
# before importing the module so regression tests never touch AgentUI's live DB.
from . import db

_DB_TMP = tempfile.TemporaryDirectory()
db.DB_PATH = Path(_DB_TMP.name) / "receipt-test.db"

from . import main  # noqa: E402


def _write_owner(root: Path) -> None:
    (root / "state").mkdir(parents=True)
    (root / "outputs").mkdir(parents=True)
    (root / "state" / "progress.md").write_text(
        "# Progress\n\n## 2026-08-10\n- initial\n", encoding="utf-8")
    (root / "outputs" / "manifest.md").write_text(
        "# Manifest\n\n## Current Contract\nInitial technical truth.\n", encoding="utf-8")
    (root / "overview.md").write_text(
        "<!-- OVERVIEW:HEADER -->\nbody_incomplete: false\n<!-- /OVERVIEW:HEADER -->\n\n"
        "<!-- OVERVIEW:BODY -->\n## Current State\nThe current technical contract is available and ready for parent routing.\n"
        "<!-- /OVERVIEW:BODY -->\n\n<!-- OVERVIEW:FOOTER -->\n"
        "open_escalation: none\n<!-- /OVERVIEW:FOOTER -->\n",
        encoding="utf-8")


class MemoryReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.owner = Path(self.tmp.name) / "MODEL"
        _write_owner(self.owner)
        self.project = {
            "slug": "receipt-demo",
            "root": self.tmp.name,
            "memory": {"reconciliation": {"enabled": True}},
        }
        self.agent = {"id": "MODEL"}
        self.events: list[dict] = []

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _run(self, receipt: str, mutate=None, final_text="short result") -> dict:
        baseline = main._memory_snapshot(self.owner)

        def stream_fn(**_kwargs):
            async def stream():
                if mutate:
                    mutate()
                yield {"type": "delta", "text": receipt}
            return stream()

        async def emit(event: dict) -> None:
            self.events.append(event)

        return await main._run_memory_reconciliation(
            project=self.project, agent=self.agent, agent_dir=self.owner,
            final_text=final_text, turn_before=baseline, stream_fn=stream_fn,
            model="fake", override={}, effort=None, resume_session_id=None,
            cwd=str(self.owner), runtime_env={}, emit=emit, session_id="test-session")

    async def test_verified_updates_match_hashes_and_real_refs(self) -> None:
        def mutate() -> None:
            (self.owner / "state" / "progress.md").write_text(
                "# Progress\n\n## 2026-08-11 Verified delta\n- changed\n", encoding="utf-8")
            (self.owner / "outputs" / "manifest.md").write_text(
                "# Manifest\n\n## Current Contract\nUpdated technical truth with evidence.\n",
                encoding="utf-8")
            (self.owner / "overview.md").write_text(
                "<!-- OVERVIEW:HEADER -->\nbody_incomplete: false\n<!-- /OVERVIEW:HEADER -->\n\n"
                "<!-- OVERVIEW:BODY -->\n## Current State\nUpdated technical truth is verified and ready for parent routing.\n"
                "<!-- /OVERVIEW:BODY -->\n\n<!-- OVERVIEW:FOOTER -->\n"
                "open_escalation: none\n<!-- /OVERVIEW:FOOTER -->\n",
                encoding="utf-8")

        receipt = """[MEMORY_RECONCILED]
status: ok
durable_delta: technical
progress: updated
progress_ref: 2026-08-11 Verified delta
manifest: updated
manifest_ref: Current Contract
overview: updated
overview_manifest_refs: Current Contract
overview_reason: parent readiness changed
resolved_escalations: none
[/MEMORY_RECONCILED]"""
        result = await self._run(receipt, mutate)
        self.assertTrue(result["ok"], result["error"])

    async def test_declared_unchanged_cannot_hide_hash_change(self) -> None:
        def mutate() -> None:
            (self.owner / "outputs" / "manifest.md").write_text(
                "# Manifest\n\n## Current Contract\nSilently changed.\n", encoding="utf-8")

        receipt = """[MEMORY_RECONCILED]
status: ok
durable_delta: technical
progress: unchanged
progress_ref: none
manifest: unchanged
manifest_ref: Current Contract
overview: unchanged
overview_manifest_refs: none
overview_reason: routing unchanged
resolved_escalations: none
[/MEMORY_RECONCILED]"""
        result = await self._run(receipt, mutate)
        self.assertFalse(result["ok"])
        self.assertIn("manifest receipt says unchanged, hash says updated", result["error"])

    async def test_long_noop_synthesis_requires_manifest_pointer(self) -> None:
        receipt = """[MEMORY_RECONCILED]
status: ok
durable_delta: none
progress: unchanged
progress_ref: none
manifest: unchanged
manifest_ref: none
overview: unchanged
overview_manifest_refs: none
overview_reason: routing unchanged
resolved_escalations: none
[/MEMORY_RECONCILED]"""
        result = await self._run(receipt, final_text="x" * 900)
        self.assertFalse(result["ok"])
        self.assertIn("long unchanged synthesis requires", result["error"])

    async def test_footer_projection_clears_resolved_escalation(self) -> None:
        opened = db.open_escalation(
            self.project["slug"], "MODEL", "HUMAN", "user", "choose gate")
        main._stamp_overview(
            self.owner, "1.0", None, project_slug=self.project["slug"],
            agent_id="MODEL", memory_status="verified")
        footer = (self.owner / "overview.md").read_text(encoding="utf-8")
        self.assertIn(opened["escalation_id"], footer)

        db.resolve_escalations(
            self.project["slug"], [opened["escalation_id"]], "MODEL")
        main._stamp_overview(
            self.owner, "1.0", None, project_slug=self.project["slug"],
            agent_id="MODEL", memory_status="verified")
        footer = (self.owner / "overview.md").read_text(encoding="utf-8")
        self.assertIn("open_escalation: none", footer)
        self.assertIn("open_escalation_ids: none", footer)
        self.assertNotIn(opened["escalation_id"], footer)


if __name__ == "__main__":
    unittest.main()
