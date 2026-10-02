# Triển khai AgentUI

Hướng dẫn theo launcher/code trong working tree, đối chiếu 2026-09-07. Ứng dụng
chưa có authentication và chạy tool với quyền của tài khoản server; giữ bind
`127.0.0.1` và truy cập từ xa qua SSH tunnel.

## Chuẩn bị

- Linux/macOS, Python >=3.10; `uv` là tùy chọn để tạo Python 3.12 environment.
- Runtime đã cài/xác thực cho adapter cần dùng: `claude`, `aas`, hoặc saved Codex login.
  DeepSeek/GLM dùng Claude harness với provider key; [README](README.md) có bảng adapter.
- `tmux` để terminal trong UI tồn tại qua browser/backend restart.
- SSH vào server; trình duyệt truy cập được CDN của các thư viện frontend.

Dùng hướng dẫn cài đặt của runtime bạn chọn và kiểm tra CLI có trên PATH. Launcher
cài các Python dependency trong `app/backend/requirements.txt`, gồm phiên bản
`openai-codex` được pin. Nó không tự cài hoặc đăng nhập Claude/aas.

## Sao chép và cấu hình

Từ thư mục cha của repository, ví dụ copy source sang server mới:

```bash
rsync -av --exclude '.venv*' --exclude '*.db*' --exclude '__pycache__' \
  --exclude '.env*' --exclude 'node_modules' --exclude 'destinations.json' \
  agent_code_IDLE/ user@server:~/agent_code_IDLE/
```

Không dùng lệnh này làm backup database. Secrets, auth, runtime và đường dẫn
project cần được cấu hình riêng trên host đích. Inventory có launch metadata/path
của host gốc; kiểm tra tính portable trước khi bật capability.

Trong `app/registry.yaml`, đặt đường dẫn thực trên server:

```yaml
workspace_root: /home/user/projects
projects:
  - /home/user/projects/my-project
```

Mỗi project có `.agentui/project.yaml` và các agent prompt/cwd tương ứng. Xem
[app/README.md](app/README.md). Loader đọc YAML trên request; refresh browser là đủ.

Cấu hình secret qua environment hoặc `app/.env.local`, tham khảo `app/.env.example`.
File `.env.local` được **source như shell**, không phải dotenv parser; các phép gán
có thể ghi đè biến đã export. `run.sh` còn có fallback `.env`/PATH/aas theo OSC của
máy gốc. GLM adapter ưu tiên `~/.config/glm/env` trước environment. Kiểm tra các
nguồn này nếu giá trị thực không như mong đợi; không in secret ra log.

## Khởi động và truy cập

```bash
cd ~/agent_code_IDLE/app
./run.sh
```

Launcher tạo/kiểm tra `.venv`, cài requirements rồi chạy Python của venv trực tiếp.
Mặc định cổng 5174, reload off. `PORT=5175 ./run.sh` đổi cổng;
`RELOAD=1 ./run.sh` chỉ dành cho development, watch `backend/` và exclude DB.
Code ngoài backend, như `notion_report/`, không thuộc watcher đó.

Trên máy có trình duyệt:

```bash
ssh -N -L 5174:127.0.0.1:5174 user@server
# mở http://127.0.0.1:5174
```

Đóng tunnel chỉ ngừng truy cập. Các agent run tiếp tục nếu backend vẫn sống;
restart backend ngắt run đang chạy. Terminal processes nằm trong tmux riêng nên
có thể mở lại sau restart; đây là lifecycle khác với agent run.

## Chạy bằng service

Ví dụ Linux user service `~/.config/systemd/user/agentui.service` sau khi bootstrap:

```ini
[Unit]
Description=AgentUI control plane
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/agent_code_IDLE/app
ExecStart=/bin/bash %h/agent_code_IDLE/app/run.sh
Environment=RELOAD=0
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agentui
systemctl --user status agentui
journalctl --user -u agentui -f
```

Service dùng launcher để giữ setup Python, import path và environment giống chạy
tay. `run.sh` hiện cài requirements mỗi lần khởi động. Việc user service sống qua
logout phụ thuộc cấu hình user manager của host.

Có thể chạy launcher trong một tmux session riêng khi không dùng systemd.
Không chạy đồng thời nhiều backend cùng ghi một `app/agentui.db`.

## Keepalive riêng của checkout này

`app/keepalive.sh` là watchdog theo site, **không phải installer chung**:

- Chỉ hoạt động nếu hostname ngắn là `ascend-login01`.
- Dùng đường dẫn `/users/PGS0407/binben14/VietHuy/agent_code_IDLE/app` và cổng 5174.
- `.maintenance` trong `app/` ngăn watchdog tự restart khi bảo trì.
- Nếu HTTP root không trả 200, nó chạy `RELOAD=0 ... bash run.sh` và ghi `/tmp/agentui.log`.

Sự tồn tại script không chứng minh cron/service đang cài hoặc host được phép chạy
persistent agent. Kiểm tra cấu hình/policy host thực tế trước khi áp dụng, và chỉnh
host/path/port nếu dùng watchdog ở nơi khác. Không bật hai cơ chế restart cạnh tranh.

## Cập nhật

Hoàn tất hoặc dừng các agent run cần thiết trước khi restart. Nếu watchdog đang
được cài, đặt `.maintenance` trong thời gian bảo trì rồi gỡ sau khi xác minh backend.
Copy/pull source và chạy lại launcher/service để cài đúng requirements. Refresh
browser cho JavaScript mới. Sửa registry không cần restart Python.

Nếu cổng bận, xác định listener rồi dừng qua service manager; tránh kill mọi process
trên cổng bằng lệnh không phân biệt chủ sở hữu. Có thể dùng cổng khác để kiểm tra.
Nếu CLI không tìm thấy, kiểm tra PATH của **service**, không chỉ interactive shell.
Nếu stream bị dồn cuối, kiểm tra PTY và SSE/proxy buffering; không cần mặc định cấp
container quyền `--privileged` để xử lý lỗi này.

## Backup

Backup tối thiểu gồm:

- `app/agentui.db`: sessions/messages, settings, ledgers, schedules và policies.
- `app/registry.yaml`, mỗi project `.agentui/project.yaml` và agent memory/artifacts.
- `notion_report/destinations.json` (hoặc đường dẫn cấu hình riêng), schema project.
- `app/capability_inventory/` nếu có import/delete tùy chỉnh; lưu secret/auth riêng.

SQLite đang hoạt động nên dùng backup API thay vì chỉ copy live `.db`:

```bash
cd ~/agent_code_IDLE
app/.venv/bin/python - <<'PYBACKUP'
from pathlib import Path
import sqlite3
backup = Path('backups')
backup.mkdir(exist_ok=True)
with sqlite3.connect('file:app/agentui.db?mode=ro', uri=True) as source:
    with sqlite3.connect(backup / 'agentui.db') as destination:
        source.backup(destination)
PYBACKUP
```

Ví dụ ghi đè file backup cùng tên khi chạy lại; dùng tên theo đợt nếu cần retention.
Tmux session/process không nằm trong SQLite backup. Restore DB có cả schedules đã
lưu; kiểm tra scheduler state trước khi khởi động một bản phục hồi.
