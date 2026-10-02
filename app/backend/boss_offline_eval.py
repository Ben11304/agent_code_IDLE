"""LangSmith dataset and experiment entrypoint for the isolated BOSS runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from langsmith import Client, evaluate

from .boss_offline_runner import run_boss_subprocess


DATASET_NAME = "cveval-boss-routing-v1"
DATASET_FILE = (
    Path(__file__).resolve().parent
    / "evaluation_data"
    / "cveval_boss_routing_v1.json"
)


def _outputs(value: Any) -> dict[str, Any]:
    outputs = value.outputs if hasattr(value, "outputs") else value.get("outputs", {})
    return outputs or {}


def routing_recall_evaluator(run, example) -> dict[str, Any]:
    """Score the fraction of required direct workers that BOSS dispatched."""
    actual = set(_outputs(run).get("trajectory") or [])
    expected = set(_outputs(example).get("expected_dispatches") or [])
    score = 1.0 if not expected else len(actual & expected) / len(expected)
    return {
        "score": score,
        "comment": f"expected={sorted(expected)} actual={sorted(actual)}",
    }


def routing_precision_evaluator(run, example) -> dict[str, Any]:
    """Penalize extra routes that are outside the case's allowed worker set."""
    actual = list(_outputs(run).get("trajectory") or [])
    allowed = set(
        _outputs(example).get("allowed_dispatches")
        or _outputs(example).get("expected_dispatches")
        or []
    )
    if not actual:
        score = 1.0 if not allowed else 0.0
    else:
        score = sum(target in allowed for target in actual) / len(actual)
    return {
        "score": score,
        "comment": f"allowed={sorted(allowed)} actual={actual}",
    }


def forbidden_dispatch_avoidance_evaluator(run, example) -> dict[str, Any]:
    """Require zero direct or sub-team boundary violations."""
    actual = set(_outputs(run).get("trajectory") or [])
    forbidden = set(_outputs(example).get("forbidden_dispatches") or [])
    violations = sorted(actual & forbidden)
    return {
        "score": 0.0 if violations else 1.0,
        "comment": f"forbidden dispatches observed={violations}",
    }


def no_duplicate_dispatch_evaluator(run, example) -> dict[str, Any]:
    """Detect repeated dispatches within one bounded orchestration run."""
    trajectory = list(_outputs(run).get("trajectory") or [])
    duplicates = sorted({target for target in trajectory if trajectory.count(target) > 1})
    return {
        "score": 0.0 if duplicates else 1.0,
        "comment": f"duplicate targets={duplicates}",
    }


def orchestration_completion_evaluator(run, example) -> dict[str, Any]:
    """Require a clean run with a non-empty synthesized final response."""
    outputs = _outputs(run)
    status = outputs.get("status")
    final_response = str(outputs.get("final_response") or "").strip()
    ok = status == "ok" and bool(final_response) and not outputs.get("errors")
    return {
        "score": 1.0 if ok else 0.0,
        "comment": (
            f"status={status!r} final_response_chars={len(final_response)} "
            f"errors={outputs.get('errors') or []}"
        ),
    }


EVALUATORS = [
    routing_recall_evaluator,
    routing_precision_evaluator,
    forbidden_dispatch_avoidance_evaluator,
    no_duplicate_dispatch_evaluator,
    orchestration_completion_evaluator,
]


def _load_examples() -> list[dict[str, Any]]:
    examples = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    if not isinstance(examples, list) or not examples:
        raise ValueError(f"dataset file must contain a non-empty list: {DATASET_FILE}")
    return examples


def sync_dataset(client: Client, dataset_name: str = DATASET_NAME) -> dict[str, Any]:
    """Create the versioned dataset and append only missing immutable cases."""
    matches = list(client.list_datasets(dataset_name=dataset_name, limit=10))
    if len(matches) > 1:
        raise RuntimeError(f"multiple datasets have the exact name {dataset_name!r}")
    if matches:
        dataset = matches[0]
        created = False
    else:
        dataset = client.create_dataset(
            dataset_name,
            description=(
                "Offline trajectory cases for cveval/BOSS routing. Runs use a "
                "temporary project/DB and deterministic captured worker transport."
            ),
            metadata={
                "agent": "cveval/BOSS",
                "dataset_type": "trajectory",
                "version": "1",
                "worker_mode": "capture",
            },
        )
        created = True

    existing = {
        str(example.inputs.get("case_id")): example
        for example in client.list_examples(dataset_id=dataset.id)
    }
    additions = []
    for candidate in _load_examples():
        case_id = str(candidate["inputs"]["case_id"])
        current = existing.get(case_id)
        if current:
            if current.inputs != candidate["inputs"] or current.outputs != candidate["outputs"]:
                raise RuntimeError(
                    f"remote case {case_id!r} differs from versioned local data; "
                    "create a v2 dataset instead of mutating v1"
                )
            continue
        additions.append(candidate)
    if additions:
        client.create_examples(dataset_id=dataset.id, examples=additions)

    count = sum(1 for _ in client.list_examples(dataset_id=dataset.id))
    return {
        "dataset_name": dataset.name,
        "dataset_id": str(dataset.id),
        "created": created,
        "added": len(additions),
        "example_count": count,
    }


def run_experiment(
    client: Client,
    *,
    dataset_name: str = DATASET_NAME,
    experiment_prefix: str = "cveval-boss-codex-capture-v1",
    repetitions: int = 1,
):
    # Local deterministic evaluators are intentionally supplied here first.
    # LangSmith uploads their feedback on every experiment run.
    return evaluate(
        run_boss_subprocess,
        data=dataset_name,
        evaluators=EVALUATORS,
        experiment_prefix=experiment_prefix,
        description=(
            "Real cveval/BOSS Codex turns in an isolated project and DB; worker "
            "execution is captured deterministically for safe routing evaluation."
        ),
        metadata={
            "agent": "cveval/BOSS",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "worker_mode": "capture",
            "isolation": "temporary-project-and-db",
        },
        max_concurrency=1,
        num_repetitions=repetitions,
        client=client,
        blocking=True,
        upload_results=True,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync-dataset", "run-experiment"))
    parser.add_argument("--dataset", default=DATASET_NAME)
    parser.add_argument("--experiment-prefix", default="cveval-boss-codex-capture-v1")
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()

    client = Client()
    dataset = sync_dataset(client, args.dataset)
    print(json.dumps(dataset, indent=2, sort_keys=True))
    if args.command == "sync-dataset":
        return 0
    results = run_experiment(
        client,
        dataset_name=args.dataset,
        experiment_prefix=args.experiment_prefix,
        repetitions=args.repetitions,
    )
    experiment_name = getattr(results, "experiment_name", None)
    if experiment_name:
        print(json.dumps({"experiment_name": experiment_name}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
