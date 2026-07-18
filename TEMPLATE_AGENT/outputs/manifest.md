# Manifest — <AGENT_ID>

## Version
0.1.0
<!-- Bump rule: schema/contract change → MAJOR · +artifact same schema → MINOR ·
     metadata/typo/path move → PATCH. No silent drift. -->

## Last updated
<YYYY-MM-DD> by <AGENT_ID>

## Artifacts

### <artifact-name>
- **Path**: absolute, under <AGENT_ID>/outputs/...
- **Format**: json | jsonl | csv | md | ...
- **Schema**: link to the schema/definition (file:line or plan §)
- **Version**: artifact semver (if it drifts from the manifest version)
- **Source**: file:line where the artifact / its schema is defined
- **Status**: ready | partial | deprecated
- **Notes**: record count; `[VERIFY]` if any value unconfirmed

## Removed/Deprecated
<!-- append-only: an artifact that is dropped MOVES here with a reason, never silently erased -->
