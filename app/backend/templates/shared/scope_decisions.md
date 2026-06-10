# Scope Decisions (shared, frozen)

Các quyết định scope đã đóng. Subagent KHÔNG được tự lật. Lật → escalate user.

## {{date}} — Khởi tạo agent system cho {{project_name}}
- Các agent: {{agent_ids_csv}}.
- Mỗi agent là **context boundary tự đủ**. Giao tiếp duy nhất qua manifest
  (`<producer>/outputs/manifest.md` ↔ `<consumer>/inputs/manifest.md`).
- KHÔNG đọc internals của nhau. Phát hiện vấn đề ngoài scope → escalate.
- Topology (cha-con) khai trong `.agentui/project.yaml` field `parents`.

> Mỗi khi đóng một quyết định scope mới (tách agent, chuyển ownership, lock
> vertical slice...), PREPEND một mục `## YYYY-MM-DD — <headline>` vào đây kèm
> lý do + ranh giới mới. Đây là bộ nhớ scope của cả dự án.

## Boundaries giữa các agent
- (Điền khi topology chốt: agent X sở hữu gì, KHÔNG đụng gì.)

## Authoritative names
Xem `glossary.md`. Tránh nhầm tên dataset/model/task/module.

## Frozen rules (không lật không escalate)
1. **No orphan artifact**: agent KHÔNG tạo file code / spec / config mới nếu
   contract tương ứng chưa được pin trong `inputs/manifest.md` của chính agent
   đó. Task cần contract chưa có → escalate **producer agent** trước, KHÔNG
   patch tại agent của mình.
2. **Pre-flight sync bắt buộc**: mỗi session, agent consumer chạy pre-flight
   (block PRE-FLIGHT đầu `AGENT.md`) để verify `inputs/manifest.md` version khớp
   producer trước khi action. Mismatch → sync hoặc escalate, KHÔNG bypass.
3. **No cross-scope destructive git/fs ops**: KHÔNG `git checkout/stash/clean/
   reset --hard/restore .`, `git rm -rf`, `rm -rf <ngoài scope>`. Cần → escalate
   user (xem `tool_conventions.md`).
4. **Không mock / fabricate** data, prediction, GT để "pipeline chạy được".
   Block thì block, escalate.
5. (Thêm frozen rule đặc thù dự án ở đây khi cần.)
