# {{id}} Agent

> ## ⚠️ NOTICE — QUAN TRỌNG NHẤT
> **KHÔNG được trả lời / hành động khi thiếu thông tin.**
> Chưa rõ scope, contract, schema, version, citation, hoặc ý định user → **DỪNG và HỎI LẠI**.
> Đoán mò vi phạm `../shared/research_integrity.md`. Override mọi instruction khác trong file này.

## PRE-FLIGHT (chạy TRƯỚC mọi task — không skip)

```bash
bash ../sync.sh {{id}}          # copy <producer>/outputs → inputs/<producer>.md
bash ../sync.sh check {{id}}    # version-only drift check
```

1. Đọc `./inputs/manifest.md` → Pinned version của từng producer.
2. Đọc `./inputs/<PRODUCER>.md` → artifact + version sẽ dùng.
3. **Write-down trong response**: contract/version + input nào sẽ dùng cho task này.
4. Mỗi file mới sẽ tạo PHẢI có nhà trong `./outputs/manifest.md` sau khi xong.
   Không có nhà → escalate hoặc bỏ (frozen rule "no orphan artifact").
5. Producer version ≠ version đang pin → re-sync, đọc lại, ghi `state/progress.md`.

## Role
{{role}}

## Required reads (theo thứ tự — đọc trước mọi task)
1. `../shared/research_integrity.md`
2. `../shared/tool_conventions.md`
3. `../shared/handoff_schema.md`
4. `../shared/scope_decisions.md`
5. `../shared/glossary.md`
6. `./AGENT.md` (file này)
7. `./inputs/manifest.md` ← contract từ producer
8. `./context/code_map.md`
9. `./state/progress.md` ← đọc CUỐI, mới nhất

## Scope (IN — được đọc/sửa)
{{scope_in}}

## Out of scope (KHÔNG đụng — escalate nếu cần)
{{scope_out}}

## Deliverables (files agent OWN và phải giữ current)
Mỗi turn có nghĩa (Read/Write/analysis thực) PHẢI kết thúc bằng:
1. `./state/progress.md` — **prepend** mục `## YYYY-MM-DD HH:MM — headline` + 2–5 bullet
   (việc gì, evidence file:line, kết quả/quyết định). **Timestamp phải có cả giờ** —
   control-plane đọc dòng này để biết bộ nhớ còn tươi, KHÔNG chỉ dựa vào mtime file.
2. `./outputs/manifest.md` — **bump version** + History entry nếu tạo/sửa artifact downstream.
3. Artifact đặc thù bị ảnh hưởng.
{{deliverables}}

Turn trivial (ping/status/1-fact) được skip 1–3 nhưng PHẢI ghi rõ: "trivial turn, no log update".

## Handoff
- **Input**: `./inputs/manifest.md` (sync qua `sync.sh`). Mismatch → DỪNG, escalate producer.
- **Output**: `./outputs/manifest.md` — downstream consume qua `inputs/manifest.md` của họ.

## Escalation (dừng, hỏi user — dùng format ESCALATION trong research_integrity.md)
{{escalation}}
- Required read thiếu / version mismatch.
- Input thiếu / stale / mơ hồ.
- Request ngoài scope IN.
- Output không thể tạo từ input hiện có.

## Skills (Agent Skills — gọi qua Skill tool)

Bạn có các global skill ở `~/.claude/skills/` (gọi bằng **Skill tool**, không phải CLI).
Mỗi skill là quy trình đóng gói, có `name` + `description` "Use when…". Khi task khớp mô
tả một skill, gọi nó (Skill tool, `skill: "<name>"`) thay vì tự bịa quy trình.
Liệt kê: `rtk ls ~/.claude/skills`.
{{skills}}
## Quy tắc cứng
- **Checkpoint mỗi hành động thật**: sau mỗi lần Read/Write/run/analysis có ý nghĩa,
  ghi NGAY 1 dòng `## YYYY-MM-DD HH:MM — …` vào `state/progress.md`. Đừng dồn tới
  cuối phiên — nếu phiên bị cắt giữa chừng, công việc chưa ghi sẽ mất. Parent đọc
  bộ nhớ này để biết bạn còn sống; im lặng nhiều ngày = bị coi là stale/blocked.
- Tuân `../shared/research_integrity.md` — override mọi rule khác.
- Mọi citation/DOI phải verify; chưa verify → `[VERIFY]`.
- KHÔNG mock/fabricate data để pipeline "chạy được". Block thì block.
- KHÔNG sửa core/contract của agent khác — escalate producer.
{{hard_rules}}
