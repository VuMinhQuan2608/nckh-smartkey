#!/usr/bin/env python3
"""
Tạo / cập nhật tài khoản admin đầu tiên trên kiosk (users.json).
Không sửa file backend — chỉ ghi kiosk_data/users.json.

Cách dùng:
  python bootstrap_admin.py
  python bootstrap_admin.py --user admin --pin 123456
  DEMO_PIN=9999 python bootstrap_admin.py --user chuha
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _data_dir() -> str:
    d = os.environ.get("KIOSK_DATA_DIR")
    if d:
        return os.path.abspath(d)
    # cạnh script → ../kiosk_data hoặc ./kiosk_data
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(os.getcwd(), "kiosk_data"),
        os.path.join(here, "..", "kiosk_data"),
        os.path.join(here, "kiosk_data"),
    ):
        cand = os.path.normpath(cand)
        if os.path.isdir(cand) or cand.endswith("kiosk_data"):
            return cand
    return os.path.join(os.getcwd(), "kiosk_data")


def pin_hash(pin: str) -> str:
    return hashlib.sha256(("smartkey-pin:" + pin).encode("utf-8")).hexdigest()


def load_users(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Lỗi đọc {path}: {e}")
        return {}


def save_users(path: str, users: dict) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Bootstrap admin kiosk")
    ap.add_argument("--user", default=os.environ.get("BOOTSTRAP_USER", "admin"))
    ap.add_argument("--pin", default=os.environ.get("DEMO_PIN") or os.environ.get("BOOTSTRAP_PIN", "1234"))
    ap.add_argument("--rfid", default=os.environ.get("BOOTSTRAP_RFID", ""))
    ap.add_argument("--fp", default=os.environ.get("BOOTSTRAP_FP", ""), help="fingerprint_id")
    ap.add_argument("--telegram", default="", help="telegram chat_id (mặc định lấy TELEGRAM_CHAT_IDS đầu)")
    ap.add_argument("--data-dir", default="", help="override KIOSK_DATA_DIR")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir) if args.data_dir else _data_dir()
    users_path = os.path.join(data_dir, "users.json")
    os.makedirs(data_dir, exist_ok=True)

    pin = str(args.pin).strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        print("PIN phải là 4–8 chữ số.")
        sys.exit(1)

    tg = (args.telegram or "").strip()
    if not tg:
        chats = os.environ.get("TELEGRAM_CHAT_IDS", "")
        tg = chats.split(",")[0].strip() if chats else ""

    users = load_users(users_path)
    name = args.user.strip()
    if not name:
        print("Thiếu tên user")
        sys.exit(1)

    existing = users.get(name, {})
    users[name] = {
        "username": name,
        "role": "admin",
        "status": "approved",
        "password_hash": existing.get("password_hash", ""),
        "pin_hash": pin_hash(pin),
        "fingerprint_id": (args.fp or existing.get("fingerprint_id") or "").strip(),
        "rfid_uid": (args.rfid or existing.get("rfid_uid") or "").strip().upper().replace(" ", ""),
        "telegram_chat_id": tg or existing.get("telegram_chat_id", ""),
        "created_at": existing.get("created_at")
        or datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }

    save_users(users_path, users)
    print("=== BOOTSTRAP OK ===")
    print(f"  data_dir : {data_dir}")
    print(f"  users    : {users_path}")
    print(f"  username : {name}")
    print(f"  role     : admin / approved")
    print(f"  PIN      : {pin}")
    if users[name]["rfid_uid"]:
        print(f"  RFID     : {users[name]['rfid_uid']}")
    if users[name]["fingerprint_id"]:
        print(f"  FP id    : {users[name]['fingerprint_id']}")
    if users[name]["telegram_chat_id"]:
        print(f"  Telegram : {users[name]['telegram_chat_id']}")
    print(f"  Tổng user trong DB: {len(users)}")


if __name__ == "__main__":
    main()
