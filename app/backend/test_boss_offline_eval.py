from __future__ import annotations

import unittest

from . import boss_offline_eval as offline_eval


class BossOfflineEvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.example = {
            "outputs": {
                "expected_dispatches": ["DATASET"],
                "allowed_dispatches": ["DATASET"],
                "forbidden_dispatches": ["GENERATION", "VLM"],
            }
        }

    def test_expected_route_scores_cleanly(self) -> None:
        run = {
            "outputs": {
                "status": "ok",
                "trajectory": ["DATASET"],
                "final_response": "Routed through DATASET.",
                "errors": [],
            }
        }

        for evaluator in offline_eval.EVALUATORS:
            with self.subTest(evaluator=evaluator.__name__):
                self.assertEqual(1.0, evaluator(run, self.example)["score"])

    def test_boundary_violation_is_detected(self) -> None:
        run = {
            "outputs": {
                "status": "ok",
                "trajectory": ["GENERATION"],
                "final_response": "Incorrect direct route.",
                "errors": [],
            }
        }

        self.assertEqual(
            0.0, offline_eval.routing_recall_evaluator(run, self.example)["score"])
        self.assertEqual(
            0.0, offline_eval.routing_precision_evaluator(run, self.example)["score"])
        self.assertEqual(
            0.0,
            offline_eval.forbidden_dispatch_avoidance_evaluator(
                run, self.example)["score"],
        )

    def test_dataset_case_ids_are_unique(self) -> None:
        examples = offline_eval._load_examples()
        case_ids = [example["inputs"]["case_id"] for example in examples]
        self.assertEqual(len(case_ids), len(set(case_ids)))


if __name__ == "__main__":
    unittest.main()
