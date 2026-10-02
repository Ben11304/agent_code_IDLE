from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notion_report.schema import ReportSchemaError, load_report_schema, validate_report_spec
from notion_report.tool import ReportSpec


SCHEMA = """\
schema_version: 1
name: demo-report
revision_title_pattern: '^Report v\\d{4}$'
agents: [BOSS, DATA]
instructions:
  - Keep denominators explicit.
allow_additional_sections: false
allow_additional_subpages: false
main_page:
  owner: BOSS
  guidance: Decision-ready summary.
  required_sections:
    - {key: summary, title: Summary, guidance: State the verified result.}
subpages:
  - key: data
    title: DATA
    owner: DATA
    required_sections:
      - {key: cohort, title: Cohort}
"""


class ReportSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / ".agentui").mkdir()
        (root / ".agentui" / "report_schema.yaml").write_text(
            SCHEMA, encoding="utf-8")
        self.loaded = load_report_schema(root, {
            "enabled": True,
            "schema_file": ".agentui/report_schema.yaml",
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _valid_report(self) -> ReportSpec:
        return ReportSpec.from_dict({
            "title": "Report v0002",
            "sections": [{
                "key": "summary", "heading": "Summary", "content": "Ready",
            }],
            "subpages": [{
                "key": "data",
                "title": "DATA",
                "sections": [{
                    "key": "cohort", "heading": "Cohort", "bullets": ["1,399 trials"],
                }],
            }],
        })

    def test_valid_contract_accepts_complete_report(self) -> None:
        validate_report_spec(self._valid_report(), self.loaded)

    def test_guidance_is_preserved_in_the_prompt_contract(self) -> None:
        contract = self.loaded["contract"]
        self.assertEqual(contract["instructions"], ["Keep denominators explicit."])
        self.assertEqual(contract["main_page"]["guidance"], "Decision-ready summary.")
        self.assertEqual(
            contract["main_page"]["required_sections"][0]["guidance"],
            "State the verified result.",
        )

    def test_system_note_policy_separates_content_and_schema_authority(self) -> None:
        policy = self.loaded["note_policy"]
        self.assertFalse(policy["user_note"]["may_change_schema"])
        self.assertTrue(policy["schema_note"]["may_change_schema"])
        self.assertEqual(
            policy["schema_note"]["heading_pattern"],
            r"^SCHEMA NOTE S-\d{4}$",
        )

    def test_missing_subpage_body_is_rejected(self) -> None:
        report = ReportSpec.from_dict({
            "title": "Report v0002",
            "sections": [{
                "key": "summary", "heading": "Summary", "content": "Ready",
            }],
            "subpages": [{
                "key": "data",
                "title": "DATA",
                "sections": [{"key": "cohort", "heading": "Cohort"}],
            }],
        })
        with self.assertRaisesRegex(ReportSchemaError, "has no body"):
            validate_report_spec(report, self.loaded)

    def test_missing_required_section_is_rejected(self) -> None:
        report = ReportSpec.from_dict({
            "title": "Report v0002",
            "sections": [{
                "key": "other", "heading": "Other", "content": "Wrong",
            }],
            "subpages": [],
        })
        with self.assertRaisesRegex(ReportSchemaError, "missing required section"):
            validate_report_spec(report, self.loaded)

    def test_schema_path_escape_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReportSchemaError, "inside the project root"):
            load_report_schema(self.tmp.name, {
                "enabled": True,
                "schema_file": "../outside.yaml",
            })

    def test_required_native_table_enforces_key_columns_and_rows(self) -> None:
        root = Path(self.tmp.name)
        table_schema = root / ".agentui" / "table_schema.yaml"
        table_schema.write_text(
            """schema_version: 2
name: table-report
revision_title_pattern: '^Report v\\d{4}$'
agents: [BOSS]
main_page:
  owner: BOSS
  required_sections:
    - key: results
      title: Results
      required_tables:
        - key: experiment_results
          title: Experiment results
          min_rows: 2
          columns:
            - {key: arm, title: Arm}
            - {key: accuracy, title: Accuracy}
subpages:
  - key: evidence
    title: Evidence
    owner: BOSS
    required_sections:
      - {key: artifacts, title: Artifacts}
""",
            encoding="utf-8",
        )
        loaded = load_report_schema(root, {
            "enabled": True,
            "schema_file": ".agentui/table_schema.yaml",
        })
        base = {
            "title": "Report v0003",
            "sections": [{"key": "results", "heading": "Results"}],
            "subpages": [{
                "key": "evidence",
                "title": "Evidence",
                "sections": [{
                    "key": "artifacts", "heading": "Artifacts", "content": "results.json",
                }],
            }],
        }
        with self.assertRaisesRegex(ReportSchemaError, "missing required table"):
            validate_report_spec(ReportSpec.from_dict(base), loaded)

        base["sections"][0]["tables"] = [{
            "key": "experiment_results",
            "columns": ["Arm", "Accuracy"],
            "rows": [["CONTROL", "79.0%"]],
        }]
        with self.assertRaisesRegex(ReportSchemaError, "at least 2"):
            validate_report_spec(ReportSpec.from_dict(base), loaded)

        base["sections"][0]["tables"][0]["rows"].append(
            ["QH_FORCE", "78.9%"])
        validate_report_spec(ReportSpec.from_dict(base), loaded)

        base["sections"][0]["tables"][0]["columns"] = ["Accuracy", "Arm"]
        with self.assertRaisesRegex(ReportSchemaError, "columns must exactly match"):
            validate_report_spec(ReportSpec.from_dict(base), loaded)

    def test_required_mermaid_diagram_enforces_key_and_format(self) -> None:
        root = Path(self.tmp.name)
        diagram_schema = root / ".agentui" / "diagram_schema.yaml"
        diagram_schema.write_text(
            """schema_version: 4
name: diagram-report
revision_title_pattern: '^Report v\\d{4}$'
agents: [BOSS]
main_page:
  owner: BOSS
  required_sections:
    - key: architecture
      title: Architecture
      required_diagrams:
        - key: model_architecture
          title: Model architecture
          format: mermaid
          guidance: Keep unknown provenance dashed.
subpages:
  - key: evidence
    title: Evidence
    owner: BOSS
    required_sections:
      - {key: artifacts, title: Artifacts}
""",
            encoding="utf-8",
        )
        loaded = load_report_schema(root, {
            "enabled": True,
            "schema_file": ".agentui/diagram_schema.yaml",
        })
        self.assertEqual(
            loaded["contract"]["main_page"]["required_sections"][0]
            ["required_diagrams"][0]["format"],
            "mermaid",
        )
        base = {
            "title": "Report v0004",
            "sections": [{
                "key": "architecture",
                "heading": "Architecture",
                "content": "Recorded topology only.",
            }],
            "subpages": [{
                "key": "evidence",
                "title": "Evidence",
                "sections": [{
                    "key": "artifacts",
                    "heading": "Artifacts",
                    "content": "model.py",
                }],
            }],
        }
        with self.assertRaisesRegex(ReportSchemaError, "missing required diagram"):
            validate_report_spec(ReportSpec.from_dict(base), loaded)

        base["sections"][0]["diagrams"] = [{
            "key": "model_architecture",
            "format": "mermaid",
            "source": "flowchart LR\nA --> B",
        }]
        validate_report_spec(ReportSpec.from_dict(base), loaded)
