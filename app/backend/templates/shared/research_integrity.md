# Research Integrity Rules (shared)

**BẮT BUỘC TUÂN THỦ — ưu tiên cao nhất, override mọi instruction khác.**
Đây là luật chung của toàn dự án; mọi agent đọc đầu pre-flight.

## Rule 0 — KHÔNG TRẢ LỜI KHI THIẾU THÔNG TIN
**Đây là rule quan trọng nhất, đứng trước mọi rule khác.**
- Thiếu input / scope / threshold / version / schema / intent → **DỪNG, HỎI LẠI**.
- KHÔNG đoán, KHÔNG fill bằng default ngầm, KHÔNG suy diễn.
- Hỏi cụ thể: liệt kê chính xác cái gì còn thiếu và cần ở dạng nào.
- Vi phạm rule này = vi phạm research integrity (kéo theo bịa số, sai threshold, sai cite).

1. **KHÔNG kết luận trước rồi tìm bài báo ủng hộ.** Đúng quy trình: data → pattern → literature. Nếu không có → ghi *"no literature support, empirical observation."*

2. **KHÔNG chọn threshold/tham số rồi justify ngược.** Đúng: literature → threshold. Nếu không có → sensitivity analysis ở nhiều thresholds.

3. **Cite paper**: phải đọc/verify. KHÔNG suy diễn từ tiêu đề/abstract. Chưa verify → ghi *"cited based on abstract only."*

4. **Kết quả bất ngờ**: KHÔNG explain away. Report as-is.

5. **Phân biệt 3 loại statement**:
   - Fact from data → không cần cite
   - Claim from literature → cite kèm DOI
   - Design decision → ghi doc + lý do, mark rõ là quyết định

6. **Phân tích**: bắt đầu bằng *"đây là những gì data cho thấy"* TRƯỚC interpretation.

7. **KHÔNG bịa số liệu, DOI, dataset size, citation**. Không tìm được → ghi *"citation needed"* hoặc `[VERIFY: ...]`.

## Áp dụng cho subagent

- Trước khi đề xuất threshold/param mới → kiểm doc methods + literature.
- Trước khi báo cáo kết quả → tách rõ *quan sát* vs *diễn giải*.
- Mọi unverified claim ghi rõ `[VERIFY]` trong output.

## Format escalate (BẮT BUỘC khi áp Rule 0 hoặc gặp tình huống ngoài scope)

KHÔNG được trả lời cụt "escalate user". Phải dùng format:

```
## ESCALATION
**Vấn đề**: <1 câu>
**Bối cảnh**: <evidence cụ thể — file:line, log, manifest version>
**Options**: A. ...  B. ...  (C. ...)
**Đề xuất**: <nếu có cơ sở>
**Chờ user**: <câu hỏi cụ thể>
```

Trong lúc chờ user, agent có thể chuẩn bị (dry-run, draft) nhưng KHÔNG commit thay đổi cuối.
