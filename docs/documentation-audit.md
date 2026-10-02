# Documentation audit — 2026-09-07

Reviewed **36 first-party Markdown documents/templates** against the current working
tree, including existing uncommitted implementation changes. Updated 27 existing
files and added this audit plus [dispatch lifecycle](agentui-dispatch-spec.md).
This is a code/documentation reconciliation, not a production health check or a
new claim about historical experiment results.

## Scope and evidence

Reviewed all project-authored operating/design docs, runtime Markdown templates,
the manual agent template, helper READMEs, evaluation docs and presentation prose.
Compared them with backend handlers/adapters, DB helpers, frontend consumers,
launcher/watchdog, template renderer/sync template, Notion MCP/schema code,
PDF extractor/corpus runner and local evaluator code.

Excluded imported packages in `caveman/` and `app/capability_inventory/{skills,plugins,mcp}`,
virtualenv/dependency/cache docs, and extracted paper text under `pdf_extractor/output*`.
They are third-party/package snapshots or research data, not AgentUI operating docs.
Imported copies of project code/docs retain their snapshot provenance; updating
an imported package is a separate inventory maintenance action.

Existing PPTX/PDF/screenshots and the pitch generator are retained as historical
presentation artifacts. Their README now explicitly says the generator has its own
text and does not consume the updated Markdown. Binary slides have not been regenerated.
No Python/JavaScript/shell implementation, project registry, credentials, production DB,
service, remote Notion pages or remote evaluation datasets were changed by this audit.
Runtime Markdown prompt templates were edited to correct their documented context/tool
assumptions; existing scaffolded projects are not automatically migrated by these edits.

## Main corrections and code evidence

| Topic | Corrected statement | Evidence |
|---|---|---|
| Dispatch | Real worker execution, ledger, up to three root continuations | [main.py](../app/backend/main.py): `_run_agent`, `_dispatched_run`, `_start_run` |
| Browser close | Unsubscribe only; Stop/backend restart interrupts runs | `main._run_subscriber_sse`, `api_stop`, `_Run` |
| Provider support | Five adapters; DeepSeek/GLM keys; Codex SDK login | [adapters.py](../app/backend/adapters.py): `get_stream`, provider functions |
| Current child context | All direct-child overviews injected every parent turn | `main._children_overview_context` |
| Rollup schema | `children` object keyed by ID; no per-row `status_color`/escalation object | `main._write_children_rollups` |
| Drift | Best-effort parsed pins + prompt warning, no automatic sync/hard return | `main._check_version_pins`, `_run_agent` |
| Structured output | Optional blocks, one corrective retry; limited shape checks | `main._parse_structured`, `_run_agent` |
| HALT/dissent | Already implemented as records/feedback; no general direction veto | `main._handle_halt`, `_handle_dissent`, `_open_dissent_warning`; [db.py](../app/backend/db.py) |
| Verification | Persisted claims + current-version comparison + prompt hint | `main._verify_delta`, `_verify_delta_hint` |
| Goal acceptance | Guidance to external acceptance; no generic metric/budget/plateau evaluation | `main._parse_goal`, `_dispatched_run` |
| Memory | Project opt-in receipt/hash/pointer validation and overview provenance | `main._run_memory_reconciliation`, `_stamp_overview` |
| Progress | Two activity dates; archival off by default; JSON migration explicit | [progress_store.py](../app/backend/progress_store.py), `main._progress_rotate_tick` |
| Scheduler | Off→on skips overdue disabled-period work; restart is different | `main._skip_overdue_on_resume`, `_scheduler_tick`, `_run_scheduled_fire` |
| Capabilities | Local mutable snapshot, Codex policy, global Delete | [capabilities.py](../app/backend/capabilities.py): `discover_catalog`, `delete_inventory_capability` |
| Notion | System Hub, project inheritance, seven scoped tools, Dashboard already present | [mcp_server.py](../notion_report/mcp_server.py), [notion_settings.py](../app/backend/notion_settings.py) |
| Deployment | Tracked launcher, local env sourcing, actual watchdog host/path gate | [run.sh](../app/run.sh), [keepalive.sh](../app/keepalive.sh) |
| PDF extraction | Heuristic extraction; hard-coded table trigger and ignored ID/device options | [extract.py](../pdf_extractor/extract.py): `extract_with_pymupdf`, `extract_paper`, `main` |

## Remaining implementation limits (documented, not fixed here)

1. A second malformed structured response has no independent hard-reject branch;
   nonempty HALT/dissent evidence is not semantic verification. Dissent resolution
   lacks a separate parent-only permission gate.
2. Root `_Run` busy checks do not serialize direct workers across parents/direct chats.
3. Version pins fail open for unsupported/missing input text. The generated sync
   `check` prints versions and exits 0 even on drift; its version parser is narrower
   than the Python reader. Generated producer mappings do not auto-follow YAML edits.
4. Manifest publication feedback compares mtime, not proof of a semantic version bump.
   Watermarks are producer-version claims, not artifact hashes or enforced tool skips.
5. Legacy overview stamping uses a placeholder/length heuristic, not complete role,
   enum, path or word-budget validation. Failed reconciliation restores the previous
   overview only if that snapshot was nonempty; new overview content remains marked
   `needs_review` when there was nothing to restore.
6. Rollup `overview_path` is formatted as `../ID/overview.md`, which can diverge for
   custom cwd layouts; actual parent pre-flight resolves through `_agent_dir`.
7. Schedule completion events may be appended after root completion. API/DB refresh
   remains necessary; no durable exhausted-chat notification or exactly-once fire
   guarantee is implemented. Global off does not cancel active fires.
8. Grok does not receive the same scoped Notion MCP launch config as Claude/Codex.
   Inventory metadata does not attest current remote authentication/availability.
9. Evaluation copies source files and preserves symlinks; workspace-write and prompt
   rules do not make a hermetic/offline container. Experiment/dataset commands call
   remote services and can write dataset/results/feedback.
10. PDF extractor injects a fixed Wagner table on keyword match, ignores passed paper
    ID/device for the actual writer, and does not clean old conditional output files.
    The injected table must not be treated as verified PDF evidence.

## Validation

- **51 existing local tests passed**, zero failures/errors/skips: memory receipt,
  escalation state, capability policy/inventory, mocked Codex SDK adapter, Notion
  report schema, BOSS evaluator, worker evaluator and worker reliability evaluator.
- Tests redirected DB and registry to temporary paths before importing `main`;
  provider calls used existing fakes and no remote experiments/services were invoked.
- Final documentation checks passed across **38 documents** (36 reviewed + 2 new):
  **100 local links**, **5 YAML examples**, Markdown fences, **20 runtime symbols**,
  two six-file agent template renders and project/shared-template rendering.
  These are documentation checks, not a full backend/UI/live deployment acceptance run.
- Historical pilot tables/results remain labelled historical rather than being
  presented as new measurements or the current state of external projects.

## File-by-file coverage

| Document | Action | Reconciliation |
|---|---|---|
| [CLAUDE.md](../CLAUDE.md) | Updated | Runtime map, lifecycle, supported adapters, enforcement boundaries; preserved terminal/user customizations. |
| [DEPLOY.md](../DEPLOY.md) | Updated | Actual launcher/env/reload, service path, site-specific watchdog, SQLite backup. |
| [README.md](../README.md) | Updated | Current feature/auth summary and documentation map. |
| [BUILD_HANDBOOK_checklist.md](../BUILD_HANDBOOK_checklist.md) | Updated | Current code status separated from historical project/live pilot claims. |
| [BUILD_HANDBOOK.md](../BUILD_HANDBOOK.md) | Updated | Replaced stale line-based pseudocode with actual schemas/helpers and limits. |
| [system_architech.md](../system_architech.md) | Updated | Preserved design rationale; corrected implementation status and annotated target behavior. |
| [app/README.md](../app/README.md) | Updated | Removed MVP-only claims; current graph/chat/config/Capabilities usage. |
| [app/backend/templates/README.md](../app/backend/templates/README.md) | Updated | Scaffold overview, slim boot and routing exception. |
| [app/backend/templates/shared/research_integrity.md](../app/backend/templates/shared/research_integrity.md) | Reviewed; retained | Project policy template; no runtime implementation claim requiring correction. |
| [app/backend/templates/shared/tool_conventions.md](../app/backend/templates/shared/tool_conventions.md) | Updated | Removed assumed Consensus/global-tool availability; RTK proxy and exposed-tool policy. |
| [app/backend/templates/shared/handoff_schema.md](../app/backend/templates/shared/handoff_schema.md) | Updated | Manifest boundary/routing exception and generated topology caveat. |
| [app/backend/templates/shared/scope_decisions.md](../app/backend/templates/shared/scope_decisions.md) | Updated | Read-only slim routing exception consistent with injected parent instructions. |
| [app/backend/templates/shared/glossary.md](../app/backend/templates/shared/glossary.md) | Reviewed; retained | Intentional project placeholders; no application behavior claims. |
| [app/backend/templates/agent/AGENT.md](../app/backend/templates/agent/AGENT.md) | Updated | Every-turn vs cold-start context, progress convention and adapter-aware skills. |
| [app/backend/templates/agent/overview.md](../app/backend/templates/agent/overview.md) | Reviewed; retained | Marker format and initial incomplete state match stamp inputs; word limit is author guidance. |
| [app/backend/templates/agent/inputs/manifest.md](../app/backend/templates/agent/inputs/manifest.md) | Reviewed; retained | Pinned-version format matches the Python checker and generated sync output. |
| [app/backend/templates/agent/outputs/manifest.md](../app/backend/templates/agent/outputs/manifest.md) | Reviewed; retained | Bootstrap version, history/artifact sections match generated contract. |
| [app/backend/templates/agent/state/progress.md](../app/backend/templates/agent/state/progress.md) | Updated | Optional archival vs explicit JSON migration; removed unearned claim that boot reads occurred. |
| [app/backend/templates/agent/context/code_map.md](../app/backend/templates/agent/context/code_map.md) | Updated | Overview ownership and slim routing reference. |
| [app/backend/templates/paper_collection/README.md](../app/backend/templates/paper_collection/README.md) | Reviewed; retained | ShortID/PDF/MISSING semantics match resource API and UI. |
| [app/backend/templates/paper_collection/CATALOG.md](../app/backend/templates/paper_collection/CATALOG.md) | Reviewed; retained | Commented row pattern matches catalog parser. |
| [app/backend/evaluation_data/README.md](../app/backend/evaluation_data/README.md) | Updated | Remote side effects, actual subprocess configuration and isolation limits. |
| [app/capability_inventory/README.md](../app/capability_inventory/README.md) | Updated | Mutable index, snapshot refresh, Codex-only policy and imported-package provenance. |
| [docs/scheduler-spec.md](../docs/scheduler-spec.md) | Updated | Toggle resume, busy/restart semantics, real events and persistence limits. |
| [pitch-deck/README.md](../pitch-deck/README.md) | Updated | Binary/generator snapshot status; generation workflow and outdated cost/comparison claims. |
| [pitch-deck/Story-Slides.md](../pitch-deck/Story-Slides.md) | Updated | Current adapters, memory/ownership scope and evidence limits. |
| [pitch-deck/AgentUI_Story_Slides_Content.md](../pitch-deck/AgentUI_Story_Slides_Content.md) | Updated | Current adapters/overview injection; avoid artifact-quality guarantees. |
| [pdf_extractor/README.md](../pdf_extractor/README.md) | Updated | Actual flags/outputs, optional fallbacks and hard-coded table behavior. |
| [TEMPLATE_AGENT/README.md](../TEMPLATE_AGENT/README.md) | Updated | Manual reference vs runtime templates; memory/archival/injection limits. |
| [TEMPLATE_AGENT/AGENT.md](../TEMPLATE_AGENT/AGENT.md) | Updated | Cold-start/every-turn distinction and newest-first progress convention. |
| [TEMPLATE_AGENT/overview.md](../TEMPLATE_AGENT/overview.md) | Updated | Initial manifest version aligned with output manifest 0.1.0. |
| [TEMPLATE_AGENT/inputs/manifest.md](../TEMPLATE_AGENT/inputs/manifest.md) | Reviewed; retained | Manual placeholder form; compatible pinned-version line syntax. |
| [TEMPLATE_AGENT/outputs/manifest.md](../TEMPLATE_AGENT/outputs/manifest.md) | Reviewed; retained | Manual contract and semver guidance; no false runtime claim. |
| [TEMPLATE_AGENT/state/progress.md](../TEMPLATE_AGENT/state/progress.md) | Updated | Optional Markdown archival and explicit JSON migration. |
| [TEMPLATE_AGENT/context/code_map.md](../TEMPLATE_AGENT/context/code_map.md) | Updated | On-demand code map; dated progress convention. |
| [notion_report/README.md](../notion_report/README.md) | Updated | System Hub/project inheritance, seven scoped tools, existing Dashboard and adapter limits. |
