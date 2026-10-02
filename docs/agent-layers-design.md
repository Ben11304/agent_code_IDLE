# Agent layers — Memory, Knowledge, Tools

Status: **proposed architecture**, grounded in the current AgentUI implementation.
This document does not claim the new policy runtime or Dashboard tabs are implemented.
Scope starts with per-agent file/document grants; Notion and retrieval backends can
implement the same source contract later. Existing project policies remain unchanged.

## 1. The three layers and their authority

| Layer | Purpose | Contents | Who controls changes |
|---|---|---|---|
| Memory | Preserve the agent's work state across turns | Recent episodes, current state, decisions, evidence-backed learned notes, checkpoints | Owner writes within policy; backend records versions/provenance |
| Knowledge | Supply reference material the agent may use | Assigned files, paper sources, contracts, project references; later scoped Notion/search | User/project owner assigns sources; source owner controls publication |
| Tools | Permit operations | File read/write, shell, web/network, MCP operations, dispatch | User/project policy; backend/runtime enforces supported permissions |

Role, identity and mandatory project rules sit above these layers in the existing
agent definition. An ordinary retrieved document or generated memory cannot change
policy, tool grants or agent identity by containing instructions.

Classification follows purpose/ownership rather than extension. A worker's current
manifest is its own durable technical state and an artifact contract; a consumer
may be granted the published manifest as a versioned knowledge reference. Do not
copy the full same content into parallel authoritative stores. `overview.md` remains
a derived routing projection rather than a separate source of technical truth.

Skills are procedural instructions with possible assets/scripts, not tool permissions.
Keep skill assignment alongside Tools/Capabilities in the Dashboard, but record it
separately. Enabling a skill never implicitly enables its required tools or gives
it access to another agent's knowledge/memory. Plugins group contributions; they
are not themselves an unrestricted permission boundary.

## 2. Current implementation mapped to this design

| Existing surface | Reuse | Missing control |
|---|---|---|
| `AGENT.md`, shared project rules | Identity and mandatory constraints | Distinguish trusted instructions from reference content |
| Sessions/provider resume/compact seed | Working conversational memory | Explicit retention/reuse policy and revision compatibility |
| Progress, owner manifest, overview | Durable state and routing projection | Per-agent inspect/read/write/retention settings |
| `memory.reconciliation` | Owner reconciliation receipt/hash gate | Dashboard policy visibility; separate provenance from semantic truth |
| Child overviews and ledger enrichment | Shared coordination memory | Explicit graph-derived read grants and exposure audit |
| Input manifest/paper collection | Published references | Source catalog, agent grants, version/freshness policy |
| `capability_inventory` and SQLite policy | Skill/plugin/MCP inventory and assignment | Built-in tool/shell/network controls; scope enforcement across channels |
| Scoped Notion MCP | Example of resource-bound operations | General source IDs and grant checking for knowledge access |

Current capability policy applies to Codex; other adapters must report unsupported
controls honestly. Current Codex defaults allow broad filesystem access, and Claude
uses bypassPermissions. A Dashboard toggle must not imply an OS boundary that these
runtime settings do not provide. See [audit](documentation-audit.md).

## 3. Dashboard model

Keep one agent selector and show three panels for that agent:

### Memory

List each memory store/projection with owner, last update, last accepted revision,
source pointers, size and publish status. Controls have distinct meanings:

- **Use in new turns**: permitted to recall this memory.
- **Record new memory**: owner may persist additional memory.
- **Retain/archive**: storage policy, independent of recall and recording.
- **Share projection**: selected agents can read a published snapshot; they cannot
  modify the owner's memory through this grant.

Disabling recall does not delete history. Disabling recording does not erase old
memory. Deleting stored memory is a distinct operation. Mandatory role/project
rules remain active when optional memory recall is off.

Prefer selectable stores over arbitrary toggles for every internal file. Initial
stores can reuse current progress/current manifest/overview; do not introduce a
second memory database containing the same technical state.

### Knowledge

A project catalog lists sources, and agent-specific bindings select what is available.
Each binding shows source owner/path, version/hash, load mode, freshness state and
last use. Start with explicit text files/documents and bounded excerpts.

- `disabled`: not available through the managed knowledge surface.
- `on_demand`: list metadata/pointers; retrieve authorized excerpts when needed.
- `startup`: inject a bounded excerpt on new provider context.
- `each_turn`: reload small volatile references where freshness requires it.

Access defaults to read-only. Updating an owned artifact requires an independent
write grant in Tools; granting it as knowledge never confers write permission.
Full histories/PDFs are not automatically loaded merely because a source is enabled.
A PDF can be an assigned source, but extraction must retain source provenance and
known extractor limitations; no claim that the current extractor is verified evidence.

Knowledge freshness and access are separate. A source can be readable but stale.
Support a selected revision/hash and an explicit refresh policy. Retrieval results
carry source ID, content hash/revision and excerpt location so claims can be traced.

### Tools / Capabilities

Retain the existing per-agent catalog and toggles, with separate groups for Skills,
MCP tools and built-in operations. Show effective policy, origin (project/agent),
application status and the scope where each operation is available.

Examples: file-read grant within assigned source roots; file-write grant within
owner outputs; named MCP read operation under one project subtree; dispatch to
approved direct children. A broad shell tool cannot honestly be displayed as
confined to those grants without a runtime boundary that enforces them.

Represent `allowed`, `disabled`, `unsupported` and `pending application` separately.
Distinguish disabling a capability for one agent from the existing global inventory
Delete. Include an explanation of effective dependency state: a skill can be enabled
while a needed tool remains disabled.

## 4. Policy contract (proposed, not accepted by existing APIs yet)

Policy has a monotonically increasing revision. Stable resource IDs are separate
from paths and display names. An illustrative resolved policy:

```json
{
  "schema_version": 1,
  "agent_id": "MODEL",
  "revision": 7,
  "memory": {
    "own_recall": true,
    "own_record": true,
    "shared_projection_ids": ["routing:BOSS"],
    "recent_activity_days": 2
  },
  "knowledge": [
    {"source_id": "project:architecture", "mode": "startup", "revision_policy": "pinned"},
    {"source_id": "dataset:contract", "mode": "on_demand", "revision_policy": "current"}
  ],
  "tools": {
    "capability_policy_revision": 3,
    "file_read_roots": ["MODEL", "assigned_sources"],
    "file_write_roots": ["MODEL/outputs", "MODEL/state"],
    "dispatch_targets": [],
    "network_policy": "managed_tools_only"
  },
  "required_enforcement": "managed"
}
```

The last fields are a design contract, not syntax supported by Codex/Claude config.
An adapter must translate only supported fields and report the rest. `managed`
means AgentUI-managed context/retrieval/tool surfaces; it must not be represented
as a complete filesystem/network denial. `isolated` would additionally require
restricted filesystem mounts, secrets/network and execution paths supported by the
chosen runtime; refuse that mode when unavailable rather than silently downgrading.

Source catalog metadata: `source_id`, type, project, owner, canonical locator,
content revision/hash, status, extraction provenance. Per-agent grant metadata:
load mode, selected revision/freshness policy, excerpt budget and enabled state.
The catalog stores pointers/metadata; preserve the underlying source as canonical.

## 5. One policy-controlled turn

1. Resolve the agent, mandatory instructions, resource bindings and tool policy.
   Compute an immutable per-turn policy snapshot/revision before constructing context.
2. Decide whether the saved provider thread and compact seed are compatible with
   that revision. Clear incompatible reusable context before any new injection.
3. Build a context manifest: mandatory instructions, allowed memory/projections,
   startup/each-turn knowledge and available tool/skill metadata, each with provenance.
4. Start the adapter with the compiled tool configuration. Managed retrieval checks
   agent/project identity and current grants before returning any excerpt.
5. Record actual memory reads, knowledge retrievals and tool outcomes against the
   turn/policy revision. Do not equate configured availability with actual usage.
6. Persist owner memory only if recording is allowed. Reconciliation verifies
   hashes/pointers, updates derived projections and preserves audit history.

Primary turns, dispatched workers, continuations, compaction, reconciliation,
scheduler fires and Telegram must all use this same policy resolver. An internal
model call is not an exemption from the agent's grants.

Example: MODEL needs a dataset schema. It receives its own current state, retrieves
only the assigned DATASET contract, then writes only its owned output via an allowed
operation. A blocked tool is reported explicitly; enabling the relevant skill alone
does not satisfy the missing tool grant. The worker publishes its output contract;
BOSS gets the authorized routing/result projection instead of unrestricted worker memory.

## 6. Changes, inheritance and revocation

- Resolve project defaults plus agent overrides; project hard exclusions cannot be
  re-enabled by agent-level configuration. Document this precedence in the UI.
- Existing graph-derived child-overview/result visibility becomes explicit coordination
  grants. Migration preserves it until the operator changes policy; do not silently
  deprive existing orchestrators of their routing context.
- Reject policy updates while the affected agent is active, including dispatched
  worker sessions. Existing root-only `_active_run` checks are insufficient here.
  A later queued-change flow can apply at a defined boundary and show pending status.
- Revocation invalidates provider resume, compact seeds and reusable retrieval caches
  that contain removed sources; default to a fresh context assembled from allowed material.
  Merely resetting a UI checkbox or only detaching the provider ID is insufficient.
- Already-derived summaries/outputs may contain information from a revoked source.
  Do not promise retroactive forgetting; dependency/provenance review is separate
  from blocking future managed reads. Running external work needs explicit stop handling.
- Snapshot revision describes the turn; a newly revoked grant must also be checked
  at a managed operation boundary when immediate revocation is supported.

## 7. Enforcement boundary

| Control | Required enforcement point |
|---|---|
| Which content is initially supplied | Shared context builder before every model entry point |
| Which source can be retrieved | Server-side source grant check before file/search/page read |
| Which named tool is exposed | Adapter/runtime tool config |
| Which operation/path/destination is permitted | Tool handler and runtime boundary |
| Whether alternate shell/network paths can bypass grants | Sandboxed execution/filesystem/network/credential restrictions |
| Who can write memory | Owner identity check, allowed store/path and validated write operation |
| Whether a memory claim is true | Evidence/reviewer process; structural receipt alone cannot establish this |

Retrieval/search must filter on grants **before** ranking/returning excerpts. Validate
canonical paths/symlinks and scope server-side; UI-hidden sources are not access control.
Do not expose broad service tokens to a shell if the intended policy requires all
service access to go through a scoped MCP handler. Dashboard deployment/access control
is also a prerequisite for multiple users; the current app is a personal localhost tool.

## 8. Implementation sequence

1. **Inventory and explanation:** expose Memory and Knowledge alongside existing
   Capabilities, showing current sources, injection points, owners and actual enforcement.
   This can first be read-only, avoiding an on/off UI without an applying backend.
2. **Shared policy/context implementation:** add source catalog and per-agent grants,
   revision persistence, resolver and per-turn context manifest. Move scattered injection
   through the resolver; preserve existing behavior until an agent explicitly opts in.
3. **Apply managed controls:** implement scoped file retrieval and memory operations,
   apply allowed source sets to every model entry point and rotate incompatible context.
   Add mutation controls only with these working backend paths.
4. **Stronger runtime isolation:** inventory built-in shell/file/network surfaces,
   choose adapter-supported boundaries, test bypass attempts, expose supported modes.
5. **Additional knowledge backends:** Notion/read retrieval and later search/RAG reuse
   the same source/grant model. A vector index is optional and does not replace grants.

Initial implementation target should be one explicitly selected agent/project and
one supported adapter, not an automatic migration of all registered projects.

## 9. Acceptance criteria

- One agent cannot retrieve another agent's unassigned memory or source through managed APIs.
- Enabled skill plus disabled tool cannot implicitly reactivate that tool.
- Memory recall, recording and retention controls produce distinguishable behavior.
- Denied knowledge never reaches initial context or managed retrieval, including
  continuation, compact/reconciliation, dispatched and scheduled model calls.
- Policy revision changes prevent incompatible resume/seed/cache reuse; UI matches
  the revision actually used by a completed turn.
- In-flight workers are recognized during policy edits; concurrent updates cannot
  silently overwrite a newer revision.
- Missing or changed pinned source yields an explicit stale/unavailable result,
  not a silent substitution. Retrieved reference content cannot modify policy.
- Symlink/path traversal and alternate tool paths are tested for the advertised
  enforcement mode. Unsupported isolation is shown as unsupported.
- Existing opt-out agents retain their current context/tool behavior through migration.
