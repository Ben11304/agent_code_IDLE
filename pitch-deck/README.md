# AgentUI Pitch Deck

Files: `AgentUI_Pitch_Deck.pptx`, `story.pdf`, ảnh chụp và hai bản Markdown nội dung.

**Đối chiếu 2026-09-07:** PPTX/PDF và `generate-deck.js` là snapshot pitch cũ.
Markdown story đã cập nhật các mô tả runtime/ownership/ledger. Generator có text
hard-coded riêng, không đọc các Markdown này; chạy lại nguyên trạng sẽ tái tạo
nội dung cũ (ví dụ chỉ Claude/Grok, “không API billing”). Chưa tái xuất binary trong
đợt audit tài liệu; không dùng deck cũ làm specification tính năng hiện tại.

## Nội dung 12 slides (tiếng Việt)

1. **Title** — AgentUI: Localhost Control Plane cho Multi-Agent AI Workflows
2. **Vấn đề** — 4 đau điểm chính của multi-agent hiện nay (orchestrator blindness, khó verify, không có scheduling thật, debug khó)
3. **Giải pháp** — 4 tính năng cốt lõi của AgentUI
4. **Tính năng nổi bật** — 10 điểm mạnh (dispatch ledger, bootstrap AI, mix Claude+Grok, slash commands...)
5. **Cơ chế Dispatch & Ledger** — Quy trình 4 bước giải quyết vấn đề feedback
6. **Scheduler** — 3 chế độ + giải thích tại sao quan trọng
7. **Giao diện thực tế** — Screenshot UI đầy đủ + caption
8. **Kiến trúc** — 4 layer (Frontend → Backend → Adapter → Agent Projects)
9. **Công nghệ & Triển khai** — Stack + cách chạy + deploy
10. **Điểm khác biệt** — định vị sản phẩm của pitch cũ; không phải benchmark so sánh framework đã xác minh
11. **Tiềm năng & Roadmap (Startup angle)** — Hiện tại → Gần → Trung hạn
12. **Kết luận** — Tóm tắt giá trị + sẵn sàng scale

## Màu sắc thiết kế
- Dark navy + Cyan accent cho các slide chính
- Light cards trên nền sáng cho nội dung
- Thiết kế hiện đại, tech-forward, phù hợp pitch cho teacher/funding

## Cách sử dụng
- Mở bằng PowerPoint / Google Slides / Keynote
- Slide 7 có ảnh chụp màn hình UI thật (để minh họa)
- Có thể chỉnh sửa text hoặc thêm logo trường / tên bạn

## Gợi ý khi trình bày
- Tập trung slide 2 (vấn đề) + slide 5 (dispatch magic) + slide 11 (roadmap)
- Mô tả auth theo adapter: Claude/Codex dùng saved login; DeepSeek/GLM cần provider key.
- Graph phản ánh dispatch events; ledger giữ kết quả. Điều này không chứng minh artifact đúng.

## Tạo lại slide (nếu cần chỉnh)
```bash
cd pitch-deck
npm ci
# Trước khi xuất, đồng bộ text trong generator với Markdown đã cập nhật.
node generate-deck.js
cp AgentUI_Pitch_Deck.pptx ../
```

## Ghi chú
Bộ slide này được tạo dành riêng cho việc report + potential small startup funding với giáo viên.
