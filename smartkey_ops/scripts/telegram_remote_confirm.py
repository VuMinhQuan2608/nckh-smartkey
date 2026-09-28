#!/usr/bin/env python3
"""
Telegram remote CÓ XÁC NHẬN cho /unlock (an toàn hơn bản cơ bản).

Luồng:
  /unlock  → bot hỏi "Gõ YES trong 60s để xác nhận"
  YES      → mới gọi backend.unlock()
  /lock    → khóa ngay (ít rủi ro hơn)
  /status /ip /help như bản thường

Chạy thay telegram_remote.py nếu muốn xác nhận 2 bước.
  python telegram_remote_confirm.py
"""
from __future__ import annotations

import os
import sys
import time
import socket
import traceback

def _load_dotenv():
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
        return
    except ImportError:
        pass
    for env_path in (
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "kiosk", ".env"),
    ):
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
        print(f"[TG] Loaded {env_path}")
        return


_load_dotenv()

TOKEN = os.environ.get("8887678556:AAF8m4mmBWW29LZYLZbZPhZyBWuJ1-RpUFM", "").strip()
ALLOWED = {
    x.strip()
    for x in os.environ.get("7426389427", "").split(",")
    if x.strip()
}
CONFIRM_TTL = int(os.environ.get("TG_UNLOCK_CONFIRM_SECONDS", "60"))

if not TOKEN or not ALLOWED:
    print("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_IDS")
    sys.exit(1)

backend = None
try:
    from kiosk_local_backend import backend as _be
    backend = _be
    print("[TG] backend OK")
except Exception as e:
    print(f"[TG] backend import fail: {e}")

# pending unlock confirms: chat_id -> expire_ts
_pending: dict[str, float] = {}


def _get_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "—"


def _api(method: str, payload: dict | None = None) -> dict:
    import requests
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def send(chat_id: str, text: str):
    try:
        _api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"send error: {e}")


def do_unlock(chat_id: str):
    if backend is None:
        send(chat_id, "Backend chưa sẵn sàng.")
        return
    try:
        backend.unlock(by="telegram", method="remote")
        send(chat_id, "Đã MỞ KHÓA (đã xác nhận).")
        if hasattr(backend, "add_login_log"):
            backend.add_login_log("telegram", "remote", "THÀNH CÔNG", "Telegram /unlock confirmed")
    except Exception as e:
        send(chat_id, f"Lỗi unlock: {e}")


def handle(chat_id: str, text: str):
    text = (text or "").strip()
    low = text.lower()
    cmd = low.split()[0] if low else ""

    # Xác nhận YES
    if low in ("yes", "y", "có", "co", "xac nhan", "xác nhận"):
        exp = _pending.get(chat_id)
        if exp and time.time() <= exp:
            del _pending[chat_id]
            do_unlock(chat_id)
        elif exp:
            del _pending[chat_id]
            send(chat_id, "Hết hạn xác nhận. Gõ /unlock lại.")
        else:
            send(chat_id, "Không có lệnh unlock đang chờ. Gõ /unlock trước.")
        return

    if cmd in ("/start", "/help"):
        send(
            chat_id,
            "Smart Key remote (có xác nhận)\n"
            f"/unlock — hỏi YES trong {CONFIRM_TTL}s rồi mới mở\n"
            "/lock — khóa ngay\n"
            "/status /ip /help",
        )
        return

    if cmd == "/ip":
        send(chat_id, f"IP: {_get_ip()}")
        return

    if cmd == "/status":
        if backend is None:
            send(chat_id, "Backend chưa sẵn sàng.")
            return
        try:
            st = backend.door_status()
            locked = st.get("locked", "?")
            send(
                chat_id,
                f"Khóa: {'ĐÃ KHÓA' if locked else 'ĐÃ MỞ'}\n"
                f"Cửa: {st.get('door', '?')}\n"
                f"IP: {_get_ip()}",
            )
        except Exception as e:
            send(chat_id, f"Lỗi: {e}")
        return

    if cmd == "/unlock":
        _pending[chat_id] = time.time() + CONFIRM_TTL
        send(
            chat_id,
            f"⚠️ Xác nhận mở khóa?\nGõ YES trong {CONFIRM_TTL} giây.\n"
            "Gõ khác hoặc hết giờ = huỷ.",
        )
        return

    if cmd == "/lock":
        if backend is None:
            send(chat_id, "Backend chưa sẵn sàng.")
            return
        try:
            backend.lock(by="telegram", method="remote")
            send(chat_id, "Đã KHÓA.")
            if hasattr(backend, "add_login_log"):
                backend.add_login_log("telegram", "remote", "THÀNH CÔNG", "Telegram /lock")
        except Exception as e:
            send(chat_id, f"Lỗi lock: {e}")
        return

    if text.startswith("/"):
        send(chat_id, "Lệnh không rõ. /help")


def main():
    print(f"[TG-CONFIRM] chats={ALLOWED} ttl={CONFIRM_TTL}s")
    offset = 0
    try:
        data = _api("getUpdates", {"timeout": 0, "offset": -1})
        results = data.get("result") or []
        if results:
            offset = results[-1]["update_id"] + 1
    except Exception as e:
        print(f"init: {e}")

    while True:
        try:
            data = _api(
                "getUpdates",
                {"timeout": 25, "offset": offset, "allowed_updates": ["message"]},
            )
            for upd in data.get("result") or []:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text") or ""
                if not chat_id or chat_id not in ALLOWED:
                    continue
                print(f"[TG] {chat_id}: {text!r}")
                handle(chat_id, text)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"loop: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
