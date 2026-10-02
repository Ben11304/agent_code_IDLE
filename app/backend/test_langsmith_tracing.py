from __future__ import annotations

import unittest

from . import langsmith_tracing as tracing


class LangSmithTracingTest(unittest.TestCase):
    def test_agent_inputs_exclude_runtime_objects(self) -> None:
        inputs = {
            "slug": "cveval",
            "agent_id": "BOSS",
            "message": "trace smoke test",
            "emit": object(),
            "tracker": [object()],
            "chain": ("BOSS",),
            "retry_count": 0,
        }

        traced = tracing.agent_inputs(inputs)

        self.assertEqual("cveval", traced["slug"])
        self.assertEqual("BOSS", traced["agent_id"])
        self.assertEqual(["BOSS"], traced["chain"])
        self.assertNotIn("emit", traced)
        self.assertNotIn("tracker", traced)

    def test_dispatch_inputs_capture_route(self) -> None:
        traced = tracing.dispatch_inputs({
            "slug": "cveval",
            "source_id": "BOSS",
            "target_id": "DATASET",
            "task": "inspect ConSynth-X",
            "chain": ("BOSS",),
            "emit": object(),
        })

        self.assertEqual("BOSS", traced["source_id"])
        self.assertEqual("DATASET", traced["target_id"])
        self.assertEqual(["BOSS"], traced["chain"])
        self.assertNotIn("emit", traced)

    def test_orchestration_agent_and_dispatch_functions_are_traceable(self) -> None:
        async def orchestration() -> str:
            return "ok"

        async def agent_turn() -> str:
            return "ok"

        async def dispatch() -> str:
            return "ok"

        traced_orchestration = tracing.trace_orchestration(orchestration)
        traced_agent = tracing.trace_agent_turn(agent_turn)
        traced_dispatch = tracing.trace_dispatch(dispatch)
        self.assertTrue(
            getattr(traced_orchestration, "__langsmith_traceable__", False))
        self.assertTrue(getattr(traced_agent, "__langsmith_traceable__", False))
        self.assertTrue(getattr(traced_dispatch, "__langsmith_traceable__", False))


if __name__ == "__main__":
    unittest.main()
