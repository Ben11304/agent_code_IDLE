# Handoff Schema — Manifest format (shared)

Manifest là **contract** giữa các agent. Mọi artifact đi qua manifest, không
agent nào được đọc internals của agent khác.

## File location
- Producer: `<PRODUCER>/outputs/manifest.md`
- Consumer: đọc qua `<CONSUMER>/inputs/manifest.md` (+ copy `inputs/<PRODUCER>.md`)
- Sync bằng `bash sync.sh <CONSUMER>` (xem `sync.sh` ở project root).

Topology (ai produce cho ai) suy ra từ `parents` trong `.agentui/project.yaml`
và được chốt trong `scope_decisions.md`.

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
- **Path**: absolute hoặc repo-relative
- **Format**: py-module | parquet | json | yaml | csv | md | ...
- **Schema**: link tới ABC / pydantic model / parquet schema
- **Source**: file:line nơi định nghĩa
- **Status**: ready | partial | deprecated
- **Notes**: edge case, [VERIFY] nếu chưa kiểm

## Removed/Deprecated
- <artifact-name> @ <version> — lý do
```

## Quy tắc

1. **Append-only thực tế**: artifact bỏ → chuyển section "Removed/Deprecated"
   thay vì xoá entry (giữ lịch sử).
2. **Bump version mỗi commit có thay đổi contract**. Consumer kiểm version
   trước khi chạy.
3. **Schema/contract mismatch** → consumer DỪNG, escalate producer. KHÔNG tự
   fix downstream.
4. Manifest CHỈ trỏ đường (path + version), KHÔNG copy data/code.
5. Khi bump **major** → ping user để consumer được trigger sync.
