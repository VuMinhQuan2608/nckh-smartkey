# ============================================================
# mqtt_kiosk_hook.py  —  Ví dụ tích hợp MQTT vào Kiosk
#
# Cách dùng:
#   1. Copy mqtt_config.py + mqtt_bridge.py + file này vào thư mục kiosk
#   2. Trong kiosk_ui_v8.py (hoặc kiosk_local_backend.py):
#
#      from mqtt_kiosk_hook import KioskMqttHook
#
#      # Trong KioskApp.__init__:
#      self.mqtt_hook = KioskMqttHook(
#          unlock_fn=self._local_unlock,   # hàm mở cửa local của bạn
#          lock_fn=self._local_lock,       # hàm khóa local
#          get_status_fn=self._get_local_door_status,  # trả về dict
#      )
#      self.mqtt_hook.start()
#
#   3. Sau mỗi lần auth thành công/thất bại gọi:
#      self.mqtt_hook.on_auth_success(user_name, method)
#      self.mqtt_hook.on_auth_fail(method, note="...")
#
#   4. Sau khi đổi trạng thái cửa local:
#      self.mqtt_hook.publish_status()
# ============================================================

import time
from typing import Callable, Optional

from mqtt_bridge import MqttBridge
from mqtt_config import (
    MQTT_CLIENT_ID_KIOSK,
    TOPIC_DOOR_CMD,
)


class KioskMqttHook:
    """
    Bọc MQTT cho phía Kiosk:
    - Subscribe lệnh unlock/lock từ web
    - Publish trạng thái cửa + access log + alert
    """

    def __init__(
        self,
        unlock_fn: Optional[Callable] = None,
        lock_fn: Optional[Callable] = None,
        get_status_fn: Optional[Callable] = None,
        source: str = "kiosk",
    ):
        """
        unlock_fn()      → thực hiện mở cửa local (GPIO)
        lock_fn()        → thực hiện khóa local
        get_status_fn()  → trả về {"locked": bool, "door": "open|closed|unknown", ...}
        """
        self.unlock_fn = unlock_fn
        self.lock_fn = lock_fn
        self.get_status_fn = get_status_fn
        self.source = source

        self.bridge = MqttBridge(
            MQTT_CLIENT_ID_KIOSK,
            on_connect_extra=self._on_connected,
        )
        self.bridge.subscribe(TOPIC_DOOR_CMD, self._on_door_command)

    def start(self):
        self.bridge.start()

    def stop(self):
        self.bridge.stop()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_connected(self):
        # Gửi status ngay khi connect / reconnect
        self.publish_status()

    def _on_door_command(self, data: dict):
        action = (data.get("action") or "").lower().strip()
        by = data.get("by", "web")
        print(f"[KioskMQTT] Nhận lệnh '{action}' từ {by}")

        try:
            if action == "unlock":
                if self.unlock_fn:
                    self.unlock_fn()
                else:
                    print("[KioskMQTT] WARNING: unlock_fn chưa được gắn")
            elif action == "lock":
                if self.lock_fn:
                    self.lock_fn()
                else:
                    print("[KioskMQTT] WARNING: lock_fn chưa được gắn")
            else:
                print(f"[KioskMQTT] action không hợp lệ: {action}")
                return

            # Đợi GPIO ổn định rồi publish status
            time.sleep(0.15)
            self.publish_status()

            self.bridge.publish_access_log(
                user=by,
                method="remote",
                result="SUCCESS",
                note=f"Lệnh {action} từ {by}",
                source=self.source,
            )
        except Exception as e:
            print(f"[KioskMQTT] Lỗi thực thi lệnh cửa: {e}")
            self.bridge.publish_alert(
                severity="warning",
                alert_type="door_command_fail",
                message=f"Không thực hiện được lệnh {action}: {e}",
                source=self.source,
            )

    # ------------------------------------------------------------------
    # Public helpers — gọi từ kiosk_ui
    # ------------------------------------------------------------------

    def publish_status(self):
        locked = True
        door = "unknown"
        extra = {}
        if self.get_status_fn:
            try:
                st = self.get_status_fn() or {}
                locked = st.get("locked", True)
                door = st.get("door", "unknown")
                extra = {k: v for k, v in st.items() if k not in ("locked", "door")}
            except Exception as e:
                print(f"[KioskMQTT] get_status_fn error: {e}")
        self.bridge.publish_door_status(
            locked=locked,
            door=door,
            source=self.source,
            extra=extra or None,
        )

    def on_auth_success(self, user_name: str, method: str, note: str = ""):
        self.bridge.publish_access_log(
            user=user_name,
            method=method,
            result="SUCCESS",
            note=note or f"Xác thực {method} tại kiosk",
            source=self.source,
        )

    def on_auth_fail(self, method: str, note: str = ""):
        self.bridge.publish_access_log(
            user="?",
            method=method,
            result="FAIL",
            note=note or f"Xác thực {method} thất bại",
            source=self.source,
        )

    def on_alert(self, severity: str, alert_type: str, message: str):
        self.bridge.publish_alert(
            severity=severity,
            alert_type=alert_type,
            message=message,
            source=self.source,
        )

    def publish_camera_status(self, online: bool, fps: float = 0.0):
        self.bridge.publish_camera_status(online=online, fps=fps, source=self.source)
