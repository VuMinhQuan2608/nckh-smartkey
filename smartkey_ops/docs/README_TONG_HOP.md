# Smart Key — README tổng hợp vận hành

## Kiến trúc nhanh

```
┌─────────────┐     HTTP :5055      ┌──────────────────┐
│   ESP32     │ ──────────────────► │  Kiosk (Pi)      │
│ RFID/FP/MQ2 │                     │ kiosk_ui_v8      │
└─────────────┘                     │ local_backend    │
                                    │ GPIO relay       │
┌─────────────┐     Telegram        │ Telegram alert   │
│ Điện thoại  │ ◄─────────────────► │ (+ remote bot)   │
└─────────────┘                     └────────┬─────────┘
                                             │ MQTT (tuỳ chọn)
┌─────────────┐                              ▼
│ Web server  │ ◄──── MQTT / HTTP ──── Mosquitto / LAN
│ YOLO+Face   │
└─────────────┘
```

- **Standalone (khuyên khi mang đi):** chỉ Kiosk + Telegram, `MQTT_ENABLED=0`.
- **LAN đầy đủ:** Kiosk + Web + Mosquitto + (ESP32).

---

## File quan trọng

| Thành phần | File |
|------------|------|
| UI kiosk | `kiosk_ui_v8.py` |
| Backend | `kiosk_local_backend.py` |
| MQTT | `mqtt_config.py`, `mqtt_bridge.py`, `mqtt_*_hook.py` |
| Web | `web_server_v4.py` |
| Ops (gói này) | `scripts/*` |

---

## Checklist máy mới (Pi)

1. Copy code kiosk + `.env`
2. `python scripts/bootstrap_admin.py --user admin --pin 123456`
3. `python scripts/download_sface_models.py` (cần internet)
4. `python enroll_face_cli.py --name admin` (tuỳ)
5. Cài systemd (gói `smartkey_remote`) hoặc chạy tay `python kiosk_ui_v8.py`
6. (Tuỳ) `telegram_remote_confirm.py` cho /unlock có YES
7. Định kỳ: `./scripts/backup_kiosk_data.sh`

---

## Checklist laptop demo

Xem gói `demo_laptop` — PIN demo `1234`, GPIO giả lập.

---

## MQTT

```env
MQTT_ENABLED=1
MQTT_BROKER=192.168.1.50
MQTT_PORT=1883
MQTT_TOPIC_PREFIX=smartkey
```

Cài broker: `sudo apt install mosquitto mosquitto-clients`  
Python: `pip install paho-mqtt`  
Copy `mqtt_config.py` + `mqtt_bridge.py` cùng thư mục kiosk/web.

---

## Đồng bộ user Web ↔ Kiosk

```bash
python scripts/sync_users_web_kiosk.py \
  --web /path/web/face_recognition/database/users.json \
  --kiosk /path/kiosk/kiosk_data/users.json \
  --direction to_kiosk --prefer kiosk
```

`--dry-run` để xem trước.

---

## Backup / restore

```bash
KIOSK_DATA_DIR=./kiosk_data ./scripts/backup_kiosk_data.sh
./scripts/restore_kiosk_data.sh backups/kiosk_data_YYYYMMDD_HHMMSS.tar.gz
```

---

## Bảo mật gợi ý

- Token Telegram chỉ trong `.env`, không hardcode.
- Dùng `telegram_remote_confirm.py` thay bản unlock một lệnh.
- Không port-forward 5000/5055; dùng Cloudflare Tunnel nếu cần web từ xa.
- PIN 4–8 số + lockout; không chia sẻ chat_id bot bừa bãi.

---

## Thứ tự ưu tiên đã làm trong gói `smartkey_ops`

1. ✅ `mqtt_config.py` / `mqtt_bridge.py` (copy kèm)
2. ✅ `bootstrap_admin.py`
3. ✅ `backup_kiosk_data.sh` / `restore_kiosk_data.sh`
4. ✅ `download_sface_models.py`
5. ✅ README này
6. ✅ `telegram_remote_confirm.py` (YES trước khi unlock)
7. ✅ `sync_users_web_kiosk.py`
