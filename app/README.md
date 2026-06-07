# AgentUI prototype

Localhost UI để điều phối các agent project bằng subscription Claude Code và Grok (qua aas), không cần API key.

## Yêu cầu
- macOS, Python 3.9 trở lên
- `claude` CLI đã đăng nhập (`claude auth`)
- `aas` CLI (tuỳ chọn, chỉ cần khi có agent model = grok)

## Chạy
```bash
cd /Users/viethuy/Working_space/UI_agentcoding/app
./run.sh
# mở http://127.0.0.1:5174
```

## Khai báo project
File `registry.yaml` liệt kê đường dẫn project. Mỗi project phải có `.agentui/project.yaml`.

Hiện đã có hai project:
- `/Users/viethuy/Working_space/ConstructionVLM-Eval-AGENT`
- `/Users/viethuy/Working_space/energy-infrastructure-risk/.claude/AGENT`

Schema `.agentui/project.yaml`:
```yaml
name: <ten>
slug: <slug>
description: <mo ta>
agents:
  - id: <ten agent>
    role: <vai tro 1 dong>
    model: claude | grok
    system_prompt_file: <duong dan tuong doi toi AGENT.md>
    cwd: <thu muc lam viec tuong doi>
    parents: [<id agent upstream>, ...]
```

`parents` quyết định mũi tên trong đồ thị, và phân tầng layer.

## Cách hoạt động
- Mỗi node = một agent. Click vào node để chọn, panel phải hiện lịch sử chat.
- Gửi prompt ở khung Chat session, backend spawn `claude -p ... --resume <session_id>` (hoặc `aas ask` cho grok), stream về browser qua SSE.
- Cuộc hội thoại của mỗi agent lưu trong SQLite (`agentui.db`). Claude session id được persist để giữ context.
- Trạng thái node: idle (xám), running (vàng), ok (xanh), error (đỏ).

## Giới hạn MVP
- Chưa có orchestration thực: orchestrator agent gửi prompt một mình, không tự dispatch xuống worker. Mũi tên trong đồ thị chỉ là topology tài liệu.
- Không lưu attachment / file.
- Không multi-user, không auth (localhost only).
- Subscription rate limit áp dụng. Đừng dispatch song song quá nhiều node Claude.

## Layout
- backend/main.py FastAPI app, endpoint `/api/projects*`, `/api/projects/{slug}/agents/{id}/chat` SSE.
- backend/adapters.py wrapper subprocess cho `claude` và `aas`.
- backend/db.py SQLite cho session và message.
- backend/projects.py đọc registry và project.yaml.
- frontend/ vanilla HTML/CSS/JS, SVG đồ thị.
