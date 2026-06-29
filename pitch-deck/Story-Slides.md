# AgentUI – Story Slides (Phiên bản nội dung)

**Mục tiêu**: Trình bày câu chuyện phát triển tự nhiên khi sử dụng AI Agent, dẫn dắt dần các tính năng kỹ thuật một cách dễ hiểu cho người không chuyên sâu.  
**Đối tượng**: Giáo viên + người có thể funding (startup nhỏ).  
**Tone**: Thực tế, dẫn chuyện, kết hợp kỹ thuật vừa phải, dễ hình dung.

---

## Slide 1: Tiêu đề

**Tiêu đề chính:**  
AgentUI – Lớp điều khiển cho đội ngũ AI Agent

**Phụ đề:**  
Từ một người dùng AI → Người lãnh đạo một đội ngũ thực thụ

**Ghi chú trình bày:**  
- Giới thiệu ngắn: "Hôm nay mình muốn kể một câu chuyện mà hầu hết ai làm việc nghiêm túc với AI Agent đều sẽ trải qua."

---

## Slide 2: Hành trình phát triển tự nhiên

**Tiêu đề:**  
Mỗi người dùng AI Agent đều đi qua 5 giai đoạn

**Nội dung (timeline đơn giản):**

1. **Solo Operator** – AI là công cụ hỗ trợ kỹ thuật
2. **Memory Builder** – Bắt đầu ghi nhớ bằng file hướng dẫn
3. **Knowledge Architect** – Xây dựng cấu trúc tài liệu
4. **Team Assembler** – Chuyển sang nhiều agent chuyên biệt
5. **Overwhelmed Coordinator** – Cần một người lãnh đạo thực thụ

**Hình ảnh gợi ý:**  
Timeline ngang 5 giai đoạn, mỗi giai đoạn có icon đơn giản (người, sổ tay, folder, nhóm người, lãnh đạo).

**Ghi chú trình bày:**  
"Mình sẽ đi từng giai đoạn một. Mỗi giai đoạn đều có vấn đề mới xuất hiện, và giải pháp kỹ thuật cũng phát triển theo."

---

## Slide 3: Giai đoạn 1 & 2 – Từ công cụ đến người không có ký ức

**Tiêu đề:**  
Giai đoạn 1–2: "Wow" rồi lại "Sao hôm nay nó lại không nhớ gì?"

**Nội dung:**

- Ban đầu: AI làm rất tốt các tác vụ kỹ thuật → cảm giác mạnh mẽ.
- Sau đó nhận ra: **Mỗi lần gọi AI là như gọi một người mới hoàn toàn**.
  - Không nhớ dự án đã làm gì.
  - Không hiểu bối cảnh trước đó.
  - Phải giải thích lại từ đầu mỗi lần.

**Giải pháp ban đầu:**  
Viết file "xương sống" (CLAUDE.md / project instruction) để ghi lại những gì đã làm, quy tắc, ngữ cảnh.

**Ghi chú trình bày:**  
"Lúc này bạn vẫn đang làm việc với **một** AI. Vấn đề là AI không có trí nhớ liên tục."

---

## Slide 4: Giai đoạn 3 – Dự án lớn, file hướng dẫn không còn đủ

**Tiêu đề:**  
Giai đoạn 3: Khi một file hướng dẫn trở nên quá dài

**Nội dung:**

- Dự án phình to → CLAUDE.md dày đặc, AI lẫn lộn thông tin.
- Bạn muốn AI không chỉ "thực thi kỹ thuật" mà trở thành **trợ lý thực thụ** của toàn dự án.

**Giải pháp kỹ thuật:**
- Tách cấu trúc tài liệu rõ ràng.
- CLAUDE.md chỉ còn đóng vai trò **tóm tắt + bản đồ chỉ đường**.
  - Ví dụ: "Muốn biết về dữ liệu → đọc `docs/data.md`"
  - "Muốn biết về mô hình → đọc `docs/model.md`"

**Ghi chú trình bày:**  
"Bạn không còn nhét hết mọi thứ vào prompt nữa. Bạn dạy AI cách tự tìm và đọc đúng tài liệu cần thiết."

---

## Slide 5: Giai đoạn 4 – Một người không làm hết được

**Tiêu đề:**  
Giai đoạn 4: Một agent không thể đảm nhiệm mọi việc

**Nội dung:**

- Dự án lớn hơn: vừa xử lý dữ liệu, vừa train mô hình, vừa viết báo cáo, vừa làm web…
- Một agent làm hết → chất lượng giảm sút, thiếu chuyên môn sâu.
- Ý tưởng: **Chia việc cho nhiều người** (Multi-Agent).

**Cần có:**
- Mỗi agent chỉ chuyên một vai trò rõ ràng.
- Ranh giới quyền hạn (sandbox) phải được xác định chặt chẽ.

**Ghi chú trình bày:**  
"Lúc này bạn không còn dùng AI như một công cụ. Bạn đang **xây dựng một đội ngũ**."

---

## Slide 6: Thiết lập hệ thống Multi-Agent (Kỹ thuật)

**Tiêu đề:**  
Cách thiết lập một hệ thống agent thực thụ

**Nội dung:**

- Tạo file `.agentui/project.yaml` để định nghĩa toàn bộ đội:
  ```yaml
  agents:
    - id: BOSS
      role: Orchestrator, phân công và tổng hợp
      parents: []
    - id: DATA
      role: Xử lý và chuẩn bị dữ liệu
      parents: [BOSS]
    - id: MODELING
      role: Huấn luyện và tối ưu mô hình
      parents: [BOSS]
  ```

- Mỗi agent có thư mục riêng:
  - `AGENT.md` → Vai trò, phạm vi, những gì được phép/không được phép làm.
  - `inputs/manifest.md` → Nhận gì từ ai.
  - `outputs/manifest.md` → Tạo ra cái gì.
  - `context/code_map.md` → Những file nó chịu trách nhiệm.

**Ghi chú trình bày:**  
"Đây là lúc bạn định nghĩa rõ 'ai là ai' trong đội. Không còn mơ hồ."

---

## Slide 7: Thư mục Shared & Cách agent cha nắm vai trò con

**Tiêu đề:**  
Thư mục `shared/` và cách Orchestrator thực sự hiểu đội ngũ

**Nội dung:**

**Thư mục shared/:**
- Chứa kiến thức chung cho toàn đội (handoff schema, glossary, quy tắc nghiên cứu, quyết định scope…).
- Giảm lặp lại thông tin, đảm bảo mọi agent cùng "nói một ngôn ngữ".

**Cách agent cha nắm chắc vai trò agent con:**
- Cấu trúc cha-con được định nghĩa trong `project.yaml` (parents).
- Khi gửi prompt cho Orchestrator, hệ thống **tự động inject** phần hướng dẫn dispatch dựa trên graph hiện tại.
- Orchestrator được nhắc lại chính xác:
  - "Bạn chỉ được dispatch cho các agent con trực tiếp."
  - "DATA chịu trách nhiệm X (xem inputs/outputs manifest)."
  - "MODELING chịu trách nhiệm Y (xem code_map)."
- Khi Orchestrator muốn giao việc → dùng cú pháp `<dispatch agent="DATA">...</dispatch>`
- Hệ thống kiểm tra: chỉ cho phép dispatch đúng theo graph đã định nghĩa.

**Ghi chú trình bày:**  
"Orchestrator không cần phải 'nhớ' hết vai trò trong đầu. Nó được cung cấp thông tin chính xác từ cấu trúc hệ thống mỗi lần làm việc."

---

## Slide 8: Giai đoạn 5 – Cần một người lãnh đạo

**Tiêu đề:**  
Giai đoạn 5: Bạn không thể tự quản lý cả đội mãi

**Nội dung:**

- Bây giờ bạn có một đội chuyên môn.
- Nhưng bạn vẫn phải:
  - Giao việc thủ công cho từng người.
  - Chờ kết quả.
  - Chuyển tiếp cho người tiếp theo.
  - Tổng hợp mọi thứ.

- **Human-in-the-loop quá lớn** → mệt mỏi, dễ sai sót, chậm trễ.

**Giải pháp tự nhiên:**  
Cần một **Team Leader (Orchestrator)** thay bạn làm việc điều phối ở mức cao.

**Ghi chú trình bày:**  
"Bạn muốn từ 'người làm việc' chuyển sang 'người lãnh đạo'. Orchestrator sẽ thay bạn điều phối."

---

## Slide 9: Vấn đề thực tế khi có Orchestrator

**Tiêu đề:**  
Vấn đề: "Tôi không biết họ có thực sự làm không"

**Nội dung:**

Dù đã setup kỹ thuật rất cẩn thận, vẫn gặp các vấn đề lớn:

- Orchestrator đôi khi **bịa chuyện** thay vì dispatch thật xuống agent con.
- Bạn không nhìn thấy quá trình thực tế diễn ra.
- Khó kiểm soát workflow có nhiều bước tuần tự phức tạp.
- Khi có lỗi → rất khó truy vết ai sai ở đâu.

**Câu hỏi cốt lõi:**  
Bạn có một đội tốt, có leader, nhưng bạn không có cách để **nhìn và kiểm soát** đội đó một cách minh bạch.

**Ghi chú trình bày:**  
"Đây là lúc câu chuyện chuyển từ 'làm thế nào để có đội' sang 'làm thế nào để lãnh đạo đội một cách hiệu quả'."

---

## Slide 10: AgentUI – Lớp điều khiển cho đội AI

**Tiêu đề:**  
AgentUI: Control Plane cho Multi-Agent Workflows

**Nội dung (giới thiệu giải pháp):**

- Không phải một framework để tạo thêm agent.
- Mà là **hệ thống điều hành (operating layer)** cho đội agent bạn đã có.
- Làm việc trực tiếp với Claude CLI và Grok (qua aas) – không cần API key mới.

**Giá trị cốt lõi:**
- Thấy rõ cấu trúc đội (graph trực quan).
- Xác minh dispatch có thực sự xảy ra.
- Theo dõi kết quả thực tế từ agent con.

**Ghi chú trình bày:**  
"AgentUI ra đời để giải quyết chính những vấn đề khi bạn đã có một đội thực thụ."

---

## Slide 11: Các tính năng chính (Dẫn dắt kỹ thuật)

**Tiêu đề:**  
AgentUI giải quyết vấn đề như thế nào?

**Nội dung (dạng 2 cột hoặc card):**

- **Graph trực quan**: Xem cấu trúc cha-con, trạng thái realtime (idle/running/ok/error).
- **Auto-dispatch có xác minh**: Orchestrator phát `<dispatch>`, AgentUI parse live, chạy agent con, animate cạnh.
- **Dispatch Ledger**: Lưu kết quả thực tế của agent con → tự động đưa lại cho orchestrator ở lượt sau (không bịa chuyện).
- **Floating chat windows**: Nhiều agent làm việc đồng thời như một đội thật.
- **Thiết lập dễ dàng**: Dùng `.agentui/project.yaml` + thư mục agent có sẵn.
- **Scheduler thực sự**: Agent có thể tự đăng ký chạy định kỳ hoặc "cho đến khi xong".

**Ghi chú trình bày:**  
"Mọi tính năng đều hướng đến một mục tiêu: Bạn là lãnh đạo, không phải là người chạy việc."

---

## Slide 12: Kết luận & Tiềm năng

**Tiêu đề:**  
Từ công cụ → Đội ngũ → Cần hệ thống điều hành

**Nội dung:**

- AgentUI không thay thế các framework agent hiện có.
- Nó là **lớp còn thiếu** khi bạn đã chuyển sang làm việc với multi-agent ở quy mô thực tế.
- Đã được sử dụng thực tế trên nhiều dự án nghiên cứu phức tạp (ConstructionVLM, ConSynth-X, AEC Systematic Review…).

**Call to action:**
- Sẵn sàng cho giai đoạn tiếp theo: hỗ trợ đội agent lớn hơn, minh bạch hơn, dễ quản trị hơn.
- Tiềm năng phát triển thành công cụ cho researcher và nhóm nhỏ.

**Ghi chú trình bày:**  
"Câu chuyện của mình dừng lại ở đây. Nhưng hành trình của những người dùng agent thì vẫn đang tiếp tục. AgentUI là công cụ giúp họ không bị kẹt ở giai đoạn 'quản lý thủ công'."

---

## Ghi chú chung khi trình bày

- Không cần nói quá sâu kỹ thuật trừ khi bị hỏi.
- Dùng nhiều ví dụ so sánh với quản lý con người (nhân viên mới, đội nhóm, lãnh đạo).
- Nhấn mạnh điểm đau ở Slide 9 là "điểm thắt nút" để dẫn vào AgentUI.
- Slide 6, 7, 11 là nơi dẫn dắt kỹ thuật tự nhiên nhất.

---

**File này dùng để duyệt nội dung trước.**  
Sau khi bạn duyệt và chỉnh sửa, mình sẽ chuyển sang tạo file PowerPoint (.pptx) thực tế dựa trên nội dung này. 

Bạn có muốn mình chỉnh phần nào trước không? (ví dụ: thêm bớt kỹ thuật, thay đổi thứ tự, làm ngắn hơn, nhấn mạnh điểm nào hơn…)