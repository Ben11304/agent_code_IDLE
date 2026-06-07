# Deploy guide

Hướng dẫn copy thư mục này lên server cá nhân và chạy. Chỉ dùng cho personal use (subscription Anthropic không cho phép multi user).

## Yêu cầu server

- Linux hoặc macOS
- Python 3.9 trở lên
- Node 18 trở lên (để cài claude CLI)
- Tài khoản Anthropic Pro hoặc Max
- Có thể SSH vào server

## Bước 1. Cài runtime trên server

SSH vào server, cài đặt một lần.

```bash
# claude CLI (cần Node)
curl -fsSL https://claude.ai/install.sh | bash
# kiểm tra
claude --version

# python venv module (Debian/Ubuntu thường thiếu)
sudo apt-get install -y python3-venv python3-pip   # nếu Ubuntu
# hoặc bỏ qua nếu macOS đã có sẵn
```

(Tuỳ chọn) Cài `aas` nếu muốn dùng node model grok. Repo aas ở `~/Working_space/AI_AGENT_SYSTEM` của user, copy theo nếu cần.

## Bước 2. Đăng nhập claude trên server

Đây là bước quan trọng nhất, không được skip.

```bash
claude auth
```

Lệnh sẽ in một URL và device code. Mở URL trên máy local trong browser, dán code, đăng nhập Anthropic, xác nhận device. Sau khi `claude --version` chạy được và `claude -p hello` trả lời được là OK.

Login lưu trong `~/.config/anthropic/` hoặc `~/.claude/`, persist giữa các phiên SSH. Không cần lặp lại.

## Bước 3. Copy thư mục lên server

Trên máy local.

```bash
cd /Users/viethuy/Working_space
rsync -av --exclude '.venv' --exclude 'agentui.db' --exclude '__pycache__' \
  UI_agentcoding/ user@server:~/UI_agentcoding/
```

Hoặc dùng git, đẩy lên repo private rồi clone trên server.

```bash
ssh user@server
git clone https://github.com/Ben11304/agent_code_IDLE.git UI_agentcoding
```

## Bước 4. Sửa registry.yaml cho đường dẫn server

```bash
ssh user@server
cd ~/UI_agentcoding/app
nano registry.yaml
```

Đổi các absolute path thành đường dẫn project trên server. Ví dụ.

```yaml
projects:
  - /home/user/projects/ConstructionVLM-Eval-AGENT
  - /home/user/projects/energy-infrastructure-risk/.claude/AGENT
```

Mỗi project phải có sẵn file `.agentui/project.yaml` ở thư mục đó. Copy hoặc tạo lại theo schema trong `CLAUDE.md`.

## Bước 5. Chạy thử lần đầu

```bash
cd ~/UI_agentcoding/app
./run.sh
```

Lần đầu mất khoảng 30 giây để tạo venv và pip install. Sau đó uvicorn boot ở 127.0.0.1:5174.

Nếu báo lỗi claude CLI not found, kiểm tra PATH. Thường thêm vào `~/.bashrc` hoặc `~/.zshrc`.

```bash
export PATH="$HOME/.local/bin:$PATH"   # tuỳ chỗ cài claude
```

## Bước 6. Truy cập từ máy local — SSH tunnel

KHÔNG mở port 5174 công khai. UI không có auth. Subscription terms cấm cho người khác dùng. Cách đúng là SSH tunnel.

Trên máy local.

```bash
ssh -L 5174:127.0.0.1:5174 user@server
```

Giữ session SSH này mở. Trong khi tunnel còn sống, mở browser local truy cập `http://127.0.0.1:5174`. Mọi request đi qua SSH encrypted, không lộ ra ngoài.

Khi xong, đóng SSH session là tunnel mất, UI không còn truy cập được.

## Bước 7. Chạy nền lâu dài

Nếu muốn server tự chạy nền, dùng systemd (Linux) hoặc launchd (macOS).

### systemd service

Tạo `~/.config/systemd/user/agentui.service`.

```ini
[Unit]
Description=AgentUI control plane
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/UI_agentcoding/app
ExecStart=%h/UI_agentcoding/app/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 5174
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agentui
# kiểm tra
systemctl --user status agentui
journalctl --user -u agentui -f
```

### tmux đơn giản (không cần root)

```bash
tmux new -s agentui
cd ~/UI_agentcoding/app && ./run.sh
# Ctrl+B rồi D để detach
# quay lại: tmux a -t agentui
```

## Cập nhật code sau này

```bash
# máy local
rsync -av --exclude '.venv' --exclude 'agentui.db' --exclude '__pycache__' \
  UI_agentcoding/ user@server:~/UI_agentcoding/

# hoặc git
ssh user@server "cd ~/UI_agentcoding && git pull"

# restart
ssh user@server "systemctl --user restart agentui"
# hoặc nếu dùng tmux: vào lại session, Ctrl+C, ./run.sh
```

## Các lỗi hay gặp

**claude CLI not found**. PATH không có claude. `which claude` để xác minh, thêm vào `~/.bashrc` rồi `source ~/.bashrc`.

**claude prompts for login mỗi lần**. Login không persist. Có thể do server filesystem ephemeral hoặc HOME bị reset. Check `~/.claude/` còn không, hoặc chạy `claude auth` lại.

**Address already in use 5174**. `lsof -ti tcp:5174 | xargs kill -9` rồi chạy lại. Hoặc đặt `PORT=5175 ./run.sh`.

**Browser local 127.0.0.1:5174 connect refused**. SSH tunnel chưa mở hoặc đã đóng. Mở lại bằng lệnh ssh -L ở Bước 6.

**Streaming lag, output dump cuối**. PTY trên server có thể không hoạt động nếu container restrictive. Kiểm tra `/dev/ptmx` tồn tại. Docker cần `--init` hoặc `--privileged`, không khuyến nghị deploy trong Docker cho use case này.

**Subscription rate limit**. Mỗi dispatch song song tốn quota nhanh. Giữ concurrency thấp. Nếu chạm five_hour limit thì chờ.

## Backup

File quan trọng cần backup định kỳ.

- `agentui.db` (chat history)
- `registry.yaml` (cấu hình project list)
- Mỗi project có `.agentui/project.yaml`

Backup đơn giản.

```bash
ssh user@server "tar czf - ~/UI_agentcoding/app/agentui.db ~/UI_agentcoding/app/registry.yaml" \
  > ~/backups/agentui-$(date +%F).tar.gz
```

## Bảo mật

- Server bind 127.0.0.1, không 0.0.0.0. UI không bao giờ expose public.
- SSH tunnel là kênh duy nhất để truy cập.
- Subscription đăng nhập của bạn dùng cho mọi chat trong UI. Đừng để người khác SSH vào server.
- File `agentui.db` chứa nội dung chat (có thể nhạy cảm), cẩn thận khi rsync hoặc commit.
- Nếu chia sẻ server với người khác, đặt quyền `chmod 700 ~/UI_agentcoding ~/.claude`.

---

Tóm tắt nhanh.

```bash
# server
curl -fsSL https://claude.ai/install.sh | bash
claude auth
git clone https://github.com/Ben11304/agent_code_IDLE.git UI_agentcoding
nano UI_agentcoding/app/registry.yaml   # sửa path
cd UI_agentcoding/app && ./run.sh

# local
ssh -L 5174:127.0.0.1:5174 user@server
# browser → http://127.0.0.1:5174
```
