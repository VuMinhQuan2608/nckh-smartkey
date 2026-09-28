#!/usr/bin/env python3
"""
Tải model OpenCV SFace (+ YuNet tuỳ chọn) vào kiosk_data/models/

Cách dùng:
  python download_sface_models.py
  KIOSK_DATA_DIR=./kiosk_data python download_sface_models.py

URL chính thức OpenCV Zoo (GitHub raw / release).
"""
from __future__ import annotations

import os
import sys
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# OpenCV model zoo — SFace 2021dec
MODELS = [
    {
        "name": "face_recognition_sface_2021dec.onnx",
        "urls": [
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        ],
    },
    {
        "name": "face_detection_yunet_2023mar.onnx",
        "urls": [
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        ],
        "optional": True,
    },
]


def data_dir() -> str:
    d = os.environ.get("KIOSK_DATA_DIR")
    if d:
        return os.path.abspath(d)
    cwd = os.path.join(os.getcwd(), "kiosk_data")
    return cwd


def download(url: str, dest: str) -> bool:
    print(f"  GET {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SmartKey-SFace-Downloader/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            total = 0
            while True:
                chunk = resp.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        if total < 1000:
            os.remove(dest)
            print(f"  File quá nhỏ ({total} bytes) — bỏ")
            return False
        print(f"  OK {dest} ({total} bytes)")
        return True
    except Exception as e:
        print(f"  Lỗi: {e}")
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except Exception:
                pass
        return False


def main():
    base = data_dir()
    model_dir = os.path.join(base, "models")
    os.makedirs(model_dir, exist_ok=True)
    print(f"Thư mục models: {model_dir}")

    ok_sface = False
    for m in MODELS:
        dest = os.path.join(model_dir, m["name"])
        if os.path.exists(dest) and os.path.getsize(dest) > 1000:
            print(f"Đã có: {m['name']} ({os.path.getsize(dest)} bytes)")
            if "sface" in m["name"]:
                ok_sface = True
            continue
        print(f"Tải {m['name']} ...")
        success = False
        for url in m["urls"]:
            if download(url, dest):
                success = True
                break
        if success and "sface" in m["name"]:
            ok_sface = True
        elif not success and not m.get("optional"):
            print(f"THẤT BẠI bắt buộc: {m['name']}")
        elif not success:
            print(f"(tuỳ chọn) bỏ qua: {m['name']}")

    # Alias tên backend đôi khi tìm
    sface = os.path.join(model_dir, "face_recognition_sface_2021dec.onnx")
    alias = os.path.join(model_dir, "face_recognizer_fast.onnx")
    if os.path.exists(sface) and not os.path.exists(alias):
        try:
            import shutil
            shutil.copy2(sface, alias)
            print(f"Copy alias → {alias}")
        except Exception as e:
            print(f"Alias skip: {e}")

    print()
    if ok_sface:
        print("SFace sẵn sàng.")
        print("Trong .env gợi ý:")
        print("  FACE_MODEL=SFace")
        print(f"  SFACE_MODEL={sface}")
        print("  FACE_SIMILARITY=0.38")
        return 0
    print("Chưa tải được SFace. Có thể dùng FACE_MODEL=DeepFace (pip install deepface) hoặc simple.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
