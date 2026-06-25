# Tool Conventions (shared)

Common tool conventions for every agent in the project. Read at pre-flight.

## RTK — token-optimized CLI (file reading/exploration)

For read-only filesystem operations, prefer `rtk` over raw commands
(saves tokens, compact output):

```bash
rtk ls <path>          # instead of ls / ls -la
rtk read <file>        # instead of cat / head / tail
rtk grep <pat> <path>  # instead of grep -n
rtk find <path> ...    # instead of find (does NOT support -not/-exec → use raw find when needed)
rtk git status         # instead of git status
rtk git diff           # instead of git diff
rtk git log            # instead of git log
rtk wc / du / df / ps / tree
```

**Raw commands are allowed** when rtk does not cover them: complex pipes `a | b | c`,
compound `&&`/`||`, exact stderr redirects, SLURM
(`sbatch`/`squeue`/`sacct`/`scancel`/`scontrol`), `module load`,
write ops (`mkdir`/`chmod`/`rm`/`mv`), one-shot `python -c`, env exports.

## Consensus MCP — academic paper search (peer-reviewed)

Search 200M+ peer-reviewed papers (Semantic Scholar, PubMed, Scopus, ArXiv) via the
`mcp__consensus__search` MCP tool. Call it directly as a tool — no CLI needed.

Key parameters (all optional except `query`):
- `query` — use academic terminology, be specific
- `year_min` / `year_max` — only when user explicitly wants a date range
- `exclude_preprints: true` — only when user asks for peer-reviewed only
- Do NOT set `domain`, `study_types`, or other filters unless user explicitly requests them

When to use: finding papers/DOIs, literature survey, verifying a paper exists,
  reviewing state-of-the-art on a topic.
When NOT to use: general web/realtime news, code-level questions, non-academic claims.

For general web research: use `WebSearch` (already permitted in settings).
For adversarial review / code critique / hard reasoning: use Claude's built-in
  capabilities (Read + analysis — no external tool needed).

## Git / filesystem safety (no cross-scope destructive ops)

Agents must **NOT** run commands that can wipe another agent's working files:
`git checkout <branch>`, `git stash`, `git clean`, `git reset --hard`,
`git restore .`, `git rm -rf`, `rm -rf <outside scope>`. If these operations are needed →
**STOP, escalate to the user**. Reason: branch/reset ops are not scope-aware.
