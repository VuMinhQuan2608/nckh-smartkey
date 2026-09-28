#!/usr/bin/env python3
"""
Đồng bộ một chiều hoặc merge users.json giữa Web server và Kiosk.

Web thường:  <web>/face_recognition/database/users.json
Kiosk:       <kiosk>/kiosk_data/users.json

Cách dùng:
  # Kiosk ← Web (ghi đè field thiếu trên kiosk)
  python sync_users_web_kiosk.py --web path/to/users.json --kiosk path/to/kiosk_data/users.json --direction to_kiosk

  # Web ← Kiosk
  python sync_users_web_kiosk.py --web ... --kiosk ... --direction to_web

  # Merge 2 chiều (union theo username; field kiosk ưu tiên nếu --prefer kiosk)
  python sync_users_web_kiosk.py --web ... --kiosk ... --direction merge --prefer kiosk

Chỉ copy các field an toàn: username, role, status, pin_hash, fingerprint_id,
rfid_uid, telegram_chat_id, password_hash, created_at.
KHÔNG đụng face_database.pkl.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime

SAFE_KEYS = (
    "username",
    "role",
    "status",
    "pin_hash",
    "password_hash",
    "fingerprint_id",
    "rfid_uid",
    "telegram_chat_id",
    "created_at",
    "updated_at",
)


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save(path: str, users: dict) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    # backup cạnh file
    if os.path.exists(path):
        bak = path + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, bak)
        print(f"  backup: {bak}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def norm_user(u: dict, fallback_name: str) -> dict:
    out = {k: u.get(k) for k in SAFE_KEYS if k in u or k == "username"}
    out["username"] = u.get("username") or fallback_name
    out.setdefault("role", "member")
    out.setdefault("status", "pending")
    return out


def merge(a: dict, b: dict, prefer: str) -> dict:
    """prefer: 'a' | 'b' khi trùng key field."""
    names = set(a) | set(b)
    out = {}
    for name in names:
        ua = a.get(name)
        ub = b.get(name)
        if ua and not ub:
            out[name] = norm_user(ua, name)
        elif ub and not ua:
            out[name] = norm_user(ub, name)
        else:
            base = dict(norm_user(ua, name))
            other = norm_user(ub, name)
            for k in SAFE_KEYS:
                va, vb = base.get(k), other.get(k)
                if prefer == "b":
                    if vb not in (None, ""):
                        base[k] = vb
                    elif va not in (None, ""):
                        base[k] = va
                else:
                    if va not in (None, ""):
                        base[k] = va
                    elif vb not in (None, ""):
                        base[k] = vb
            base["updated_at"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            out[name] = base
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", required=True, help="path users.json phía web")
    ap.add_argument("--kiosk", required=True, help="path users.json phía kiosk")
    ap.add_argument(
        "--direction",
        choices=("to_kiosk", "to_web", "merge"),
        default="to_kiosk",
    )
    ap.add_argument("--prefer", choices=("web", "kiosk"), default="kiosk")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    web = load(args.web)
    kiosk = load(args.kiosk)
    print(f"Web users:   {len(web)}  ({args.web})")
    print(f"Kiosk users: {len(kiosk)}  ({args.kiosk})")

    if args.direction == "to_kiosk":
        result = merge(kiosk, web, prefer="b" if args.prefer == "web" else "a")
        # direction to_kiosk: start from kiosk, overlay web if prefer web
        if args.prefer == "web":
            result = merge(kiosk, web, prefer="b")
        else:
            result = merge(web, kiosk, prefer="b")  # kiosk wins
        target = args.kiosk
    elif args.direction == "to_web":
        if args.prefer == "kiosk":
            result = merge(web, kiosk, prefer="b")
        else:
            result = merge(kiosk, web, prefer="b")
        target = args.web
    else:
        if args.prefer == "kiosk":
            result = merge(web, kiosk, prefer="b")
        else:
            result = merge(kiosk, web, prefer="b")
        # merge ghi cả hai
        if args.dry_run:
            print(f"DRY-RUN merge → {len(result)} users")
            for n in sorted(result):
                print(f"  - {n} role={result[n].get('role')} status={result[n].get('status')}")
            return
        save(args.web, result)
        save(args.kiosk, result)
        print(f"OK merge cả 2 file ({len(result)} users)")
        return

    if args.dry_run:
        print(f"DRY-RUN → {target} ({len(result)} users)")
        for n in sorted(result):
            print(f"  - {n}")
        return

    save(target, result)
    print(f"OK ghi {target} ({len(result)} users)")


if __name__ == "__main__":
    main()
