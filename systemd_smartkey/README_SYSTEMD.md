# Cấu hình systemd chi tiết — Smart Key trên Raspberry Pi

## Tổng quan service

| Unit | Mục đích | Boot target |
|------|----------|-------------|
| `smartkey-kiosk.service` | UI Tkinter + camera + auth | `graphical.target` |
| `smartkey-telegram-remote.service` | Bot `/unlock` `/lock` | `multi-user.target` |
| `smartkey-wifi-connect.service` | Portal WiFi khi offline (tuỳ chọn) | `multi-user.target` |
| `smartkey-backup.timer` + `.service` | Backup `kiosk_data` mỗi ngày | `timers.target` |

---

## Điều kiện trước khi cài

1. User desktop (thường `pi`) đăng nhập auto-login vào GUI (Raspberry Pi Imager / `raspi-config` → System → Auto Login).
2. Code nằm ví dụ: `/home/pi/kiosk/` với `kiosk_ui_v8.py`, `kiosk_local_backend.py`, `.env`.
3. Đã test tay OK:
   ```bash
   cd /home/pi/kiosk
   python3 kiosk_ui_v8.py
   ```
4. (Telegram) `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_IDS` trong `.env`.

---

## Cài nhanh bằng script

```bash
# Giải nén gói systemd_smartkey lên Pi
cd /home/pi/systemd_smartkey
chmod +x scripts/install_systemd.sh

sudo KIOSK_DIR=/home/pi/kiosk \
     REMOTE_DIR=/home/pi/smartkey_remote \
     OPS_DIR=/home/pi/smartkey_ops \
     ./scripts/install_systemd.sh

sudo systemctl enable --now smartkey-kiosk
sudo systemctl enable --now smartkey-telegram-remote
sudo systemctl enable --now smartkey-backup.timer
```

---

## Cài tay (hiểu từng bước)

### 1. Copy unit

```bash
sudo cp units/smartkey-kiosk.service /etc/systemd/system/
sudo cp units/smartkey-telegram-remote.service /etc/systemd/system/
sudo cp units/smartkey-backup.service /etc/systemd/system/
sudo cp units/smartkey-backup.timer /etc/systemd/system/
# tuỳ chọn:
sudo cp units/smartkey-wifi-connect.service /etc/systemd/system/
```

Sửa path trong file nếu user không phải `pi` hoặc thư mục khác:

```bash
sudo nano /etc/systemd/system/smartkey-kiosk.service
# User=, WorkingDirectory=, EnvironmentFile=, ExecStart=, XAUTHORITY=
```

### 2. Nạp lại & bật

```bash
sudo systemctl daemon-reload
sudo systemctl enable smartkey-kiosk
sudo systemctl enable smartkey-telegram-remote
sudo systemctl enable smartkey-backup.timer
sudo systemctl start smartkey-kiosk
sudo systemctl start smartkey-telegram-remote
```

### 3. Kiểm tra

```bash
systemctl status smartkey-kiosk --no-pager
systemctl status smartkey-telegram-remote --no-pager
systemctl list-timers | grep smartkey

journalctl -u smartkey-kiosk -n 50 --no-pager
journalctl -u smartkey-kiosk -f
```

---

## Giải thích từng section (kiosk UI)

```ini
[Unit]
After=network-online.target graphical.target
```
- Đợi mạng (Telegram) và môi trường đồ họa.

```ini
[Service]
User=pi
WorkingDirectory=/home/pi/kiosk
EnvironmentFile=-/home/pi/kiosk/.env
```
- Dấu `-` trước path `.env`: thiếu file vẫn không fail unit.
- Mọi `FACE_*`, `TELEGRAM_*`, `RELAY_PIN` đọc từ đây.

```ini
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/pi/.Xauthority
```
- Bắt buộc với Tkinter trên màn HDMI/DSI.
- User service **phải** là user đang sở hữu session X (thường `pi`).

```ini
Restart=on-failure
RestartSec=5
```
- Crash → chờ 5s → chạy lại (camera lỗi tạm thời).

```ini
[Install]
WantedBy=graphical.target
```
- Chỉ auto-start khi Pi boot vào desktop (không phải pure console).

---

## Pi Bookworm / Wayland

Nếu `echo $WAYLAND_DISPLAY` có giá trị khi đăng nhập GUI:

Trong `smartkey-kiosk.service` thử thêm:

```ini
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_RUNTIME_DIR=/run/user/1000
```

UID 1000 = user `pi` mặc định (`id -u pi`).

Hoặc buộc X11: `raspi-config` → Advanced → Wayland → X11.

---

## Auto-login GUI (cần cho kiosk)

```bash
sudo raspi-config
# System Options → Auto Login → Desktop
```

Hoặc LightDM:

```bash
sudo nano /etc/lightdm/lightdm.conf
# autologin-user=pi
```

---

## Telegram: bản có YES

Sửa unit:

```bash
sudo systemctl edit --full smartkey-telegram-remote
```

Đổi `ExecStart` thành:

```ini
ExecStart=/usr/bin/python3 /home/pi/smartkey_ops/scripts/telegram_remote_confirm.py
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart smartkey-telegram-remote
```

---

## Lệnh vận hành thường dùng

```bash
# Dừng / chạy lại UI
sudo systemctl stop smartkey-kiosk
sudo systemctl start smartkey-kiosk
sudo systemctl restart smartkey-kiosk

# Tắt không chạy khi boot
sudo systemctl disable smartkey-kiosk

# Xem log hôm nay
journalctl -u smartkey-kiosk --since today

# Backup chạy tay
sudo systemctl start smartkey-backup.service
```

---

## Sự cố thường gặp

| Triệu chứng | Cách xử lý |
|-------------|------------|
| `cannot open display :0` | Auto-login desktop; đúng `User=` session GUI; `DISPLAY=:0` |
| Service active nhưng không thấy cửa sổ | Đang chạy user khác / Wayland; xem `journalctl` |
| Import `kiosk_local_backend` fail | `WorkingDirectory` + `PYTHONPATH` trỏ đúng thư mục |
| Telegram không phản hồi | `.env` token/chat_id; `journalctl -u smartkey-telegram-remote -f` |
| GPIO không chạy | User trong group `gpio`; cài `gpiozero`; test tay trước |
| Restart liên tục | `journalctl` tìm traceback Python; `StartLimitBurst` sẽ tạm dừng |

Thêm user vào group (logout/login lại):

```bash
sudo usermod -aG gpio,video,i2c,spi pi
```

---

## Gỡ cài

```bash
sudo systemctl disable --now smartkey-kiosk smartkey-telegram-remote smartkey-backup.timer smartkey-wifi-connect 2>/dev/null
sudo rm -f /etc/systemd/system/smartkey-*.service /etc/systemd/system/smartkey-*.timer
sudo systemctl daemon-reload
```

---

## Thứ tự boot gợi ý

1. `NetworkManager` / mạng  
2. `graphical.target` + auto-login  
3. `smartkey-kiosk` (sau sleep 3s trong `ExecStartPre`)  
4. `smartkey-telegram-remote` (song song, multi-user)  
5. Đêm: `smartkey-backup.timer`  

ESP32 **không** cần systemd trên Pi — chỉ cần `kiosk_local_backend` đã `start_esp32_api_server` khi import (API :5055).
