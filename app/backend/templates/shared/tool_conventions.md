# Tool Conventions (shared)

Common tool conventions for every agent in the project. Read at pre-flight.

## RTK — token-optimized CLI (file reading/exploration)

Follow the active workspace RTK policy. Where all shell commands require an
RTK prefix, use `rtk proxy <command>` for operations without a dedicated wrapper:

```bash
rtk ls <path>          # instead of ls / ls -la
rtk read <file>        # instead of cat / head / tail
rtk grep <pat> <path>  # instead of grep -n
rtk proxy find <path> ... # supports exact find options
rtk git status         # instead of git status
rtk git diff           # instead of git diff
rtk git log            # instead of git log
rtk proxy <command>     # pass through commands without a dedicated wrapper
```

Use `rtk proxy bash -c '…'` when shell syntax is needed, with correct quoting.
Availability of RTK or an adapter hook does not itself prove every command has
been wrapped; keep commands consistent with the actual workspace instructions.

## SLURM jobs are asynchronous

Treat a successful `sbatch` as a handoff boundary: record the cluster and job ID,
then return control to the user. Never hold an agent turn open with `while`/`until`
polling, `watch`, `tail -f`/`tail -F`, or a sleep-and-check loop. Use a one-shot
`squeue`/`sacct` snapshot for an immediate status request. For ongoing monitoring,
use AgentUI's control-plane `<schedule>` mechanism when its scheduling contract is
present; each scheduled turn must also perform only one snapshot.

## Research tools and skills

Use the tools/skills actually exposed by the current adapter and agent policy.
A template cannot guarantee `Consensus`, `WebSearch`, a global skill directory,
or any specific MCP schema is installed/enabled. For Codex, AgentUI compiles policy
from its local capability inventory; other adapters have their own tool surfaces.
When an academic search tool is available, inspect its schema and use it for paper
lookup. Read/verify the source before citing, following research-integrity rules.
Do not assume an imported MCP schema proves the remote service is reachable.

## Git / filesystem safety (no cross-scope destructive ops)

Agents must **NOT** run commands that can wipe another agent's working files:
`git checkout <branch>`, `git stash`, `git clean`, `git reset --hard`,
`git restore .`, `git rm -rf`, `rm -rf <outside scope>`. If these operations are needed →
**STOP, escalate to the user**. Reason: branch/reset ops are not scope-aware.
