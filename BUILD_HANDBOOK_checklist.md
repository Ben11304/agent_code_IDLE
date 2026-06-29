# BUILD_HANDBOOK — Checklist tiến độ

> Trạng thái triển khai slim-overview. Cập nhật 2026-06-27.
> Handbook gốc (đặc tả how-exact): `BUILD_HANDBOOK.md`. Spec: `system_architech.md`.
> Repo: `VietHuy/DFU-Pipeline-AGENT/` (contract) + `VietHuy/agent_code_IDLE/` (control-plane).

Ký hiệu: ✅ xong · 🟡 một phần · ⬜ chưa · 👤 cần user.

---

## A. Lớp contract/markdown — `DFU-Pipeline-AGENT/` (✅ HOÀN TẤT)

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

## B. Control-plane code — `agent_code_IDLE/app/backend/main.py` (đã `py_compile` PASS)

| # | § | Hạng mục | TT | Ghi chú |
|---|---|---|---|---|
| B1 | §6.2 | `[AUGMENT]` `_session_preamble` inject `overview.md` + fallback | ✅ | down-cap manifest khi overview usable; `body_incomplete:true`/rỗng → giữ full manifest |
| B2 | §6.4 | `[NEW]` `_parse_structured` (regex `[RESULT]`/`[ESCALATE]`, detect malformed) | ✅ | absent block ≠ malformed |
| B3 | §6.4 | `[NEW]` `_read_manifest_version` + `_stamp_overview` (đóng dấu HEADER máy) | ✅ | manifest_version/last_updated/body_incomplete + open_escalation |
| B4 | §6.4 | retry ĐÚNG 1 lần (param `retry_count`) wired vào finalize | ✅ | malformed → 1 corrective re-run; không loop |
| B5 | §6.4 | escalate routing (superseded bởi B9) | ✅ | xem B9 |
| B6 | §6.1 | Ledger `<dispatch_result>` | ✅ | REUSE nguyên, không đụng |
| B7 | §6.3 | `[AUGMENT]` `_write_children_rollups` +3 field (`manifest_version`/`overview_path`/`body_incomplete`) | ✅ | smoke-test trên DFU: version+flag đọc đúng |
| B8 | §6.5 | `[NEW]` `_check_version_pins` drift detect (Python, không phụ thuộc sync.sh exit) | ✅ | **soft-block** (warn+instruct vào message, KHÔNG hard-return → tránh deadlock self-sync). Bắt đúng drift INTEGRITY thật |
| B9 | §6.6 | `[NEW]` `_handle_escalate` → route vào ledger của parent | ✅ | auto-pull(DATA)/auto-dispatch(SUBTASK)/tool(TOOL) **cố ý KHÔNG tự chạy** (rủi ro pull sai/chạy tool tùy ý) → đẩy BOSS arbitrate |
| B10 | §6.8 | `[NEW]` acceptance goal-gate trong `_dispatched_run` | ✅ | enforce **PROCESS** (child tự-khai goal = CLAIM, không phải acceptance; route acceptance_by). Metric-plateau detector KHÔNG generic-được → không build |
| B11 | §6.9 | `[AUGMENT]` `_format_results_as_context` digest + exception-guidance | ✅ | `[digest] status/goal/escalate/summary`; BOSS deep-read chỉ exception. Full-scan định kỳ (cần counter bền) **chưa** tự động |

---

## C. Bootstrap / vận hành

| # | Hạng mục | TT | Ghi chú |
|---|---|---|---|
| C1 | overview.md lần đầu cho agent cũ (§8) | 🟡 | DFU làm TAY (6 file); chưa có script tự sinh cho AEC/VLM |
| C2 | Restart uvicorn để nạp B1-B4 | 👤 ⬜ | **off-cluster only** (không OSC login node). `lsof -ti tcp:5174 \| xargs kill -9; cd app && ./run.sh` |
| C3 | Acceptance tests §10 (A1-A10) | ⬜ | CHƯA chạy — sau khi restart |

---

## D. Map theo build order spec §11

| Bước | Nội dung | TT |
|---|---|---|
| **P0** | §6.2 preamble + fallback | ✅ |
| **P1** | schema overview ✅ · children_status +3 field (B7) ✅ · bootstrap tay | 🟡 (chỉ thiếu script bootstrap cho AEC/VLM) |
| **P2** | Phase 0-4 vào AGENT.md + BOSS overview (DFU) | ✅ |
| **P3** | parse/stamp/retry (B2-B4) ✅ · version-pin (B8) ✅ | ✅ |
| **P4** | `_handle_escalate` (B9) ✅ + acceptance tests (C3) ⬜ | 🟡 (code xong; chưa chạy live) |
| **P5** | goal grammar ✅ · acceptance gate code (B10) ✅ · agency: Propose&Commit field ✅, CONTINUE_SELF/micro-orch ⬜ | 🟡 |

---

## E. Tóm tắt 1 dòng

> **Contract DFU = XONG. Control-plane B1–B11 = XONG** (P0/§6.2 + §6.4 parse/stamp/retry + B7 rollup +
> B8 drift + B9 escalate-route + B10 goal-gate + B11 digest) — `py_compile` PASS + smoke-test helper trên
> DFU thật (version/flag/drift đọc đúng).
> **Còn lại = vận hành + hardening:** 👤 restart off-cluster (C2) · chạy acceptance §10 live (C3) ·
> bootstrap-script cho AEC/VLM (C1) · phần cố-ý-hoãn: escalate auto-resolve theo type, metric-plateau
> detector, full-scan định kỳ, CONTINUE_SELF/micro-orchestrator (đều cần project-specific / state bền).
