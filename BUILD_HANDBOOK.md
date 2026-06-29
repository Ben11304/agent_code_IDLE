# BUILD HANDBOOK — Slim-Overview Orchestration

> Tầng **hiện thực** của `system_architech.md` (design spec). Đọc spec trước để hiểu *what/why*;
> file này là *how-exact*: schema máy-kiểm, text paste-ready, code control-plane theo line thật,
> và **1 worked trace** chạy end-to-end.
> Control-plane thật: `Hoang/agent_code_IDLE/app/backend/` (FastAPI + asyncio + SQLite, adapter `claude -p`).

---

## 0. ⚠️ Trạng thái THẬT của control-plane (đọc trước khi build)

**Code đích: `VietHuy/agent_code_IDLE` (bản 2508 dòng).** ⚠️ ĐỪNG nhầm với `Hoang/agent_code_IDLE`
(bản 783 dòng, rút gọn/đã diverge — line number KHÁC hẳn). Mọi line dưới đây theo bản 2508.

**Tin tốt: backbone đã CÓ SẴN.** Bản này không phải "viết mới từ đầu" — phần lớn guarantee cơ học
trong spec §5.4 đã tồn tại, ta chỉ **augment** vài chỗ.

| Tính năng | Code thật (2508 dòng) | Việc của handbook |
|---|---|---|
| Cold-start preamble | ✅ **CÓ** `_session_preamble` (main.py:1195-1243), inject ở 1389-1398; đọc `state/progress.md` + `state/children_status.json` + `inputs/manifest.md` | **AUGMENT**: thêm đọc `overview.md` |
| Ledger `<dispatch_result>` | ✅ **CÓ** `_format_results_as_context` (172-208) + table `dispatch_results` (db.py:55-67) + enrich (1338-1349) + consume (1524-1525) | **REUSE** nguyên |
| `children_status.json` sinh | ✅ **CÓ** `_write_children_rollups` (432-499), auto-derived (read-only, "do NOT hand-edit") | **AUGMENT**: thêm field `manifest_version`/`overview_path`/`body_incomplete` |
| Manifest-bump verify | ✅ **CÓ một phần** (1615-1631): sau dispatch, check `outputs/manifest.md` có bump không → append cờ `[control-plane verify]` | **EXTEND** thành version-pin drift đầy đủ |
| Post-turn finalize | ✅ **CÓ** (1517-1530) + stream-loop parse `<dispatch>`/`<schedule>` (1433-1495) | **AUGMENT**: thêm parse `[RESULT]`/`[ESCALATE]` |
| Auto-compact 80%, continuation 3-round, scheduler, detached runs | ✅ CÓ | không đụng |
| **overview.md slim** | ❌ **CHƯA có** | **BUILD MỚI** |
| **`[RESULT]`/`[ESCALATE]` parse** | ❌ **CHƯA có** | **BUILD MỚI** |
| **Version-pin enforcement** | ❌ sync.sh có template (projects.py) nhưng **CHƯA BAO GIỜ được gọi từ code** | **BUILD MỚI** |

> **Đính chính 2 lần trước của tôi:** (1) report gốc nói các tính năng "đã có" — **đúng cho bản 2508**,
> không phải bịa. (2) Tôi từng nói chúng "không có" — đó là vì tôi map **nhầm bản Hoang 783 dòng**.
> Spec §8 ("tái dùng children_status.json + cold-start") **chính xác** với bản 2508; chỉ cần augment field.

**Kiến trúc nền:** FastAPI `/api/projects/{slug}/agents/{id}/chat` (1908-1930) → `_start_run`
(1818-1905) → `_run_agent` (1308-1530) → `claude_stream` (adapters.py:30-173) chạy `claude -p
--output-format stream-json --include-partial-messages --permission-mode bypassPermissions
--model <m> [--effort <e>] [--resume <sid>] --append-system-prompt <sys> <message>`, `cwd`=thư mục agent.
Ta chỉ **cộng/augment lớp data-flow**, không sửa kiến trúc PTY/streaming.

---

## 1. File layout mỗi agent (sau khi build)

```
<AGENT>/
├── AGENT.md            # + khối Phase 0-4 paste-ready (§5)
├── overview.md         # NEW: slim snapshot (header máy + body agent)   ← parent đọc
├── inputs/
│   ├── manifest.md     # roll-up version (đã có)
│   └── <PRODUCER>.md   # manifest producer (đã có)
├── outputs/
│   └── manifest.md     # contract của agent (đã có) ← LUẬT
└── state/
    └── progress.md     # append-only history (đã có)

BOSS/
├── overview.md                  # NEW: rollup cấp dự án (§3.7 spec)
├── state/children_status.json   # AUGMENT: control-plane đã auto-sinh; thêm 3 field (§3)
└── context/team_role.md         # = team_map.md mở rộng (đã có ở AEC/VLM)
```

`overview.md` đặt ở **root thư mục agent** (ngang `AGENT.md`) để `_build_cold_start_preamble` đọc 1 path cố định.

---

## 2. Schema A — `overview.md`

### 2.1 Format chuẩn (3 phần, marker cố định để parser cắt)

```markdown
<!-- OVERVIEW:HEADER -->
# OVERVIEW • <AGENT> • <ISO8601>
status: <green|yellow|blocked>
manifest_version: <semver>        # echo từ outputs/manifest.md — MÁY ghi
ready_for_parent: <yes|no|partial>
body_incomplete: <true|false>     # MÁY set; true = body chưa do agent viết
<!-- /OVERVIEW:HEADER -->

<!-- OVERVIEW:BODY -->
## Bức tranh tổng thể
- <dimension-label>: <value>      # agent viết, bullet, ≤200 từ, dimension theo role (§2.3)
...
<!-- /OVERVIEW:BODY -->

<!-- OVERVIEW:FOOTER -->
last_artifact: <abs-path>
manifest_ref: <path>#<section>
open_escalation: <none | id + 1 dòng>
last_updated: <ISO8601>           # MÁY ghi
<!-- /OVERVIEW:FOOTER -->
```

### 2.2 Quyền ghi từng field (machine vs agent)

| Field | Ai ghi | Validate |
|---|---|---|
| `status` | agent đề xuất, máy **chấp nhận nếu** ∈{green,yellow,blocked} | enum |
| `manifest_version` | **MÁY** (đọc `outputs/manifest.md`) — agent KHÔNG sửa | = version thật |
| `ready_for_parent` | agent | enum {yes,no,partial} |
| `body_incomplete` | **MÁY** (true khi body còn placeholder) | bool |
| BODY | **agent** (synthesis) | ≤200 từ + có đủ dimension role |
| `last_artifact`, `manifest_ref` | agent | path tồn tại |
| `last_updated` | **MÁY** | ISO8601 |

### 2.3 Dimension BẮT BUỘC theo role (thiếu → máy set `body_incomplete=true`)

| Role | Label bắt buộc |
|---|---|
| DATA/DATASET | `version` · `schema` · `size` · `quality` · `method` |
| MODEL/VLM | `adapter@ver` · `benchmark` · `pending` |
| SYNTH/CRAFTER | `draft@ver` · `coverage` · `merged_from` · `needs` |
| VERIFIER/AUDIT | `drift` · `violations` · `pass_rate` · `recommendation` |
| BOSS (cấp dự án) | **ANCHOR** (ổn định): `scope` · `lifecycle` · `funnel/headline` · `frozen` · **ROLLUP** (động): `bottleneck` · `next_milestone` · `risk` · `pending` — hướng C, spec §3.7 |

> ⚠️ BOSS overview budget ~350–400 từ (cao hơn con vì đọc 1×/pass + human, không bị nhân N). **KHÔNG mirror
> per-child status** (children_status lo) → chống stale. ANCHOR re-write hiếm, ROLLUP refresh mỗi pass. 📋 chốt mitigate sau.

### 2.4 Validation rule (control-plane chạy)
- Header có đủ 5 field, enum hợp lệ → nếu không: **retry 1 lần**, vẫn fail → flag + fallback manifest.
- BODY ≤ 200 từ (đếm word giữa marker BODY) → vượt: cảnh báo (không block).
- BODY chứa đủ label role → thiếu: set `body_incomplete=true`.
- `manifest_version` phải == version trong `outputs/manifest.md` → lệch: máy ghi đè bằng version thật.

---

## 3. Schema B — `children_status.json`

Control-plane **đã auto-sinh** file này tại `<BOSS>/state/children_status.json` (hàm `_write_children_rollups`,
gọi trên `/stats`). Ta chỉ **augment** thêm 3 field cuối (`manifest_version`, `overview_path`, `body_incomplete`).
Schema sau khi augment:

```json
{
  "$schema": "children_status/v1",
  "parent": "BOSS",
  "generated_at": 1719500000.0,
  "children": [
    {
      "id": "CRAFTER",
      "status_color": "yellow",          // idle|running|ok|error|blocked
      "manifest_version": "0.7.14",       // đọc từ <child>/outputs/manifest.md
      "overview_path": "../CRAFTER/overview.md",
      "body_incomplete": false,
      "context_pct": 45.2,                // ước lượng từ message tokens / window
      "message_count": 12,
      "last_activity": 1719499000.0,
      "memory_headline": "manuscript v0.4, chờ raw_records 42",
      "open_escalation": null             // hoặc {"id":"E-12","type":"DATA","target":"CURATOR"}
    }
  ]
}
```

**Field type (cho validator):**
`id`:str, `status_color`:enum, `manifest_version`:str|null, `overview_path`:str,
`body_incomplete`:bool, `context_pct`:float, `message_count`:int, `last_activity`:float|null,
`memory_headline`:str, `open_escalation`:null|object.

> BOSS đọc file này để **routing** (ai green/blocked, ai có escalation mở). Chi tiết → mở `overview_path`.
> Chỉ mở `outputs/manifest.md` của child khi **thật sự consume artifact**.

---

## 4. Grammar — `[RESULT]` & `[ESCALATE]`

Agent **emit ở cuối turn**. Control-plane parse bằng regex (§6.3). Định dạng cố định, có tag đóng:

### 4.1 `[RESULT]` (mọi child, cuối turn)
```
[RESULT]
summary: <1-2 câu, delta của turn này>
artifacts: <path1>, <path2>
new_pointers: <none | label → path>
status: <green|yellow|blocked>
ready_for: <agent_name | back_to_boss>
[/RESULT]
```

### 4.2 `[ESCALATE]` (khi cần — thay cho việc tự đi tiếp)
```
[ESCALATE]
type: <DATA|BOSS_DECISION|HUMAN|SUBTASK|TOOL|BLOCKED>
target: <agent | human | tool_name>
payload: <text hoặc {json}>
reason: <1 dòng>
priority: <high|medium|low>
[/ESCALATE]
```

**Regex (Python, dùng nguyên):**
```python
RESULT_RE   = re.compile(r'\[RESULT\](?P<body>.*?)\[/RESULT\]', re.DOTALL | re.IGNORECASE)
ESCALATE_RE = re.compile(r'\[ESCALATE\](?P<body>.*?)\[/ESCALATE\]', re.DOTALL | re.IGNORECASE)
KV_RE       = re.compile(r'^\s*(?P<k>\w+)\s*:\s*(?P<v>.+?)\s*$', re.MULTILINE)
ESC_TYPES   = {"DATA","BOSS_DECISION","HUMAN","SUBTASK","TOOL","BLOCKED"}
```
Malformed = có `[RESULT]` nhưng thiếu `[/RESULT]`, hoặc `type` ∉ `ESC_TYPES` → retry 1 lần.

### 4.3 Phân biệt SUBTASK vs TOOL (quy tắc 1 dòng)
> Target **có manifest/overview** (là agent trong hệ)? → **SUBTASK** (đi qua boundary).
> Không (web/script/API external)? → **TOOL** (control-plane chạy, append output thô).

**Ví dụ:**
- `type: SUBTASK, target: VERIFIER` → nhờ VERIFIER audit một artifact (agent có manifest).
- `type: TOOL, target: web_search` → tra cứu ngoài (không phải agent, không manifest).

### 4.4 Dispatch payload — Outcome / Pointer + `goal` block (spec §14)

BOSS dispatch qua `<dispatch agent="X">…</dispatch>` (cơ chế đã có). **Nội dung** payload phải là
**Outcome** hoặc **Pointer**, KHÔNG tự-soạn detail (spec §14.1-14.2):

```
<dispatch agent="CRAFTER">
mode: outcome | pointer
goal:                         # bắt buộc nếu mode=outcome
  type: threshold | directional | judgment
  predicate: "coverage ≥ 75% AND drift-audit pass"   # threshold
  baseline_value: 0.71        # BẮT BUỘC nếu type=directional (chụp lúc dispatch)
  metric_pinned: "val_acc on Kaggle-v6 holdout"      # BẮT BUỘC nếu directional
  acceptance_by: control_plane | VERIFIER            # KHÔNG bao giờ = child
budget: { turns: 4, tokens: 60000 }                  # bắt buộc; càng mềm càng quan trọng
pointer:                      # thay cho goal nếu mode=pointer
  artifact: "<path>/bench_3arch.py@0.5.2"
scope_ref: "AGENT.md#scope"   # goal KHÔNG override boundary
</dispatch>
```

`[RESULT]` (§4.1) thêm field khi đáp một goal:
```
goal_status: met | partial | missed       # child báo; acceptance_by sẽ VERIFY lại
goal_evidence: "val_acc 0.71→0.78 (+0.07)"
proposal: "route tiếp VERIFIER" | "escalate v0.5"   # Propose&Commit (spec §15)
```

`[CONTINUE_SELF]` (self-advance, spec §15 — capped):
```
[CONTINUE_SELF]
reason: "chưa đạt goal, còn budget, đang tiến (0.74→0.76)"
[/CONTINUE_SELF]
```
→ control-plane resume child KHÔNG qua BOSS, **nhưng** chỉ khi: còn budget + đang tiến (không plateau)
+ chưa chạm hard cap N. Chạm cap/plateau → STOP + escalate (spec §14.4).

### 4.5 `[HALT]` — Evaluative Self-Halt (spec §15.1 — Mức 2) ⚠️ SPEC ONLY, CHƯA IMPLEMENT

Worker **tự dừng task giữa chừng** khi phán tiếp tục là vô ích (saturated/dead-end/false-premise),
**report KHÔNG chờ ratify**; BOSS overturn sau. Khác `[ESCALATE]` (xin data/hành động) và
`[CONTINUE_SELF]` (chạy tiếp) — đây là **dừng-sớm-có-chủ-đích kèm phán xét**.

```
[HALT]
reason: saturated | dead_end | false_premise | diminishing_returns
evidence: <ĐỊNH LƯỢNG + cụ thể — BẮT BUỘC; vd "NS1–NS9 (9×20=180), 1 net-new (R040), 0 từ NS5–NS9; arXiv/PwC/OpenReview cạn vs corpus 0.15">
did: <đã hoàn tất gì trước khi halt>
recommendation: <BOSS nên làm gì: "freeze corpus @26" | "redirect gap X" | "stop, premise sai">
confidence: high | medium | low
[/HALT]
```

**Control-plane (dự kiến khi build — CHƯA code):**
- Worker turn kết thúc self-halted (KHÔNG auto-resume). Record `[HALT]` vào ledger BOSS (như escalate) → BOSS thấy turn sau.
- **Validation guardrail (anti-self-cert):** `evidence` rỗng/vacuous → **malformed** → retry 1 lần đòi evidence (hoặc hạ xuống `[RESULT].proposal` soft). Worker được quyền dừng-effort-mình, KHÔNG được chốt kết-luận-dự-án.
- BOSS turn kế: đọc HALT+evidence → **ratify** (nhận recommendation) hoặc **overturn** (re-dispatch "continue, halt sai vì…").
- Thêm `halt_log` cho audit halt-rate (agent kêu bão hoà quá thường → flag).

> ⚠️ **Trạng thái: SPEC ONLY — chưa implement** (user 2026-06-27: chốt mitigate sau).
> Khi build: mở rộng `_parse_structured` (§6.4) + `_HALT_RE` + evidence-required check; route như
> `_handle_escalate` (§6.6) nhưng **KHÔNG auto-resume**; bảng `halt_log` (db.py) như `context_log`.

### 4.6 `[DISSENT]` — Forced-acknowledge Dissent (spec §15.2) ⚠️ SPEC ONLY, CHƯA IMPLEMENT

Worker **phản đối một HƯỚNG/quyết định** (không phải task của nó) → raise blocking-flag + evidence →
BOSS **không được im lặng proceed**. Khác `[HALT]` (dừng effort mình) và `[ESCALATE] BOSS_DECISION`
(xin BOSS quyết, non-blocking). Chỉ cho strategy-sai; **integrity-sai → refuse + `[ESCALATE] HUMAN`**.

```
[DISSENT]
against: <direction/artifact/decision — vd "freeze tier scheme @current" | "taxonomy X của SYNTHESIZER">
reason: <vì sao sai (strategy/method)>
evidence: <ĐỊNH LƯỢNG/cụ thể — BẮT BUỘC>
proposed_correction: <hướng đúng nên là gì>
severity: blocking | strong | advisory
[/DISSENT]
```

**Control-plane (dự kiến — CHƯA code):**
- Ghi **BLOCKING-FLAG (open)** gắn `against`. BOSS dispatch chạm hướng đó → control-plane chèn "OPEN DISSENT phải xử" vào context BOSS.
- BOSS resolve bằng `[DISSENT_RESOLVE] against: … verdict: ratify|overrule reason: …` → **log** (accountability: ratify sai về sau truy được).
- `severity=blocking` + integrity-grade → **auto-loop VERIFIER/human**; `advisory` → surface, không gate.
- `evidence` rỗng → invalid → retry. Worker **KHÔNG veto** (ratify/overrule ở BOSS/VERIFIER/human).

> ⚠️ **SPEC ONLY.** Khi build: `_DISSENT_RE` + bảng `dissent_flags` (open/resolved) + gate trong BOSS
> dispatch path + `[DISSENT_RESOLVE]` parse + dissent-rate monitor.

---

## 5. Khối paste-ready cho `AGENT.md`

### 5.1 Child agent — dán vào cuối `AGENT.md` (mọi child)

```markdown
## WORKFLOW BẮT BUỘC (5 phase — executor)

**Phase 0 · PRE-FLIGHT**
- Chạy `sync.sh <NAME>` + `sync.sh check <NAME>` (control-plane đã verify version-pin; mismatch → STOP).
- Đọc: `inputs/manifest.md` + `overview.md` của producer liên quan + Key Pointers BOSS gửi.

**Phase 1 · RECEIVE & CLARIFY**
- Thiếu data/điều kiện → emit `[ESCALATE]` ngay (đúng grammar §4.2). KHÔNG đoán.

**Phase 2 · PLAN & DECLARE** (declare-before-implement)
- Khai rõ: "sẽ implement <ABC/artifact> v<X.Y.Z> tại <path>". Không khai = không tạo (chống orphan).
- ABC chưa đủ biểu đạt → `[ESCALATE] type:BOSS_DECISION` tới producer, KHÔNG tự patch.

**Phase 3 · EXECUTE**
- Làm việc chính. Mỗi artifact tạo ra → ghi path. Bump version trong `outputs/manifest.md` theo bump-rule.

**Phase 4 · REFLECT & SNAPSHOT** (cuối turn, BẮT BUỘC)
- Ghi đè BODY của `overview.md`: bức tranh TỔNG THỂ hiện tại (KHÔNG phải nhật ký), ≤200 từ,
  bullet, đủ dimension theo role (xem bảng §2.3). KHÔNG sửa field MÁY (version/last_updated).
- Emit `[RESULT]` block (grammar §4.1) — control-plane sẽ append vào `state/progress.md` + đóng dấu header.
```

### 5.2 BOSS — dán vào `BOSS/AGENT.md` (PRE-FLIGHT + 4-phase)

```markdown
## PRE-FLIGHT (orchestrator)
- Đọc `children_status.json` (rollup) + `context/team_role.md` → CHỌN agent.
- Đọc `overview.md` của agent được chọn + `BOSS/overview.md` (toàn cảnh dự án).
- KHÔNG đọc full manifest team nào trừ khi sắp bàn giao 1 artifact cụ thể.

## LOOP 4-PHASE
**1 · PLAN & DISPATCH** — chọn route, soạn message + Key Pointers, dispatch.
**2 · RECEIVE & ENRICH** — (control-plane tự append `<dispatch_result from=...>`).
**3 · ANALYZE & DECIDE** — phân tích response; quyết:
   CONTINUE_CHAIN(next) | FINALIZE(user) | ESCALATE(request thêm). (judgment — viết tự do, không ép tag cứng).
**4 · EXECUTE** — continue→dispatch tiếp · finalize→trả user + ghi đè BODY `BOSS/overview.md`.
```

> Lưu ý: BOSS **không** bị ép format `[BOSS_ANALYSIS]/[DECISION]` cứng (tránh determinism giả + tốn token).
> Phần judgment để model tự do; control-plane chỉ parse `[RESULT]`/`[ESCALATE]` nếu có.

---

## 6. Thay đổi control-plane (bản 2508 dòng — REUSE/AUGMENT/NEW)

Tất cả ở `VietHuy/agent_code_IDLE/app/backend/`. Nhãn: **REUSE** = đã có, dùng nguyên ·
**AUGMENT** = sửa hàm có sẵn · **NEW** = viết mới. Line theo bản 2508.

### 6.1 [REUSE] Ledger `<dispatch_result>` — KHÔNG đụng
Đã đủ: `dispatch_results` table (db.py:55-67), `record_dispatch_result`, `get_unconsumed_results`,
`_format_results_as_context` (main.py:172-208, trunc 8000 char), enrich ở 1338-1349, consume ở 1524-1525.
→ BOSS đã thấy kết quả con. **Không build lại** (handbook bản trước sai chỗ này).

### 6.2 [AUGMENT] Inject `overview.md` vào `_session_preamble` (main.py:1195-1243)
Hàm đã đọc `state/progress.md` + `state/children_status.json` + `inputs/manifest.md`. **Thêm** đọc
`overview.md` (ưu tiên hơn excerpt manifest), và fallback nếu chưa có:
```python
# trong _session_preamble, cạnh chỗ đọc inputs/manifest.md (~1231-1235):
ov = adir / "overview.md"                                   # NEW
if ov.exists():
    parts.append("### Overview (slim, current)\n" + ov.read_text(encoding="utf-8"))
else:
    parts.append("### (fallback) Input contract head\n" + _read_capped(man, 600))  # §10 spec fallback
```
> ⚠️ Caveat thật: `_session_preamble` chỉ chạy khi **không resume được** (1394: `resume_sid is None and
> not seed_text`). Turn resume KHÔNG re-inject. Với routing freshness của BOSS, children_status mới
> đã đi kèm preamble khi cold-start; nếu muốn BOSS đọc overview con **mỗi** turn, thêm 1 block slim ở
> nhánh every-turn cạnh ledger (1338) — **optional**, cân nhắc token.

### 6.3 [AUGMENT] children_status rollup (main.py:`_write_children_rollups` 432-499)
Hàm đã sinh `status/context_pct/message_count/last_activity/memory_headline/stale_memory` (per-child
450-468). **Thêm 3 field** spec §3:
```python
# trong vòng per-child (~450-468), thêm:
children[cid]["manifest_version"] = _read_manifest_version(project_root, cid)        # NEW
children[cid]["overview_path"]    = f"../{cid}/overview.md"                            # NEW
children[cid]["body_incomplete"]  = _read_overview_flag(project_root, cid, "body_incomplete")  # NEW
```
`_read_manifest_version`: regex `## Version\n(\S+)` trong `<cid>/outputs/manifest.md`.
`_read_overview_flag`: đọc field trong block HEADER của `<cid>/overview.md`.
→ REUSE nguyên cơ chế ghi file + calling site (579 trên `/stats`); chỉ cộng field.

### 6.4 [NEW] Parse `[RESULT]`/`[ESCALATE]` + stamp overview + retry
Stream-loop đã parse `<dispatch>`/`<schedule>` (1433-1495); finalize ở 1517-1530. **Thêm** ở
finalize, sau `db.update_session_status` (1523):
```python
if final_text and final_status == "ok":
    res = _parse_structured(final_text)                    # NEW §4 regex
    if res["malformed"] and retry_count == 0:              # retry ĐÚNG 1 lần
        return await _run_agent(slug, agent_id,
            "[CONTROL-PLANE] Output sai format: "+res["error"]+". Emit lại [RESULT]/[ESCALATE] đúng.",
            emit, tracker, chain, grok_options, retry_count=1)
    if res["result"]:
        _stamp_overview_header(project["root"], agent_id)  # NEW: ghi manifest_version/last_updated/body_incomplete
        # progress.md: agent tự append ở Phase 4, hoặc control-plane append res["result"] (chọn 1, đừng double)
    if res["escalate"]:
        await _handle_escalate(res["escalate"], slug, agent_id, project, emit, tracker)  # §6.6
```
Thêm param `retry_count: int = 0` vào chữ ký `_run_agent` (1308-1316).
`_stamp_overview_header`: đọc `outputs/manifest.md` version → ghi đè field MÁY trong HEADER overview;
đếm word BODY + check dimension role (§2.3) → set `body_incomplete`.

### 6.5 [NEW/EXTEND] Version-pin enforcement
sync.sh được **sinh** bởi `projects.generate_sync_sh` nhưng **chưa bao giờ gọi từ code**. Đồng thời
đã có manifest-bump verify một phần ở **1615-1631** (sau dispatch, check `outputs/manifest.md` bump).
→ **Mở rộng**: trong `_run_agent` trước khi gọi adapter (~1389), check drift inputs-pin:
```python
drift = _check_version_pins(project["root"], agent_id)     # NEW: chạy `sync.sh check <agent>` (§7)
if drift:
    return f"[CONTROL-PLANE BLOCK] stale pin: {drift}. Run sync.sh."
```

### 6.6 [NEW] `_handle_escalate` (theo type — §4 / spec §7)
```python
async def _handle_escalate(esc, slug, agent_id, project, emit, tracker):
    t = esc["type"]
    if t == "DATA":          # auto pull + STAMP version (vá an toàn spec §7) — KHÔNG cần BOSS
        ver  = _read_manifest_version(project["root"], esc["target"])
        data = _pull_section(project["root"], esc["target"], esc["payload"])
        _append_progress(project["root"], agent_id, f"consumed {esc['target']}@{ver} {esc['payload']}")
        # append data+tag vào message child rồi resume (tái dùng cơ chế dispatch/continuation sẵn có)
    elif t == "SUBTASK":     # target là agent (có manifest) → REUSE _dispatched_run
        ...
    elif t == "TOOL":        # external (không manifest) → chạy tool, append output thô
        ...
    else:                    # BOSS_DECISION|HUMAN|BLOCKED → REUSE ledger: record_dispatch_result lên BOSS / pause+notify
        db.record_dispatch_result(project_slug=slug, source_agent=agent_id,
            target_agent="BOSS", task=f"ESCALATE:{t}", result_text=json.dumps(esc), status="ok")
```
> Chú ý: nhánh BOSS_DECISION/HUMAN **tái dùng** chính ledger `dispatch_results` đã có (§6.1) để đẩy lên
> BOSS — không cần table escalation riêng.

### 6.7 Bảng tổng hợp edit (bản 2508)

| Việc | Nhãn | File:line | Hàm |
|---|---|---|---|
| Ledger dispatch_result | REUSE | main.py:172-208,1338-1349 / db.py:55-67 | có sẵn |
| Inject overview vào preamble | AUGMENT | main.py:`_session_preamble` 1195-1243 (~1231) | + đọc overview.md |
| children_status + 3 field | AUGMENT | main.py:`_write_children_rollups` 432-499 (~450-468) | `_read_manifest_version`,`_read_overview_flag` |
| Parse [RESULT]/[ESCALATE]+stamp+retry | NEW | main.py finalize ~1523 | `_parse_structured`,`_stamp_overview_header` |
| Version-pin drift | NEW/EXTEND | main.py ~1389 (+ verify 1615-1631) | `_check_version_pins` |
| Escalate router | NEW | post-turn | `_handle_escalate` (reuse `_dispatched_run`,ledger) |
| retry_count param | NEW | main.py:1308-1316 | sửa chữ ký `_run_agent` |

### 6.8 [EXTEND] Acceptance gate + stopping rule (goal contract — spec §14)
Hook manifest-bump verify đã có ở **1615-1631** (sau dispatch, check `outputs/manifest.md` bump).
**Mở rộng** thành acceptance gate cho goal:
```python
# sau khi worker dispatch xong (~1615-1631), nếu dispatch có goal:
if goal:
    ok = _check_acceptance(goal, project["root"], target)        # NEW
    # threshold: metric >= predicate ; directional: metric > goal["baseline_value"] ; judgment: route VERIFIER
    if not ok:
        if _plateau(target) or _budget_exhausted(target, goal["budget"]):  # NEW: stopping rule §14.4
            await _emit_escalate_partial(target, goal, emit)     # STOP · escalate partial — KHÔNG retry vô hạn
        # else: còn budget & đang tiến → continuation round (REUSE 3-round cap 1843-1871) xử lý
```
- `_check_acceptance`: **không bao giờ** tin `goal_status` child tự khai; tự đọc metric/đẩy VERIFIER.
- `_plateau`: so metric N round gần nhất, Δ < ε → true. `_budget_exhausted`: đếm turn/token đã dùng vs `goal.budget`.
- **REUSE** continuation-round cap (1843-1871) + scheduler (`<schedule until=goal>`/`<schedule_stop>`) làm
  khung lặp-có-điểm-dừng; chỉ thêm plateau-detector + escalate-partial.

### 6.9 [AUGMENT] BOSS Summary-only + Exception (spec §15)
- Trong `_format_results_as_context` (172-208, đã trunc 8000 char): thêm dòng digest đầu mỗi block
  (`summary/status/goal_status`) để BOSS đọc nhanh — **surface cơ học, KHÔNG synthesize**.
- BOSS chỉ deep-analyze khi `status=blocked` hoặc `goal_status=missed` hoặc `open_escalation≠null`.
- **Chống silent drift**: thêm full-scan định kỳ — mỗi N turn, một pass đọc đủ overview tất cả child
  (hoặc 1 critic agent hỏi "cái gì đang sai mà không raise flag?").

---

## 7. Thay đổi `sync.sh`

`sync.sh check <CONSUMER>` đã so version. Thêm **exit code rõ** để control-plane (§6.1) dùng:
```bash
# trong nhánh check: nếu pinned != current
echo "DRIFT <PRODUCER> pinned=<a> current=<b>"; exit 3   # 0=ok, 3=drift, 2=missing
```
`_check_version_pins` gọi `sync.sh check` qua subprocess, đọc exit code + stdout.

---

## 8. Bootstrap (sinh `overview.md` lần đầu cho agent cũ)

Script 1 lần (chạy ngoài turn). Pseudo:
```python
for agent in all_agents(root):
    ver = read_manifest_version(agent/"outputs/manifest.md")
    latest = read_latest_artifact(agent/"outputs/manifest.md")
    write(agent/"overview.md", HEADER(status="yellow", manifest_version=ver, body_incomplete=True)
          + BODY_PLACEHOLDER(f"*(chờ agent tự tổng hợp; artifact mới nhất: {latest})*")
          + FOOTER(last_artifact=latest))
```
→ Sau bootstrap mọi overview có header thật + body placeholder + `body_incomplete=true`.
Turn kế của mỗi agent (Phase 4) ghi đè body thật → cờ tự gỡ.

---

## 9. WORKED TRACE — end-to-end (AEC: BOSS → CRAFTER → escalate DATA → CURATOR)

Kịch bản: user yêu cầu "đưa raw records vào manuscript". CRAFTER cần raw 42 entries của CURATOR.

**T0 — user → BOSS** (`/api/.../BOSS/chat`, message="merge raw records vào manuscript")
- `_check_version_pins(BOSS)` → ok. [NEW §6.5]
- `_session_preamble(BOSS)` [AUGMENT §6.2]: BOSS là orchestrator → `_write_children_rollups` ghi
  `BOSS/state/children_status.json` (augmented):
  ```json
  {"children":[{"id":"CRAFTER","status_color":"yellow","manifest_version":"0.7.14",
   "overview_path":"../CRAFTER/overview.md","body_incomplete":false,
   "memory_headline":"manuscript v0.4, chờ raw_records 42","open_escalation":null}]}
  ```
- Preamble = `BOSS/overview.md` + children_status. BOSS đọc → thấy CRAFTER yellow, headline nói "chờ raw".

**T1 — BOSS Phase 3 ANALYZE+DECIDE**
- BOSS không đọc full manifest CRAFTER. Quyết: `CONTINUE_CHAIN → CRAFTER`, Key Pointers=`CURATOR records_42`.
- BOSS dispatch CRAFTER (message + pointer).

**T2 — CRAFTER turn** (`cwd=CRAFTER/`)
- `_check_version_pins(CRAFTER)`: `inputs/CURATOR.md` pinned 0.5.2 == `CURATOR/outputs/manifest.md` 0.5.2 → ok.
- `_session_preamble(CRAFTER)`: inject `CRAFTER/overview.md` (slim, không full manifest).
- Phase 1: CRAFTER thấy thiếu raw 42 → emit:
  ```
  [ESCALATE]
  type: DATA
  target: CURATOR
  payload: records_42
  reason: cần merge vào manuscript
  priority: high
  [/ESCALATE]
  ```

**T3 — post-turn hook `_handle_escalate` (type=DATA, auto, KHÔNG cần BOSS)**
- `ver = _read_manifest_version(CURATOR)` → `0.5.2`.
- `_pull_section(CURATOR, records_42)` → raw json.
- `_append_progress(CRAFTER, "consumed CURATOR@0.5.2 records_42")`  ← **vá an toàn version (spec §7)**.
- raw + tag `CURATOR@0.5.2` append vào message CRAFTER → **resume** cùng session.

**T4 — CRAFTER tiếp Phase 3-4**
- Merge raw → `outputs/crafter_draft_v0.5.md`, bump manifest 0.7.14→0.7.15.
- Phase 4: ghi đè BODY `CRAFTER/overview.md`:
  ```
  ## Bức tranh tổng thể
  - draft@ver: manuscript v0.5 (coverage 72%)
  - merged_from: CURATOR records_42 @0.5.2, SYNTHESIZER taxonomy
  - needs: none
  ```
  emit:
  ```
  [RESULT]
  summary: merged raw 42 records → manuscript v0.5
  artifacts: outputs/crafter_draft_v0.5.md
  new_pointers: draft → outputs/crafter_draft_v0.5.md
  status: green
  ready_for: back_to_boss
  [/RESULT]
  ```
- post-turn: `_append_progress(CRAFTER, result)`; `_stamp_overview_header(CRAFTER)` → ghi
  `manifest_version: 0.7.15`, `last_updated`, kiểm dimension SYNTH đủ → `body_incomplete=false`.
- Ledger (§6.6): ghi `dispatch_results(BOSS-session, from=CRAFTER, result=...status green)`.

**T5 — BOSS turn kế**
- `_session_preamble(BOSS)`: children_status mới (CRAFTER giờ `status_color:green, manifest_version:0.7.15`)
  + `<dispatch_result from="CRAFTER">...green...</dispatch_result>` (ledger).
- BOSS Phase 3: thấy CRAFTER green, draft v0.5 ready → `FINALIZE` → trả user + ghi đè `BOSS/overview.md`.

> Lưu ý suốt trace: **không lần nào** BOSS nạp full manifest CRAFTER/CURATOR. Routing chỉ dùng
> children_status + overview. Manifest chỉ bị đọc bởi `_check_version_pins` (so version, không nạp body)
> và `_pull_section` (cắt đúng section cần). Đây là chỗ token được tiết kiệm.

---

## 10. Acceptance tests

| # | Test | Pass khi |
|---|---|---|
| A1 | Agent chưa có overview | preamble fallback đọc head manifest, không crash |
| A2 | overview header thiếu field | retry 1 lần; vẫn fail → flag + fallback |
| A3 | manifest_version trong overview ≠ outputs/manifest.md | `_stamp_overview_header` ghi đè đúng version thật |
| A4 | BODY > 200 từ | cảnh báo, không block |
| A5 | BODY thiếu dimension role | `body_incomplete=true` |
| A6 | version drift (inputs pin ≠ producer) | `_check_version_pins` block turn, message yêu cầu sync |
| A7 | `[ESCALATE] type:DATA` | auto pull + dòng `consumed <PROD>@<ver>` xuất hiện trong progress |
| A8 | producer bump version SAU khi child consume | turn kế của child: A6 bắt drift |
| A9 | `[RESULT]` malformed | retry đúng 1 lần (không vòng vô hạn) |
| A10 | BOSS routing | KHÔNG đọc full manifest child nào (verify qua log đọc file) |

---

## 11. Thứ tự build (khớp checklist spec §12)

1. **P0** AUGMENT `_session_preamble` đọc overview + **fallback** (A1) → lưới an toàn, hệ vẫn chạy.
2. **P1** schema overview + AUGMENT `_write_children_rollups` (+3 field) + bootstrap (§8).
3. **P2** paste khối Phase 0-4 vào `AGENT.md` (§5) + `BOSS/overview.md`.
4. **P3** `_check_version_pins` + sync.sh exit code (§7) + post-turn parse/stamp/retry (§6.4).
5. **P4** `_handle_escalate` (reuse ledger §6.1) + chạy acceptance §10.
6. **P5** goal contract: dispatch `goal` block (§4.4) + acceptance gate/stopping rule (§6.8) + Summary/Exception (§6.9).
   Agency: Propose&Commit (field `proposal`) trước; `[CONTINUE_SELF]` capped sau; Micro-orchestrator (field `parents`) chọn lọc cho AEC synth-trio.
7. **P6** Evaluative Self-Halt (§4.5 / spec §15.1) — ✅ **DONE 2026-06-27**: `_HALT_RE` + parse trong `_parse_structured` (evidence-required → malformed/retry) + `_handle_halt` (ledger route, KHÔNG auto-resume) + `halt_log` table. Soft-trigger: protocol §6 (3 hệ). Smoke PASS.
8. **P7** Forced-acknowledge Dissent (§4.6 / spec §15.2) — ✅ **DONE 2026-06-27**: `_DISSENT_RE`/`_DISSENT_RESOLVE_RE` + parse (evidence+against required) + `dissent_flags` table + `_handle_dissent` (open flag) + **`_open_dissent_warning` gate trong `_run_agent` cho orchestrator** (forcing — re-surface mỗi turn tới khi resolve) + `_handle_dissent_resolve` (ratify/overrule). Smoke PASS (flag lifecycle open→resolve).
9. **P8** Incremental Verification & Delta-Processing (spec §16) — ✅ **DONE 2026-06-27** (hard skip + soft protocol). **Nguyên lý KIẾN TRÚC CHUNG** (§16.0). Implement:
   (a) **Artifact chunking** — ✅ behavioral (protocol §7 "surgical Edit, đừng re-generate"); ⬜ structural reorg (per-project, chưa);
   (b) **Verified-watermark** — ✅ **HARD**: `verify_watermark` table + `_verify_delta` (so producer semver, `==`→SKIP, đọc version thật) + `_verify_delta_hint` inject đầu turn auditor + store khi auditor emit `verified: PROD@ver` trong [RESULT]. Smoke PASS trên AEC thật (DISCOVERER unchanged→SKIP, CURATOR đổi→RE-VERIFY);
   (c) monotone findings (`closed_at`) — ✅ **soft** (auditor protocol: reopen chỉ khi version vượt);
   (d) tiered (cơ học mỗi pass · judgment/web chỉ DELTA+publish-gate) — ✅ **soft** (auditor protocol — model-choice là config, không hard-enforce được);
   (e) batch-at-gate — ✅ **soft** (auditor protocol). Soft-trigger: §INCREMENTAL VERIFICATION trong VERIFIER/INTEGRITY/AUDIT AGENT.md (3 hệ).
   **Số chống lưng**: findings 173KB/86 re-work mention, F-2026 47× F-066 22×, log không có verified-at-version; cascade +2 bench ≈1.8M ctx-tok.

> Mỗi bước có đường lui (fallback §6.2). Không big-bang. **Ledger + children_status generator + cold-start
> preamble ĐÃ chạy sẵn** trên bản 2508 (cả AEC lẫn VLM) — ta chỉ augment, không build từ đầu.
> (Khác hẳn handbook bản nháp trước vốn theo nhầm bản Hoang 783 dòng.)
```
