# Meshy for Codex

Generate digital 3D assets or prepare printable models with the two included skills:
`meshy-3d-generation` and `meshy-3d-printing`. Version **0.6.0**.

## Just ask

Install the plugin, then describe the job:

> Turn this photo into a textured GLB and save it to `./assets/chest.glb`. Set it up if it
> isn't set up yet.

The skill checks for a usable `meshy` CLI, falls back to running the pinned package temporarily
(`npm exec --yes --package=meshy-cli@0.4.0 -- meshy …`), and reuses an existing Meshy session if
there is one. If there is not, it starts one browser login and shows you a verification link and
a code while the login keeps running; you approve in the browser and it carries on with the
request you made. No API key is ever pasted into the chat, and no task ID is copied by hand.

Files land where you asked — the exact path, or `./meshy_output` when you name none — with a
rendered preview and the task IDs for the next request ("a 1500-face LOD", "now as FBX",
"scale it to 150 mm"). Purely local print work (rescaling an OBJ, opening a 3MF in a slicer)
runs with no account, no balance check and no credits.

Requires Node.js 22.12+. A global install is optional and only makes startup faster:

```bash
npm install -g meshy-cli@0.4.0
```

If you would rather log in yourself, run `meshy auth login` in a desktop terminal (or
`meshy auth login --device` over SSH) and keep it running until you have approved in the
browser. An existing `MESHY_API_KEY` takes priority over the stored session.

## Installing

Install this directory as the plugin root: it contains `.codex-plugin/plugin.json` and a real
`skills/` tree. Alternatively copy either complete skill directory into `.agents/skills/`.
Printing works without the generation skill. Replace an old managed skill directory rather
than merging files, so old runtime scripts are removed; preserve your projects and CLI state.

The skills have no Python dependency and no bundled runtime scripts. Generation covers 3D and
2D, textures, rigging and animation; printing covers white and multicolor models, analysis and
repair, Creative Lab products and launching installed slicers. Costs are estimated from the
CLI's own planner or the published price list before any unapproved spend; task IDs and
downloaded files are preserved for recovery.

See [generation](skills/meshy-3d-generation/SKILL.md),
[printing](skills/meshy-3d-printing/SKILL.md), and
[shared setup details](skills/meshy-3d-generation/references/setup.md).

This package is generated from the meshy-3d-agent canonical skills and OpenAI overlay.
`provenance.json` records actual content hashes and source base revision; edit the source,
not this artifact. Runtime credentials stay in the CLI config directory, never this plugin.
Real Codex discovery, browser login and slicer UI require host verification in addition to the
isolated command tests. OpenClaw is not part of this package.

[Source and maintainer instructions](https://github.com/meshy-dev/meshy-3d-agent)
