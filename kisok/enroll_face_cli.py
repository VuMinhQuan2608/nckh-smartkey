#!/usr/bin/env python3
"""
Enroll khuon mat vao database kiosk (kiosk_data/face_database.pkl).

Cach dung tren Pi / PC:
  python3 enroll_face_cli.py                    # camera, nhap ten
  python3 enroll_face_cli.py --name admin       # ten san
  python3 enroll_face_cli.py --info             # xem DB
  python3 enroll_face_cli.py --purge            # xoa mau sai dim
  python3 enroll_face_cli.py --clear            # xoa toan bo face DB

Bam SPACE de chup / enroll, Q de thoat.
Can enroll 3-5 goc mat (thang, trai, phai) cho moi nguoi.
"""
import argparse
import os
import sys

# load .env neu co
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import cv2
from kiosk_local_backend import (
    backend, register_face, face_db_info,
    clear_face_db, purge_incompatible_faces, delete_face,
)

def show_info():
    info = face_db_info()
    print("=== FACE DATABASE ===")
    for k, v in info.items():
        if k != "people":
            print(f"  {k}: {v}")
    print("  people:")
    for name, p in (info.get("people") or {}).items():
        print(f"    - {name}: {p}")
    if info.get("hint"):
        print("  !!", info["hint"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--delete", default="", help="Xoa 1 nguoi khoi face DB")
    ap.add_argument("--camera", type=int, default=0)
    args = ap.parse_args()

    if args.info:
        show_info()
        return
    if args.purge:
        print(purge_incompatible_faces())
        show_info()
        return
    if args.clear:
        print(clear_face_db())
        return
    if args.delete:
        print(delete_face(args.delete))
        return

    name = args.name.strip() or input("Ten nguoi enroll: ").strip()
    if not name:
        print("Thieu ten")
        sys.exit(1)

    print(f"Model: {backend.face.mode}")
    show_info()
    print("SPACE = chup enroll | Q = thoat")
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print("Khong mo duoc camera", args.camera)
        sys.exit(1)

    count_ok = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        disp = frame.copy()
        cv2.putText(disp, f"Enroll: {name} | SPACE=save Q=quit", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow("enroll_face", disp)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break
        if key == ord(" "):
            r = register_face(name, frame)
            print(r)
            if r.get("ok"):
                count_ok += 1
                print(f"  -> mau #{count_ok}")
    cap.release()
    cv2.destroyAllWindows()
    print("Done. Tong mau moi session:", count_ok)
    show_info()

if __name__ == "__main__":
    main()
