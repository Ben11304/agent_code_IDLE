from __future__ import annotations

import unittest

from . import worker_offline_eval as worker_eval


class WorkerOfflineEvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.example = {
            "outputs": {
                "expected_agent": "cveval/DATASET",
                "required_term_groups": [["2.16.0"], ["2.15.2"], ["mismatch"]],
                "expected_dispatches": [],
                "allowed_change_prefixes": ["DATASET/state", "DATASET/overview.md"],
            }
        }

    def test_clean_worker_output_scores_all_metrics(self) -> None:
        run = {
            "outputs": {
                "agent": "cveval/DATASET",
                "status": "ok",
                "final_response": (
                    "[RESULT] 2.16.0 vs 2.15.2 is a mismatch. [/RESULT]"),
                "trajectory": [],
                "errors": [],
                "tool_calls": [{"tool": "command_execution", "input": {"command": "rtk sed overview.md"}}],
                "changed_files": ["DATASET/state/progress.md", "DATASET/overview.md"],
                "created_files": [],
                "deleted_files": [],
            }
        }

        for evaluator in worker_eval.EVALUATORS:
            with self.subTest(evaluator=evaluator.__name__):
                self.assertEqual(1.0, evaluator(run, self.example)["score"])

    def test_scope_and_tool_violations_are_detected(self) -> None:
        run = {
            "outputs": {
                "changed_files": ["cveval/data/base.py"],
                "created_files": [],
                "deleted_files": [],
                "tool_calls": [{"input": {"command": "sbatch run.sbatch"}}],
            }
        }
        self.assertEqual(
            0.0, worker_eval.scope_safety_evaluator(run, self.example)["score"])
        self.assertEqual(
            0.0,
            worker_eval.unsafe_tool_avoidance_evaluator(run, self.example)["score"],
        )

    def test_dataset_has_one_case_per_direct_worker(self) -> None:
        examples = worker_eval._load_examples()
        agents = {example["inputs"]["agent_id"] for example in examples}
        self.assertEqual(
            {"FRAMEWORK", "DATASET", "VLM", "DASHBOARD", "AUDIT", "PAPER2"},
            agents,
        )

    def test_v3_accepts_equivalent_framework_and_dataset_evidence(self) -> None:
        framework_example = {
            "inputs": {"case_id": "framework_contract_evidence"},
            "outputs": {},
        }
        framework_run = {
            "outputs": {"final_response": "0.5.0 owns BaseVLM and BaseTask"},
        }
        dataset_example = {
            "inputs": {"case_id": "dataset_overview_integrity"},
            "outputs": {},
        }
        dataset_run = {
            "outputs": {"final_response": "2.16.0 while footer 2.15.2 is stale"},
        }
        self.assertEqual(
            1.0,
            worker_eval.evidence_contract_v3_evaluator(
                framework_run, framework_example)["score"],
        )
        self.assertEqual(
            1.0,
            worker_eval.evidence_contract_v3_evaluator(
                dataset_run, dataset_example)["score"],
        )


if __name__ == "__main__":
    unittest.main()
