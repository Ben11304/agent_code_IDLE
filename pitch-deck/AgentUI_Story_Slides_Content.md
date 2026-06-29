# AgentUI - Nội dung Slide (Phiên bản sẵn sàng đưa vào PowerPoint)

**Hướng dẫn sử dụng:**
- Mỗi phần là một slide.
- **Tiêu đề** dùng làm title slide.
- **Nội dung chính** copy trực tiếp vào slide (giữ ngắn gọn).
- **Hình minh họa** đề xuất từ file story.pdf.
- **Lời dẫn / Ghi chú trình bày** dùng để nói (không cần đưa hết lên slide).
- Tone: Dễ hiểu, dẫn chuyện, có yếu tố kỹ thuật nhẹ nhàng.

---

## Slide 1: Tiêu đề

**Tiêu đề lớn:**
AgentUI

**Phụ đề:**
Lớp điều khiển cho đội ngũ AI Agent

**Dòng nhỏ:**
Từ một người dùng AI → Người lãnh đạo một đội ngũ thực thụ

**Hình minh họa:** Không cần (hoặc logo nhỏ)

**Lời dẫn:**
"Hôm nay mình muốn chia sẻ một hành trình mà hầu hết những ai làm việc nghiêm túc với AI Agent đều sẽ trải qua."

---

## Slide 2: Hành trình phát triển

**Tiêu đề:**
5 Giai đoạn khi làm việc với AI Agent

**Nội dung (dạng timeline ngang):**

1. **Solo Operator**  
   AI là công cụ hỗ trợ kỹ thuật

2. **Memory Builder**  
   Bắt đầu ghi nhớ bằng tài liệu

3. **Knowledge Architect**  
   Xây dựng cấu trúc tài liệu

4. **Team Assembler**  
   Chuyển sang nhiều agent chuyên biệt

5. **Overwhelmed Coordinator**  
   Cần một người lãnh đạo thực thụ

**Hình minh họa:** Timeline + icon đơn giản (có thể tự vẽ hoặc dùng icon)

**Lời dẫn:**
"Mỗi giai đoạn đều mang theo một vấn đề mới. Và giải pháp cũng ngày càng phức tạp hơn."

---

## Slide 3: Giai đoạn 1 – AI là công cụ

**Tiêu đề:**
Giai đoạn 1: "Wow, cái này làm được!"

**Nội dung:**
- Bạn làm chủ dự án
- Khám phá AI có thể thực thi tốt các công việc kỹ thuật
- Cảm thấy mình mạnh lên rất nhiều

**Hình minh họa:**  
**Figure 1** (story.pdf trang 1) – Chat thành công với một agent

**Lời dẫn:**
"Lúc đầu rất hứng khởi. Bạn đưa task, AI làm. Nhanh, tiện, hiệu quả."

---

## Slide 4: Giai đoạn 2 – Vấn đề ký ức

**Tiêu đề:**
Giai đoạn 2: Mỗi lần gọi AI là một người mới

**Nội dung:**
- AI không nhớ những gì đã làm trước đó
- Phải giải thích lại từ đầu mỗi lần
- Không có "ký ức chung" về dự án

**Hình minh họa:**  
**Figure 2** (story.pdf trang 2) – So sánh: Trái thành công, Phải quên sạch ("who are you?")

**Lời dẫn:**
"Bạn nhận ra vấn đề lớn: AI không có trí nhớ liên tục. Mỗi lần gọi là như bắt đầu lại từ con số 0."

---

## Slide 5: Giải pháp ban đầu – Tài liệu

**Tiêu đề:**
Giải pháp: Dùng tài liệu để tạo trí nhớ

**Nội dung:**
- Viết file hướng dẫn chi tiết (CLAUDE.md, AGENT.md…)
- Ghi lại:
  - Mục tiêu dự án
  - Những gì đã làm
  - Quy tắc và ràng buộc

**Hình minh họa:**  
**Figure 3** (story.pdf trang 3) – Giới thiệu AGENT.md + agent đọc và nhớ lại

**Lời dẫn:**
"Bạn bắt đầu viết 'sổ tay bàn giao' cho AI. Lúc này AI đã có thể nhớ được bối cảnh dự án."

---

## Slide 6: Giai đoạn 3 – Tài liệu không còn đủ

**Tiêu đề:**
Giai đoạn 3: Dự án lớn → Tài liệu phình to

**Nội dung:**
- Một file hướng dẫn quá dài, AI lẫn lộn thông tin
- Bạn muốn AI không chỉ "làm việc" mà trở thành trợ lý thực thụ
- Cần tổ chức tài liệu có cấu trúc rõ ràng

**Hình minh họa:**  
Figure 3 (nếu cần nhắc lại) hoặc icon folder/docs

**Lời dẫn:**
"CLAUDE.md dần trở nên quá dài. Bạn không thể nhét hết mọi thứ vào một file nữa."

---

## Slide 7: Giai đoạn 4 – Cần chia việc

**Tiêu đề:**
Giai đoạn 4: Một agent không làm hết được

**Nội dung:**
- Dự án lớn: dữ liệu + mô hình + báo cáo + web + …
- Một agent làm nhiều việc → chất lượng giảm, hay bịa chuyện
- Giải pháp tự nhiên: **Chia thành nhiều agent chuyên biệt**

**Hình minh họa:**  
**Figure 4** (story.pdf trang 4) – Một agent làm quá nhiều việc (có lửa hallucination)

**Lời dẫn:**
"Khi dự án phức tạp, một người không thể giỏi hết mọi thứ. Bạn cần một đội ngũ."

---

## Slide 8: Thiết lập hệ thống Multi-Agent

**Tiêu đề:**
Thiết lập hệ thống agent

**Nội dung:**
- Tạo file `.agentui/project.yaml` định nghĩa toàn bộ đội:
  - id, role, model, parents
- Mỗi agent có thư mục riêng:
  - `AGENT.md` (vai trò + phạm vi)
  - `inputs/manifest.md` & `outputs/manifest.md`
  - `context/code_map.md`
- Thư mục `shared/` chứa kiến thức chung cho cả đội

**Hình minh họa:**  
Sơ đồ folder structure (có thể vẽ đơn giản) hoặc dùng Figure 4 làm nền

**Lời dẫn:**
"Đây là lúc bạn chính thức xây dựng một đội. Mỗi người có vai trò rõ ràng và ranh giới công việc được xác định."

---

## Slide 9: Agent cha nắm vai trò con như thế nào?

**Tiêu đề:**
Làm sao agent cha biết việc nào nên giao cho ai?

**Nội dung:**
- Cấu trúc cha – con được định nghĩa trong `project.yaml`
- Mỗi agent con có `AGENT.md` + manifests + code_map rõ ràng
- Khi làm việc, hệ thống tự động cung cấp cho agent cha:
  - Danh sách agent con trực tiếp
  - Vai trò và phạm vi của từng người
- Agent cha chỉ được dispatch đúng theo cấu trúc đã định nghĩa

**Hình minh họa:**  
Sơ đồ graph (cha-con) + text giải thích ngắn

**Lời dẫn:**
"Agent cha không cần nhớ hết trong đầu. Nó được cung cấp 'bản đồ vai trò' mỗi lần làm việc."

---

## Slide 10: Giai đoạn 5 – Cần người lãnh đạo

**Tiêu đề:**
Giai đoạn 5: Có đội rồi, nhưng quản lý thủ công quá nặng

**Nội dung:**
- Bạn phải tự giao việc cho từng agent
- Thu kết quả, chuyển tiếp cho người tiếp theo
- Theo dõi tiến độ, tổng hợp thông tin
- **Human-in-the-loop quá lớn**

**Hình minh họa:**  
**Figure 5** (story.pdf trang 5) – User ở giữa, phải quản lý tất cả agent trực tiếp

**Lời dẫn:**
"Bạn không còn là người làm việc. Bạn đang trở thành quản lý của cả một đội. Nhưng bạn không có công cụ hỗ trợ."

---

## Slide 11: Giải pháp – Orchestrator

**Tiêu đề:**
Cần một người lãnh đạo (Orchestrator)

**Nội dung:**
- Một agent đóng vai trò Team Leader
- Nhận task từ bạn
- Phân công và điều phối các agent chuyên môn
- Tổng hợp kết quả ở mức cao

**Hình minh họa:**  
**Figure 6** (story.pdf trang 6) – User giao việc cho agent có vương miện, nó phân công xuống các agent khác

**Lời dẫn:**
"Bạn chỉ cần nói chuyện với một người. Người đó sẽ điều phối cả đội."

---

## Slide 12: Vấn đề vẫn còn tồn tại

**Tiêu đề:**
Vấn đề: Bạn không biết họ có thực sự làm không

**Nội dung:**
- Orchestrator có thể **bịa chuyện** thay vì dispatch thật
- Bạn không nhìn thấy quá trình agent con làm việc
- Khó kiểm soát workflow dài, nhiều bước tuần tự
- Khi có lỗi → rất khó truy vết

**Hình minh họa:**  
Figure 6 + icon dấu hỏi hoặc "???" trên agent

**Lời dẫn:**
"Dù có leader, bạn vẫn không có cách để **thấy và kiểm soát** thực tế đội đang làm gì."

---

## Slide 13: AgentUI ra đời

**Tiêu đề:**
AgentUI – Hệ thống để xây dựng và lãnh đạo đội AI hiệu quả

**Nội dung:**
- Cung cấp khung template và công cụ kiểm soát để tạo agent chuyên biệt có cấu trúc rõ ràng (AGENT.md, inputs/outputs manifests, code_map, shared/)
- Giúp bạn định nghĩa vai trò, phạm vi công việc và cách giao việc một cách chuyên nghiệp
- Đồng thời là lớp điều khiển trực quan (graph, dispatch tracking, ledger) để bạn thấy thực tế và chỉ đạo cả đội
- Hoạt động trực tiếp với Claude CLI và Grok (không cần API key riêng)

**Hình minh họa:**  
Screenshot AgentUI (graph + floating windows) + hình minh họa cấu trúc folder agent

**Lời dẫn:**
"AgentUI không chỉ giải quyết vấn đề điều phối. Nó còn mang theo khung template và cơ chế kiểm soát mà mình đã dành thời gian xây dựng để mỗi agent trong đội có vai trò rõ ràng và chất lượng được đảm bảo."

---

## Slide 14: AgentUI giải quyết như thế nào?

**Tiêu đề:**
Các tính năng chính của AgentUI

**Nội dung (dạng grid hoặc bullet):**

- **Graph trực quan**  
  Xem cấu trúc đội, trạng thái realtime (đang chạy / xong / lỗi)

- **Auto-dispatch có xác minh**  
  Thấy rõ orchestrator có thực sự giao việc không

- **Dispatch Ledger**  
  Lưu kết quả thực tế từ agent con → đưa lại cho leader (không bịa)

- **Floating chat windows**  
  Nhiều agent làm việc đồng thời như một đội thật

- **Khung template & thiết lập chuyên nghiệp**  
  Dùng `.agentui/project.yaml`, AGENT.md, manifests, shared/ để kiểm soát vai trò và chất lượng agent

**Hình minh họa:**  
Screenshot thực tế từ AgentUI (graph + floating window)

**Lời dẫn:**
"Mọi tính năng đều hướng đến một điều: minh bạch và kiểm soát."

---

## Slide 15: Kết luận

**Tiêu đề:**
Từ công cụ → Đội ngũ → Cần hệ thống điều hành

**Nội dung:**
- AgentUI không thay thế các framework agent
- Nó là **lớp còn thiếu** khi bạn làm việc với multi-agent ở quy mô thực tế
- Đã được dùng thực tế trên nhiều dự án nghiên cứu phức tạp

**Call-to-action:**
Sẵn sàng hỗ trợ đội agent lớn hơn, minh bạch hơn, dễ lãnh đạo hơn.

**Hình minh họa:**  
Figure 6 (phiên bản có AgentUI) hoặc logo + tagline

**Lời dẫn:**
"Câu chuyện của mình dừng lại ở đây. Nhưng hành trình của những người xây dựng đội AI thì vẫn đang tiếp tục. AgentUI là công cụ giúp họ không bị kẹt ở giai đoạn quản lý thủ công."

---

## Ghi chú cuối cùng khi làm slide

- Giữ chữ trên slide **ngắn gọn**, lời dẫn để nói.
- Ưu tiên dùng hình từ story.pdf (rất dễ hiểu).
- Ở các slide kỹ thuật (8, 9, 14), giải thích đơn giản, dùng ví dụ "như quản lý đội nhân viên".
- Nếu cần, có thể gộp một số slide thành 10-12 slide tổng cộng.

Bạn duyệt nội dung này rồi cho mình biết chỉnh phần nào, mình sẽ cập nhật file và sau đó tạo PowerPoint.