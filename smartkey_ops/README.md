# smartkey_ops — Gói vận hành (không sửa code lõi)

## Cài nhanh

```bash
# Giải nén cạnh thư mục kiosk hoặc copy scripts vào máy
cd smartkey_ops
chmod +x scripts/*.sh scripts/*.py

# 1. Admin + PIN
cd /path/to/kiosk   # có kiosk_data hoặc sẽ tạo
python /path/to/smartkey_ops/scripts/bootstrap_admin.py --user admin --pin 123456

# 2. Model SFace
python /path/to/smartkey_ops/scripts/download_sface_models.py

# 3. Backup
KIOSK_DATA_DIR=./kiosk_data /path/to/smartkey_ops/scripts/backup_kiosk_data.sh

# 4. MQTT files — copy vào thư mục kiosk & web
cp mqtt_config.py mqtt_bridge.py /path/to/kiosk/
# pip install paho-mqtt

# 5. Telegram có xác nhận (thay telegram_remote.py)
PYTHONPATH=/path/to/kiosk python scripts/telegram_remote_confirm.py
```

Chi tiết: `docs/README_TONG_HOP.md`
