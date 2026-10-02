# BUILD HANDBOOK — Implementation hiện tại

Đối chiếu working tree ngày **2026-09-07**. File này mô tả code hiện có trong
`app/backend/`; [system_architech.md](system_architech.md) giữ rationale và thiết kế
mục tiêu. Các đoạn pseudocode cũ theo bản “2508 dòng” đã được thay bằng tên hàm,
hành vi thực tế và giới hạn. Xem [checklist](BUILD_HANDBOOK_checklist.md) và
[audit](docs/documentation-audit.md) để biết phạm vi xác minh.

## 0. Bản đồ implementation

| Cơ chế | Nguồn | Hiện trạng |
|---|---|---|
| Detached run / continuation | `main._start_run`, `_Run`, `_dispatched_run` | Có; tối đa 3 root continuations |
| Ledger feedback | `_format_results_as_context`, `db.dispatch_results` | Có; prompt enrichment và consume sau `ok` |
| Slim preamble | `_session_preamble` | Có; own overview + recent progress + input head |
| Parent pre-flight | `_children_overview_context` | Có; every-turn đọc overview mọi direct child |
| Rollup | `_write_children_rollups` | Có; ID-keyed `children`, sinh qua stats/rollup |
| Structured blocks | `_parse_structured`, `_run_agent` | Có; optional, một corrective retry |
| Version pins | `_check_version_pins` | Best-effort Python parser + prompt warning; không gọi sync.sh/hard-return |
| Overview stamp | `_stamp_overview` | Version/date/placeholder heuristic; không validate đầy đủ role schema |
| Escalations | `db.open_escalation`, `_handle_escalate` | DB lifecycle + ledger parent đầu tiên; không auto-resolve DATA/TOOL/SUBTASK |
| HALT / DISSENT | `_handle_halt`, `_handle_dissent`, `_handle_dissent_resolve` | Có DB và feedback; dissent là prompt guidance |
| Verification watermark | `_verify_delta`, `_verify_delta_hint` | So version và inject skip hint; không chặn tool re-audit |
| Goal acceptance | `_parse_goal`, `_dispatched_run` | Ghi nhận claim, nhắc external acceptance; không tự đo metric/budget |
| Memory reconciliation | `_run_memory_reconciliation` | Opt-in; receipt/hash/pointer validation và provenance publish gate |
| Progress rotation | `progress_store`, `_progress_rotate_tick` | Opt-in global; giữ hai ngày hoạt động gần nhất |

Có code không đồng nghĩa đã deploy/đã qua live acceptance. Ranh giới hard check,
prompt guidance và target design cần được giữ rõ khi mở rộng hệ thống.

## 1. Layout và nguồn sự thật

```text
<AGENT>/
├── AGENT.md
├── overview.md
├── inputs/manifest.md
├── inputs/<PRODUCER>.md       # copy khi sync cụ thể
├── outputs/manifest.md
├── context/code_map.md
└── state/
    ├── progress.md           # dated Markdown, hoặc JSON sau explicit migration
    ├── children_status.json  # derived cho parent có children
    └── archive/<AGENT>_progress.json
```

`projects._AGENT_TEMPLATE_FILES` render sáu file từ `app/backend/templates/agent/`.
`TEMPLATE_AGENT/` là bản tham khảo để copy tay, không được loader đọc trực tiếp.
`_agent_dir` resolve qua cwd/system prompt/agent ID; không mặc định mọi project có
layout `<root>/<ID>` nếu cấu hình tùy chỉnh.

Manifest là contract/version và evidence pointers. Với memory reconciliation,
manifest còn chứa current technical truth theo chủ đề; overview là projection từ
manifest, progress là lịch sử delta. Chi tiết receipt ở §12.

## 2. Overview: format và kiểm tra thật

```markdown
<!-- OVERVIEW:HEADER -->
# OVERVIEW • WORKER • 2026-09-07
status: yellow
manifest_version: 0.1.0
ready_for_parent: no
body_incomplete: true
<!-- /OVERVIEW:HEADER -->
<!-- OVERVIEW:BODY -->
- (chờ first task) — owner tổng hợp trạng thái thật tại đây.
<!-- /OVERVIEW:BODY -->
<!-- OVERVIEW:FOOTER -->
last_artifact: none
manifest_ref: ./outputs/manifest.md
open_escalation: none
last_updated: 2026-09-07
<!-- /OVERVIEW:FOOTER -->
```

Owner viết BODY và trạng thái do mình đánh giá. `_stamp_overview` đóng dấu version
thực, ngày và `body_incomplete`: BODY chứa `(chờ` hoặc ngắn hơn 40 ký tự → incomplete.
Nó không kiểm tra enum status/readiness, đủ nhãn theo role, path `last_artifact`,
hoặc hard-limit 200 từ. Nhãn role và độ dài gọn là hướng dẫn tác giả.

Project opt-in reconciliation thêm `memory_status`, `source_manifest_sha256`,
`derived_at`, escalation IDs; semantic lint có word-budget warning. Hash/receipt
xác minh provenance cơ học, không tự chứng minh mọi claim khoa học.

Prompt projection có thể cắt BODY: own 4.000 ký tự, child 6.000; HEADER/FOOTER vẫn
đầy đủ khi marker hợp lệ. File trên đĩa không bị cắt bởi việc đọc projection.

## 3. `children_status.json` hiện tại

`_write_children_rollups` nhận stats và sinh cấu trúc **object keyed by ID**:

```json
{
  "generated_at": "2026-09-07T12:00:00Z",
  "generated_by": "agentui control-plane (DERIVED, read-only — do NOT hand-edit)",
  "parent": "BOSS",
  "digest": "example-digest",
  "children": {
    "WORKER": {
      "status": "idle",
      "context_pct": null,
      "context_tokens": null,
      "message_count": 0,
      "last_activity": null,
      "last_activity_iso": null,
      "memory_mtime": null,
      "memory_updated_iso": null,
      "memory_headline": null,
      "memory_hash": null,
      "stale_memory": false,
      "manifest_version": "0.1.0",
      "overview_path": "../WORKER/overview.md",
      "body_incomplete": true
    }
  }
}
```

Đây là ví dụ minh họa shape, không phải snapshot production. Không có `$schema`,
`status_color` hoặc `open_escalation` trong từng child row hiện tại. Các status được
map qua `_JOB_STATUS`. `overview_path` đang được tạo theo `../<ID>/overview.md`;
đường dẫn này có thể không phản ánh cwd tùy chỉnh. Parent pre-flight thực tế dùng
`_agent_dir` để đọc file. Digest tránh rewrite khi chỉ thời gian thay đổi.

## 4. Grammar được parser hỗ trợ

```text
[RESULT]
summary: Delta của lượt này
artifacts: outputs/result.json
status: green
ready_for: back_to_boss
goal_status: partial
goal_evidence: Evidence pointer
proposal: Nhờ verifier kiểm tra
verified: PRODUCER@0.1.0
resolves: esc-0123456789ab
[/RESULT]
```

Các field là quy ước theo task, không phải tất cả đều bắt buộc trong parser.
Parser lấy block đầu tiên mỗi loại và đọc key/value trên một dòng; không phải
trình parse YAML lồng nhau. Không emit block nào vẫn hợp lệ.

| Block | Check parser hiện tại | Xử lý sau turn |
|---|---|---|
| `[RESULT]` | Key/value, balanced canonical tags | Watermark/resolution fields; overview stamp |
| `[ESCALATE]` | Type ∈ DATA/BOSS_DECISION/HUMAN/SUBTASK/TOOL/BLOCKED | Tạo DB row + route parent đầu tiên |
| `[HALT]` | Evidence không rỗng | `halt_log` + ledger; không tự resume |
| `[DISSENT]` | Evidence và against không rỗng | `dissent_flags` + ledger + every-parent-turn warning |
| `[DISSENT_RESOLVE]` | Verdict ratify/overrule | Resolve theo project và against |

Dùng tag uppercase canonical. Regex body không phân biệt case nhưng bộ đếm tag
balance hiện dùng chuỗi uppercase. Evidence không rỗng chưa đồng nghĩa evidence
định lượng/chính xác. `_HALT_REASONS` được khai báo nhưng parser chưa enforce enum đó.

Malformed ở lần đầu → một corrective turn (`retry_count=1`). Lần thứ hai vẫn có thể
đi qua structured finalization; không có hard rejection riêng. Không mô tả cơ chế
này là bảo đảm mọi block cuối cùng đều hợp lệ.

`[CONTINUE_SELF]` chưa có parser/resume path. `[HALT]` không tạo run-status mới:
lượt model sạch vẫn có thể mang status `ok` với halt record riêng.

## 5. Quy trình tác giả agent

- Boot đọc shared rules, own overview và input pins; không đọc toàn bộ progress.
  Parent nhận current child overviews every turn; rollup nằm trong cold-start khi có.
- Khi có drift, sync producer cần dùng rồi kiểm tra lại trước khi consume artifact.
- Viết artifact đúng scope; cập nhật manifest/version khi contract thay đổi.
- Thêm dated progress entry và cập nhật overview bằng trạng thái tổng thể, không
  lấy nguyên delta cuối làm overview. `[RESULT]` không tự append progress trong backend.
- Nếu opt-in memory reconciliation, owner làm internal reconciliation theo §12.
- Route bằng outcome/pointer đủ rõ; acceptance vẫn thuộc verifier/parent/user theo contract.

## 6. Enforcement và giới hạn

### 6.1 Ledger và continuation

Xem [dispatch lifecycle](docs/agentui-dispatch-spec.md). Root continuation cap là 3,
không phải cap số worker hay token trong một task. Browser disconnect không cancel.

### 6.2 Drift

`_check_version_pins` nhận dòng `- PRODUCER: version` trong input rollup, so với
version đọc được từ output manifest. File thiếu/không parse được có thể cho kết quả
rỗng. Có drift thì `_run_agent` prepend warning và yêu cầu agent tự sync; model vẫn chạy.

### 6.3 Escalation

`[ESCALATE]` tạo state open/resolve trong DB, route vào ledger của parent đầu tiên;
root chỉ có state/meta. DATA không tự pull, SUBTASK không tự dispatch, TOOL không tự
execute, HUMAN không tạo workflow approval độc lập. Parent/user quyết bước tiếp theo.

### 6.4 Dissent

Open flags được đưa vào prompt của các orchestrator trong project, tới khi resolve.
Không có kiểm tra semantic target để hard-reject dispatch, không auto-loop verifier
cho integrity-grade dissent, và chưa có quyền resolve riêng được enforce chỉ cho parent.

### 6.5 Verification watermark

`verified: PROD@ver` được lưu từ claim của agent. `_verify_delta` so version thực
với watermark đã có, tạo skip/reverify sets; đây là deterministic comparison rồi
prompt hint. Nó không cưỡng chế tool skip, không hash artifact, và không tự discover
producer chưa có watermark. `_verify_delta_hint` chỉ emit khi skip set không rỗng.
Monotone findings, tiered verification và structural chunking vẫn phụ thuộc project.

### 6.6 Goal acceptance

`_parse_goal` đọc type/predicate/baseline_value/metric_pinned/acceptance_by.
`_dispatched_run` thêm lời nhắc claim ≠ acceptance vào ledger. Chưa có `_check_acceptance`,
metric reader, plateau detector hoặc parser/enforcement cho budget turns/tokens.
Không tự dispatch verifier chỉ vì có `acceptance_by: VERIFIER`.
Manifest publication check dùng mtime, không bảo đảm semantic version đã tăng.

## 7. Sync và progress

`projects.generate_sync_sh` render script từ `templates/sync.sh.tmpl`; backend không gọi
script tự động. Agent/user gọi script khi cần. Script dùng mapping sinh lúc scaffold,
không tự refresh mapping khi chỉnh topology về sau. Script `check` in version/pin
rồi exit 0 ngay cả khi lệch; không có drift exit code 3. Template shell đọc `## Version`,
trong khi Python version reader còn hỗ trợ `version:`. Không coi hai parser là tương đương.

Progress reader ưu tiên `state/progress.json`; nếu không tồn tại mới parse Markdown.
Cold start lấy hai ngày **có entry** gần nhất, sort theo ngày/giờ và cap excerpt.
Rotator global mặc định off; khi bật, tick theo giờ và chạy một pass ngay lúc bật.
Markdown rotation giữ hot Markdown, merge phần cũ vào archive JSON; explicit migration
API mới chuyển sang hot JSON. Đừng ghi riêng hai hot log vì reader sẽ ưu tiên JSON.

## 8. Bootstrap và migration

New-project/new-agent UI render sáu file cùng shared templates và project config.
Project mới có `paper_collection/README.md`/`CATALOG.md`. Có API explicit migrate progress;
chưa có bulk migration tổng quát để viết overview có nội dung thật cho mọi project cũ.
Memory reconciliation phải được opt-in rõ ràng, không suy ra từ việc file overview tồn tại.

## 9. Worked flow phù hợp implementation

Ví dụ minh họa: BOSS dispatch CRAFTER cần dữ liệu CURATOR.
CRAFTER emit `[ESCALATE] type: DATA`, target CURATOR và evidence/pointer cần thiết.
Backend ghi escalation và ledger cho BOSS; **không tự pull raw data**.
Root continuation nhận kết quả, rồi quyết định dispatch CURATOR nếu là direct child
hoặc đưa yêu cầu về parent phù hợp. CRAFTER được giao tiếp khi input đã sẵn sàng.
Worker cập nhật artifact/manifest/progress; ledger phản hồi lại BOSS. Đây là flow
minh họa, không phải claim đã chạy live trên một corpus cụ thể.

## 10. Kiểm tra

Existing tests gồm `test_memory_receipt.py`, `test_escalation_state.py`, adapter,
capability inventory, terminal và các suite Notion/evaluation. Chạy test trong DB/
registry tạm; import `main.py` khởi tạo DB và có startup bookkeeping ngay lập tức.
[audit](docs/documentation-audit.md) ghi chính xác các kiểm tra đã chạy trong đợt này;
không coi kết quả pilot lịch sử là kết quả regression mới.

## 11. Phần còn thiếu

Hard reject sau corrective retry thứ hai; generic metric/budget/plateau enforcement;
typed escalation auto-resolution; direction-level dissent veto/role enforcement;
`CONTINUE_SELF`; global worker lock; bulk project migrations và structural chunking.
Đây là backlog/giới hạn được ghi nhận, không phải thay đổi code trong đợt cập nhật tài liệu.

## 12. Memory reconciliation có backend enforcement (GelSight pilot, 2026-08-11)

> **Có implementation, opt-in theo project.** Đợt audit 2026-09-07 đối chiếu code,
> không xác nhận trạng thái service hoặc rollout của từng project. Escalation DB áp dụng
> cho structured finalization nói chung; projection footer từ DB và receipt publish gate
> phụ thuộc memory policy. Các mô tả pilot bên dưới là ghi nhận lịch sử.

### 12.1 Ba file, ba audience, một chiều dẫn xuất

Luồng memory bắt buộc là:

```text
delta của turn → state/progress.md → outputs/manifest.md → overview.md:BODY
                         history         current truth        parent projection
```

| File | Chức năng | Audience | Không được biến thành |
|---|---|---|---|
| `state/progress.md` | Nhật ký delta có thời gian: đã thử gì, thay đổi gì, evidence, outcome | audit/resume gần | technical report hiện tại |
| `outputs/manifest.md` | Nguồn sự thật kỹ thuật hiện tại của chính owner; tổ chức theo chủ đề | agent owner | changelog theo turn/ngày |
| `overview.md:BODY` | Bản tóm tắt gọn được **rút ra từ manifest**, chỉ giữ thông tin làm thay đổi routing/accept/block/stop | parent + human | bản sao manifest hoặc nơi chứa claim mới |

Chiều `manifest → overview` là một chiều. Không dùng overview để tái tạo
manifest. Mọi claim kỹ thuật trong overview phải truy được tới một section
hiện có trong manifest hoặc evidence pointer do section đó nêu.

Reconciliation **không tự cắt file memory theo character/word** và không tự viết technical prose.
Riêng prompt projection có cap BODY (4.000 ký tự cho own overview, 6.000 cho child) nhưng giữ HEADER/FOOTER.
Ngưỡng overview (GelSight: 240 words) chỉ sinh warning; agent phải rewrite semantic,
không truncate vì truncate có thể làm mất thông tin.

### 12.2 Ai thực hiện reconciliation

Sau mỗi primary turn thành công có nội dung, control-plane gọi **chính agent owner đó** thêm
một internal turn hẹp mang tên `MEMORY_RECONCILIATION`:

1. Owner kiểm tra delta ý nghĩa của task và update progress nếu có thay đổi thật.
2. Owner reconcile manifest in-place nếu current technical truth thay đổi; không append lịch sử.
3. Owner derive `OVERVIEW:BODY` từ manifest đã finalize.
4. Backend validate receipt và quyết định có publish overview mới hay không.

Parent không viết memory cho child. Backend chỉ orchestration, hash, validate pointer,
restore/publish và stamp machine-owned fields. UI emit status `reconciling_memory` trong pha này.
Pha này không được browse, train, dispatch, tiếp tục primary task hay sửa artifact
ngoài ba file memory của owner.

### 12.3 Receipt bắt buộc và validation theo hash thật

Control-plane chụp hash baseline **trước primary turn**, không phải chỉ trước pha
reconciliation. Vì vậy edit mà agent đã làm ngay trong primary task vẫn được
tính là `updated`.

Owner kết thúc internal turn bằng block:

```text
[MEMORY_RECONCILED]
status: ok
durable_delta: <technical|coordination|none>
progress: <updated|unchanged>
progress_ref: <dated heading fragment or none>
manifest: <updated|unchanged>
manifest_ref: <manifest section or none>
overview: <updated|unchanged>
overview_manifest_refs: <manifest section(s), separated by |, or none>
overview_reason: <why parent routing changed or remained unchanged>
resolved_escalations: <explicit esc-ID(s) or none>
[/MEMORY_RECONCILED]
```

Backend không tin lời khai `updated/unchanged`; nó so với hash cuối whole-turn:

- `progress` và `manifest` hash toàn file; riêng `overview` chỉ hash BODY (HEADER/FOOTER do máy sở hữu).
- File khai `updated` phải thật sự đổi hash và pointer phải match text/heading có thật.
- File khai `unchanged` nhưng hash đổi là receipt invalid.
- `durable_delta: none` mà bất kỳ memory hash nào đổi là mâu thuẫn.
- Technical/coordination delta nhưng manifest không đổi phải chỉ tới section đã
  chứa sự thật đó; như vậy một synthesis từ knowledge cũ vẫn audit được.
- Primary response dài hơn 800 character nhưng khai no-op phải có `manifest_ref`
  thật; quy tắc này bắt case agent trả lời kỹ thuật dài nhưng không lưu/
  không chỉ ra durable knowledge.
- Overview BODY đổi phải khai `overview_manifest_refs`; từng ref phải tồn tại
  trong manifest. `overview_reason` luôn bắt buộc.
- `resolved_escalations` chỉ hợp lệ với ID đang open và thuộc owner đó.

Semantic lint hiện cảnh báo overview vượt advisory word budget, paragraph manifest
bị lặp, và nhiều dated top-level section trong manifest. Warning không tự động
xóa/cắt content.

### 12.4 Publish gate: `verified` và `needs_review`

Receipt hợp lệ:

- Stamp `memory_status: verified`.
- Stamp `source_manifest_sha256` bằng hash manifest hiện tại.
- Stamp `derived_at`, `manifest_version`, `last_updated` và `body_incomplete`.
- Parent được phép dùng overview làm routing projection.

Receipt thiếu/sai, adapter error, pointer không tồn tại, hoặc receipt mâu thuẫn hash:

- **Giữ** progress và manifest edits để audit/review; backend không xóa technical work.
- Restore overview pre-turn nếu snapshot đó không rỗng. Nếu trước turn chưa có overview, nhánh hiện tại không xóa file mới; nó vẫn bị đánh dấu `needs_review`.
- Stamp `memory_status: needs_review`.
- Không stamp manifest SHA/version mới, do overview chưa được chứng minh là projection
  của manifest mới.

Với project opt-in, pre-flight coi overview là stale khi `memory_status != verified`,
thiếu `source_manifest_sha256`, hoặc SHA không khớp manifest. Agent vẫn phải làm
primary task trước; internal reconciliation sẽ sửa memory sau, tránh biến task của
user thành một memory-only turn.

Machine-owned footer chuẩn hiện tại:

```text
open_escalation: <none | esc-ID TYPE→target: reason [| ...]>
open_escalation_ids: <none | esc-id,...>
memory_status: <verified|needs_review>
source_manifest_sha256: <sha256 của manifest đã verify>
derived_at: <ISO8601>
last_updated: <YYYY-MM-DD>
```

### 12.5 Escalation là state machine, không còn là footer text dính vĩnh viễn

Mỗi `[ESCALATE]` hợp lệ tạo một row trong SQLite table `escalations`; backend gán
ID `esc-<12 hex>`. Hai escalation open giống hệt nhau của cùng owner được
deduplicate. Row lưu owner, type, target, reason/evidence, opened session, status, và
resolution provenance.

Lifecycle:

```text
[ESCALATE] → open(esc-ID) → route ledger/meta → explicit resolve(ID) → resolved
```

Agent không tự bịa ID. Resolve bằng một trong hai cách:

```text
[RESULT]
outcome: <blocker đã được gỡ thế nào>
resolves: esc-0123456789ab, esc-fedcba987654
[/RESULT]
```

hoặc field `resolved_escalations` trong receipt. Backend ghi `resolved_by`,
`resolved_session`, `resolution_reason`, `resolved_at`; ID không tồn tại/đã đóng
bị reject và emit meta warning.

`open_escalation` và `open_escalation_ids` trong overview chỉ là **projection từ DB**.
Mỗi stamp luôn ghi lại projection; nếu không còn row open thì bắt buộc ghi
`none`. Vì vậy blocker cũ không thể bị kẹt chỉ vì footer từ turn trước.
Top-level BOSS vẫn có DB state dù không có parent để route.

### 12.6 Progress hot window: hai ngày **có nội dung** gần nhất

Khi bật rotator (global `progress_rotate_enabled`, mặc định off), `state/progress.md`
không tăng vô hạn. Rotator giữ hai date mới nhất
thực sự có entry, không phải hai ngày calendar gần hôm nay. Agent dù idle lâu
vẫn giữ được hai ngày hoạt động cuối.

Markdown vẫn giữ định dạng Markdown; `progress.json` chỉ dùng khi đã migrate rõ ràng.
Reader ưu tiên JSON nếu tồn tại, nên không duy trì hai hot log độc lập. Phần cũ được merge/de-duplicate vào:

```text
state/archive/<AGENT>_progress.json
```

Lần rotate Markdown đầu tiên còn tạo backup phục hồi một lần:

```text
state/archive/<AGENT>_progress_backup.md
```

Thứ tự an toàn là backup → merge archive atomically → rewrite hot file atomically.
Crash giữa chừng có thể tạo duplicate tạm thời nhưng không làm mất dữ liệu;
lần rotate sau de-duplicate.

### 12.7 Bật policy cho một project

Project chỉ dùng pipeline mới khi `.agentui/project.yaml` khai báo:

```yaml
memory:
  reconciliation:
    enabled: true
    protocol_file: shared/memory_protocol.md
    overview_advisory_words: 240
```

Project phải có `shared/memory_protocol.md` nêu semantic contract và
`shared/overview_protocol.md` nêu `[RESULT]`, `[ESCALATE]`, `resolves` cùng machine-owned
footer. Bật config mà thiếu provenance/footer field sẽ làm pre-flight báo stale cho
đến khi một reconciliation hợp lệ migrate agent đó.

Chi phí cố ý: mỗi successful primary turn có thêm một model call ngắn của
chính owner. Đổi lại, parent chỉ đọc overview đã có provenance thay vì nạp
full child manifest, và control-plane có audit trail cho failure/false no-op.

### 12.8 Acceptance tests bắt buộc cho memory policy

| ID | Case | Pass khi |
|---|---|---|
| M1 | Agent khai `unchanged` nhưng file đổi | receipt fail với hash mismatch |
| M2 | `updated` nhưng pointer không tồn tại | receipt fail; overview `needs_review` |
| M3 | Overview đổi nhưng không có manifest refs | receipt fail; không stamp SHA mới |
| M4 | Long no-op synthesis không có `manifest_ref` | receipt fail |
| M5 | Receipt hợp lệ, refs có thật | `memory_status=verified`; SHA khớp manifest |
| M6 | Reconciliation fail sau khi overview bị edit | restore overview pre-turn; progress/manifest vẫn còn |
| M7 | Open escalation trùng nhau | chỉ một open ID |
| M8 | Resolve ID hợp lệ | DB row resolved; footer lần stamp sau thành `none` nếu hết blocker |
| M9 | Resolve ID lạ/đã đóng | reject + meta warning; không đổi row khác |
| M10 | Progress có ba ngày hoạt động | hot file giữ hai ngày mới nhất; ngày cũ vào archive |

Regression tests hiện tại:

```bash
rtk proxy app/.venv/bin/python -m unittest \
  app.backend.test_memory_receipt \
  app.backend.test_escalation_state -v
```

Ghi nhận rollout GelSight trước đợt audit này: cả hai nhánh runtime: validator từng reject
`long unchanged synthesis requires an existing manifest_ref`, sau đó BOSS/MODEL
reconcile lại thành công và overview trở về `verified`. Đó là behavior mong muốn:
fail closed ở projection cho parent, nhưng không làm mất technical work của owner.

## 13. Notion reporting thống nhất ở cấp project

Notion là project resource, không phải connector cài riêng cho từng agent. Contract
chuẩn là:

```text
một AgentUI server token
        ↓
một AgentUI System Hub được bind + live-verify đúng một lần
        ↓
project mới tự tạo/reuse một direct child: <name> [<slug>]
        ↓
destination project được persist dưới canonical root agent
        ↓
mọi agent trong project chỉ kế thừa đúng project subtree
        ↓
agent inventory/read/sync report trong subtree đó
```

### 13.1 Identity do control plane sở hữu

Model không được nhập hoặc suy luận `project_slug`/`agent_id`. Backend truyền canonical
identity qua argv của MCP process; tool schema cố ý không expose hai field này. Nhờ đó
tên thư mục như `OpenConstruction-research` không thể bị dùng nhầm thay cho registry
slug `openconstruction-meta`.

MCP không có scope từ control plane phải fail closed. Global/raw Notion MCP vẫn bị tắt
trong AgentUI turn để agent không bypass destination lock.

### 13.2 System Hub, auto-provision và inheritance

- Dashboard có nút **Notion System Hub**. User bind một parent đúng **một lần**;
  backend live-verify workspace/page trước khi lưu.
- Project đã có binding riêng tiếp tục dùng binding đó, không bị di chuyển tự động.
- Project chưa bind sẽ tạo hoặc reuse đúng một direct child tên
  `<project name> [<registry slug>]` dưới Hub, rồi persist một destination bình thường
  do canonical root agent sở hữu.
- Provisioning dùng inter-process file lock và re-check destination bên trong lock để
  hai first-turn chạy đồng thời không tạo page trùng.
- Destination registry chỉ lưu URL/page/workspace và **tên** biến token, không lưu secret.
- Worker không được rebind nhưng `DestinationStore.resolve(project, worker)` tự kế thừa
  binding duy nhất của project.
- Nếu chưa có System Hub: báo đúng một system-level setup action; không yêu cầu cài
  plugin, không yêu cầu token/binding riêng cho từng agent hoặc project.
- Nếu project có nhiều binding và worker không có exact binding: fail vì ambiguous,
  không tự chọn.

Dashboard trả `configured`, `destination_owner_agent_id` và `inherited` cho cả worker,
thay vì placeholder rỗng. Một token `NOTION_REPORT_TOKEN` có thể phục vụ mọi project
trong cùng workspace; token riêng theo project vẫn được hỗ trợ qua `token_env`.

### 13.3 Read/audit semantics

Trong AgentUI turn, “đọc toàn bộ Notion” luôn có nghĩa là **toàn bộ bound project
subtree**, không phải toàn personal workspace:

- `list_notion_project_tree`: BFS có cap depth/page, chỉ đi qua `child_page` dưới root.
- `read_notion_project_page`: chỉ đọc sau khi page ID được chứng minh thuộc inventory;
  block traversal có cap và trả `content_sha256` để audit staleness.
- Page nằm ngoài subtree bị reject, kể cả token thật sự có quyền truy cập page đó.

Raw/global Notion MCP bị disable trong AgentUI subprocess. System prompt do control plane
inject cấm agent đề xuất “install/connect Notion plugin”; connector nội bộ là đường duy nhất.

### 13.4 Publish semantics

Ba operation ghi chuẩn:

- `create_notion_report`: tạo report mới dưới parent đã bind.
- `append_notion_child_page`: match exact direct-child title, append rồi read-back.
  `create_if_missing=false` là mặc định; chỉ bật khi user đã cho phép tạo page con.
- `sync_notion_managed_report`: dry-run mặc định, chỉ replace vùng nằm giữa marker
  `AGENTUI_REPORT_START/END`; manual blocks ngoài vùng managed không bị sửa.

Multiple exact-title match luôn fail. Retry cùng payload đã nằm ở page tail trả
`action: unchanged`, không ghi duplicate. Thành công chỉ được công bố khi
`read_back_verified: true`; page creation hoặc append không có read-back là chưa hoàn tất.

### 13.5 Acceptance tests

| ID | Case | Pass khi |
|---|---|---|
| N1 | Worker cùng project gọi status | kế thừa root parent; `inherited=true` |
| N2 | Model cố gửi project/agent identity | schema không có field đó |
| N3 | MCP launch không có control-plane scope | tool fail closed |
| N4 | Project chưa bind, Hub đã bind | tạo/reuse một project child rồi persist root-owned destination |
| N5 | Project có nhiều destination | worker nhận ambiguous error |
| N6 | Authorized create | page con tạo dưới bound parent và read-back pass |
| N7 | Retry cùng nội dung | `action=unchanged`, không tạo/append duplicate |
| N8 | Codex resume làm rơi env allow-list | argv scope + scoped local-token fallback vẫn verify |
| N9 | “All Notion” | chỉ inventory project subtree, không quét workspace |
| N10 | Read page ngoài subtree | reject trước khi đọc block |
| N11 | Managed sync dry-run | không create/archive/append |
| N12 | Managed replace | chỉ archive marker range; manual content còn nguyên; read-back pass |
| N13 | Hai first-turn đồng thời | provision lock + re-check không tạo duplicate project child |

Ghi nhận pilot lịch sử (không chạy lại trong audit) GelSight ngày 2026-08-16: inventory thấy 7 page, `truncated=false`;
page `propose` đọc được 7 block, `truncated=false`, và trả content SHA-256. Pilot chỉ đọc,
không sửa Notion.

Regression suite:

```bash
rtk proxy app/.venv/bin/python -m unittest \
  app.backend.test_adapters_notion \
  notion_report.tests.test_run_mcp \
  notion_report.tests.test_config \
  notion_report.tests.test_client \
  notion_report.tests.test_tool \
  notion_report.tests.test_mcp_protocol \
  notion_report.tests.test_mcp_smoke \
  notion_report.tests.test_dashboard_settings
```
