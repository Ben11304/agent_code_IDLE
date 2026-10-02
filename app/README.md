# AgentUI application

Giao diện localhost điều phối agent, stream chat và lưu lịch sử. Backend dùng
FastAPI + SQLite; frontend dùng HTML/CSS/JavaScript thuần. Đối chiếu code: 2026-09-07.

## Chạy

Từ thư mục repository:

```bash
cd app
./run.sh
# mở http://127.0.0.1:5174
```

- Python >= 3.10; launcher tạo `.venv` bằng `uv` với Python 3.12 nếu có `uv`.
- Runtime tương ứng với adapter phải được cài và xác thực; xem [bảng adapter](../README.md).
- `tmux` cho terminal persistent; trình duyệt cần tải các thư viện frontend từ CDN.
- `PORT=5175 ./run.sh` đổi cổng; `RELOAD=1 ./run.sh` bật watcher chỉ cho `backend/`.
  Reload mặc định tắt. Thay đổi Python ngoài thư mục được watch cần restart.
- `app/.env.local` là file shell được launcher source; không commit secret.
  Các phép gán trong file này có thể ghi đè environment có sẵn.

## Khai báo hoặc tạo project

UI có chức năng preview và tạo project/agent. Cấu hình thủ công:

```yaml
# app/registry.yaml — dùng đường dẫn trên máy chạy server
workspace_root: /absolute/path/to/workspace
projects:
  - /absolute/path/to/workspace/my-project
```

```yaml
# my-project/.agentui/project.yaml
name: My project
slug: my-project
description: Example agent team
agents:
  - id: BOSS
    role: Điều phối và tổng hợp
    model: claude
    claude_model: claude-sonnet-4-6
    system_prompt_file: BOSS/AGENT.md
    cwd: BOSS
    parents: []
  - id: WORKER
    role: Thực hiện phần việc được giao
    model: codex
    codex_model: gpt-5.6-terra
    system_prompt_file: WORKER/AGENT.md
    cwd: WORKER
    parents: [BOSS]
```

Tạo các file được tham chiếu hoặc dùng UI scaffold. Registry/project YAML được
đọc lại khi gọi API; refresh giao diện để thấy thay đổi, không cần restart chỉ
vì sửa registry. Model/effort overrides trong SQLite được ưu tiên hơn YAML.
`parents` tạo cạnh và xác định direct-child dispatch; vị trí node đã kéo được lưu riêng.

## Chat và điều phối

Click node mở cửa sổ chat nổi. Agent cha phát `<dispatch agent="WORKER">…</dispatch>`;
backend chạy worker và ghi kết quả vào ledger. Agent cha nhận kết quả trong prompt
của lượt tiếp theo, với tối đa ba lượt continuation trong cùng run.

Đóng trình duyệt không hủy run. Mở lại để reattach; Stop/Esc gọi API hủy run.
Restart backend vẫn ngắt agent đang chạy; SQLite giữ lịch sử nhưng không tự tiếp tục
run cũ. Worker chạy qua dispatch chia sẻ stream của parent, chưa có event bus riêng.

| Lệnh | Chức năng |
|---|---|
| `/help`, `/status` | Xem lệnh và trạng thái |
| `/clear`, `/compact` | Phiên mới hoặc tóm tắt rồi chuyển phiên |
| `/model`, `/effort`, `/adapter` | Đổi cấu hình agent |
| `/focus ID`, `/dispatch ID task` | Mở chat hoặc gửi trực tiếp tới agent |
| `/stop` | Hủy run đang theo dõi |
| `/schedule 30m task`, `/schedule once 2h task` | Lịch lặp hoặc một lần |
| `/track 30m goal`, `/schedules`, `/unschedule ID` | Goal loop, xem/hủy lịch |
| `/best-of`, `/check`, `/memory` | Tùy chọn lượt kế cho Grok |
| `/reset-next` | Xóa tùy chọn lượt kế |

`/dispatch` là thao tác người dùng gửi chat trực tiếp, khác tag dispatch do agent cha phát.
Scheduler có công tắc global; bật lại sẽ dời lịch lặp quá hạn sang chu kỳ kế và
ngừng lịch một lần đã quá hạn. Chi tiết: [scheduler](../docs/scheduler-spec.md).

## Workspace và Dashboard

- Cây thư mục workspace, file viewer, Papers/Models, Process running và usage widget.
- Terminal tmux: kéo pane thành hàng/cột/lưới; đóng pane chỉ detach. Open/Focus mở lại,
  Kill kết thúc session; session không có TTL và tồn tại qua restart AgentUI.
- Capabilities đọc **inventory local của AgentUI**, không quét toàn bộ home/runtime.
  Toggle là policy riêng cho agent Codex; Delete xóa package khỏi inventory toàn cục.
  Reset provider session vẫn giữ lịch sử chat và memory. Xem [inventory](capability_inventory/README.md).
- Notion System Hub và binding cấp project: [hướng dẫn connector](../notion_report/README.md).
- Template graph Plan/todo đã bị loại bỏ; không có giao thức `<plan>` trong control plane.

## Giới hạn và nơi tra cứu

Ứng dụng chưa có authentication/multi-user isolation; bind localhost và dùng SSH tunnel.
Ranh giới thư mục agent là contract trong prompt, không phải sandbox hệ điều hành riêng.
Không có hard gate tổng quát cho metric acceptance, drift hoặc dissent; xem
[handbook](../BUILD_HANDBOOK.md) và [audit](../docs/documentation-audit.md).
Backend tập trung trong `backend/main.py`; adapter ở `backend/adapters.py`, dữ liệu ở
`backend/db.py`, scaffold ở `backend/projects.py`, giao diện ở `frontend/`.
