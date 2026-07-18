# TEMPLATE_AGENT — canonical agent folder (slim boot, AEC architecture)

This is the **standard reference layout** for a single agent in an AgentUI
project. It encodes the architecture proven in `AECPlayGround-AGENT` after the
2026-07 boot-cost refactor: one boot-list, slim reads only, heavy artifacts
opened on-demand, and progress trimmed by the control-plane (not re-read by the
agent).

Copy this folder as `<AGENT_ID>/`, replace the `<PLACEHOLDER>` tokens, and wire
it into `.agentui/project.yaml`. Machine files (the ones the control-plane reads
and stamps) must keep their exact paths — see the contract below.

## Files (the control-plane reads/stamps these — keep the paths)

| Path | Owner | Purpose |
|---|---|---|
| `AGENT.md` | you (soft prose) | system prompt: identity, boot-list, boundaries. **Keep < 80 lines, no routing tables** (dispatch targets are injected at runtime from the live graph). |
| `overview.md` | you (BODY) + control-plane (HEADER/FOOTER) | slim state pane the parent reads to route; version echoes the manifest. |
| `inputs/manifest.md` | control-plane (`sync.sh`) | pinned producer versions — the tiny roll-up. |
| `outputs/manifest.md` | you | contract you publish: version + artifact paths (never artifact bodies). |
| `state/progress.md` | you (append) | append-only log. Control-plane rotates it into `progress.json` and auto-injects the recent slice — **you never read the full file at boot**. |
| `context/code_map.md` | you | owned files + read-only references. |

## The 4 boot rules (why this template exists)

1. **ONE boot-list.** No second "Required reads" section. Duplicated/contradicting
   lists are what bloated the old projects.
2. **Boot reads only slim things** — shared rules, own `AGENT.md`, own
   `overview.md`, and the tiny `inputs/manifest.md` (version pins). A parent reads
   `state/children_status.json` (the derived rollup) instead of each child's overview.
3. **Producer manifest blobs are on-demand**, never at boot. Open
   `inputs/<PRODUCER>.md` (or the artifact it points to) only when actually
   consuming/auditing that artifact.
4. **Never read `state/progress.md` at boot.** The control-plane injects the
   recent slice (`progress.json`, newest ~2 days) into the cold-start preamble.
   Reading the full log defeats the trim. You only **append** to it.

Version tracing: `overview.md`'s `manifest_version` is stamped by the
control-plane to equal `outputs/manifest.md`'s `## Version` — same version, no
separate model.
