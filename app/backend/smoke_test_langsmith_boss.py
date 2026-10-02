"""Run one side-effect-free BOSS turn and verify its LangSmith root trace.

Set ``AGENTUI_SMOKE_ADAPTER=codex`` to exercise the BOSS prompt and control
plane with the local Codex subscription without changing the persisted AgentUI
adapter override.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from langsmith import Client

from . import main


_SMOKE_MESSAGE = (
    "LangSmith tracing smoke test on the Codex adapter only. Do not dispatch, "
    "call tools, edit files, create schedules, or modify project state. Reply "
    "exactly BOSS_TRACE_OK."
)


async def _run() -> dict:
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("LANGSMITH_API_KEY is required")
    if os.environ.get("LANGSMITH_TRACING", "").lower() not in {"1", "true"}:
        raise RuntimeError("LANGSMITH_TRACING=true is required")

    adapter = os.environ.get("AGENTUI_SMOKE_ADAPTER", "").strip().lower()
    if adapter not in {"", "codex"}:
        raise RuntimeError(f"unsupported smoke adapter: {adapter}")
    codex_model = os.environ.get(
        "AGENTUI_SMOKE_CODEX_MODEL", "gpt-5.6-terra").strip()

    # Keep the adapter switch process-local: the real AgentUI override and
    # project.yaml remain untouched. All orchestration/session code below is the
    # production path used by the UI.
    original_get_override = main.db.get_agent_override
    if adapter == "codex":
        def smoke_get_override(slug: str, agent_id: str) -> dict:
            override = dict(original_get_override(slug, agent_id) or {})
            if slug == "cveval" and agent_id == "BOSS":
                override.update(model="codex", codex_model=codex_model)
            return override

        main.db.get_agent_override = smoke_get_override

    try:
        run = main._start_run(
            "cveval", "BOSS", _SMOKE_MESSAGE, origin="smoke-test")
        await run.task
    finally:
        main.db.get_agent_override = original_get_override

    errors = [event.get("message", "unknown error") for event in run.events
              if event.get("type") == "error"]
    response = "".join(
        event.get("text", "")
        for event in run.events
        if event.get("type") == "delta" and event.get("agent") == "BOSS"
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    if "BOSS_TRACE_OK" not in response:
        raise RuntimeError("BOSS did not return the smoke-test marker")

    client = Client()
    project = os.environ.get("LANGSMITH_PROJECT", "default")
    deadline = time.monotonic() + 20
    trace = None
    while time.monotonic() < deadline:
        for candidate in client.list_runs(
            project_name=project,
            is_root=True,
            run_type="chain",
            limit=20,
        ):
            if (candidate.name == "agentui_orchestration_run"
                    and candidate.inputs.get("root_agent_id") == "BOSS"
                    and candidate.inputs.get("user_message") == _SMOKE_MESSAGE):
                trace = candidate
                break
        if trace:
            break
        await asyncio.sleep(1)
    if trace is None:
        raise RuntimeError("BOSS completed, but its root trace was not queryable")

    child_runs = list(client.list_runs(
        project_name=project,
        trace_id=trace.trace_id,
        run_type="chain",
        limit=50,
    ))
    return {
        "status": "ok",
        "agent": "cveval/BOSS",
        "adapter": adapter or "configured",
        "model": codex_model if adapter == "codex" else "configured",
        "trace_id": str(trace.trace_id),
        "root_run_id": str(trace.id),
        "run_names": sorted({child.name for child in child_runs}),
        "dispatch_count": sum(child.name == "agentui_dispatch" for child in child_runs),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_run()), indent=2, sort_keys=True))
