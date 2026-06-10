# {{id}} Progress log (newest on top)

> **Convention**: PREPEND một mục mới ở ĐẦU file mỗi turn có nghĩa
> (Read/Write/analysis thực). Format:
>
> ```
> ## YYYY-MM-DD — one-line headline
> - bullet 1: việc gì (kèm file:line hoặc evidence)
> - bullet 2: kết quả / quyết định / số liệu
> - bullet 3: open question / follow-up
> ```
>
> Turn trivial (ping, 1-fact, status) được skip nhưng turn có nghĩa kế tiếp phải
> tham chiếu nếu liên quan.

## {{date}} — Bootstrapped via AgentUI
- Parents trong graph: {{parents_csv}}.
- Required reads nạp ở turn đầu (theo AGENT.md).
- Chờ dispatch/task thực đầu tiên từ parent hoặc user.
