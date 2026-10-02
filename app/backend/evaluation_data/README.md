# BOSS offline evaluation

This is a native LangSmith offline experiment for `cveval/BOSS` using the
Codex SDK adapter. Checked against the local evaluation modules on 2026-09-07. “Offline” means a
dataset-based evaluation, not disconnected execution: experiment commands call real
Codex and LangSmith services. No remote experiment was run for this documentation audit.

Every dataset example runs in a new pinned Codex app-server process with:

- a temporary copy of `ConstructionVLM-Eval-AGENT`;
- a temporary SQLite database;
- AgentUI scheduling disabled;
- Codex `workspace-write` sandboxing;
- LangSmith credentials removed from the Codex app-server subprocess;
- deterministic worker capture for routing-only evaluation.

The runner redirects AgentUI DB and registry to temporary paths before importing
`main`, then operates on a copied project. It reads the source project to make that
copy (including external prompt files if configured), and preserves symlinks.
The workspace-write setting and injected instructions constrain evaluated work;
this is not a hermetic container or a blanket guarantee that all external reads,
network access or credentials are removed. Only LangSmith keys are explicitly
stripped from the Codex subprocess in the adapter. Review source paths/capabilities
before a live experiment.

## Run

From `agent_code_IDLE/app`:

```bash
set -a
source .env.local
set +a
.venv/bin/python -m backend.boss_offline_eval sync-dataset
.venv/bin/python -m backend.boss_offline_eval run-experiment
```

The versioned dataset is `cveval-boss-routing-v1`. The experiment records five
high-is-good metrics per example:

- `routing_recall_evaluator`
- `routing_precision_evaluator`
- `forbidden_dispatch_avoidance_evaluator`
- `no_duplicate_dispatch_evaluator`
- `orchestration_completion_evaluator`

`BOSS_EVAL_WORKER_MODE=capture` evaluates the real BOSS model, prompt,
dispatch parser, continuation loop, and synthesis while replacing worker work
with a deterministic result fixture. This prevents benchmark jobs and artifact
writes during routing evaluation. `live` is available for developing a later
end-to-end worker dataset, but it should not be used with this routing dataset.

## Direct-worker experiment

The second dataset, `cveval-workers-evidence-v1`, runs one real isolated Codex
turn for each direct worker: `FRAMEWORK`, `DATASET`, `VLM`, `DASHBOARD`,
`AUDIT`, and `PAPER2`.

```bash
.venv/bin/python -m backend.worker_offline_eval sync-dataset
.venv/bin/python -m backend.worker_offline_eval run-experiment
```

It evaluates agent identity, factual evidence, balanced `[RESULT]` output,
filesystem scope, unsafe tool avoidance, unexpected dispatches, and clean
completion. `evidence_contract_v3_evaluator` is the authoritative evidence
metric. Earlier `evidence_coverage_evaluator` and
`evidence_coverage_v2_evaluator` columns in the first experiment are retained
as evaluator-development history; they were wording-sensitive and do not
represent agent failures.

## Reliability baseline: false claims and consistency

`cveval-workers-reliability-v1` contains one false-claim probe in two
paraphrases for each of the six direct workers. The default experiment repeats
all 12 examples three times and computes row-level rejection/ground-truth
scores plus experiment-level exact-prompt and paraphrase consistency scores.

```bash
.venv/bin/python -m backend.worker_reliability_eval sync-dataset
.venv/bin/python -m backend.worker_reliability_eval run-experiment --repetitions 3
```

This is a bounded, deterministic false-claim test, not a universal detector for
all possible hallucinations. Each run remains isolated in a temporary project
copy and SQLite database.

The current scoring entrypoint uses these evaluator columns; earlier output analysis
is historical, not revalidated in this documentation audit:
`false_claim_acceptance_avoidance_evaluator`,
`semantic_grounding_v2_evaluator`, `repeat_consistency_v2_evaluator`, and
`paraphrase_consistency_v2_evaluator`. The initial wording-sensitive columns
remain useful as evaluator-development history. Re-score an existing experiment
without rerunning workers with:

```bash
.venv/bin/python -m backend.worker_reliability_eval score-experiment \
  --experiment <experiment-id-or-name>
```

## Runner configuration

- `BOSS_EVAL_SOURCE_ROOT`: source project; default sibling `ConstructionVLM-Eval-AGENT`.
  Runner lookup still expects slug `cveval`.
- `BOSS_EVAL_CODEX_MODEL`: default `gpt-5.6-terra` in this checkout.
- `BOSS_EVAL_WORKER_MODE`: `capture` (default) or `live`. Capture limits root
  continuation to one and substitutes worker results; it does not substitute BOSS.
- Parent subprocess wrapper sets evaluation mode and workspace-write; default
  timeout is 900 seconds. It uses the same Python interpreter as the caller.
- `LANGSMITH_API_KEY` and saved Codex authentication are needed for real runs.
- `sync-dataset` creates/appends remote dataset examples; it is not read-only.
  Existing changed v1 examples are rejected in favor of a new dataset version.
- `run-experiment` also syncs the dataset before executing; `score-experiment`
  writes feedback to an existing remote experiment.

Use the existing `test_boss_offline_eval.py`, `test_worker_offline_eval.py`, and
`test_worker_reliability_eval.py` for local evaluator checks. Historical result
columns do not establish current deployed agent quality.
