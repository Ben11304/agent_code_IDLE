# Agent Idle capability inventory

This directory is the canonical, application-owned inventory for capabilities
that Agent Idle can assign to individual agents. Runtime discovery must not
crawl arbitrary project, home, or cache directories.

- `inventory.json`: application-owned snapshot index used by the dashboard/harness.
  Import and explicit global Delete update it; it is not an immutable file.
- `skills/<source>--<name>/`: self-contained standalone skill packages.
- `plugins/<name>--<marketplace>/`: complete imported plugin packages,
  including their skills and shared assets/scripts.
- `mcp/<server>/manifest.json`: secret-free MCP launch metadata and tool schema
  snapshot. Some local MCP implementations are vendored beside their manifests; remote
  services retain launch metadata rather than a copied service.
- Enabling a skill adds only this inventory root to that agent's ephemeral
  Codex SDK process. It does not mutate `~/.codex` or another agent's runtime.
- Enabling an MCP server compiles its `launch` block and tool deny-list into
  that agent's ephemeral Codex SDK configuration. Local implementation paths
  are resolved back into this directory; remote servers retain only their URL.
- Dashboard deletion is global: it removes the indexed package directory,
  purges stale per-agent overrides, resets bound Codex runtimes, and records a
  tombstone in `inventory.json` so the maintenance importer does not silently
  restore the deleted capability.
- Keep third-party license and provenance files beside imported skills.

Downloaded repositories under an agent's `context/` directory are not scanned
automatically. Import reviewed capabilities here before exposing them. The
explicit maintenance command is:

```bash
app/.venv/bin/python app/capability_inventory/import_current.py
```

## Runtime behavior and audit scope

Checked against `backend/capabilities.py` on 2026-09-07. `discover_catalog` reads
this index and referenced manifests; `force=true` refreshes the snapshot/cache,
not a live crawl of arbitrary home/runtime directories. The maintenance importer
is the explicit path that discovers external capabilities and copies them here.
It is a mutation, not a dashboard refresh or read-only verification command.

Policy enforcement is currently **Codex-only**. A policy change clears the saved
provider identity while keeping AgentUI chat/memory. Missing package paths are
reported in catalog errors. Snapshot `auth_status` and tool schemas are inventory
metadata, not proof of current remote availability or valid credentials.

Skill-use tracking begins prospectively: an explicit `SkillInput` or completed
read of the owned `SKILL.md` is counted once per provider turn, with no inferred
historical backfill. It proves invocation/read, not fulfillment of the skill.

`skills/`, `plugins/`, and `mcp/*/implementation/` are imported packages with their
own provenance. Maintain upstream instruction/license files through a deliberate
import/update; do not rewrite them as though they were AgentUI operating docs.
See the [documentation audit](../../docs/documentation-audit.md).
