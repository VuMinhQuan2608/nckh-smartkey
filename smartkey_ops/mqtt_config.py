# ============================================================
# mqtt_config.py  —  Smart Key MQTT configuration
# Copy file này vào cùng thư mục kiosk và web_server.
# Có thể ghi đè bằng biến môi trường.
# ============================================================

import os

# Broker (IP máy chạy Mosquitto)
MQTT_BROKER = os.environ.get("MQTT_BROKER", "192.168.1.50")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Auth (để trống nếu Mosquitto allow_anonymous true)
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# Client ID (phải khác nhau giữa kiosk và web)
MQTT_CLIENT_ID_KIOSK = os.environ.get("MQTT_CLIENT_ID_KIOSK", "smartkey-kiosk")
MQTT_CLIENT_ID_WEB = os.environ.get("MQTT_CLIENT_ID_WEB", "smartkey-web")

MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))

# Topic prefix — đổi nếu muốn namespace riêng
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "smartkey")

TOPIC_DOOR_CMD = f"{TOPIC_PREFIX}/door/command"
TOPIC_DOOR_STATUS = f"{TOPIC_PREFIX}/door/status"
TOPIC_ACCESS_LOG = f"{TOPIC_PREFIX}/access/log"
TOPIC_ALERT = f"{TOPIC_PREFIX}/alert"
TOPIC_CAMERA_STATUS = f"{TOPIC_PREFIX}/camera/status"
