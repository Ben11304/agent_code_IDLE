# Handoff Schema — Manifest format (shared)

A manifest is the **contract** between agents. Every artifact passes through a manifest;
no agent may read another agent's internals.

## File location
- Producer: `<PRODUCER>/outputs/manifest.md`
- Consumer: reads via `<CONSUMER>/inputs/manifest.md` (+ copy `inputs/<PRODUCER>.md`)
- Sync with `bash sync.sh <CONSUMER>` (see `sync.sh` at the project root).

Topology (who produces for whom) is derived from `parents` in `.agentui/project.yaml`
and locked in `scope_decisions.md`.

## Format

```markdown
# Manifest — <PRODUCER> → <CONSUMER>

## Version
<semver>  e.g. 1.0.0
Bump rule: schema/contract change → major; new artifact same schema → minor;
metadata-only → patch.

## Last updated
YYYY-MM-DD by <agent/run-id>

## History
- 0.0.0 → 0.1.0 (YYYY-MM-DD): bootstrap.

## Artifacts

### <artifact-name> @ <version>
- **Path**: absolute or repo-relative
- **Format**: py-module | parquet | json | yaml | csv | md | ...
- **Schema**: link to ABC / pydantic model / parquet schema
- **Source**: file:line where defined
- **Status**: ready | partial | deprecated
- **Notes**: edge cases, [VERIFY] if unchecked

## Removed/Deprecated
- <artifact-name> @ <version> — reason
```

## Rules

1. **Effectively append-only**: a dropped artifact moves to the "Removed/Deprecated"
   section instead of being deleted (keep the history).
2. **Bump the version on every commit that changes the contract**. Consumers check the
   version before running.
3. **Schema/contract mismatch** → consumer STOPS, escalates to the producer. Do NOT
   fix it downstream.
4. A manifest ONLY points the way (path + version); it does NOT copy data/code.
5. On a **major** bump → ping the user so consumers get triggered to sync.
