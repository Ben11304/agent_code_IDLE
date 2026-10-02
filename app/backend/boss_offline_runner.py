"""Side-effect-isolated runner for LangSmith AgentUI evaluations.

The parent process calls :func:`run_agent_subprocess`. Each example is executed
in a fresh Python process with a temporary project clone and SQLite database so
AgentUI sessions, dispatch ledgers, schedules, and project files used by the UI
are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


_RESULT_PREFIX = "BOSS_OFFLINE_EVAL_RESULT="
_APP_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PROJECT_ROOT = _WORKSPACE_ROOT / "ConstructionVLM-Eval-AGENT"


def _file_snapshot(root: Path) -> dict[str, str]:
    """Hash regular files in the disposable project for scope evaluation."""
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = str(path.relative_to(root))
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def _rewrite_project_for_eval(
    source_root: Path,
    isolated_root: Path,
    *,
    model: str,
) -> None:
    """Point every agent at Codex and remove external cwd/prompt paths."""
    config_path = isolated_root / ".agentui" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for agent in config.get("agents", []) or []:
        agent["model"] = "codex"
        agent["codex_model"] = model

        raw_prompt = Path(str(agent.get("system_prompt_file") or ""))
        source_prompt = raw_prompt if raw_prompt.is_absolute() else source_root / raw_prompt
        raw_cwd = Path(str(agent.get("cwd") or "."))
        source_cwd = raw_cwd if raw_cwd.is_absolute() else source_root / raw_cwd
        try:
            source_cwd.resolve().relative_to(source_root.resolve())
            cwd_is_external = False
        except (OSError, ValueError):
            cwd_is_external = True
        try:
            source_prompt.resolve().relative_to(source_root.resolve())
            prompt_is_external = False
        except (OSError, ValueError):
            prompt_is_external = True

        if cwd_is_external or prompt_is_external:
            relative_dir = Path("_eval_external_agents") / str(agent.get("id") or "agent")
            destination_dir = isolated_root / relative_dir
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination_prompt = destination_dir / "AGENT.md"
            if source_prompt.is_file():
                shutil.copy2(source_prompt, destination_prompt)
            else:
                destination_prompt.write_text(
                    "# Offline evaluation agent\n\n"
                    "Respond only to the routed evaluation task. Do not use tools or external services.\n",
                    encoding="utf-8",
                )
            agent["cwd"] = str(relative_dir)
            agent["system_prompt_file"] = str(relative_dir / "AGENT.md")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


async def _captured_dispatch(
    db_module,
    slug: str,
    source_id: str,
    target_id: str,
    task: str,
    emit,
    tracker,
    chain,
) -> dict[str, Any]:
    """A deterministic worker transport for routing-only evaluation cases."""
    result_text = (
        "[RESULT]\n"
        "status: done\n"
        "goal_status: met\n"
        f"summary: Offline routing fixture accepted the dispatch to {target_id}.\n"
        "[/RESULT]\n\n"
        "[control-plane evaluation] Routing was captured successfully. No production "
        "work was executed; synthesize the final response without another dispatch."
    )
    db_module.record_dispatch_result(
        project_slug=slug,
        source_agent=source_id,
        target_agent=target_id,
        task=task,
        result_text=result_text,
        status="ok",
        meta={"chain": list(chain), "offline_fixture": True},
    )
    await emit({
        "type": "dispatch_complete",
        "source": source_id,
        "target": target_id,
        "status": "ok",
        "message": None,
    })
    return {
        "status": "ok",
        "result": result_text,
        "error": None,
        "manifest_before": None,
        "manifest_after": None,
        "manifest_changed": None,
        "goal_claimed": "met",
        "offline_fixture": True,
    }


async def _run_isolated(inputs: dict[str, Any]) -> dict[str, Any]:
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("LANGSMITH_API_KEY is required")
    message = str(inputs.get("message") or "").strip()
    if not message:
        raise ValueError("inputs.message is required")
    agent_id = str(inputs.get("agent_id") or "BOSS").strip().upper()

    source_root = Path(
        os.environ.get("BOSS_EVAL_SOURCE_ROOT", str(_DEFAULT_PROJECT_ROOT))
    ).expanduser().resolve()
    if not (source_root / ".agentui" / "project.yaml").is_file():
        raise FileNotFoundError(f"AgentUI project not found: {source_root}")
    model = os.environ.get("BOSS_EVAL_CODEX_MODEL", "gpt-5.6-terra").strip()
    worker_mode = os.environ.get("BOSS_EVAL_WORKER_MODE", "capture").strip().lower()
    if worker_mode not in {"capture", "live"}:
        raise ValueError("BOSS_EVAL_WORKER_MODE must be capture or live")

    with tempfile.TemporaryDirectory(prefix="cveval-langsmith-") as temp_dir:
        temp_root = Path(temp_dir)
        isolated_root = temp_root / source_root.name
        shutil.copytree(
            source_root,
            isolated_root,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        _rewrite_project_for_eval(source_root, isolated_root, model=model)

        registry_path = temp_root / "registry.yaml"
        registry_path.write_text(
            yaml.safe_dump({
                "workspace_root": str(temp_root),
                "projects": [str(isolated_root)],
            }, sort_keys=False),
            encoding="utf-8",
        )

        # These modules only define path constants. Configure them before main
        # is imported, because main initializes the database during import.
        from . import db, projects

        db.DB_PATH = temp_root / "agentui-eval.db"
        projects.REGISTRY_PATH = registry_path
        from . import main

        if not projects.get_agent("cveval", agent_id):
            raise ValueError(f"unknown cveval agent: {agent_id}")

        db.set_setting("scheduler_enabled", "0")
        db.set_setting("context_log_enabled", "0")
        db.set_setting("token_count_enabled", "0")
        if worker_mode == "capture":
            async def captured(slug, source_id, target_id, task, emit, tracker, chain):
                return await _captured_dispatch(
                    db, slug, source_id, target_id, task, emit, tracker, chain)

            main._dispatched_run = main.trace_dispatch(captured)
            main._MAX_CONT_ROUNDS = 1

        files_before = _file_snapshot(isolated_root)
        case_id = str(inputs.get("case_id") or "adhoc")
        evaluation_message = f"[OFFLINE_EVAL_CASE={case_id}]\n{message}"
        run = main._start_run(
            "cveval",
            agent_id,
            evaluation_message,
            origin="langsmith-offline-eval",
        )
        result = await run.task
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected orchestration result: {type(result).__name__}")
        files_after = _file_snapshot(isolated_root)
        changed_files = sorted(
            path for path in files_before.keys() & files_after.keys()
            if files_before[path] != files_after[path]
        )
        created_files = sorted(files_after.keys() - files_before.keys())
        deleted_files = sorted(files_before.keys() - files_after.keys())
        tool_calls = [
            {
                "agent": event.get("agent"),
                "tool": event.get("tool"),
                "input": event.get("input") or {},
            }
            for event in run.events
            if event.get("type") == "tool_use"
        ]
        result.update({
            "case_id": case_id,
            "agent": f"cveval/{agent_id}",
            "adapter": "codex",
            "model": model,
            "worker_mode": worker_mode,
            "tool_calls": tool_calls,
            "tool_count": len(tool_calls),
            "changed_files": changed_files,
            "created_files": created_files,
            "deleted_files": deleted_files,
            "isolation": {
                "workspace": "temporary_project_copy",
                "database": "temporary_sqlite",
                "scheduler": "disabled",
                "codex_sandbox": os.environ.get(
                    "AGENTUI_CODEX_SANDBOX_MODE", "workspace-write"),
            },
        })
        return result


def run_agent_subprocess(
    inputs: dict[str, Any],
    *,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """LangSmith-compatible run function with per-example process isolation."""
    env = os.environ.copy()
    env.setdefault("LANGSMITH_TRACING", "true")
    env["LANGSMITH_PROJECT"] = os.environ.get(
        "BOSS_EVAL_LANGSMITH_PROJECT", "consynth-x-boss-offline")
    env.setdefault("LANGCHAIN_CALLBACKS_BACKGROUND", "false")
    env["AGENTUI_EVALUATION_MODE"] = "true"
    env["AGENTUI_CODEX_SANDBOX_MODE"] = "workspace-write"
    env["AGENTUI_CODEX_APPROVAL_POLICY"] = "never"
    completed = subprocess.run(
        [sys.executable, "-m", "backend.boss_offline_runner", "worker"],
        input=json.dumps(inputs, ensure_ascii=False),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=_APP_ROOT,
        env=env,
        timeout=timeout_seconds,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            return json.loads(line[len(_RESULT_PREFIX):])
    details = (completed.stderr or completed.stdout).strip()[-2000:]
    raise RuntimeError(
        f"offline AgentUI worker exited {completed.returncode} without a result: {details}"
    )


def run_boss_subprocess(
    inputs: dict[str, Any],
    *,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Backward-compatible BOSS-only entrypoint used by the routing dataset."""
    boss_inputs = dict(inputs)
    boss_inputs["agent_id"] = "BOSS"
    return run_agent_subprocess(boss_inputs, timeout_seconds=timeout_seconds)


def _worker_entrypoint() -> int:
    try:
        inputs = json.loads(sys.stdin.read())
        result = asyncio.run(_run_isolated(inputs))
        print(_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"AgentUI offline evaluation failed: {exc}", file=sys.stderr)
        return 1


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("worker", "smoke"))
    parser.add_argument("--message", default=(
        "Offline routing evaluation only. Route a request to verify benchmark result "
        "integrity and reproducibility to the correct direct worker. Do not ask a "
        "clarifying question."
    ))
    parser.add_argument("--case-id", default="smoke-audit-route")
    parser.add_argument("--agent", default="BOSS")
    args = parser.parse_args()
    if args.command == "worker":
        return _worker_entrypoint()
    result = run_agent_subprocess({
        "agent_id": args.agent,
        "case_id": args.case_id,
        "message": args.message,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
