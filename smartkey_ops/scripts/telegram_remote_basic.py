#!/usr/bin/env python3
"""
Smart Key — Telegram remote control (file ĐỘC LẬP, không sửa code gốc).

Chạy trên cùng Pi với kiosk_local_backend.py:
  - /unlock  mở cửa
  - /lock    khóa cửa
  - /status  trạng thái cửa
  - /ip      IP LAN
  - /help

Chỉ chấp nhận chat_id trong TELEGRAM_CHAT_IDS (.env).
"""

from __future__ import annotations

import os
import sys
import time
import socket
import traceback

# ---- load .env (cùng logic nhẹ như kiosk) ----
def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
        return
    except ImportError:
        pass
    # tìm .env cạnh cwd hoặc thư mục script
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kiosk", ".env"),
    ]
    for env_path in candidates:
        env_path = os.path.normpath(env_path)
        if not os.path.isfile(env_path):
            continue
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        print(f"[TG-REMOTE] Loaded .env: {env_path}")
        return


_load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if x.strip()
}

if not TOKEN:
    print("[TG-REMOTE] THIẾU TELEGRAM_BOT_TOKEN — thoát.")
    sys.exit(1)
if not ALLOWED:
    print("[TG-REMOTE] THIẾU TELEGRAM_CHAT_IDS — thoát.")
    sys.exit(1)

# ---- backend (không đụng file gốc; chỉ import) ----
backend = None
try:
    from kiosk_local_backend import backend as _be
    backend = _be
    print("[TG-REMOTE] Đã gắn kiosk_local_backend.backend")
except Exception as e:
    print(f"[TG-REMOTE] Không import được backend: {e}")
    print("[TG-REMOTE] /unlock /lock sẽ báo lỗi cho đến khi PYTHONPATH đúng.")


def _get_ip() -> str:
    ips = []
    try:
        # UDP trick — không gửi thật
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ", ".join(ips) if ips else "không xác định"


def _api(method: str, payload: dict | None = None) -> dict:
    import requests
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def send_message(chat_id: str, text: str):
    try:
        _api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"[TG-REMOTE] sendMessage error: {e}")


def handle(chat_id: str, text: str):
    text = (text or "").strip()
    cmd = text.split()[0].lower() if text else ""

    if cmd in ("/start", "/help"):
        send_message(
            chat_id,
            "Smart Key remote\n"
            "/unlock — mở khóa\n"
            "/lock — khóa cửa\n"
            "/status — trạng thái\n"
            "/ip — địa chỉ IP\n"
            "/help — trợ giúp",
        )
        return

    if cmd == "/ip":
        send_message(chat_id, f"IP: {_get_ip()}")
        return

    if backend is None:
        send_message(chat_id, "Backend chưa sẵn sàng (kiểm tra PYTHONPATH / kiosk_local_backend).")
        return

    if cmd == "/status":
        try:
            st = backend.door_status() if hasattr(backend, "door_status") else {}
            locked = st.get("locked", "?")
            door = st.get("door", "unknown")
            auto = st.get("auto_lock_remaining", "")
            msg = (
                f"Khóa: {'ĐÃ KHÓA' if locked else 'ĐÃ MỞ'}\n"
                f"Cửa: {door}\n"
                f"IP: {_get_ip()}"
            )
            if auto not in ("", None, 0):
                msg += f"\nAuto-lock còn: {auto}s"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Lỗi status: {e}")
        return

    if cmd == "/unlock":
        try:
            if hasattr(backend, "unlock"):
                backend.unlock(by="telegram", method="remote")
            elif hasattr(backend, "door_control"):
                backend.door_control("unlock")
            else:
                send_message(chat_id, "Backend không có unlock().")
                return
            send_message(chat_id, "Đã gửi lệnh MỞ KHÓA.")
            if hasattr(backend, "add_login_log"):
                backend.add_login_log("telegram", "remote", "THÀNH CÔNG", "Telegram /unlock")
        except Exception as e:
            send_message(chat_id, f"Lỗi unlock: {e}")
        return

    if cmd == "/lock":
        try:
            if hasattr(backend, "lock"):
                backend.lock(by="telegram", method="remote")
            elif hasattr(backend, "door_control"):
                backend.door_control("lock")
            else:
                send_message(chat_id, "Backend không có lock().")
                return
            send_message(chat_id, "Đã gửi lệnh KHÓA.")
            if hasattr(backend, "add_login_log"):
                backend.add_login_log("telegram", "remote", "THÀNH CÔNG", "Telegram /lock")
        except Exception as e:
            send_message(chat_id, f"Lỗi lock: {e}")
        return

    # bỏ qua tin không phải lệnh
    if text.startswith("/"):
        send_message(chat_id, "Lệnh không rõ. Gõ /help")


def main():
    print(f"[TG-REMOTE] Allowed chats: {ALLOWED}")
    offset = 0
    # bỏ tin cũ khi start
    try:
        data = _api("getUpdates", {"timeout": 0, "offset": -1})
        results = data.get("result") or []
        if results:
            offset = results[-1]["update_id"] + 1
    except Exception as e:
        print(f"[TG-REMOTE] getUpdates init: {e}")

    print("[TG-REMOTE] Polling…")
    while True:
        try:
            data = _api(
                "getUpdates",
                {"timeout": 25, "offset": offset, "allowed_updates": ["message"]},
            )
            for upd in data.get("result") or []:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = msg.get("text") or ""
                if not chat_id:
                    continue
                if chat_id not in ALLOWED:
                    print(f"[TG-REMOTE] Bỏ qua chat_id không được phép: {chat_id}")
                    continue
                print(f"[TG-REMOTE] {chat_id}: {text!r}")
                handle(chat_id, text)
        except KeyboardInterrupt:
            print("Stop.")
            break
        except Exception as e:
            print(f"[TG-REMOTE] loop error: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
