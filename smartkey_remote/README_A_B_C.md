# Smart Key — A / B / C (không sửa code gốc)

Bộ file này **bổ sung** cho kiosk Raspberry Pi. Không bắt buộc chỉnh `kiosk_ui_v8.py` / `kiosk_local_backend.py` (trừ phần C — chỉ vài dòng tùy chọn).

| Mục | File chính | Mô tả |
|-----|------------|--------|
| **A** | `systemd/*`, `install_services.sh` | Tự chạy kiosk + (tuỳ chọn) wifi-connect khi boot |
| **B** | `telegram_remote.py` | Lệnh Telegram `/unlock` `/lock` `/status` `/ip` |
| **C** | `optional_patches/show_ip_on_ui.txt` | Hiển thị IP WiFi trên màn kiosk |

---

## Yêu cầu

- Raspberry Pi OS (Bookworm khuyến nghị), có `python3`, NetworkManager
- Đã có thư mục kiosk với: `kiosk_ui_v8.py`, `kiosk_local_backend.py`, `.env`
- Token Telegram kiosk trong `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`)

---

## A — Cài service boot

```bash
# Copy cả thư mục smartkey_remote vào Pi, ví dụ:
#   /home/pi/smartkey_remote/

cd /home/pi/smartkey_remote
chmod +x install_services.sh telegram_remote.py

# Chỉnh đường dẫn kiosk trong install_services.sh nếu khác
# Mặc định: /home/pi/kiosk  (đổi cho đúng máy bạn)

sudo ./install_services.sh
```

Script sẽ:

1. Cài unit `smartkey-kiosk.service` (chạy UI kiosk)
2. Cài unit `smartkey-telegram-remote.service` (bot /unlock /lock)
3. (Tuỳ chọn) gợi ý wifi-connect — **không** ép cài binary nếu bạn chưa tải

Bật/tắt:

```bash
sudo systemctl enable --now smartkey-kiosk
sudo systemctl enable --now smartkey-telegram-remote
sudo systemctl status smartkey-kiosk
```

Log:

```bash
journalctl -u smartkey-kiosk -f
journalctl -u smartkey-telegram-remote -f
```

### WiFi-connect (tuỳ chọn)

1. Tải binary phù hợp Pi từ: https://github.com/balena-io/wifi-connect/releases  
2. Đặt vào `/usr/local/bin/wifi-connect`, `chmod +x`  
3. `sudo cp systemd/wifi-connect.service /etc/systemd/system/`  
4. `sudo systemctl enable wifi-connect`  

Khi **không** có mạng, Pi phát AP `SmartKey-Setup` để nhập WiFi.

---

## B — Telegram remote

Chạy cùng Pi với backend (import `kiosk_local_backend`).

Lệnh (chỉ chat_id trong `TELEGRAM_CHAT_IDS`):

| Lệnh | Tác dụng |
|------|----------|
| `/start` hoặc `/help` | Hướng dẫn |
| `/unlock` | Mở khóa cửa |
| `/lock` | Khóa cửa |
| `/status` | Trạng thái cửa + online |
| `/ip` | IP LAN hiện tại |

Biến môi trường (đã có trong `.env` kiosk là đủ):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_IDS` (phẩy nếu nhiều)
- `KIOSK_DATA_DIR` / đường dẫn backend

Chạy tay để test:

```bash
cd /home/pi/kiosk          # thư mục có kiosk_local_backend.py
export $(grep -v '^#' .env | xargs)   # hoặc dùng python-dotenv
python3 /home/pi/smartkey_remote/telegram_remote.py
```

Service đã trỏ sẵn nếu bạn chạy `install_services.sh` đúng path.

**Bảo mật:** Chỉ chat_id được liệt kê mới được lệnh. Không share bot token.

---

## C — Hiện IP trên UI kiosk

Xem file `optional_patches/show_ip_on_ui.txt` — copy **tối thiểu** vài dòng vào `kiosk_ui_v8.py` (vị trí đã ghi trong file). Không đụng logic auth/GPIO.

Hoặc chỉ dùng `/ip` trên Telegram (không sửa UI).

---

## Gỡ cài

```bash
sudo systemctl disable --now smartkey-kiosk smartkey-telegram-remote wifi-connect 2>/dev/null
sudo rm -f /etc/systemd/system/smartkey-kiosk.service
sudo rm -f /etc/systemd/system/smartkey-telegram-remote.service
sudo rm -f /etc/systemd/system/wifi-connect.service
sudo systemctl daemon-reload
```
