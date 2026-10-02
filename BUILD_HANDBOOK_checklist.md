# BUILD_HANDBOOK — Checklist implementation

Đối chiếu working tree **2026-09-07**, không suy ra deployment/live test từ việc có code.
Nguồn: [handbook](BUILD_HANDBOOK.md), [audit](docs/documentation-audit.md).
Ký hiệu: ✅ có implementation · 🟡 có một phần/điều kiện · ⬜ chưa có.

| Hạng mục | Trạng thái | Evidence / giới hạn |
|---|---|---|
| Dispatch ledger + 3 continuations | ✅ | `_start_run`, `_dispatched_run`, `_format_results_as_context` |
| Detached browser runs, replay, Stop | ✅ | `_Run`, `_run_subscriber_sse`; restart vẫn mất run/buffer |
| Overview cold-start + fallback | ✅ | `_session_preamble`; input head 600/1200 chars |
| Child overviews every parent turn | ✅ | `_children_overview_context`; không chỉ cold start |
| Derived children rollup | ✅ | `_write_children_rollups`; object keyed by ID |
| Structured parse + retry | 🟡 | Một retry; absent blocks hợp lệ, lần hai không có hard reject |
| Stamp overview | 🟡 | Version/date/placeholder heuristic; không kiểm đủ role schema |
| Version pin check | 🟡 | Python best-effort + warning; không auto sync/hard block |
| Escalation state + route | ✅ | `db.open_escalation`, resolve, ledger parent đầu tiên |
| DATA/SUBTASK/TOOL auto-resolution | ⬜ | Không tự pull/dispatch/execute theo escalation type |
| HALT | 🟡 | Parse, log, ledger; evidence chỉ kiểm không rỗng |
| DISSENT | 🟡 | DB open/resolve + prompt warning; không hard veto/role enforcement |
| Verify watermark | 🟡 | Lưu claim, so version/hint; không cưỡng chế tool skip |
| Goal acceptance / budget / plateau | 🟡 | External-acceptance guidance; metric/budget/plateau checker chưa có |
| CONTINUE_SELF | ⬜ | Không có parser/resume path |
| Multi-level dispatch | ✅ | Graph children + ancestor-loop rejection |
| Global worker serialization | ⬜ | Busy guard chỉ nhìn root `_Run` |
| Owner reconciliation | ✅ | Project opt-in; receipt/hash/provenance gate |
| Progress archival | ✅ | Global toggle mặc định off; hai ngày hoạt động, explicit JSON migration |
| New project/agent bootstrap | ✅ | Sáu agent files + shared templates + paper collection |
| Bulk migration mọi project cũ | ⬜ | Không có generic overview-content migration |
| Claude/Codex/Grok/DeepSeek/GLM | ✅ | `adapters.get_stream`; auth tùy adapter |
| Capabilities | ✅ | Inventory local; per-agent policy áp dụng Codex; Delete global |
| Persistent terminal | ✅ | tmux session + transient attachment; không session TTL |
| Scheduler | ✅ | Ba mode, global toggle; không replay thời gian disabled |
| Notion System Hub/project scope/report schema | ✅ | `notion_settings`, `notion_report`; runtime config cần verify riêng |
| Offline evaluation harnesses | ✅ | BOSS/worker/reliability; kết quả remote không chạy lại trong audit |
| Plan/todo trên graph | ⬜ | Đã chủ động xóa; không phải pending implementation |

## Lịch sử pilot (không phải snapshot hiện tại)

Các bảng dưới được giữ để truy vết ghi nhận trước đây. Số corpus, phiên bản agent,
trạng thái project ngoài repo, pass counts và live Notion không được xác minh lại
trong audit tài liệu 2026-09-07. Kết quả kiểm tra của đợt này nằm trong audit.

## H1. Ghi nhận 2026-06-27 — Lớp contract/markdown — `DFU-Pipeline-AGENT/` (✅ HOÀN TẤT)

| # | Hạng mục | TT | File |
|---|---|---|---|
| A1 | `overview.md` slim cho 6 agent (header/body/footer, body THẬT từ manifest) | ✅ | `{BOSS,DATA,MODELING,ENERGY,EVAL,INTEGRITY}/overview.md` |
| A2 | Protocol chung: schema + grammar `[RESULT]`/`[ESCALATE]` + Outcome/Pointer + 3 goal + stopping | ✅ | `shared/overview_protocol.md` |
| A3 | Phase 4 (REFLECT & SNAPSHOT) + dimension theo role | ✅ | 5 child `*/AGENT.md` |
| A4 | BOSS PRE-FLIGHT overview-first + LOOP 4-phase | ✅ | `BOSS/AGENT.md` |
| A5 | BOSS dispatch Outcome/Pointer + 3 goal + stopping + cấm chế-độ-3 | ✅ | `BOSS/AGENT.md` |
| A6 | BOSS route `[ESCALATE]` theo TYPE | ✅ | `BOSS/AGENT.md` |
| A7 | Static routing tier `team_role.md` | ✅ | `BOSS/context/team_role.md` |
| A8 | README wire reads + section trạng thái + caveat control-plane | ✅ | `README.md` |

> Bodies = state THẬT, không bịa: ENERGY 🔴 0 measurements · MODELING `student_registry=[]` = critical path ·
> INTEGRITY pins stale (MODELING 1.18→1.20, EVAL 0.19→0.20.4).

---

## H2. Ghi nhận 2026-08-07 — chuẩn tài nguyên dự án — cập nhật 2026-08-07

| # | Hạng mục | TT | Ghi chú |
|---|---|---|---|
| F1 | Mỗi agent-system mới có `paper_collection/` | ✅ | Scaffold tự sinh `README.md` + `CATALOG.md` |
| F2 | `.agentui/project.yaml` khai báo `papers.roots` + `papers.catalogs` | ✅ | PDF local và metadata-only cùng xuất hiện trên panel |
| F3 | Ghép PDF với catalog bằng `ShortID` đầu filename | ✅ | Ví dụ `Yuan2017_....pdf` ↔ `**Yuan2017**` |
| F4 | GELSIGHT migration | ✅ | 62 catalog entries; 3 local PDFs trong `GELSIGHT-AGENT/paper_collection/` |
| F5 | Parent pre-flight đọc overview của mọi direct child mỗi turn | ✅ | Nội dung được inject thật vào prompt; UI phát vàng trên overview của child (tối thiểu 2.4 giây) |

---

## H3. Ghi nhận 2026-08-16 — Notion reporting cấp project — cập nhật 2026-08-16

| # | Hạng mục | TT | Ghi chú |
|---|---|---|---|
| G1 | Một binding Notion ở root dùng chung cho toàn project | ✅ | Worker resolve/inherit, không cần destination record riêng |
| G2 | Canonical project/agent scope do backend inject qua MCP argv | ✅ | Model không còn tự gửi hoặc đoán slug/agent ID |
| G3 | Một server token, secret không lưu trong destination registry/argv | ✅ | `env_vars` + scoped `.env.local` fallback cho Codex resume |
| G4 | Worker thấy inherited Notion status nhưng không được rebind | ✅ | API trả `destination_owner_agent_id` + `inherited` |
| G5 | Exact-child create/append + read-back | ✅ | Create cần `create_if_missing=true`; retry identical không duplicate |
| G6 | Ambiguous/missing project destination fail closed | ✅ | Không workspace-root write, không tự chọn parent |
| G7 | System Hub bind một lần cho toàn AgentUI | ✅ | Dashboard có UI + API `/api/notion-system-settings` |
| G8 | Project mới auto-provision dưới Hub | ✅ | Exact child `<name> [<slug>]`, root-owned destination, inter-process lock |
| G9 | Inventory/read toàn project subtree | ✅ | Page ngoài subtree bị reject; read trả content SHA-256 |
| G10 | Managed report sync không đè manual content | ✅ | Dry-run mặc định; chỉ replace marker range; read-back bắt buộc |
| G11 | Agent không còn yêu cầu cài Notion plugin | ✅ | Control-plane system prompt + raw/global Notion MCP bị disable |
| G12 | Live GelSight read-only pilot | ✅ | 7 pages; `propose` 7 blocks; không truncate, không write |
| G13 | Regression suite Notion | ✅ | 36 pass, 1 optional MCP SDK smoke skipped |
