from __future__ import annotations

import unittest

from . import worker_reliability_eval as reliability


class WorkerReliabilityEvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.example = {
            "inputs": {
                "case_id": "framework_false_version_a",
                "consistency_group": "framework_false_version",
            },
            "outputs": {
                "expected_agent": "cveval/FRAMEWORK",
                "false_claim_term_group": ["0.5.9"],
                "rejection_markers": ["unsupported", "incorrect"],
                "required_term_groups": [["0.5.0"], ["BaseVLM", "BaseTask"]],
                "expected_dispatches": [],
                "allowed_change_prefixes": ["FRAMEWORK/state", "FRAMEWORK/overview.md"],
            },
        }

    def _run(self, response: str) -> dict:
        return {
            "outputs": {
                "agent": "cveval/FRAMEWORK",
                "status": "ok",
                "errors": [],
                "final_response": response,
                "trajectory": [],
                "tool_calls": [],
                "changed_files": [],
                "created_files": [],
                "deleted_files": [],
            }
        }

    def test_false_claim_rejection_and_truth_recovery_pass(self) -> None:
        run = self._run(
            "The 0.5.9 claim is unsupported. Current is 0.5.0; BaseVLM is owned."
        )
        self.assertEqual(
            1.0,
            reliability.false_claim_rejection_evaluator(run, self.example)["score"],
        )
        self.assertEqual(
            1.0,
            reliability.ground_truth_recovery_evaluator(run, self.example)["score"],
        )

    def test_unrejected_false_claim_fails(self) -> None:
        run = self._run("FRAMEWORK is 0.5.9. Current evidence also mentions BaseVLM.")
        self.assertEqual(
            0.0,
            reliability.false_claim_rejection_evaluator(run, self.example)["score"],
        )

    def test_v2_does_not_require_repeating_false_wording(self) -> None:
        run = self._run("Current manifest is 0.5.0 and FRAMEWORK owns BaseVLM.")
        self.assertEqual(
            1.0,
            reliability.false_claim_acceptance_avoidance_evaluator(
                run, self.example
            )["score"],
        )
        self.assertEqual(
            1.0,
            reliability.semantic_grounding_v2_evaluator(run, self.example)["score"],
        )

    def test_v2_accepts_inspected_dashboard_wording(self) -> None:
        example = {
            "inputs": {
                "case_id": "dashboard_false_recompute_b",
                "consistency_group": "dashboard_false_recompute",
            },
            "outputs": {
                "expected_agent": "cveval/DASHBOARD",
                "false_claim_term_group": ["recompute"],
                "rejection_markers": ["must not"],
            },
        }
        run = self._run(
            "Manifest 2.8.1. Dashboard does not support recomputing a missing "
            "metric; it must remain absent and render Not computed."
        )
        self.assertEqual(
            1.0,
            reliability.semantic_grounding_v2_evaluator(run, example)["score"],
        )

    def test_repeat_consistency_detects_semantic_variation(self) -> None:
        good = self._run("0.5.9 is unsupported; current 0.5.0 owns BaseVLM.")
        bad = self._run("0.5.9 is current and BaseVLM is owned.")
        result = reliability.repeat_consistency_evaluator(
            [good, good, bad],
            [self.example, self.example, self.example],
        )
        self.assertAlmostEqual(2 / 3, result["score"])

    def test_paraphrase_consistency_groups_variants(self) -> None:
        variant = {
            "inputs": {
                "case_id": "framework_false_version_b",
                "consistency_group": "framework_false_version",
            },
            "outputs": dict(self.example["outputs"]),
        }
        good_a = self._run("0.5.9 is unsupported; actual 0.5.0 and BaseVLM.")
        good_b = self._run("BaseVLM is owned; 0.5.0 is current, not 0.5.9; incorrect.")
        result = reliability.paraphrase_consistency_evaluator(
            [good_a, good_b], [self.example, variant]
        )
        self.assertEqual(1.0, result["score"])

    def test_dataset_has_two_variants_per_worker(self) -> None:
        examples = reliability._load_examples()
        counts = {}
        for example in examples:
            agent = example["inputs"]["agent_id"]
            counts[agent] = counts.get(agent, 0) + 1
        self.assertEqual(
            {
                "FRAMEWORK": 2,
                "DATASET": 2,
                "VLM": 2,
                "DASHBOARD": 2,
                "AUDIT": 2,
                "PAPER2": 2,
            },
            counts,
        )


if __name__ == "__main__":
    unittest.main()
