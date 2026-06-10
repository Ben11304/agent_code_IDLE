# Tool Conventions (shared)

Quy ước công cụ chung cho mọi agent trong dự án. Đọc ở pre-flight.

## RTK — token-optimized CLI (đọc/khám phá file)

Khi thao tác read-only trên filesystem, ưu tiên `rtk` thay cho lệnh thô
(tiết kiệm token, output gọn):

```bash
rtk ls <path>          # thay ls / ls -la
rtk read <file>        # thay cat / head / tail
rtk grep <pat> <path>  # thay grep -n
rtk find <path> ...    # thay find (KHÔNG hỗ trợ -not/-exec → dùng find thô khi cần)
rtk git status         # thay git status
rtk git diff           # thay git diff
rtk git log            # thay git log
rtk wc / du / df / ps / tree
```

**Cho phép lệnh thô** khi rtk không cover: pipe phức tạp `a | b | c`,
compound `&&`/`||`, redirect stderr cần exact, SLURM
(`sbatch`/`squeue`/`sacct`/`scancel`/`scontrol`), `module load`,
write ops (`mkdir`/`chmod`/`rm`/`mv`), `python -c` 1-shot, env exports.

## AAS — Grok delegate (research / verify / survey)

`aas` là CLI dùng Grok cho task Grok làm tốt hơn Claude: realtime web,
fresh-eyes review, adversarial red-team, multi-source survey.

```bash
aas research "<query>"     # tìm paper, latest tech, realtime web + X
aas verify "<claim>"       # cross-check technical claim (model độc lập)
aas survey "<topic>"       # 15-30 source literature survey
aas redteam <path-or-desc> # adversarial critique design/spec
aas review <path>          # code review từ góc nhìn khác
aas reason "<question>"    # STEM/math hard reasoning (không cần web)
aas ask "<prompt>"         # free-form, khi không fit cmd nào trên
```

Khi nào dùng: tìm paper/DOI, verify claim kỹ thuật, research latest, critique spec.
Khi nào KHÔNG: thao tác file/code (Read/Edit/Bash), chạy experiment (Bash+SLURM).
`aas` lỗi → chạy `aas doctor`.

## Git / filesystem an toàn (no cross-scope destructive ops)

Agent **KHÔNG** chạy lệnh có thể wipe working files của agent khác:
`git checkout <branch>`, `git stash`, `git clean`, `git reset --hard`,
`git restore .`, `git rm -rf`, `rm -rf <ngoài scope>`. Cần các thao tác này →
**DỪNG, escalate user**. Lý do: branch/reset op không git-aware về scope.
