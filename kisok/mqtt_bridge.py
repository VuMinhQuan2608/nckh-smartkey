# ============================================================
# mqtt_bridge.py  —  Lightweight MQTT bridge for Smart Key
# Dùng chung cho cả Kiosk (Pi) và Web Server.
#
# Cài đặt:
#   pip install paho-mqtt
#
# Ví dụ:
#   from mqtt_bridge import MqttBridge
#   from mqtt_config import MQTT_CLIENT_ID_KIOSK, TOPIC_DOOR_CMD
#
#   bridge = MqttBridge(MQTT_CLIENT_ID_KIOSK)
#   bridge.subscribe(TOPIC_DOOR_CMD, my_handler)
#   bridge.start()
#   bridge.publish_door_status(locked=True)
# ============================================================

import json
import threading
import time
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

from mqtt_config import (
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_USER,
    MQTT_PASS,
    MQTT_KEEPALIVE,
    TOPIC_DOOR_CMD,
    TOPIC_DOOR_STATUS,
    TOPIC_ACCESS_LOG,
    TOPIC_ALERT,
    TOPIC_CAMERA_STATUS,
)


class MqttBridge:
    """
    Thread-safe MQTT client với auto-reconnect.
    - subscribe(topic, handler)
    - publish(topic, dict)
    - helpers: publish_door_status / publish_access_log / ...
    """

    def __init__(self, client_id: str, on_connect_extra: Optional[Callable] = None):
        self.client_id = client_id
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        self._handlers: Dict[str, Callable] = {}
        self._connected = False
        self._lock = threading.Lock()
        self._on_connect_extra = on_connect_extra

        if MQTT_USER:
            self._client.username_pw_set(MQTT_USER, MQTT_PASS)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = rc == 0
        if rc == 0:
            print(f"[MQTT] Connected as {self.client_id} → {MQTT_BROKER}:{MQTT_PORT}")
            for topic in list(self._handlers.keys()):
                client.subscribe(topic, qos=1)
                print(f"[MQTT] Subscribed: {topic}")
            if self._on_connect_extra:
                try:
                    self._on_connect_extra()
                except Exception as e:
                    print(f"[MQTT] on_connect_extra error: {e}")
        else:
            print(f"[MQTT] Connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print(f"[MQTT] Disconnected rc={rc}")

    def _on_message(self, client, userdata, msg):
        handler = self._handlers.get(msg.topic)
        if not handler:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
        try:
            handler(payload)
        except Exception as e:
            print(f"[MQTT] Handler error on {msg.topic}: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Chạy loop_forever trong thread daemon (auto-reconnect)."""

        def _loop():
            while True:
                try:
                    self._client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE)
                    self._client.loop_forever()
                except Exception as e:
                    print(f"[MQTT] Connection error: {e} — retry in 5s")
                    time.sleep(5)

        t = threading.Thread(target=_loop, daemon=True, name=f"mqtt-{self.client_id}")
        t.start()
        print(f"[MQTT] Bridge started ({self.client_id})")

    def stop(self):
        try:
            self._client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    def subscribe(self, topic: str, handler: Callable):
        """Đăng ký handler(payload_dict) cho topic. Tự subscribe lại sau reconnect."""
        self._handlers[topic] = handler
        if self._connected:
            self._client.subscribe(topic, qos=1)

    def publish(self, topic: str, data: dict, qos: int = 1, retain: bool = False):
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock:
            try:
                self._client.publish(topic, payload, qos=qos, retain=retain)
            except Exception as e:
                print(f"[MQTT] Publish error {topic}: {e}")

    # ------------------------------------------------------------------
    # Helpers tiện dụng cho Smart Key
    # ------------------------------------------------------------------

    def publish_door_status(
        self,
        locked: bool,
        door: str = "unknown",
        source: str = "kiosk",
        extra: Optional[dict] = None,
    ):
        data = {
            "locked": bool(locked),
            "door": door,
            "source": source,
            "ts": time.time(),
        }
        if extra:
            data.update(extra)
        # retain=True → client mới join vẫn nhận được trạng thái cuối
        self.publish(TOPIC_DOOR_STATUS, data, retain=True)

    def publish_access_log(
        self,
        user: str,
        method: str,
        result: str,
        note: str = "",
        source: str = "kiosk",
    ):
        self.publish(
            TOPIC_ACCESS_LOG,
            {
                "time": time.strftime("%d/%m/%Y %H:%M:%S"),
                "user": user or "?",
                "method": method,
                "result": result,
                "note": note,
                "source": source,
                "ts": time.time(),
            },
        )

    def publish_alert(
        self,
        severity: str,
        alert_type: str,
        message: str,
        source: str = "kiosk",
    ):
        self.publish(
            TOPIC_ALERT,
            {
                "severity": severity,  # critical | warning | notice
                "type": alert_type,
                "message": message,
                "source": source,
                "ts": time.time(),
            },
        )

    def publish_door_command(self, action: str, by: str = "web"):
        action = action.lower().strip()
        if action not in ("unlock", "lock"):
            raise ValueError("action must be 'unlock' or 'lock'")
        self.publish(
            TOPIC_DOOR_CMD,
            {
                "action": action,
                "by": by,
                "ts": time.time(),
            },
        )

    def publish_camera_status(self, online: bool, fps: float = 0.0, source: str = "kiosk"):
        self.publish(
            TOPIC_CAMERA_STATUS,
            {
                "online": bool(online),
                "fps": round(fps, 1),
                "source": source,
                "ts": time.time(),
            },
            retain=True,
        )
