# ============================================================
# kiosk_camera.py  —  Camera abstraction cho Smart Key Kiosk
#
# Hỗ trợ:
#   1) Picamera2  — Raspberry Pi Camera Module V2 / HQ / V3 (CSI)
#   2) OpenCV     — USB webcam / V4L2 fallback
#
# API thống nhất (giống cv2.VideoCapture):
#   cap = open_camera()          # tự chọn backend theo .env / hardware
#   ok, frame_bgr = cap.read()   # luôn trả BGR uint8
#   cap.isOpened()
#   cap.release()
#
# Biến môi trường:
#   CAMERA_BACKEND=auto|picamera2|opencv   (mặc định: auto)
#   CAMERA_WIDTH=640
#   CAMERA_HEIGHT=480
#   CAMERA_FPS=15
#   CAMERA_HFLIP=0|1                       (lật ngang — gương)
#   CAMERA_VFLIP=0|1
#   CAMERA_ROTATION=0|90|180|270           (Picamera2)
#   LOCAL_CAMERA_INDEX=0                   (chỉ dùng khi opencv)
# ============================================================

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
    CV2_OK = True
except Exception:
    CV2_OK = False
    cv2 = None  # type: ignore

# ---------------------------------------------------------------------------
# Config từ env
# ---------------------------------------------------------------------------
CAMERA_BACKEND = os.environ.get("CAMERA_BACKEND", "auto").strip().lower()
CAMERA_WIDTH = int(os.environ.get("CAMERA_WIDTH", os.environ.get("CAPTURE_WIDTH", "640")))
CAMERA_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", os.environ.get("CAPTURE_HEIGHT", "480")))
CAMERA_FPS = int(os.environ.get("CAMERA_FPS", "15"))
CAMERA_HFLIP = os.environ.get("CAMERA_HFLIP", "0").strip().lower() in ("1", "true", "yes", "on")
CAMERA_VFLIP = os.environ.get("CAMERA_VFLIP", "0").strip().lower() in ("1", "true", "yes", "on")
CAMERA_ROTATION = int(os.environ.get("CAMERA_ROTATION", "0") or 0)
LOCAL_CAMERA_INDEX = int(os.environ.get("LOCAL_CAMERA_INDEX", "0"))


# ---------------------------------------------------------------------------
# Picamera2 availability
# ---------------------------------------------------------------------------
def _picamera2_available() -> bool:
    try:
        from picamera2 import Picamera2  # noqa: F401
        return True
    except Exception:
        return False


def _has_csi_camera() -> bool:
    """Heuristic: Pi Camera Module attached (libcamera / /dev/video*)."""
    # 1) libcamera-hello style — list cameras via Picamera2 if importable
    try:
        from picamera2 import Picamera2
        cams = Picamera2.global_camera_info()
        if cams:
            return True
    except Exception:
        pass
    # 2) device nodes common on Bookworm / Bullseye
    for path in (
        "/dev/video0",
        "/base/soc/i2c0mux/i2c@1/imx219@10",  # Module V2 sensor path (dt)
    ):
        if os.path.exists(path):
            # On Pi with only CSI, video0 often is the CSI cam under libcamera
            # Avoid false positive when only USB webcam: still OK, auto will try picam first
            return True
    return False


# ---------------------------------------------------------------------------
# Unified camera wrappers
# ---------------------------------------------------------------------------

class _BaseCamera:
    def isOpened(self) -> bool:
        raise NotImplementedError

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Return (ok, frame_bgr). frame is HxWx3 BGR uint8 or None."""
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError

    def get_backend(self) -> str:
        return "unknown"


class Picamera2Camera(_BaseCamera):
    """
    Raspberry Pi Camera Module V2 (IMX219) / HQ / V3 via Picamera2 + libcamera.
    Output always BGR for OpenCV pipeline.
    """

    def __init__(
        self,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: int = CAMERA_FPS,
        hflip: bool = CAMERA_HFLIP,
        vflip: bool = CAMERA_VFLIP,
        rotation: int = CAMERA_ROTATION,
        camera_num: int = 0,
    ):
        from picamera2 import Picamera2

        self._lock = threading.Lock()
        self._picam = Picamera2(camera_num=camera_num)
        self._ok = False
        self._width = width
        self._height = height
        self._hflip = bool(hflip)
        self._vflip = bool(vflip)
        self._rotation = int(rotation) % 360
        self._transform_applied = False

        # Prefer RGB888 → convert to BGR (ổn định trên Bookworm)
        config = None
        try:
            from libcamera import Transform  # type: ignore
            transform = Transform(
                hflip=1 if self._hflip else 0,
                vflip=1 if self._vflip else 0,
            )
            config = self._picam.create_preview_configuration(
                main={"size": (width, height), "format": "RGB888"},
                controls={"FrameRate": float(fps)},
                transform=transform,
            )
            self._transform_applied = True
        except Exception:
            config = self._picam.create_preview_configuration(
                main={"size": (width, height), "format": "RGB888"},
                controls={"FrameRate": float(fps)},
            )
        self._picam.configure(config)
        self._picam.start()
        # Warm-up: discard a few frames
        time.sleep(0.3)
        for _ in range(3):
            try:
                self._picam.capture_array("main")
            except Exception:
                break
        self._ok = True
        print(
            f"[CAM] Picamera2 OK {width}x{height}@{fps} "
            f"hflip={self._hflip} vflip={self._vflip} rot={self._rotation} "
            f"transform={self._transform_applied}"
        )

    def isOpened(self) -> bool:
        return self._ok

    def get_backend(self) -> str:
        return "picamera2"

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self._ok:
            return False, None
        try:
            with self._lock:
                arr = self._picam.capture_array("main")
            if arr is None:
                return False, None
            # RGB888 → BGR
            if CV2_OK:
                frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            else:
                # fallback channel swap
                frame = arr[:, :, ::-1].copy()
            if self._rotation == 90:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE) if CV2_OK else np.rot90(frame, k=1).copy()
            elif self._rotation == 180:
                frame = cv2.rotate(frame, cv2.ROTATE_180) if CV2_OK else np.rot90(frame, k=2).copy()
            elif self._rotation == 270:
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE) if CV2_OK else np.rot90(frame, k=3).copy()
            # Manual flip nếu libcamera Transform không áp dụng được
            if not self._transform_applied:
                if self._hflip and CV2_OK:
                    frame = cv2.flip(frame, 1)
                if self._vflip and CV2_OK:
                    frame = cv2.flip(frame, 0)
            return True, frame
        except Exception as e:
            print(f"[CAM] Picamera2 read error: {e}")
            return False, None

    def release(self) -> None:
        self._ok = False
        try:
            with self._lock:
                self._picam.stop()
                self._picam.close()
        except Exception:
            pass
        print("[CAM] Picamera2 released")


class OpenCVCamera(_BaseCamera):
    """USB / V4L2 via OpenCV VideoCapture."""

    def __init__(
        self,
        index: int = LOCAL_CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        hflip: bool = CAMERA_HFLIP,
        vflip: bool = CAMERA_VFLIP,
    ):
        if not CV2_OK:
            raise RuntimeError("OpenCV không có")
        self._hflip = hflip
        self._vflip = vflip
        self._cap = None
        self._ok = False

        backends = []
        if hasattr(cv2, "CAP_V4L2"):
            backends.append(cv2.CAP_V4L2)
        if hasattr(cv2, "CAP_DSHOW"):
            backends.append(cv2.CAP_DSHOW)
        backends.append(None)

        # Silence probe noise
        devnull = None
        old_stderr = None
        try:
            devnull = open(os.devnull, "w")
            old_stderr = os.dup(2)
            os.dup2(devnull.fileno(), 2)
        except Exception:
            old_stderr = None

        try:
            for backend in backends:
                cap = None
                try:
                    if backend is None:
                        cap = cv2.VideoCapture(index)
                    else:
                        cap = cv2.VideoCapture(index, backend)
                except Exception:
                    continue
                if cap is None or not cap.isOpened():
                    try:
                        if cap is not None:
                            cap.release()
                    except Exception:
                        pass
                    continue
                try:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                try:
                    ret, _ = cap.read()
                except Exception:
                    ret = False
                if ret:
                    self._cap = cap
                    self._ok = True
                    print(f"[CAM] OpenCV OK index={index} backend={backend}")
                    return
                try:
                    cap.release()
                except Exception:
                    pass
        finally:
            try:
                if old_stderr is not None:
                    os.dup2(old_stderr, 2)
                    os.close(old_stderr)
            except Exception:
                pass
            try:
                if devnull is not None:
                    devnull.close()
            except Exception:
                pass

        raise RuntimeError(f"Không mở được OpenCV camera index={index}")

    def isOpened(self) -> bool:
        return self._ok and self._cap is not None and self._cap.isOpened()

    def get_backend(self) -> str:
        return "opencv"

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self.isOpened():
            return False, None
        try:
            ret, frame = self._cap.read()
            if not ret or frame is None:
                return False, None
            if self._hflip:
                frame = cv2.flip(frame, 1)
            if self._vflip:
                frame = cv2.flip(frame, 0)
            return True, frame
        except Exception:
            return False, None

    def release(self) -> None:
        self._ok = False
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        print("[CAM] OpenCV released")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_camera(
    backend: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    index: Optional[int] = None,
    camera_num: int = 0,
) -> _BaseCamera:
    """
    Mở camera theo thứ tự ưu tiên:
      auto → Picamera2 (nếu có CSI + thư viện) → OpenCV
      picamera2 → chỉ Picamera2
      opencv → chỉ OpenCV

    Raises RuntimeError nếu không mở được.
    """
    be = (backend or CAMERA_BACKEND or "auto").strip().lower()
    w = width if width is not None else CAMERA_WIDTH
    h = height if height is not None else CAMERA_HEIGHT
    idx = index if index is not None else LOCAL_CAMERA_INDEX

    errors = []

    def try_picam():
        if not _picamera2_available():
            raise RuntimeError("picamera2 chưa cài (sudo apt install python3-picamera2)")
        return Picamera2Camera(
            width=w, height=h, fps=CAMERA_FPS,
            hflip=CAMERA_HFLIP, vflip=CAMERA_VFLIP,
            rotation=CAMERA_ROTATION, camera_num=camera_num,
        )

    def try_opencv():
        return OpenCVCamera(index=idx, width=w, height=h, hflip=CAMERA_HFLIP, vflip=CAMERA_VFLIP)

    if be == "picamera2":
        return try_picam()
    if be == "opencv":
        return try_opencv()

    # auto
    if _picamera2_available():
        try:
            return try_picam()
        except Exception as e:
            errors.append(f"picamera2: {e}")
            print(f"[CAM] Picamera2 fail → fallback OpenCV: {e}")
    else:
        errors.append("picamera2: not installed")

    try:
        return try_opencv()
    except Exception as e:
        errors.append(f"opencv: {e}")

    raise RuntimeError("Không mở được camera. " + " | ".join(errors))


def camera_info() -> dict:
    """Thông tin chẩn đoán (in ra console / system health)."""
    info = {
        "backend_pref": CAMERA_BACKEND,
        "width": CAMERA_WIDTH,
        "height": CAMERA_HEIGHT,
        "fps": CAMERA_FPS,
        "hflip": CAMERA_HFLIP,
        "vflip": CAMERA_VFLIP,
        "rotation": CAMERA_ROTATION,
        "picamera2_available": _picamera2_available(),
        "opencv_available": CV2_OK,
        "csi_heuristic": _has_csi_camera(),
    }
    return info
