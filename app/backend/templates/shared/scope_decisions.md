# Scope Decisions (shared, frozen)

Closed scope decisions. Subagents must NOT overturn them on their own. To overturn → escalate to the user.

## {{date}} — Agent system initialized for {{project_name}}
- Agents: {{agent_ids_csv}}.
- Each agent is a **self-contained context boundary**. Communication happens only via manifests
  (`<producer>/outputs/manifest.md` ↔ `<consumer>/inputs/manifest.md`).
- Do NOT read each other's internals. Found an issue outside your scope → escalate.
- Topology (parent-child) is declared in `.agentui/project.yaml` field `parents`.

> Whenever a new scope decision is closed (splitting an agent, transferring ownership, locking
> a vertical slice...), PREPEND a `## YYYY-MM-DD — <headline>` entry here with the
> reasoning + the new boundaries. This is the project-wide scope memory.

## Boundaries between agents
- (Fill in once the topology is locked: what agent X owns, what it must NOT touch.)

## Authoritative names
See `glossary.md`. Avoid confusing dataset/model/task/module names.

## Frozen rules (no overturning without escalation)
1. **No orphan artifact**: an agent must NOT create a new code / spec / config file if
   the corresponding contract is not yet pinned in that agent's own `inputs/manifest.md`.
   A task needing a missing contract → escalate to the **producer agent** first; do NOT
   patch it inside your own agent.
2. **Mandatory pre-flight sync**: every session, a consumer agent runs the pre-flight
   (the PRE-FLIGHT block at the top of `AGENT.md`) to verify `inputs/manifest.md` matches the
   producer's version before acting. Mismatch → sync or escalate, do NOT bypass.
3. **No cross-scope destructive git/fs ops**: no `git checkout/stash/clean/
   reset --hard/restore .`, `git rm -rf`, `rm -rf <outside scope>`. If needed → escalate
   to the user (see `tool_conventions.md`).
4. **No mocking / fabricating** data, predictions, or GT to make "the pipeline run".
   If blocked, stay blocked and escalate.
5. (Add project-specific frozen rules here as needed.)
