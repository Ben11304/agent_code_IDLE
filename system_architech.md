# Thiết kế Hệ thống Agent — Tiết kiệm Token mà vẫn Hiệu quả

> Tài liệu tổng hợp thiết kế sau quá trình thảo luận (2026-06-27).
> Mục tiêu: giảm context bloat cho agent cấp trên (parent/BOSS) khi điều phối,
> nhưng **không** đánh đổi tính an toàn (version/drift) của hệ thống manifest hiện có.
> Áp dụng cho 2 hệ thực tế: `ConstructionVLM-Eval-AGENT` (VLM) và `AECPlayGround-AGENT` (AEC).

---

## 0. Vấn đề xuất phát

Khi audit 2 hệ thống thật:

- **VLM**: BOSS **nhẹ** — đọc `progress.md` → `children_status.json` (rollup) → `team_map.md`
  → chỉ đọc manifest của team mà task đang chạm (selective). Topology thực tế là **DAG**
  (DASHBOARD đọc FRAMEWORK+DATASET+VLM; AUDIT đọc cả 4), không phải cây thuần.
- **AEC**: BOSS **nặng** — PRE-FLIGHT ép đọc **full cả 5 manifest** mỗi orchestration pass.
  Manifest AEC giàu lịch sử append-only (Rxxx/Fxxx, PRISMA audit) → BOSS context phình dần.

**Chẩn đoán cốt lõi:** AEC không "vi phạm triết lí" (boundary manifest-only vẫn được giữ).
Vấn đề là **manifest gánh 2 vai trong 1 file**:
1. Lớp **contract mỏng** (version + path + status) — thứ BOSS *thực sự* cần để route.
2. Lớp **sổ cái audit dày** (bump history, Rxxx/Fxxx) — phục vụ VERIFIER, không phải BOSS.

→ Thiếu một **lớp mỏng (slim view)** để parent đọc khi routing. Đó chính là `overview.md`.

---

## 1. Nguyên tắc nền (bất biến)

1. **Manifest vẫn là LUẬT.** Mọi tính đúng đắn (verify version bump, `sync.sh check`,
   "không act trên stale corpus", drift detection) dựa vào **version semver của manifest**.
2. **`overview.md` chỉ là ô kính đọc nhanh, PHÁI SINH từ manifest** — không phải nguồn
   sự thật đối thủ. Parent đọc overview để **ra quyết định dispatch**, và chỉ drill xuống
   manifest khi **thật sự bàn giao 1 artifact** qua boundary.
3. **Hard-code phần plumbing, soft-prompt phần judgment** (xem §5).
4. **Boundary manifest-only**: không agent nào đọc internals của agent khác; mọi thứ qua
   boundary đi qua manifest/overview (trỏ path, không copy data).

---

## 2. Mô hình 3 tầng context cho Parent

Mỗi tầng trả lời một câu hỏi khác nhau → nạp đúng tầng, không nạp thừa.

| Tầng | File | Trả lời câu gì cho parent | Khi nào nạp | Ai ghi |
|---|---|---|---|---|
| **Tĩnh** | `team_role.md` | "Ai **có thể** làm gì" → chọn ứng viên | mỗi pass (nhỏ, ít đổi) | người/BOSS |
| **Động mỏng** | `overview.md` | "Ứng viên **đang ở trạng thái nào**" → quyết định dispatch | mỗi pass, chỉ agent liên quan | **agent (body) + control-plane (header)** |
| **Đầy đủ** | `manifest.md` | "Contract/version/path **chính xác**" | **chỉ khi bàn giao 1 artifact** | producer agent |

**Điểm mấu chốt:** Parent **KHÔNG đọc manifest để routing**. Parent đọc overview để *quyết định*,
chỉ mở manifest khi đã quyết chuyển một artifact cụ thể. (Đây là pattern "selective manifest"
của VLM, được hình thức hóa: overview = lớp routing, manifest = lớp consume.)

---

## 3. `overview.md` — đặc tả

### 3.1 overview LÀ gì và KHÔNG LÀ gì

- **LÀ**: *bức tranh tổng thể* công việc của agent đó đang đứng ở đâu, ở **mức ngắn** (holistic, brief).
  Ghi **đè** mỗi turn (không tích lũy lịch sử).
- **KHÔNG LÀ**: ảnh chụp kết quả của *turn cuối*. (Delta của turn → `[RESULT]` block + `progress.md`.)
- **KHÔNG LÀ**: manifest thứ hai. Chỉ chứa field **ảnh hưởng quyết định routing**; chi tiết kỹ
  thuật đầy đủ + lịch sử thuộc về manifest/progress.

### 3.2 Ba phần của một overview

```
┌─ HEADER  (machine-stamped — control-plane ghi/validate, agent KHÔNG sửa)
│   status, version (echo từ manifest), last_updated, ready_for_parent
├─ BODY    (agent-authored — judgment/synthesis, linh hoạt theo role)
│   bức tranh tổng thể "việc của tôi đang đứng ở đâu", chỉ field ảnh-hưởng-routing
└─ FOOTER  (pointers cố định)
    last_artifact_path, manifest path, open_escalation (nếu có)
```

**Vì sao tách quyền ghi:**
- Body = **synthesis** = judgment → **agent tự viết** (chỉ agent biết toàn cảnh thân việc của mình).
- Header version/status = **cơ học** → **control-plane đóng dấu** từ manifest để **giữ version đáng tin**
  (nếu overview nói v3 thì manifest đúng là v3 — đây là chốt an toàn).

### 3.3 Field trong overview (chỉ những gì đổi quyết định dispatch)

- `status`: 🟢 green / 🟡 yellow / 🔴 blocked → có dispatch được không, hay phải gỡ blocker trước.
- `produces @ version`: artifact sẵn ở version nào → parent biết downstream có stale không.
- `last_artifact_path`: nếu quyết route, parent cầm sẵn pointer.
- `open_escalation`: có việc cần parent xử lý trước không.

> Những thứ KHÔNG đổi routing — schema đầy đủ, chi tiết preprocess, lịch sử — **không** nhét vào overview.

### 3.4 Template chung

```markdown
# OVERVIEW • <AGENT_NAME> • <YYYY-MM-DD HH:mm>
<!-- HEADER: machine-stamped, agent không sửa -->
**Status**: 🟢 Green | **Version**: <echo từ manifest, vd v1.4.2> | **Ready for parent**: Yes

────────────────────────────────────
<!-- BODY: agent-authored, holistic & brief, linh hoạt theo role -->
**Bức tranh tổng thể** (việc của tôi đang đứng ở đâu):
( agent tự viết theo bản chất công việc — xem mẫu theo role bên dưới )

────────────────────────────────────
<!-- FOOTER: pointers -->
**Key Pointers**:
- Latest artifact: <path>
- Full contract: <manifest path>#<section>
- Open escalation: <none | tóm tắt ngắn>
**Last updated**: <machine-stamped>
```

### 3.5 Mẫu BODY theo role (linh hoạt, không ép template cứng)

**DATASET (data agent):**
```markdown
**Bức tranh tổng thể**:
- Data version mới nhất: flood_2025_v3 + earthquake_2025_v2
- Schema: {asset_id, exposure_score, risk_level, geojson}
- Datasets hiện có: 142 assets | 3.2M records
- Preprocess đang dùng: histogram-eq + augmentation (no annotation)
- Quality: 98.7% completeness | drift 0.4% so v2
```

**VLM (model/eval agent):**
```markdown
**Bức tranh tổng thể**:
- Adapter: BaseVLM v2 @ cveval/models/qwen25vl.py
- Benchmark đã chạy: condition_cls (Kaggle v6) → acc 0.71
- Pending: detection task (chờ BaseTask mask-IoU)
```

**CRAFTER / SYNTHESIZER (tổng hợp):**
```markdown
**Bức tranh tổng thể**:
- Draft hiện tại: manuscript v0.4 (coverage 65%)
- Đã merge: 7-group records (CURATOR) + taxonomy (SYNTHESIZER)
- Citation count: 38 | còn thiếu: raw_records 42 entries
```

**VERIFIER / AUDIT (read-only):**
```markdown
**Bức tranh tổng thể**:
- Drift: 0 mismatch trên 5 team
- Violations mở: F-067 (metric-vs-source R032), F-069 (PRISMA count)
- Pass rate: 44/47 tool_records | Recommendation: re-verify R032
```

### 3.6 Guideline viết BODY (giữ linh hoạt nhưng có trục chung)

Body **tự do về câu chữ** nhưng **bắt buộc về trục**, để BOSS so sánh được giữa các agent
(tránh tình trạng CRAFTER viết narrative, DATASET viết bullet, VLM viết table → khó parse).

Quy ước cứng:
- **Độ dài**: ≤ ~150–200 từ (giữ "brief"; vượt → control-plane cảnh báo).
- **Hình thức**: **bullet `- `**, không narrative dài. Mỗi dòng 1 dimension.
- **Thứ tự logic**: (1) cái đang có/version → (2) cái đang làm → (3) cái thiếu/chặn.
- **Từ khóa BOSS dễ parse**: bắt đầu dòng bằng nhãn cố định + dấu hai chấm
  (`Version:`, `Pending:`, `Blocker:`, `Needs:`), để control-plane/BOSS quét nhanh.

**Dimension BẮT BUỘC theo role** (thiếu = control-plane flag "incomplete overview"):

| Role | Dimension bắt buộc trong body |
|---|---|
| **DATA / DATASET** | `Version:` · `Schema:` · `Size:` · `Quality/drift:` · `Method:` |
| **MODEL / VLM** | `Adapter@ver:` · `Benchmark+score:` · `Pending:` |
| **SYNTH / CRAFTER** | `Draft@ver:` · `Coverage%:` · `Merged-from:` · `Needs:` |
| **VERIFIER / AUDIT** | `Drift:` · `Violations mở:` · `Pass-rate:` · `Recommendation:` |

> Nhãn còn lại tự do. Mục tiêu: BOSS đọc 4 overview của 4 role khác nhau vẫn so sánh được
> "ai sẵn sàng / ai chặn / ở version nào" mà không phải diễn giải style riêng từng agent.

### 3.7 Overview của BOSS (cấp dự án) — hướng C: ANCHOR + ROLLUP (📋 chốt mitigate sau)

BOSS overview là **ca đặc biệt**: KHÔNG có parent đọc nó để route → **không bị nhân N** → chi phí chặn ở
**1 file/pass**; người đọc = **chính BOSS** (tự-định-vị đầu mỗi pass) **+ con người** (liếc nắm dự án).
→ Nó **được phép GIÀU HƠN overview con** (budget ~350–400 từ, không phải 200), vì làm giàu nó *thay* việc
BOSS phải dựng lại bức tranh từ progress.md + manifest mỗi lần.

**Hai khối tách bạch:**

**(A) ANCHOR — strategic state, ỔN ĐỊNH** (ít đổi → gần như không re-synthesize mỗi pass):
- `scope`: review/dự án này LÀ gì (1 dòng) — anchor cho người đọc cold.
- `lifecycle`: đang ở đâu trong vòng đời (vd PRISMA: identification ✓ → screening ✓ → tier ✓ → synthesis ✓ → **manuscript in-progress**).
- `funnel/headline`: số chốt định lượng (vd 157 identified → 26 included; tier 18/7/1) + đóng-góp 1 dòng.
- `frozen`: quyết định đã KHÓA (4-tier scheme · unit-of-analysis · search strings) — để khỏi mở lại.

**(B) ROLLUP — dynamic, SLIM** (như §3.7 cũ):
- `bottleneck/critical-path` · `next_milestone` · `risk` · `open decisions (pending user)`.

```markdown
# OVERVIEW • BOSS • <YYYY-MM-DD>
status: 🟡 yellow | lifecycle: synthesis ✓ → manuscript in-progress | ready_to_report: partial

──────────── ANCHOR (ổn định, re-write hiếm) ────────────
- scope: <1 dòng — dự án LÀ gì>
- funnel: 157 identified → 26 included | tier 18 T1 / 7 T2 / 1 T3
- headline: <đóng góp 1 dòng>
- frozen: 4-tier scheme · unit-of-analysis · 6 search strings

──────────── ROLLUP (động, refresh mỗi pass) ────────────
- bottleneck: <critical-path 1 dòng>
- next_milestone: <…>
- risk: 🔴 <…> · 🟡 <…>
- pending (user): <…>

──────────── FOOTER ────────────
team status chi tiết → state/children_status.json | open escalation → <…> | last_updated: <máy>
```

**Hai luật mấu chốt (vừa chữa "thiếu" vừa chữa "stale"):**
1. **KHÔNG mirror per-child status** (version/màu từng team) — đó là việc của `children_status.json` (luôn tươi).
   BOSS copy tay mấy số đó → trùng lặp + **stale ngay khi child bump** (lỗi thật: ghi 24 khi child đã 26).
   → ANCHOR/ROLLUP chỉ chứa **synthesis cấp-dự-án** mà children_status KHÔNG cho được.
2. **ANCHOR re-write rất hiếm** (chỉ khi scope/lifecycle/funnel/frozen thật đổi) → big-picture rẻ; **ROLLUP
   refresh mỗi pass** nhưng mỏng → giàu mà không tốn judgment mỗi lần.

> Vì sao tách: phần "bức tranh tổng thể" (scope/lifecycle/funnel/frozen) **hầu như không đổi giữa các turn**
> → thành ANCHOR thì BOSS khỏi re-synthesize mỗi pass mà người đọc vẫn đủ. ANCHOR cũng chính là
> **strategic standing-context** cho BOSS (§15.1/§15.2: nhận ra + frame quyết-định-hướng).

---

## 4. `progress.md` vs `overview.md` vs `[RESULT]` — 3 thứ KHÁC NHAU

| Artifact | Bản chất | Ghi kiểu gì | Ai đọc |
|---|---|---|---|
| **`[RESULT]` block** | *Delta của riêng turn này* | emit 1 lần/turn | → progress, → ledger của parent |
| **`progress.md`** | *Nhật ký* các delta | **append-only** | hầu như không ai đọc full (chỉ excerpt khi cần) |
| **`overview.md`** | *Toàn cảnh đang đứng ở đâu* | **ghi đè** mỗi turn, holistic | parent đọc để dispatch |

> Sai lầm cần tránh: lấy `[RESULT]` block đè thẳng vào overview → overview thoái hóa thành
> "ảnh chụp turn cuối". overview phải là **tổng hợp toàn cảnh**, không phải delta turn cuối.

---

## 5. Mức enforcement: "Thin Waist" (KHÔNG over-engineer)

### 5.1 Nguyên tắc: hard-code plumbing, soft-prompt judgment

- **Plumbing (cơ học, luôn-phải-xảy-ra):** sync, version-pin check, stamp header overview,
  append progress. → **control-plane lo.** Đây không phải reasoning; bắt model "nhớ" qua prompt
  là fragile (long session sẽ drift, quên update).
- **Judgment (suy luận):** chọn route, đánh giá chất lượng câu trả lời child, có nên escalate.
  → **để prompt + model (Opus) lo.** Không ép cứng.

### 5.2 Vì sao KHÔNG dùng State Machine 6-state / lifecycle hooks

- **Determinism giả**: FSM ép BOSS *bước vào* phase ANALYZE nhưng không ép nó *analyze tốt*.
  Đảm bảo "bước đã xảy ra" ≠ "bước đúng". Có giá trị cho plumbing, vô nghĩa cho judgment.
- **Tốn token ngược**: ép model "diễn" tag `[ANALYSIS]/[DECISION]` mỗi turn = thêm verbiage.
- **Phức tạp + fragile** với deploy thực (`agent_code_IDLE` off-cluster, `agentui.db` nhạy reload).
- **Mất tính per-agent**: luật trong control-plane là global/ẩn; luật đặc thù dự án nên ở
  `AGENT.md`/`team_role.md` (visible, git-track, AEC khác VLM được).

### 5.3 Phản trực giác quan trọng

> Đẩy **plumbing** vào control-plane lại **TIẾT KIỆM** token: vì xóa được nguyên khối prose
> "bạn phải làm phase 0→4" khỏi system prompt — khối đó hiện nạp lại **mỗi turn × mỗi agent**.
> Prompt-based enforcement mới là thứ âm thầm đốt token.

### 5.4 Control-plane chỉ ôm 4 guarantee cơ học (dừng ở đây)

1. `sync` + **version-pin check trước mỗi turn** (fail → block, không vào model).
2. Parse `[RESULT]`/`[ESCALATE]` block; **thiếu/sai shape → retry đúng 1 lần** (không FSM).
3. **Stamp + validate HEADER overview** (status + version echo từ manifest) + enforce overview
   được refresh cuối turn + enforce **độ dài "brief"** (vượt ngưỡng → cảnh báo).
4. **Append `progress.md`** từ `[RESULT]` block.

Mọi reasoning (route/đánh giá/escalate) → **prompt + judgment của model.**

---

## 6. Pipeline điều phối

### 6.1 BOSS / Parent (orchestrator = router + analyzer)

```
Phase 1 — PLAN & DISPATCH
  - Đọc children_status.json (rollup) + team_role.md → chọn agent (routing)
  - Đọc overview.md của agent được chọn + overview.md của chính mình (toàn cảnh dự án)
  - Output routing + message cho agent

Phase 2 — RECEIVE & ENRICH (control-plane tự làm)
  - Append <dispatch_result from="XXX"> vào message của BOSS

Phase 3 — ANALYZE & DECIDE (BOSS reasoning — judgment, KHÔNG ép FSM)
  - Phân tích response; đánh giá đủ/thiếu data
  - Quyết định: CONTINUE_CHAIN (route tiếp) | FINALIZE (trả user) | ESCALATE (xin thêm)

Phase 4 — EXECUTE DECISION
  - Continue → dispatch tiếp | Finalize → compile + trả user + refresh overview của BOSS
```

> BOSS đọc **manifest đầy đủ chỉ khi** quyết bàn giao một artifact cụ thể — không đọc full mọi lúc.

### 6.2 Child agent (executor) — workflow gọn

```
Phase 0 — PRE-FLIGHT
  - sync.sh (control-plane đảm bảo version-pin check)
  - Đọc inputs/manifest.md + overview của producer liên quan + Key Pointers từ BOSS

Phase 1 — RECEIVE & CLARIFY
  - Thiếu data/điều kiện → output [ESCALATE] ngay (xem §7)

Phase 2 — PLAN & DECLARE (declare-before-implement)
  - Khai "sẽ implement <ABC> v<X> tại <path>" — không khai = không tạo (chống orphan artifact)

Phase 3 — EXECUTE
  - Làm việc chính; tạo artifact → ghi path

Phase 4 — REFLECT & SNAPSHOT (cuối turn, nhẹ)
  - Tự viết BODY overview.md (toàn cảnh, ghi đè — KHÔNG append lịch sử vào đây)
  - Output [RESULT] block (delta turn) → control-plane append vào progress.md + stamp header overview
```

---

## 7. `[ESCALATE]` có TYPE (phân biệt "xin data" vs "xin hành động")

Định dạng thống nhất, dễ parse cho control-plane:

```
[ESCALATE]
type: DATA | BOSS_DECISION | HUMAN | SUBTASK | TOOL | BLOCKED
target: <CURATOR | BOSS | human | tool_name | ...>
payload: { ... }
reason: "..."
priority: high | medium | low
```

| type | Control-plane làm gì | BOSS can thiệp? |
|---|---|---|
| **DATA** | Auto `request_full_view` section cần → append cho child → resume | Không |
| **BOSS_DECISION** | Append vào ledger BOSS, pause child | Có |
| **HUMAN** | Pause run + notify, chờ human | Có |
| **SUBTASK** | Tự dispatch agent chỉ định, chờ result rồi append | Không (trừ target=BOSS) |
| **TOOL** | Thực thi tool → append kết quả | Không |
| **BLOCKED** | Pause + append full context lên BOSS + notify | Có |

**Làm rõ SUBTASK vs TOOL vs BLOCKED:**
- `SUBTASK`: target là **một agent trong hệ** (có manifest/overview) → control-plane dispatch agent đó,
  chờ `[RESULT]`, append về child. Nếu target=BOSS → BOSS phải can thiệp.
- `TOOL`: target là **công cụ external** (không phải agent, không có manifest — vd web search, script,
  API). Control-plane thực thi trực tiếp → append output thô. **Không** đi qua boundary manifest.
  → Quy tắc phân biệt: "có manifest/overview không?" Có → SUBTASK. Không → TOOL.
- `BLOCKED`: child **không tự đi tiếp được** vì lý do ngoài data/action cụ thể (mâu thuẫn contract,
  scope không rõ, deadlock chờ 2 producer) → pause + đẩy full context lên BOSS để arbitrate.

### ⚠️ Vá an toàn cho `type: DATA` (quan trọng)

Auto-resolve DATA-escalate **bypass BOSS** rất tiện nhưng nếu kéo thẳng raw của producer mà
**không pin version** → tái tạo bug "downstream vá quanh stale manifest".

**Bắt buộc**: data kéo về phải **kèm version producer tại thời điểm kéo**, ghi vào progress của
child dòng `consumed CURATOR@<ver>#records_42`. Producer bump version sau đó → drift check phải bắt
được. Boundary giữ nguyên, chỉ đi nhanh hơn.

**Thời điểm stamp (chính xác):** control-plane đọc version producer **đúng tại lúc đọc manifest để
fulfill cú pull**, và **stamp + ghi `consumed@ver` ngay trong cùng thao tác append** data cho child
(atomic: đọc-ver → append-data → ghi-consumed-line, không tách rời).
→ Không có "race condition phá correctness": `consumed@ver` luôn phản ánh **đúng** snapshot đã inject.
Nếu producer bump **sau** đó, sai lệch được phát hiện ở **PRE-FLIGHT sync kế tiếp** của child
(drift check so `consumed@ver` với version hiện tại) — không phải lỗ hổng, chỉ là độ trễ phát hiện
1 turn, đúng như mọi consumer khác trong hệ.

---

## 8. Tái dùng cái đã có (đừng phát minh lại)

- **`children_status.json`**: VLM **đã có** (control-plane sinh: `context_pct`, `message_count`,
  `last_activity`, `memory_headline`). → **Mở rộng** thêm `manifest_version` + `overview_path` +
  `status_color`. KHÔNG tạo `status.json` song song.
- **Cold-start preamble của `agent_code_IDLE`** hiện đã inject excerpt `progress.md` +
  `children_status.json` + head `manifest.md`. → **Thay phần head manifest bằng `overview.md`**
  để tránh nạp trùng.

---

## 9. Bảng "giữ / sửa / bỏ" so với hiện trạng

| Hạng mục | Quyết định | Ghi chú |
|---|---|---|
| Manifest = contract + version semver | **GIỮ** | Nền an toàn, không đụng |
| BOSS đọc full N manifest mỗi pass (AEC) | **BỎ** | Thay bằng overview + selective manifest |
| `overview.md` slim, holistic, theo role | **THÊM** | Body agent-viết, header máy-đóng-dấu |
| Tách `overview` (snapshot) / `progress` (history) | **THÊM** | overview ghi đè, progress append-only |
| `team_role.md` tĩnh cho routing | **THÊM/GIỮ** | (AEC có `team_map.md` — mở rộng) |
| `children_status.json` rollup | **GIỮ + MỞ RỘNG** | thêm version/overview_path/color |
| `[ESCALATE]` có type + vá version cho DATA | **THÊM** | giữ boundary khi auto-resolve |
| Validator + retry 1 lần | **THÊM** | lớp enforce nhẹ |
| State machine 6-state / lifecycle hooks | **BỎ** | over-engineer, determinism giả |
| Control-plane viết overview từ `[RESULT]` | **BỎ** | overview là synthesis = judgment = agent viết |

---

## 10. Migration & Backward Compatibility (hệ đang chạy)

Không big-bang. Chuyển từ "BOSS đọc full manifest" sang "BOSS đọc overview" theo 4 bước, **không phá vỡ**:

**Bước 0 — Fallback an toàn (làm TRƯỚC):** control-plane thêm 1 rule:
> Nếu `overview.md` của agent **chưa tồn tại** hoặc **rỗng** → fallback **đọc head manifest** (hành vi cũ).

→ Hệ vẫn chạy y như cũ kể cả khi chưa agent nào có overview. Đây là lưới an toàn cho toàn bộ migration.

**Bước 1 — Bootstrap overview lần đầu:** với mỗi agent cũ, **generate `overview.md` đầu tiên từ
manifest hiện có** (control-plane đọc header version + artifact mới nhất → đổ vào header + footer;
**body để placeholder** `*(chờ agent tự tổng hợp ở turn kế)*`). Đây là tác vụ 1 lần, không cần agent chạy.

**Bước 2 — Agent tự làm giàu body:** turn kế tiếp của mỗi agent, Phase 4 ghi đè body bằng bức tranh
thật theo §3.6. Sau 1 vòng, mọi overview đã có body thật → fallback Bước 0 gần như không còn kích hoạt.

**Bước 3 — Chuyển BOSS sang overview-first:** đổi PRE-FLIGHT của BOSS: đọc `children_status.json`
+ `overview.md` của team liên quan; **chỉ mở manifest khi bàn giao artifact**. Fallback Bước 0 vẫn nằm dưới.

> Thứ tự đảm bảo **luôn có đường lui**: nếu overview lỗi/thiếu ở bất kỳ điểm nào → tự rơi về đọc manifest.
> Migration là cộng thêm một lớp, không thay thế đột ngột.

**⚠️ Rủi ro lớn nhất của migration & mitigation:** ở Bước 1–2, overview có header (máy sinh) nhưng
**body còn rỗng/placeholder** → BOSS đọc overview mà **không có thông tin để judgment** (chỉ biết
version, không biết "đang làm gì / chặn gì").
→ **Mitigation**: (a) control-plane tự sinh placeholder body **tóm tắt từ artifact mới nhất của manifest**
(không để trống hẳn); (b) set cờ **`body_incomplete: true`** cho agent đó trong `children_status.json`
→ BOSS thấy cờ này thì **biết phải fallback đọc manifest** cho agent đó thay vì tin overview nửa vời.
Cờ tự gỡ khi agent ghi đè body thật ở Phase 4.

**Áp dụng cụ thể:**
- **VLM** đã có `children_status.json` → chỉ cần thêm overview + mở rộng field (§8). Migration nhẹ.
- **AEC** chưa có rollup → làm `children_status.json` trước (từ `team_map.md`), rồi theo 4 bước trên.

---

## Phụ lục — Diagram

**A. 3-tier context flow (parent route 1 task):**
```
            ┌─────────────────────────────────────────────┐
 task ─────▶│ 1. team_role.md (tĩnh)   → CHỌN ứng viên     │
            │ 2. children_status.json  → rollup trạng thái │
            │ 3. overview.md (mỏng)    → QUYẾT ĐỊNH route  │
            └───────────────┬─────────────────────────────┘
                            │ (đã quyết route artifact X)
                            ▼
            ┌─────────────────────────────────────────────┐
            │ 4. manifest.md (đầy đủ)  → CHỈ khi consume   │  ← không nạp lúc routing
            └─────────────────────────────────────────────┘
```

**B. Cấu trúc overview (ai ghi phần nào):**
```
┌──────────── HEADER ────────────┐  control-plane stamp (version echo ← manifest)
│ status · version · ready       │  agent KHÔNG sửa  → giữ version đáng tin
├──────────── BODY ──────────────┐  agent tự viết (synthesis = judgment)
│ bức tranh tổng thể, brief,     │  trục bắt buộc theo role (§3.6)
│ bullet, ≤200 từ                │
├──────────── FOOTER ────────────┐  pointers
│ artifact path · manifest# · esc │
└────────────────────────────────┘
```

**C. BOSS 4-phase:** `PLAN&DISPATCH → [CP enrich] → ANALYZE&DECIDE → EXECUTE` (xem §6.1).
Plumbing (xám) = control-plane; ANALYZE&DECIDE (judgment) = model, KHÔNG ép FSM.

---

## 11. Ghi chú phạm vi — cái gì KHÔNG nằm trong tài liệu này

Đây là **design doc** (kiến trúc + hợp đồng + template), **không** phải implementation spec.
Các thứ sau thuộc **artifact kế tiếp** (`control_plane_spec.md`), cố tình **không** đưa vào đây để
tránh lẫn tầng:
- Tên hàm cụ thể (`stamp_header_overview()`, `handle_escalate()`...) nằm trong `_run_agent` hay script riêng.
- Retry logic chi tiết: số lần, timeout, backoff.
- Schema JSON đầy đủ của `children_status.json` mở rộng (field + type + default).
- Regex/parser cho `[RESULT]`/`[ESCALATE]`.

> Việc thiếu các thứ này **không** phải lỗ hổng thiết kế — chúng là tầng hiện thực, viết sau khi
> design này được chốt.

---

## 12. Implementation Checklist (bắt đầu từ đâu)

Thứ tự ưu tiên để triển khai mà **không phá hệ đang chạy** (mỗi bước có đường lui):

**P0 — Lưới an toàn (làm trước mọi thứ):**
- [ ] Control-plane: thêm rule fallback "overview thiếu/rỗng/`body_incomplete` → đọc head manifest" (§10 Bước 0).

**P1 — Slim view + rollup:**
- [ ] Định nghĩa schema `overview.md` (header/body/footer) — file template/spec.
- [ ] AEC: tạo `children_status.json` từ `team_map.md` (VLM đã có).
- [ ] Mở rộng `children_status.json`: + `manifest_version`, `overview_path`, `status_color`, `body_incomplete`.
- [ ] Bootstrap: script sinh `overview.md` lần đầu cho mọi agent (header từ manifest, body placeholder).

**P2 — Agent tự làm giàu:**
- [ ] Thêm Phase 4 (REFLECT & SNAPSHOT) vào `AGENT.md` của mọi child + guideline §3.6.
- [ ] Thêm BODY-dimension bắt buộc theo role (§3.6) vào `AGENT.md` tương ứng.
- [ ] Tạo `BOSS/overview.md` cấp dự án (§3.7).

**P3 — Chuyển BOSS sang overview-first:**
- [ ] Sửa PRE-FLIGHT của BOSS: đọc `children_status.json` + overview team liên quan; manifest chỉ khi consume.
- [ ] Thêm pipeline 4-phase BOSS + `[ESCALATE]` có type vào `BOSS/AGENT.md`.

**P4 — Enforce (thin waist) + đo:**
- [ ] Control-plane: 4 guarantee cơ học (§5.4) + retry 1 lần.
- [ ] Viết `control_plane_spec.md` (tầng code — §11).
- [ ] Đo baseline trước/sau (§13).

> Files sẽ tạo/sửa: `overview.md` (mỗi agent), `BOSS/overview.md`, `children_status.json` (AEC mới),
> `*/AGENT.md` (Phase 4 + dimensions), `BOSS/AGENT.md` (PRE-FLIGHT + pipeline), `control_plane_spec.md`.

---

## 13. Đo lường lợi ích (mục tiêu cần VALIDATE — chưa phải số đã đo)

> ⚠️ **Chưa có con số thực nào** vì chưa triển khai. Phần này định nghĩa **đo CÁI GÌ + đo THẾ NÀO**,
> KHÔNG dán số bịa. Sau khi có P4 baseline mới điền số thật.

**Metric cần đo (trước vs sau migration):**
- **Token/pass của BOSS**: đếm input token mỗi orchestration pass (full manifest vs overview). Đây là
  lợi ích chính, kỳ vọng giảm đáng kể với AEC (BOSS đang đọc full 5 manifest có lịch sử) — **mức cụ thể
  phải đo**, không đoán.
- **Latency/pass**: thời gian từ nhận task → ra dispatch (giảm theo token nhưng phụ thuộc model/IO).
- **Tỉ lệ fallback**: % pass phải rơi về đọc manifest (đo độ "đủ dùng" của overview; cao = overview thiếu signal).
- **Overview staleness**: số lần `body_incomplete=true` khi BOSS đọc (đo chất lượng Phase 4).

**KHÔNG claim**: "giảm drift false-positive" — thiết kế này **không đụng** logic drift detection
(vẫn là so version manifest), nên không có cơ sở nói nó cải thiện drift. Đừng gán lợi ích không liên quan.

**Cách đo**: log token/latency ở control-plane cho N pass trên cùng một kịch bản (vd 1 vòng PRISMA của AEC),
chạy A/B (manifest-mode vs overview-mode) → so trung vị. Baseline AEC quan trọng hơn VLM (VLM vốn đã nhẹ).

---

## 14. Dispatch Model — Outcome / Pointer (hệ quả TRỰC TIẾP của §2)

### 14.1 slim-BOSS mất tư cách tự-soạn detail
Một khi BOSS chỉ đọc overview (§2), detail của việc **không nằm trong tay BOSS** mà ở child
(manifest/internals). → BOSS **không đủ thông tin để TỰ SOẠN task chi tiết**; nếu cố, nó hoặc phải
drill xuống manifest (đắt, phá tối ưu §2) hoặc **đoán** từ snapshot (sai + fragile).
**Hard-task-tự-soạn và slim-BOSS không thể cùng tồn tại.** Đây là **ràng buộc kiến trúc**, không phải sở thích.

### 14.2 Hai chế độ dispatch hợp lệ (khớp đúng info BOSS có)

| Chế độ | BOSS đặc tả (info nó CÓ) | Ai điền detail | Dùng khi |
|---|---|---|---|
| **Outcome dispatch** | *kết quả mong muốn* (mức overview) | child (có detail) | việc mở/sáng tạo |
| **Pointer dispatch** | *trỏ artifact đã pin* (`reproduce script@ver`) | artifact (detail nằm trong nó, không trong đầu BOSS) | việc cơ học/tái lập |

> **Nguyên tắc: BOSS chỉ dispatch OUTCOME hoặc POINTER — KHÔNG bao giờ tự-soạn DETAIL.**
> Vụ TIP/PRU hỏng vì ai đó *tự soạn* task thay thế (chế độ bị cấm) thay vì *trỏ* `bench_3arch.py` (Pointer).
> [[feedback_reproduce_before_rebuild]]

### 14.3 Ba loại goal (goal không nhị phân cứng/mềm)

| Loại | Ví dụ | Acceptance check | Stopping rule |
|---|---|---|---|
| **Threshold (cứng)** | `coverage ≥ 75%` | metric ≥ T (tuyệt đối, máy) | đạt T → stop. **Tự dừng được.** |
| **Directional (mềm)** | "retrain cho accuracy **tốt hơn**" | `new > baseline` (tương đối, máy) | **plateau là chính** (Δ<ε) + budget. **KHÔNG có vạch đích.** |
| **Judgment (định tính)** | "taxonomy mạch lạc" | VERIFIER/human phán | budget + satisfice (1 critique pass) |

> **Phản trực giác:** goal càng MỀM, limit càng KHÔNG thể thiếu. Threshold tự dừng khi vượt T;
> "cải thiện" không có trần → **không cap = lặp vô hạn đảm bảo**. Với directional, budget+plateau **là** vạch đích.

### 14.4 Stopping rule 3-nhánh (control-plane enforce, KHÔNG để child tự quyết)

| Tình huống | Ai phán | Hành động |
|---|---|---|
| Goal **đạt** | acceptance authority (metric/VERIFIER) — **không phải child** | STOP · success |
| Chưa đạt, **còn budget & đang tiến** | control-plane | lặp thêm 1 round |
| Chưa đạt, **plateau** | control-plane (no-progress detector) | STOP sớm · escalate |
| **Hết budget** | control-plane | STOP · escalate **partial** + blocker |

Hai failure mode bị chặn: **(a) lặp vô hạn** (cap + plateau); **(b) giả mạo hoàn thành** — acceptance do
**bên ngoài** phán, không để child tự chấm (áp lực budget + tự-chứng-nhận = động cơ bịa → research integrity cấm).

### 14.5 Outcome Contract — 4 phần (cập nhật từ §goal)
1. **Goal predicate** — 1 trong 3 dạng §14.3. Directional **bắt buộc** kèm `baseline_value` + `metric_pinned`
   (đo trên tập nào, thế nào — chống "cải thiện ma" do đo nhầm chỗ).
2. **Budget envelope** — turn/token cap. Càng mềm càng load-bearing.
3. **Scope fence** — "đạt goal chỉ bằng contract đã khai; goal KHÔNG cho phép vượt boundary/patch ABC → escalate".
4. **Acceptance authority** — metric tuyệt đối / metric tương đối / VERIFIER tùy dạng. Không bao giờ là child tự khai.

> Bonus: cap cũng là **máy dò goal sai** — goal bất khả thi biểu hiện = chạm cap + plateau → escalate surface nó lên.

---

## 15. Child Agency & De-bottlenecking (subset đã chốt)

Mở agency **chỉ ở chỗ không vượt một manifest boundary có-mutate mà không trọng tài**. Phân loại theo
autonomy tác động ở đâu: intra-agent = an toàn · external-tool = an toàn · cross-boundary-mutate = rủi ro.

| Cơ chế | Chữa | Trạng thái | Ghi chú |
|---|---|---|---|
| **Propose & Commit** | agency | ✅ làm trước | child *đề xuất* route trong `[RESULT].proposal`; BOSS *ratify* (rẻ hơn nghĩ-từ-zero). Boundary nguyên. |
| **BOSS Summary-only + Exception** | bottleneck | ✅ | control-plane **surface** `summary/status/goal_status` (cơ học), BOSS chỉ deep-analyze khi `blocked`/`goal missed`. **+ full-scan định kỳ mỗi N turn** (chống silent drift). |
| **Self-advance (capped)** | bottleneck intra-agent | ✅ | child `[CONTINUE_SELF]` nếu không blocker; **hard cap N turn + budget guard + notify BOSS khi chạm cap**. Tái dùng scheduler/continuation. |
| **Micro-orchestrator** | scale | ✅ chọn lọc | mini-BOSS cho sub-chain cohesive (AEC CURATOR↔SYNTHESIZER↔CRAFTER). KHÔNG cho chuỗi tuyến tính VLM. Code đã hỗ trợ qua field `parents`. |
| **Cross-agent self-dispatch** | bottleneck | ⚠️ mở có điều kiện | chỉ **TOOL** (external, không manifest) + **SUBTASK read-only**; mutating cross-agent giữ qua BOSS. Cần whitelist target + chống double-dispatch. |
| **Evaluative Self-Halt (Mức 2)** | agency + ROI | 📋 spec — chốt mitigate sau | worker tự DỪNG task giữa chừng khi phán "vô ích" (saturated/dead-end/false-premise), **report KHÔNG chờ ratify**; BOSS overturn sau. Bắt buộc **evidence**; quyền = dừng-effort-của-mình, KHÔNG = chốt kết-luận-dự-án. Xem §15.1. |
| **Forced-acknowledge Dissent** | agency (chặn hướng sai) | 📋 spec — chốt mitigate sau | worker **phản đối một HƯỚNG/quyết định** (không phải task của nó), raise BLOCKING-FLAG + evidence → BOSS **KHÔNG được im lặng cán qua**, phải explicit ratify/overrule; integrity-grade auto-loop VERIFIER/human. Vẫn **không veto**. Xem §15.2. |

> Lưu ý: "agency" và "bottleneck" là **hai vấn đề khác nhau**. Propose&Commit tăng agency nhưng KHÔNG
> giảm số hop BOSS; muốn giảm bottleneck phải dùng Summary/Self-advance/Micro-orchestrator.

### 15.1 Evaluative Self-Halt — worker được "nói đã bão hoà" (Mức 2, 📋 chốt mitigate sau)

**Vì sao cần (hệ quả của directional goal §14.3):** với goal mở/khám phá ("mở rộng corpus"),
tri thức *"vô ích rồi"* **sinh ra TRONG lúc worker làm** — BOSS không thể biết trước (chưa search
thì chưa có saturation). Nên đường đúng là **bottom-up**: worker **trồi phán xét lên + tự dừng**,
thay vì cắm đầu chạy hết task rồi trả kết quả ROI thấp. (Ca thật: DISCOVERER chạy hết NS1–NS9 →
+1 net-new near-saturated, đáng lẽ dừng sớm và báo "bão hoà".)

**Mức 2 (đã chọn):** worker **tự dừng task giữa chừng**, **chỉ report — KHÔNG chờ BOSS ratify mới
được dừng**. BOSS **overturn** sau nếu cần (re-dispatch "continue, halt sai vì…"). Nhanh hơn Mức 1,
đổi lại tin worker nhiều hơn.

**Ranh giới quyền (mấu chốt — đừng lẫn):**
- Worker **CÓ** quyền: dừng **effort của CHÍNH NÓ** (thôi đốt budget cho việc nó xét là low-value).
- Worker **KHÔNG** có quyền: chốt **kết-luận cấp-dự-án** ("corpus đã đầy đủ → freeze 26"). Kết luận đó
  vẫn do **BOSS/VERIFIER/user ratify**. Halt + evidence chỉ là **INPUT** cho quyết định, không phải quyết định.

**Guardrail chống tự-phục-vụ (cùng nguyên tắc goal-gate):** "saturated" có thể là claim lười để né việc.
- `[HALT]` **bắt buộc evidence định lượng** (đã làm gì · tín hiệu số · nguồn nào cạn). Halt rỗng/mơ hồ
  → **invalid** (control-plane xử như malformed → retry đòi evidence, hoặc hạ xuống `[RESULT].proposal` soft).
- BOSS overturn **rẻ** → halt sai chỉ tốn 1 re-dispatch, KHÔNG sụp integrity.
- Halt được **log** (audit) → agent halt quá thường (kêu bão hoà để né) → flag (monitoring, future).

**Phân biệt với cơ chế cũ:**
- `[ESCALATE] BLOCKED` = *bất lực* ("không đi tiếp được"). **Halt = phán xét** ("đi được nhưng không đáng").
- `[CONTINUE_SELF]` = "chưa xong, đang tiến, cho chạy tiếp". **Halt = ngược lại**: dừng sớm có chủ đích.
  Hai mặt của autonomy worker với effort của mình.
- `goal_status: missed` = *thất bại thụ động*. **Halt = kết luận chủ động + khuyến nghị hướng** → đóng khung
  early-stop thành **đóng-góp-thông-minh**, không phải fail.

> Bản chất: biến worker của directional-goal từ executor mù → **người-suy-nghĩ một cấp dưới routing**
> (đúng triết lý "giao task + goal, không giao detail" của §14).

### 15.2 Forced-acknowledge Dissent — worker được "chặn hướng sai" (📋 chốt mitigate sau)

**Phân biệt với Self-Halt:** `[HALT]` = "dừng **effort của TÔI**" (về task của worker). `[DISSENT]` =
"chặn **một HƯỚNG/quyết định**" — kể cả khi đó không phải task của worker, và buộc BOSS phải đối mặt.
HALT về *task*, DISSENT về *direction*.

**Vì sao cần:** "hướng sai" có 2 loại, xử khác nhau:
- **Sai vì integrity** (bịa số, count không reconcile, ép gỡ `[VERIFY]`, method invalid) → **ĐÃ chặn được**
  bằng research-integrity Rule 0 + **refuse-to-fabricate** + `[ESCALATE] HUMAN`. Agent không thể bị ép →
  đây là near-veto thật, không cần thêm gì.
- **Sai vì strategy/judgment** (taxonomy phân sai, scope dở, hướng low-value) → hiện **chỉ có VOICE**
  (`[ESCALATE] BOSS_DECISION` / `[HALT].recommendation` / `proposal`): surfaced nhưng **không blocking** →
  BOSS có thể **im lặng cán qua**. Đây là lỗ hổng `[DISSENT]` vá.

**Cơ chế (Mức 2 — Forced-acknowledge, KHÔNG veto):**
- Worker emit `[DISSENT]` nhắm vào một **direction/artifact/decision** + **evidence** → control-plane ghi
  một **BLOCKING-FLAG (open)** lên direction đó.
- BOSS **KHÔNG được proceed** trên hướng bị dissent nếu chưa **explicit ratify** (đi tiếp, kèm lý do) hoặc
  **overrule** (đổi hướng). Forcing function: control-plane chèn "OPEN DISSENT phải xử" vào context BOSS;
  cả dissent lẫn cách BOSS xử đều **on record** (accountability — nếu ratify sai sau này truy được).
- **Integrity-grade dissent** → auto-loop **VERIFIER/human** (không để mình BOSS chốt).

**Ranh giới quyền (đối xứng Self-Halt §15.1):**
- Worker **CÓ**: raise một blocking-flag có sức nặng — không bị steamroll.
- Worker **KHÔNG**: **veto** (tự chặn cứng). Veto = "worker tự quyết thay cả hệ" → một worker sai sẽ treo
  cả dự án. Ratify/overrule vẫn ở **BOSS/VERIFIER/human**.

**Guardrail (cùng nguyên tắc goal-gate/HALT):** `[DISSENT]` **bắt buộc evidence** (dissent rỗng → invalid →
retry/đòi evidence). **Dissent-rate monitoring**: worker spam dissent để cản → flag (future). Dissent chỉ
gate **đúng hướng bị phản đối**, không treo việc khác.

> Đối xứng hoàn chỉnh: HALT = worker dừng *effort mình*; DISSENT = worker chặn *một hướng* (forcing, không
> veto). Cả hai: **được trồi tiếng nói có sức nặng + kèm evidence, ratify vẫn ở ngoài** — đúng triết lý
> "agency = nói lên, không phải tự quyết thay hệ".

---

## 16. Incremental Verification & Delta-Processing — nguyên lý KIẾN TRÚC CHUNG (📋 đo 2026-06-27, chốt mitigate sau)

### 16.0 Đây là thuộc tính kiến trúc, KHÔNG riêng AEC

**Cơ chế gốc (chung mọi hệ trên agentui):** turn `claude -p` là process **stateless** — muốn sửa artifact
phải Read lại nó vào context (→ cache_creation), cộng history resume phình dần. → **Cost ∝ kích-thước-working-set,
KHÔNG ∝ kích-thước-delta.** Hai mặt: **writer re-GENERATE** (cost ở output) · **auditor re-READ** (cost ở cache).

**Severity do data-shape dự án — 3 trục:**
| Trục | Nặng khi | Nhẹ khi |
|---|---|---|
| **Artifact granularity** | monolithic blob (1 manuscript 163k, 1 JSON to) | nhiều file nhỏ / chunked |
| **Coupling / cascade** | tight (count reconcile xuyên N stage — vd PRISMA) | agent độc lập, isolated |
| **Tần suất × size** | đổi thường trên artifact to | đổi hiếm / nhỏ |

**Hệ nào bị nặng/nhẹ:**
| Loại hệ | Mức | Vì sao |
|---|---|---|
| Sửa **CODE** (MagAI/ADE) | 🟢 nhẹ nhất | filesystem **vốn chunk** — sửa 1 hàm = 1 file, incremental by default |
| **Document/synthesis** (AEC, DFU EVAL manuscript, Hoang paper) | 🔴 nặng | artifact = 1 blob → sửa tí = re-generate/re-read cả khối |
| **AEC** | 🔴🔴 worst-case | monolith + PRISMA cascade chặt + thêm-candidate thường xuyên |

**Inherent vs Fixable:** *inherent* — reload một phần state + history phình (đã có **auto-compact 80%** chặn phần history);
*fixable* — **artifact chunking** (16.6) + **surgical edit thay vì re-generate** + **delta-watermark** (16.1).
Granularity là trục MagAI **có sẵn nhờ filesystem** mà hệ paper/synthesis chưa.

**Bằng chứng (AEC = ca grounding, đo THẬT):** cả pipeline re-process FULL trên mỗi delta —
- *Token* (logger): +2 benchmark → cascade **~1.8M ctx-tok** (CRAFTER re-write 163k-char manuscript 402k cr/82k out;
  SYNTHESIZER 383k; VERIFIER 504k). Orchestration bơm chỉ **~3k** → overview-mode đã rẻ; cost thật ở **agent re-load working-set**.
- *Verify churn*: findings.md 173KB/72-block, **86 re-work mention**, F-2026 **47×** F-066 **22×**;
  `verification_log` **không có cột verified-at-version** → không có skip.

**Nguyên lý (cùng "delta-not-full" của §2/§7):** đừng re-process/re-verify thứ **chưa đổi version**.
"Verified @version" = "consumed @version" = "overview thay full-manifest". **Áp cho MỌI hệ, không riêng AEC.**

### 16.1 Verified-watermark (delta-verify) — fix chính
- `verification_log` thêm cột `verified_at: <producer>@<semver>` per benchmark/claim.
- Pass VERIFIER: drift-check → mỗi record, nếu `source-version == verified_at` → **SKIP** (tin verdict cũ);
  chỉ re-verify record **đổi** + **mới**. **O(N mỗi đổi) → O(Δ).**
- Integrity giữ nguyên: watermark khóa theo **semver producer** (anchor an toàn sẵn có, §6.5/B8). Producer bump →
  watermark invalid → re-verify. Không tin mù — "verified @ đúng version X, X chưa đổi → còn hiệu lực".

### 16.2 Monotone findings
- Closed finding mang `closed_at: <target>@<version>`; **chỉ reopen khi target version vượt qua đó**.
  Hết re-confirm finding đã đóng (chữa churn F-066/F-067).

### 16.3 Tiered verify intensity (đúng model cho đúng loại check)
- **Cơ học/deterministic** (prisma-arith reconcile · dedup · double-count · drift · schema-align) → **script/sonnet rẻ,
  chạy MỖI pass** (nhanh, luôn phải đúng).
- **Judgment/web** (existence · metric-vs-source · identifier-resolve qua WebFetch) → **opus + chỉ DELTA + chỉ tại
  publish-gate**. Hiện opus làm cả hai = phí lớn nhất.

### 16.4 Batch-at-gate
- Gom thay đổi; pass identifier-resolution + metric-vs-source **đầy đủ chạy 1 lần/milestone (pre-publish)**,
  KHÔNG per-PATCH. Check cơ học (16.3) vẫn chạy liên tục.

### 16.5 Tổng quát hóa cho producer (cùng bệnh)
Token data cho thấy CRAFTER/SYNTHESIZER cũng full-re-process. Nguyên lý áp được: **incremental manuscript edit**
(chèn 2 dòng, không re-write 163k-char), **incremental synthesis** (cập nhật ô đổi). Verify là ca làm trước;
producer là mở rộng.

### 16.6 Artifact chunking — trục đòn-bẩy cross-system (cái MagAI có sẵn, hệ paper chưa)
**Gốc rễ severity nằm ở granularity (16.0):** code-system nhẹ vì **filesystem đã chunk** artifact thành nhiều file
→ sửa = đụng 1 file, surgical by default. Hệ document/synthesis nặng vì artifact là **1 blob** (manuscript 163k,
benchmarks.json 297KB) → sửa tí = re-load/re-generate cả khối. **Đây là đòn bẩy gốc, đứng trên cả delta-watermark.**

Cách áp (mọi hệ paper/synthesis):
- **Chunk artifact theo đơn-vị-thay-đổi-tự-nhiên**: manuscript → 1 file/§ (hoặc 1 file/record-row trong catalog);
  corpus → 1 file/record thay vì 1 JSON khổng lồ; synthesis → CSV per-dimension (AEC đã làm phần này — 11 CSV).
- **Manifest trỏ tới chunk** (đã đúng triết lý §2: manifest = pointer, không copy body) → consumer/auditor mở
  **đúng chunk đổi**, không cả blob.
- **Surgical edit (Edit) thay vì re-Write toàn file**: agent sửa đúng đoạn → output ∝ delta, không ∝ artifact.
  (Behavioral — nhắc trong AGENT.md: "không re-generate cả doc để thêm 1 dòng".)

> Quan hệ tầng: **chunking (16.6) cắt cost ở GỐC** (đơn vị làm việc nhỏ lại) → **delta-watermark (16.1) + monotone
> findings (16.2)** cắt phần còn lại (skip chunk chưa đổi). Hai cái cộng hưởng: chunk nhỏ + skip-unchanged = O(Δ) thật.

> ⚠️ **KHÔNG cắt integrity core:** chỉ bỏ **re-check thứ chưa đổi** + **dùng đúng model cho đúng loại check**.
> Độ phủ integrity y nguyên — đây là *delta-aware*, KHÔNG phải *buông*. SPEC ONLY, chốt mitigate sau.

---

## 17. Chốt một câu

> **Manifest vẫn là luật; `overview.md` là lớp mỏng holistic do agent tự tổng hợp để parent
> đọc-nhanh-mà-route, với version do control-plane đóng dấu để luôn đáng tin.**
> Tiết kiệm token bằng cách *tách tầng đọc*, KHÔNG bằng cách vứt bỏ tính an toàn version/drift.
> Enforce ở mức "thin waist": control-plane lo plumbing (sync/version/stamp/append),
> model lo judgment (route/đánh giá/escalate). Dừng ở đó — đừng leo state machine.
