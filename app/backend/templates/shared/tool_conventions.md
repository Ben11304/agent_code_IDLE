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

## AAS — Grok delegate (research / verify / survey)

`aas` is a CLI that uses Grok for tasks Grok does better than Claude: realtime web,
fresh-eyes review, adversarial red-teaming, multi-source surveys.

```bash
aas research "<query>"     # find papers, latest tech, realtime web + X
aas verify "<claim>"       # cross-check a technical claim (independent model)
aas survey "<topic>"       # 15-30 source literature survey
aas redteam <path-or-desc> # adversarial critique of a design/spec
aas review <path>          # code review from another perspective
aas reason "<question>"    # hard STEM/math reasoning (no web needed)
aas ask "<prompt>"         # free-form, when none of the above fits
```

When to use: finding papers/DOIs, verifying technical claims, researching the latest, critiquing specs.
When NOT to use: file/code operations (Read/Edit/Bash), running experiments (Bash+SLURM).
`aas` failing → run `aas doctor`.

## Git / filesystem safety (no cross-scope destructive ops)

Agents must **NOT** run commands that can wipe another agent's working files:
`git checkout <branch>`, `git stash`, `git clean`, `git reset --hard`,
`git restore .`, `git rm -rf`, `rm -rf <outside scope>`. If these operations are needed →
**STOP, escalate to the user**. Reason: branch/reset ops are not scope-aware.
