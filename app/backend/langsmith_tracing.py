"""LangSmith spans for AgentUI's orchestration control plane."""

from __future__ import annotations

from langsmith import traceable


_TRACE_METADATA = {"application": "AgentUI", "component": "orchestration"}


def trace_fields(inputs: dict, names: tuple[str, ...]) -> dict:
    """Keep trace payloads useful without serializing callbacks or task handles."""
    traced = {name: inputs[name] for name in names if name in inputs}
    if isinstance(traced.get("chain"), tuple):
        traced["chain"] = list(traced["chain"])
    return traced


def orchestration_inputs(inputs: dict) -> dict:
    return trace_fields(
        inputs,
        ("project_slug", "root_agent_id", "user_message", "run_origin"),
    )


def agent_inputs(inputs: dict) -> dict:
    return trace_fields(
        inputs,
        ("slug", "agent_id", "message", "chain", "grok_options", "retry_count"),
    )


def dispatch_inputs(inputs: dict) -> dict:
    return trace_fields(
        inputs,
        ("slug", "source_id", "target_id", "task", "chain"),
    )


trace_orchestration = traceable(
    name="agentui_orchestration_run",
    run_type="chain",
    tags=["agentui", "orchestration"],
    metadata=_TRACE_METADATA,
    process_inputs=orchestration_inputs,
)

trace_agent_turn = traceable(
    name="agentui_agent_turn",
    run_type="chain",
    tags=["agentui", "agent-turn"],
    metadata=_TRACE_METADATA,
    process_inputs=agent_inputs,
)

trace_dispatch = traceable(
    name="agentui_dispatch",
    run_type="chain",
    tags=["agentui", "dispatch"],
    metadata=_TRACE_METADATA,
    process_inputs=dispatch_inputs,
)
