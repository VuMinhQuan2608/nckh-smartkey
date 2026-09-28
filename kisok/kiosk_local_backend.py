# ============================================================
# kiosk_local_backend.py
# Backend local cho Kiosk standalone (Raspberry Pi).
# - GPIO cửa (gpiozero, fallback giả lập)
# - Users + PIN / RFID / Fingerprint (users.json)
# - Face DB + nhận diện (OpenCV SFace → DeepFace → fallback)
# - MFA session, Telegram OTP + cảnh báo
# - Login log + security alerts local
# - Cảnh báo người đứng quá lâu (prolonged presence)
# - MQTT hook (optional) — remote unlock + đồng bộ log
#
# Tương thích kiosk_ui_v8.py + enroll_face_cli.py
# ============================================================

from __future__ import annotations

import json
import os
import pickle
import random
import string
import threading
import time
import uuid
import hashlib
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Optional heavy deps
# ---------------------------------------------------------------------------
try:
    import cv2
    CV2_OK = True
except Exception:
    CV2_OK = False
    cv2 = None  # type: ignore

try:
    import requests
    REQUESTS_OK = True
except Exception:
    REQUESTS_OK = False
    requests = None  # type: ignore

# GPIO
try:
    from gpiozero import OutputDevice, InputDevice
    RELAY_PIN = int(os.environ.get("RELAY_PIN", "17"))
    DOOR_SENSOR_PIN = int(os.environ.get("DOOR_SENSOR_PIN", "27"))
    # Nhiều module relay low-trigger: dat RELAY_ACTIVE_HIGH=0
    _ah = os.environ.get("RELAY_ACTIVE_HIGH", "1").strip().lower() not in (
        "0", "false", "no", "low",
    )
    relay = OutputDevice(RELAY_PIN, active_high=_ah, initial_value=False)
    try:
        door_sensor = InputDevice(DOOR_SENSOR_PIN, pull_up=True)
        DOOR_SENSOR_OK = True
    except Exception:
        door_sensor = None
        DOOR_SENSOR_OK = False
    GPIO_OK = True
    print(f"[LOCAL] GPIO OK — relay pin={RELAY_PIN} active_high={_ah} sensor={DOOR_SENSOR_OK}")
except Exception as e:
    relay = None
    door_sensor = None
    GPIO_OK = False
    DOOR_SENSOR_OK = False
    print(f"[LOCAL] GPIO GIA LAP (khong co gpiozero/Pi): {e}")
    print("[LOCAL] /unlock se OK phan mem nhung KHONG dieu khien relay that")

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
BASE_DIR = os.environ.get(
    "KIOSK_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiosk_data"),
)
os.makedirs(BASE_DIR, exist_ok=True)

USERS_PATH = os.path.join(BASE_DIR, "users.json")
FACE_DB_PATH = os.path.join(BASE_DIR, "face_database.pkl")
LOGIN_LOG_PATH = os.path.join(BASE_DIR, "login_log.json")
ALERTS_PATH = os.path.join(BASE_DIR, "security_alerts.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Face
FACE_SIMILARITY = float(os.environ.get("FACE_SIMILARITY", "0.45"))  # SFace cosine; DeepFace ~0.55
FACE_MODEL = os.environ.get("FACE_MODEL", "SFace")  # SFace | DeepFace | simple
FACE_AUTO_UNLOCK = os.environ.get("FACE_AUTO_UNLOCK", "1") not in ("0", "false", "False")
FACE_UNLOCK_COOLDOWN = float(os.environ.get("FACE_UNLOCK_COOLDOWN", "10"))
# Do chinh xac
FACE_MIN_SIZE = int(os.environ.get("FACE_MIN_SIZE", "80"))
FACE_BLUR_MIN = float(os.environ.get("FACE_BLUR_MIN", "40"))
FACE_MATCH_MARGIN = float(os.environ.get("FACE_MATCH_MARGIN", "0.05"))
FACE_TOP_K = int(os.environ.get("FACE_TOP_K", "3"))
FACE_QUALITY_CHECK = os.environ.get("FACE_QUALITY_CHECK", "1") not in ("0", "false", "False")
# Toc do (Pi)
FACE_FAST = os.environ.get("FACE_FAST", "1") not in ("0", "false", "False")
FACE_DETECT_MAX_WIDTH = int(os.environ.get("FACE_DETECT_MAX_WIDTH", "320"))
FACE_INFER_MAX_WIDTH = int(os.environ.get("FACE_INFER_MAX_WIDTH", "400"))
FACE_INFER_INTERVAL = float(os.environ.get("FACE_INFER_INTERVAL", "1.0"))
# Nhieu nguoi / chon mat
FACE_CENTER_ONLY = os.environ.get("FACE_CENTER_ONLY", "1") not in ("0", "false", "False")
FACE_CENTER_RATIO = float(os.environ.get("FACE_CENTER_RATIO", "0.55"))  # mat trong vung giua
FACE_MULTI_WARN = os.environ.get("FACE_MULTI_WARN", "1") not in ("0", "false", "False")
FACE_FRONTAL_ONLY = os.environ.get("FACE_FRONTAL_ONLY", "1") not in ("0", "false", "False")
FACE_FRONTAL_MIN_EYES = int(os.environ.get("FACE_FRONTAL_MIN_EYES", "0"))  # 0 = chi check aspect  # >=1 mat (eye) coi la nhin thang

# Door
AUTO_LOCK_ENABLED = os.environ.get("AUTO_LOCK_ENABLED", "1") not in ("0", "false", "False")
AUTO_LOCK_SECONDS = int(os.environ.get("AUTO_LOCK_SECONDS", "90"))

# PIN lockout
MAX_PIN_ATTEMPTS = int(os.environ.get("MAX_PIN_ATTEMPTS", "5"))
PIN_LOCKOUT_SECONDS = int(os.environ.get("PIN_LOCKOUT_SECONDS", "60"))

# Prolonged presence / stranger alerts (tranh spam Telegram)
# PROLONGED_ALERT_SECONDS: dung bao lau moi gui canh bao lan dau
# PROLONGED_ALERT_COOLDOWN: khoang cach toi thieu giua 2 tin (cung 1 lan dung)
# STRANGER_ALERT_COOLDOWN: cooldown canh bao "phat hien nguoi la" (neu bat instant)
# STRANGER_INSTANT_ALERT: 1 = gui ngay khi thay nguoi la; 0 = chi gui khi dung lau
PROLONGED_SECONDS = float(os.environ.get("PROLONGED_ALERT_SECONDS", "60"))
PROLONGED_COOLDOWN = float(os.environ.get("PROLONGED_ALERT_COOLDOWN", "120"))
STRANGER_ALERT_COOLDOWN = float(os.environ.get("STRANGER_ALERT_COOLDOWN", "120"))
STRANGER_INSTANT_ALERT = os.environ.get("STRANGER_INSTANT_ALERT", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Telegram (kiosk riêng)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [
    x.strip()
    for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if x.strip()
]
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS)

# MFA
MFA_SESSION_TTL = int(os.environ.get("MFA_SESSION_TTL", "180"))
OTP_TTL = int(os.environ.get("OTP_TTL", "120"))

# MQTT (optional)
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "0").strip() in ("1", "true", "True", "yes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _norm_name(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "").strip())


def _norm_fp(v) -> str:
    return "" if v is None else str(v).strip()


def _norm_rfid(v) -> str:
    return "" if v is None else str(v).strip().upper().replace(" ", "")


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(("smartkey-pin:" + pin).encode("utf-8")).hexdigest()


def _check_pin(stored_hash: str, pin: str) -> bool:
    if not stored_hash or not pin:
        return False
    # support both our sha256 and werkzeug-style if migrated
    if stored_hash.startswith("pbkdf2:") or stored_hash.startswith("scrypt:"):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored_hash, pin)
        except Exception:
            return False
    return stored_hash == _hash_pin(pin)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LOCAL] load {path} error: {e}")
        return default


def _save_json(path: str, data) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        print(f"[LOCAL] save {path} error: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _load_pickle(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        print(f"[LOCAL] pickle load error: {e}")
        return default


def _save_pickle(path: str, data) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        print(f"[LOCAL] pickle save error: {e}")


# ---------------------------------------------------------------------------
# Face engine
# ---------------------------------------------------------------------------

class FaceEngine:
    """
    Ưu tiên:
      1) OpenCV FaceRecognizerSF (SFace) — nhanh trên Pi
      2) DeepFace Facenet512 — giống web
      3) simple histogram fallback — chỉ demo
    """

    def __init__(self):
        self.mode = "none"
        self.detector = None
        self.recognizer = None
        self.cascade = None
        self.deepface_ready = False
        self._lock = threading.Lock()
        self._init_models()

    def _init_models(self):
        if not CV2_OK:
            print("[FACE] OpenCV không có — face disabled")
            return

        # Haar cascade luôn có
        self.eye_cascade = None
        try:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self.cascade = cv2.CascadeClassifier(path)
            eye_path = cv2.data.haarcascades + "haarcascade_eye.xml"
            self.eye_cascade = cv2.CascadeClassifier(eye_path)
        except Exception:
            self.cascade = None

        prefer = FACE_MODEL.lower()

        # --- SFace ---
        if prefer in ("sface", "auto", "opencv"):
            try:
                # Model files: download once if missing
                model_dir = os.path.join(BASE_DIR, "models")
                os.makedirs(model_dir, exist_ok=True)
                det_path = os.path.join(model_dir, "face_detection_yunet_2023mar.onnx")
                rec_path = os.path.join(model_dir, "face_recognizer_fast.onnx")
                # Nếu chưa có file onnx → thử FaceRecognizerSF_create với tên mặc định
                if hasattr(cv2, "FaceDetectorYN_create") and hasattr(cv2, "FaceRecognizerSF_create"):
                    # YuNet optional; fallback Haar
                    self.recognizer = None
                    # Try load SFace from opencv_zoo style path or env
                    sface = os.environ.get("SFACE_MODEL", rec_path)
                    if os.path.exists(sface):
                        self.recognizer = cv2.FaceRecognizerSF_create(sface, "")
                        self.mode = "sface"
                        print(f"[FACE] SFace loaded: {sface}")
                    else:
                        # Một số bản OpenCV đóng gói sẵn
                        for cand in (
                            "face_recognition_sface_2021dec.onnx",
                            os.path.join(model_dir, "face_recognition_sface_2021dec.onnx"),
                        ):
                            if os.path.exists(cand):
                                self.recognizer = cv2.FaceRecognizerSF_create(cand, "")
                                self.mode = "sface"
                                print(f"[FACE] SFace loaded: {cand}")
                                break
            except Exception as e:
                print(f"[FACE] SFace init fail: {e}")

        # --- DeepFace ---
        if self.mode == "none" and prefer in ("deepface", "auto", "sface"):
            try:
                from deepface import DeepFace  # noqa: F401
                self.deepface_ready = True
                self.mode = "deepface"
                print("[FACE] DeepFace available (lazy load on first call)")
            except Exception as e:
                print(f"[FACE] DeepFace not available: {e}")

        if self.mode == "none":
            self.mode = "simple"
            print("[FACE] Using simple fallback (low accuracy — chỉ demo)")

    def detect_all_faces(self, frame_bgr) -> list:
        """Tra ve list (x,y,w,h) tren frame goc."""
        if not CV2_OK or frame_bgr is None or self.cascade is None:
            return []
        h0, w0 = frame_bgr.shape[:2]
        scale = 1.0
        work = frame_bgr
        if w0 > FACE_DETECT_MAX_WIDTH:
            scale = FACE_DETECT_MAX_WIDTH / float(w0)
            work = cv2.resize(
                frame_bgr,
                (FACE_DETECT_MAX_WIDTH, max(1, int(h0 * scale))),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        if not FACE_FAST:
            try:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                gray = clahe.apply(gray)
            except Exception:
                pass
        min_sz = max(24, int((FACE_MIN_SIZE // 2) * max(scale, 0.01)))
        faces = self.cascade.detectMultiScale(
            gray,
            scaleFactor=1.15 if FACE_FAST else 1.08,
            minNeighbors=3 if FACE_FAST else 4,
            minSize=(min_sz, min_sz),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        out = []
        inv = (1.0 / scale) if scale != 1.0 else 1.0
        for (x, y, w, h) in faces:
            out.append((int(x * inv), int(y * inv), int(w * inv), int(h * inv)))
        return out

    def is_frontal(self, frame_bgr, box) -> bool:
        """Uoc luong nhin thang: ty le khung + so mat (eye cascade)."""
        if not FACE_FRONTAL_ONLY or box is None:
            return True
        x, y, w, h = box
        if h <= 0:
            return False
        aspect = w / float(h)
        # Profile thuong hep (aspect thap) hoac qua rong
        if aspect < 0.65 or aspect > 1.45:
            return False
        if self.eye_cascade is None or not CV2_OK:
            return True  # khong co eye model → chi dung aspect
        try:
            roi = frame_bgr[max(0, y):y + h, max(0, x):x + w]
            if roi.size == 0:
                return False
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            # chi quet nua tren khuon mat (vung mat)
            upper = gray[0:max(1, h * 2 // 3), :]
            eyes = self.eye_cascade.detectMultiScale(
                upper, scaleFactor=1.1, minNeighbors=3,
                minSize=(max(8, w // 10), max(8, h // 12)),
            )
            return len(eyes) >= FACE_FRONTAL_MIN_EYES
        except Exception:
            return True

    def select_face(self, frame_bgr) -> dict:
        """
        Chon 1 mat de nhan dien.
        Tra ve: {box, face_count, reason, multi}
        """
        empty = {"box": None, "face_count": 0, "reason": "no_face", "multi": False}
        if frame_bgr is None:
            return empty
        faces = self.detect_all_faces(frame_bgr)
        n = len(faces)
        if n == 0:
            return empty

        h0, w0 = frame_bgr.shape[:2]
        cx0, cy0 = w0 / 2.0, h0 / 2.0
        # Ban kinh vung giua (theo canh ngan)
        max_dist = FACE_CENTER_RATIO * min(w0, h0)

        candidates = []
        for (x, y, w, h) in faces:
            fcx, fcy = x + w / 2.0, y + h / 2.0
            dist = ((fcx - cx0) ** 2 + (fcy - cy0) ** 2) ** 0.5
            area = w * h
            frontal = self.is_frontal(frame_bgr, (x, y, w, h))
            in_center = (dist <= max_dist) if FACE_CENTER_ONLY else True
            candidates.append({
                "box": (int(x), int(y), int(w), int(h)),
                "dist": dist,
                "area": area,
                "frontal": frontal,
                "in_center": in_center,
            })

        multi = n >= 2
        # Loc: uu tien trong vung giua + nhin thang
        pool = [c for c in candidates if c["in_center"]]
        if not pool and FACE_CENTER_ONLY:
            return {
                "box": None,
                "face_count": n,
                "reason": "multi_off_center" if multi else "off_center",
                "multi": multi,
            }

        pool_f = [c for c in pool if c["frontal"]]
        if FACE_FRONTAL_ONLY and not pool_f:
            return {
                "box": None,
                "face_count": n,
                "reason": "not_frontal",
                "multi": multi,
            }
        use = pool_f if pool_f else pool

        # Nhieu mat deu hop le → canh bao, van chon mat gan giua nhat
        if multi and FACE_MULTI_WARN:
            use_sorted = sorted(use, key=lambda c: (c["dist"], -c["area"]))
            best = use_sorted[0]
            # Neu mat lon thu 2 gan bang mat tot nhat → tu choi de tranh nham
            if len(use_sorted) >= 2:
                a0, a1 = use_sorted[0]["area"], use_sorted[1]["area"]
                if a1 >= a0 * 0.75 and use_sorted[1]["dist"] < max_dist * 1.2:
                    return {
                        "box": None,
                        "face_count": n,
                        "reason": "multi_face",
                        "multi": True,
                    }
            return {
                "box": best["box"],
                "face_count": n,
                "reason": "ok_multi",
                "multi": True,
            }

        best = sorted(use, key=lambda c: (c["dist"], -c["area"]))[0]
        return {
            "box": best["box"],
            "face_count": n,
            "reason": "ok",
            "multi": multi,
        }

    def detect_largest_face(self, frame_bgr) -> Optional[Tuple[int, int, int, int]]:
        """Tuong thich cu — uu tien mat giua + frontal."""
        sel = self.select_face(frame_bgr)
        return sel.get("box")

    def _crop(self, frame, box, margin=0.25):
        x, y, w, h = box
        m = int(margin * max(w, h))
        x1 = max(0, x - m)
        y1 = max(0, y - m)
        x2 = min(frame.shape[1], x + w + m)
        y2 = min(frame.shape[0], y + h + m)
        return frame[y1:y2, x1:x2]

    def face_quality(self, frame_bgr, box=None) -> dict:
        """Danh gia chat luong mat: size + do net (blur)."""
        out = {"ok": False, "reason": "no_face", "size": 0, "blur": 0.0}
        if frame_bgr is None or not CV2_OK:
            return out
        if box is None:
            box = self.detect_largest_face(frame_bgr)
        if box is None:
            return out
        x, y, w, h = box
        out["size"] = int(min(w, h))
        if out["size"] < FACE_MIN_SIZE:
            out["reason"] = f"mat qua nho ({out['size']}px < {FACE_MIN_SIZE})"
            return out
        crop = self._crop(frame_bgr, box, margin=0.05)
        try:
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            out["blur"] = float(cv2.Laplacian(g, cv2.CV_64F).var())
        except Exception:
            out["blur"] = 999.0
        if FACE_QUALITY_CHECK and out["blur"] < FACE_BLUR_MIN:
            out["reason"] = f"anh mo (blur={out['blur']:.0f} < {FACE_BLUR_MIN})"
            return out
        out["ok"] = True
        out["reason"] = "ok"
        return out

    def _preprocess_face(self, crop_bgr) -> Optional[np.ndarray]:
        """Can sang — bo qua khi FACE_FAST=1 de tang toc."""
        if crop_bgr is None or crop_bgr.size == 0:
            return None
        if FACE_FAST:
            return crop_bgr
        img = crop_bgr
        try:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            l = clahe.apply(l)
            img = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
        except Exception:
            pass
        return img

    @staticmethod
    def _l2_normalize(v: np.ndarray) -> np.ndarray:
        v = np.asarray(v, dtype=np.float32).flatten()
        n = float(np.linalg.norm(v))
        if n < 1e-8:
            return v
        return (v / n).astype(np.float32)

    def embed(self, frame_bgr, box=None) -> Optional[np.ndarray]:
        if frame_bgr is None:
            return None
        if box is None:
            box = self.detect_largest_face(frame_bgr)
        if box is None:
            return None
        crop = self._crop(frame_bgr, box)
        if crop is None or crop.size == 0:
            return None
        crop = self._preprocess_face(crop)
        if crop is None:
            return None

        with self._lock:
            if self.mode == "sface" and self.recognizer is not None:
                try:
                    face = cv2.resize(crop, (112, 112))
                    feat = self.recognizer.feature(face)
                    return self._l2_normalize(np.asarray(feat).flatten())
                except Exception as e:
                    print(f"[FACE] SFace embed error: {e}")

            if self.mode == "deepface" or self.deepface_ready:
                try:
                    from deepface import DeepFace
                    result = DeepFace.represent(
                        img_path=crop,
                        model_name="Facenet512",
                        enforce_detection=False,
                    )
                    emb = result[0]["embedding"]
                    self.deepface_ready = True
                    return self._l2_normalize(np.asarray(emb, dtype=np.float32))
                except Exception as e:
                    print(f"[FACE] DeepFace embed error: {e}")

            try:
                small = cv2.resize(crop, (32, 32))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
                hist = cv2.calcHist([gray], [0], None, [64], [0, 256]).flatten()
                return self._l2_normalize(hist)
            except Exception:
                return None

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        a = np.asarray(a, dtype=np.float32).flatten()
        b = np.asarray(b, dtype=np.float32).flatten()
        if a.shape != b.shape:
            return -1.0
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-8 or nb < 1e-8:
            return -1.0
        return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# Main backend
# ---------------------------------------------------------------------------

class LocalBackend:
    def __init__(self):
        self._lock = threading.RLock()
        self.users: Dict[str, dict] = _load_json(USERS_PATH, {})
        self.face_db: Dict[str, List] = _load_pickle(FACE_DB_PATH, {})
        self.logs: List[dict] = _load_json(LOGIN_LOG_PATH, [])
        self.alerts: List[dict] = _load_json(ALERTS_PATH, [])
        self.config: dict = _load_json(
            CONFIG_PATH,
            {"access_mode": "mfa", "access_mode_label": "Đăng nhập 2 lần (MFA)"},
        )

        self.face = FaceEngine()
        try:
            self._rebuild_face_index()
        except Exception:
            self._face_index = {}

        # Door state
        self._locked = True
        self._last_unlock_time = 0.0
        self._auto_lock_timer: Optional[threading.Timer] = None

        # PIN lockout
        self._pin_fails = 0
        self._pin_lockout_until = 0.0

        # MFA sessions: id -> dict
        self._mfa: Dict[str, dict] = {}
        # OTP: username -> {code, exp}
        self._otp: Dict[str, dict] = {}

        # Scan status (face)
        self._scan = {"state": "idle", "name": None, "score": 0.0, "message": ""}
        self._last_face_unlock: Dict[str, float] = {}

        # Prolonged presence + last frame for Telegram photo alerts
        self._presence_since: Optional[float] = None
        self._last_prolonged_alert = 0.0
        self._last_stranger_alert = 0.0
        self._last_presence_frame = None
        self._presence_thread_started = False

        # Trang thai ESP32 / cam bien (UI poll)
        self._fp_status = {"state": "idle", "user_name": None, "fingerprint_id": None}
        self._rfid_status = {"state": "idle", "user_name": None, "rfid_uid": None}

        # Chot an toan (safety bolt): chan mo tu xa / open mode
        self._safety_bolt = bool(self.config.get("safety_bolt", False))

        # Telegram bot command polling (mo khoa qua "App"/Telegram)
        self._tg_offset = 0
        self._tg_poll_stop = threading.Event()
        if TELEGRAM_ENABLED and REQUESTS_OK:
            t = threading.Thread(target=self._telegram_poll_loop, daemon=True)
            t.start()
            print("[LOCAL] Telegram command poller started (/unlock /lock /status /bell)")
            # Huong dan mo cua qua Telegram (thay App mobile)
            try:
                self._telegram_send(
                    "📱 Smart Key — MỞ CỬA TỪ XA (Telegram App)\n"
                    "━━━━━━━━━━━━━━━━\n"
                    "Gửi lệnh trong chat với bot:\n"
                    "/unlock — Mở khóa\n"
                    "/lock — Khóa cửa\n"
                    "/status — Trạng thái\n"
                    "/bell — Chuông\n"
                    "/safety_on — Bật chốt an toàn\n"
                    "/safety_off — Tắt chốt\n"
                    "/help — Trợ giúp\n\n"
                    "Chỉ chat_id trong TELEGRAM_CHAT_IDS mới được phép."
                )
            except Exception:
                pass

        # MQTT
        self._mqtt = None
        if MQTT_ENABLED:
            self._init_mqtt()

        # Ensure GPIO starts locked
        if GPIO_OK and relay is not None:
            try:
                relay.off()
            except Exception:
                pass

        # Kiem tra chieu embedding trong DB
        _db_dims = set()
        for _embs in self.face_db.values():
            for _e in _embs:
                try:
                    _db_dims.add(int(np.asarray(_e).flatten().shape[0]))
                except Exception:
                    pass
        if _db_dims:
            print(f"[FACE] DB embedding dims: {sorted(_db_dims)} | runtime mode={self.face.mode}")
            # SFace ~128, Facenet512=512, simple hist=64
            if self.face.mode == "sface" and 512 in _db_dims and 128 not in _db_dims:
                print(
                    "[FACE] *** DB dang la 512-dim (DeepFace) nhung runtime SFace (~128). "
                    "Can enroll lai mat HOAC dat FACE_MODEL=DeepFace trong .env ***"
                )
            if self.face.mode == "deepface" and 128 in _db_dims and 512 not in _db_dims:
                print(
                    "[FACE] *** DB dang la 128-dim (SFace) nhung runtime DeepFace (512). "
                    "Can enroll lai mat HOAC dat FACE_MODEL=SFace trong .env ***"
                )

        print(
            f"[LOCAL] Backend ready | users={len(self.users)} "
            f"faces={list(self.face_db.keys())} face_mode={self.face.mode} "
            f"gpio={GPIO_OK} telegram={TELEGRAM_ENABLED} mqtt={MQTT_ENABLED}"
        )

    # ===================== persistence =====================

    def _save_users(self):
        _save_json(USERS_PATH, self.users)

    def _save_faces(self):
        _save_pickle(FACE_DB_PATH, self.face_db)

    def _save_logs(self):
        _save_json(LOGIN_LOG_PATH, self.logs[:200])

    def _save_alerts(self):
        _save_json(ALERTS_PATH, self.alerts[:200])

    def _save_config(self):
        _save_json(CONFIG_PATH, self.config)

    def _find_user(self, username: str) -> Optional[dict]:
        target = _norm_name(username)
        for k, u in self.users.items():
            if _norm_name(k) == target or _norm_name(u.get("username", "")) == target:
                return u
        return None

    def _find_user_key(self, username: str) -> Optional[str]:
        target = _norm_name(username)
        for k, u in self.users.items():
            if _norm_name(k) == target or _norm_name(u.get("username", "")) == target:
                return k
        return None

    # ===================== logging / alerts =====================

    def add_login_log(self, identifier: str, method: str, result: str, note: str = ""):
        entry = {
            "time": _now_str(),
            "identifier": identifier,
            "method": method,
            "result": result,
            "note": note,
        }
        with self._lock:
            self.logs.insert(0, entry)
            del self.logs[200:]
            self._save_logs()
        print(f"[LOG] {entry['time']} {method} {identifier} {result} {note}")
        self._mqtt_publish_log(entry)
        # Telegram notify on successful auth
        result_u = str(result).upper()
        method_l = str(method).lower()
        note_l = str(note or "").lower()
        if note_l in ("mở khóa", "khóa cửa") or "tự khóa" in note_l:
            return
        skip_methods = ("auto-lock", "settings", "manual", "remote")
        if (
            ("THÀNH" in result_u or "SUCCESS" in result_u or "GRANTED" in result_u)
            and method_l not in skip_methods
            and "THAY ĐỔI" not in result_u
            and "KHÓA TẠM" not in result_u
        ):
            if method_l in (
                "pin", "rfid", "fingerprint", "face", "mfa",
                "mfa/pin", "mfa/rfid", "mfa/fingerprint", "mfa/face", "mfa/telegram_otp",
            ) or method_l.startswith("mfa"):
                self._telegram_notify_login_success(identifier, method, note)

    def add_security_alert(
        self,
        alert_type: str,
        severity: str,
        message: str,
        frame_bgr=None,
        notify_telegram: bool = True,
    ):
        entry = {
            "id": int(time.time() * 1000) % 10_000_000,
            "time": _now_str(),
            "type": alert_type,
            "severity": severity,
            "message": message,
            "status": "new",
        }
        with self._lock:
            self.alerts.insert(0, entry)
            del self.alerts[200:]
            self._save_alerts()
        print(f"[ALERT] {severity} {alert_type}: {message}")
        if notify_telegram:
            text_msg = f"⚠ [{severity.upper()}] {alert_type}\n{message}\n{_now_str()}"
            if frame_bgr is not None and CV2_OK:
                self._telegram_send_photo(frame_bgr, caption=text_msg)
            else:
                self._telegram_send(text_msg)
        self._mqtt_publish_alert(entry)
        return entry

    def login_log(self) -> dict:
        with self._lock:
            return {"logs": list(self.logs[:100])}

    def security_alerts(self) -> dict:
        with self._lock:
            return {"alerts": list(self.alerts[:50]), "new_count": sum(1 for a in self.alerts if a.get("status") == "new")}

    # ===================== Telegram =====================

    def _telegram_send(self, text: str) -> Tuple[bool, str]:
        if not TELEGRAM_ENABLED or not REQUESTS_OK:
            return False, "Telegram chua cau hinh (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS)"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        ok_any = False
        last_err = ""
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=8)
                if r.status_code == 200:
                    ok_any = True
                else:
                    last_err = r.text
            except Exception as e:
                last_err = str(e)
        return ok_any, "OK" if ok_any else last_err

    def _telegram_send_photo(self, frame_bgr, caption: str = "") -> Tuple[bool, str]:
        # Gui anh JPEG + caption
        if not TELEGRAM_ENABLED or not REQUESTS_OK or not CV2_OK:
            return False, "Telegram/OpenCV chua san sang"
        if frame_bgr is None:
            return self._telegram_send(caption or "(khong co anh)")
        try:
            h, w = frame_bgr.shape[:2]
            frame = frame_bgr
            if w > 640:
                scale = 640.0 / w
                frame = cv2.resize(frame_bgr, (640, int(h * scale)))
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                return self._telegram_send(caption or "(encode anh loi)")
            photo_bytes = buf.tobytes()
        except Exception as e:
            return False, str(e)

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        ok_any = False
        last_err = ""
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                files = {"photo": ("capture.jpg", photo_bytes, "image/jpeg")}
                data = {"chat_id": chat_id, "caption": (caption or "")[:1024]}
                r = requests.post(url, data=data, files=files, timeout=20)
                if r.status_code == 200 and (r.json() or {}).get("ok"):
                    ok_any = True
                else:
                    last_err = (r.text or "")[:200]
            except Exception as e:
                last_err = str(e)
        return ok_any, "OK" if ok_any else last_err

    def _telegram_notify_login_success(self, username: str, method: str, note: str = ""):
        method_labels = {
            "pin": "PIN",
            "rfid": "RFID",
            "fingerprint": "Van tay",
            "face": "Khuon mat",
            "mfa": "MFA (2 lop)",
            "mfa/pin": "MFA - PIN",
            "mfa/rfid": "MFA - RFID",
            "mfa/fingerprint": "MFA - Van tay",
            "mfa/face": "MFA - Khuon mat",
            "mfa/telegram_otp": "MFA - Telegram OTP",
        }
        label = method_labels.get(str(method).lower(), str(method))
        text = (
            f"✅ ĐĂNG NHẬP THÀNH CÔNG\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👤 User: {username or '?'}\n"
            f"🔑 Phương thức: {label}\n"
            + (f"📝 {note}\n" if note else "")
            + f"🕐 {_now_str()}\n"
            f"📍 Smart Key Kiosk — Cửa đã mở"
        )
        ok, msg = self._telegram_send(text)
        if ok:
            print(f"[TG] Da gui thong bao dang nhap: {username} / {method}")
        else:
            print(f"[TG] Gui thong bao that bai: {msg}")
        return ok

    def telegram_test(self) -> dict:
        ok, msg = self._telegram_send(f"Smart Key Kiosk — test {_now_str()}")
        return {"ok": ok, "message": msg}

    def _telegram_otp_send(self, chat_id: str, code: str, username: str) -> bool:
        if not TELEGRAM_BOT_TOKEN or not REQUESTS_OK:
            return False
        text = f"🔐 Smart Key OTP cho {username}:\n\n{code}\n\nHết hạn sau {OTP_TTL}s."
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=8,
            )
            return r.status_code == 200
        except Exception:
            return False

    # ===================== Door =====================

    def door_status(self) -> dict:
        with self._lock:
            locked = self._locked
            last = self._last_unlock_time
        door = "unknown"
        if DOOR_SENSOR_OK and door_sensor is not None:
            try:
                door = "closed" if door_sensor.is_active else "open"
            except Exception:
                door = "unknown"
        remaining = 0
        if not locked and AUTO_LOCK_ENABLED and last > 0:
            remaining = max(0, int(AUTO_LOCK_SECONDS - (time.time() - last)))
        return {
            "locked": locked,
            "door": door,
            "auto_lock_enabled": AUTO_LOCK_ENABLED,
            "auto_lock_remaining": remaining,
            "online": True,
            "source": "kiosk",
        }

    def _cancel_auto_lock(self):
        if self._auto_lock_timer is not None:
            try:
                self._auto_lock_timer.cancel()
            except Exception:
                pass
            self._auto_lock_timer = None

    def _schedule_auto_lock(self):
        self._cancel_auto_lock()
        if not AUTO_LOCK_ENABLED:
            return

        def _do():
            with self._lock:
                if not self._locked:
                    self._locked = True
                    if GPIO_OK and relay is not None:
                        try:
                            relay.off()
                        except Exception:
                            pass
            self.add_login_log("Hệ thống", "auto-lock", "THÀNH CÔNG", f"Tự khóa sau {AUTO_LOCK_SECONDS}s")
            self._mqtt_publish_door()

        t = threading.Timer(AUTO_LOCK_SECONDS, _do)
        t.daemon = True
        t.start()
        self._auto_lock_timer = t

    def unlock(self, by: str = "kiosk", method: str = "manual") -> dict:
        method_l = (method or "").lower()
        # Chot an toan: chan mo tu xa (telegram/mqtt/remote/app)
        if self._safety_bolt and method_l in (
            "telegram", "mqtt", "remote", "app", "bot",
        ):
            self.add_login_log(by, method, "TỪ CHỐI", "Chốt an toàn đang bật")
            return {
                "ok": False, "success": False,
                "message": "Chốt an toàn đang BẬT — chỉ mở tại kiosk",
                "safety_bolt": True,
            }
        with self._lock:
            self._locked = False
            self._last_unlock_time = time.time()
        gpio_real = False
        if GPIO_OK and relay is not None:
            try:
                relay.on()
                gpio_real = True
                print(f"[GPIO] relay ON (unlock) by={by} method={method}")
            except Exception as e:
                return {"ok": False, "success": False, "message": f"GPIO error: {e}"}
        else:
            print(f"[GPIO] GIA LAP unlock by={by} — khong co relay that")
        self._schedule_auto_lock()
        self.add_login_log(by, method, "THÀNH CÔNG", "Mở khóa" + ("" if gpio_real else " [gia lap]"))
        self._mqtt_publish_door()
        out = {"ok": True, "success": True, "gpio_real": gpio_real, **self.door_status()}
        if not gpio_real:
            out["message"] = (
                "Phần mềm đã mở khóa nhưng GPIO/relay chưa kết nối "
                "(chạy giả lập hoặc thiếu gpiozero). Kiểm tra Pi + relay pin."
            )
        else:
            out["message"] = "Đã mở khóa (relay ON)"
        return out

    def lock(self, by: str = "kiosk", method: str = "manual") -> dict:
        with self._lock:
            self._locked = True
        self._cancel_auto_lock()
        if GPIO_OK and relay is not None:
            try:
                relay.off()
            except Exception as e:
                return {"ok": False, "success": False, "message": f"GPIO error: {e}"}
        self.add_login_log(by, method, "THÀNH CÔNG", "Khóa cửa")
        self._mqtt_publish_door()
        return {"ok": True, "success": True, **self.door_status()}

    def door_control(self, action: str) -> dict:
        action = (action or "").lower().strip()
        if action == "unlock":
            return self.unlock()
        if action == "lock":
            return self.lock()
        if action in ("safety_on", "safety_bolt_on"):
            return self.set_safety_bolt(True)
        if action in ("safety_off", "safety_bolt_off"):
            return self.set_safety_bolt(False)
        return {"ok": False, "success": False, "message": "action phải là unlock/lock/safety_on/safety_off"}

    # ===================== Chuong / Chot an toan / Pin / Telegram App =====================

    def doorbell(self, by: str = "kiosk") -> dict:
        """Chuong cua — gui Telegram ngay."""
        msg = (
            f"🔔 CHUÔNG CỬA\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Có người bấm chuông tại kiosk\n"
            f"👤 Nguồn: {by}\n"
            f"🕐 {_now_str()}\n"
            f"📍 Smart Key Kiosk"
        )
        ok, detail = self._telegram_send(msg)
        self.add_security_alert(
            "Chuông cửa", "notice",
            f"Có người bấm chuông ({by})",
            notify_telegram=False,
        )
        self.add_login_log(by, "doorbell", "THÀNH CÔNG", "Bấm chuông")
        return {"ok": ok, "success": ok, "message": "Đã gửi chuông" if ok else f"Gửi thất bại: {detail}"}

    def set_safety_bolt(self, enabled: bool, by: str = "kiosk") -> dict:
        """Chot an toan: chan mo tu xa khi bat."""
        self._safety_bolt = bool(enabled)
        self.config["safety_bolt"] = self._safety_bolt
        self._save_config()
        state = "BẬT" if self._safety_bolt else "TẮT"
        self.add_login_log(by, "safety_bolt", "THÀNH CÔNG", f"Chốt an toàn {state}")
        self._telegram_send(
            f"🔒 CHỐT AN TOÀN: {state}\n"
            f"👤 Bởi: {by}\n"
            f"🕐 {_now_str()}\n"
            f"{'Chỉ mở khóa tại kiosk' if self._safety_bolt else 'Đã cho phép mở từ xa (Telegram/App)'}"
        )
        return {
            "ok": True, "success": True,
            "safety_bolt": self._safety_bolt,
            "message": f"Chốt an toàn đã {state}",
        }

    def get_safety_bolt(self) -> dict:
        return {"ok": True, "safety_bolt": bool(self._safety_bolt)}

    def system_health(self) -> dict:
        """Bao pin yeu / suc khoe he thong (Pi)."""
        info = {
            "ok": True,
            "power": "AC/unknown",
            "battery_percent": None,
            "battery_low": False,
            "cpu_temp_c": None,
            "disk_free_mb": None,
            "uptime_s": None,
            "safety_bolt": bool(self._safety_bolt),
            "door": self.door_status(),
        }
        # CPU temp (Pi)
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                info["cpu_temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
        except Exception:
            pass
        # Disk
        try:
            st = os.statvfs(BASE_DIR)
            info["disk_free_mb"] = int((st.f_bavail * st.f_frsize) / (1024 * 1024))
        except Exception:
            pass
        # Uptime
        try:
            with open("/proc/uptime") as f:
                info["uptime_s"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        # Optional battery (UPS HAT / capacity file)
        for cap in (
            "/sys/class/power_supply/BAT0/capacity",
            "/sys/class/power_supply/battery/capacity",
            os.environ.get("BATTERY_CAPACITY_PATH", ""),
        ):
            if not cap:
                continue
            try:
                with open(cap) as f:
                    pct = int(f.read().strip())
                info["battery_percent"] = pct
                info["power"] = "battery"
                thr = int(os.environ.get("BATTERY_LOW_PERCENT", "20"))
                info["battery_low"] = pct <= thr
                break
            except Exception:
                continue
        return info

    def check_battery_alert(self) -> dict:
        """Gui Telegram neu pin yeu (goi dinh ky)."""
        h = self.system_health()
        if not h.get("battery_low"):
            return {"ok": True, "alerted": False, **h}
        # cooldown 30 phut
        now = time.time()
        last = getattr(self, "_last_battery_alert", 0.0)
        if now - last < 1800:
            return {"ok": True, "alerted": False, "cooldown": True, **h}
        self._last_battery_alert = now
        pct = h.get("battery_percent")
        self.add_security_alert(
            "Pin yếu",
            "warning",
            f"Pin còn khoảng {pct}% — sạc sớm để tránh mất nguồn kiosk",
            notify_telegram=True,
        )
        return {"ok": True, "alerted": True, **h}

    def _telegram_poll_loop(self):
        """Lang nghe lenh Telegram: /unlock /lock /status /bell /safety_on /safety_off /help"""
        if not TELEGRAM_ENABLED or not REQUESTS_OK:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        allowed = set(str(x) for x in TELEGRAM_CHAT_IDS)
        while not self._tg_poll_stop.is_set():
            try:
                r = requests.get(
                    url,
                    params={"offset": self._tg_offset, "timeout": 25},
                    timeout=35,
                )
                data = r.json() if r.content else {}
                for upd in data.get("result") or []:
                    self._tg_offset = max(self._tg_offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    chat_id = str(chat.get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text or chat_id not in allowed:
                        continue
                    self._handle_telegram_command(chat_id, text)
            except Exception as e:
                print(f"[TG-POLL] {e}")
                time.sleep(5)

    def _handle_telegram_command(self, chat_id: str, text: str):
        cmd = text.split()[0].lower().split("@")[0]
        reply = ""
        if cmd in ("/start", "/help"):
            reply = (
                "Smart Key Bot\n"
                "/unlock — Mở khóa từ xa\n"
                "/lock — Khóa cửa\n"
                "/status — Trạng thái cửa + hệ thống\n"
                "/bell — Mô phỏng chuông\n"
                "/safety_on — Bật chốt an toàn\n"
                "/safety_off — Tắt chốt an toàn\n"
                "/help — Trợ giúp"
            )
        elif cmd == "/unlock":
            r = self.unlock(by=f"tg:{chat_id}", method="telegram")
            if r.get("ok"):
                if r.get("gpio_real"):
                    reply = "✅ Đã mở khóa (relay ON — cửa vật lý)"
                else:
                    reply = (
                        "⚠️ Phần mềm OK nhưng CHƯA có relay GPIO\n"
                        "Chạy trên máy không có gpiozero/Pi,\n"
                        "hoặc RELAY_PIN sai / relay chưa cấp nguồn.\n"
                        "Xem log kiosk: [GPIO] GIA LAP"
                    )
            else:
                reply = f"❌ {r.get('message')}"
        elif cmd == "/lock":
            r = self.lock(by=f"tg:{chat_id}", method="telegram")
            reply = ("🔒 Đã khóa" if r.get("ok") else f"❌ {r.get('message')}")
        elif cmd == "/status":
            h = self.system_health()
            d = h.get("door") or {}
            reply = (
                f"📊 STATUS\n"
                f"Cửa khóa: {'CÓ' if d.get('locked') else 'KHÔNG'}\n"
                f"Cảm biến: {d.get('door')}\n"
                f"GPIO relay: {'CÓ' if GPIO_OK else 'KHÔNG (giả lập)'}\n"
                f"Chốt an toàn: {'BẬT' if h.get('safety_bolt') else 'TẮT'}\n"
                f"Pin: {h.get('battery_percent') if h.get('battery_percent') is not None else 'N/A'}\n"
                f"CPU: {h.get('cpu_temp_c')}°C\n"
                f"Đĩa trống: {h.get('disk_free_mb')} MB"
            )
        elif cmd == "/bell":
            r = self.doorbell(by=f"tg:{chat_id}")
            reply = r.get("message", "OK")
        elif cmd == "/safety_on":
            r = self.set_safety_bolt(True, by=f"tg:{chat_id}")
            reply = r.get("message", "OK")
        elif cmd == "/safety_off":
            r = self.set_safety_bolt(False, by=f"tg:{chat_id}")
            reply = r.get("message", "OK")
        else:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": reply.replace("\\n", "\n")},
                timeout=8,
            )
        except Exception as e:
            print(f"[TG-CMD] reply fail: {e}")

    # ===================== Access mode =====================

    def get_access_mode(self) -> dict:
        mode = self.config.get("access_mode", "mfa")
        labels = {
            "single": "Đăng nhập 1 lần",
            "mfa": "Đăng nhập 2 lần (MFA)",
            "open": "Luôn mở (không cần xác thực)",
        }
        return {
            "ok": True,
            "mode": mode,
            "label": self.config.get("access_mode_label") or labels.get(mode, mode),
        }

    def set_access_mode(self, mode: str, changed_by: str = "kiosk") -> dict:
        mode = (mode or "").lower().strip()
        if mode not in ("single", "mfa", "open"):
            return {"ok": False, "message": "mode phải là single|mfa|open"}
        labels = {
            "single": "Đăng nhập 1 lần",
            "mfa": "Đăng nhập 2 lần (MFA)",
            "open": "Luôn mở (không cần xác thực)",
        }
        self.config["access_mode"] = mode
        self.config["access_mode_label"] = labels[mode]
        self._save_config()
        self.add_login_log(changed_by, "settings", "THÀNH CÔNG", f"Đổi access_mode → {mode}")
        # Thong bao Telegram khi doi che do truy cap
        tg = (
            f"⚙️ ĐỔI CHẾ ĐỘ TRUY CẬP\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📌 Mode mới: {labels[mode]} ({mode})\n"
            f"👤 Bởi: {changed_by or 'kiosk'}\n"
            f"🕐 {_now_str()}\n"
            f"📍 Smart Key Kiosk"
        )
        self._telegram_send(tg)
        return self.get_access_mode()

    # ===================== Users / credentials =====================

    def set_user_credentials(
        self,
        name: str,
        pin: Optional[str] = None,
        fingerprint_id: Optional[str] = None,
        rfid_uid: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        status: str = "approved",
    ) -> dict:
        name = _norm_name(name)
        if not name:
            return {"ok": False, "message": "Thiếu tên user"}
        with self._lock:
            u = self.users.get(name) or {
                "username": name,
                "status": status,
                "role": "member",
                "created_at": _now_str(),
            }
            if pin:
                if not pin.isdigit() or not (4 <= len(pin) <= 8):
                    return {"ok": False, "message": "PIN phải 4-8 chữ số"}
                u["pin_hash"] = _hash_pin(pin)
            if fingerprint_id is not None:
                u["fingerprint_id"] = _norm_fp(fingerprint_id)
            if rfid_uid is not None:
                u["rfid_uid"] = _norm_rfid(rfid_uid)
            if telegram_chat_id is not None:
                u["telegram_chat_id"] = str(telegram_chat_id).strip()
            u["status"] = status
            self.users[name] = u
            self._save_users()
        return {"ok": True, "user": name, "message": "Đã lưu credentials"}

    # ===================== Auth: PIN / RFID / FP =====================

    def pin_login(self, pin: str) -> dict:
        pin = (pin or "").strip()
        if not pin:
            return {"success": False, "ok": False, "message": "Vui lòng nhập PIN"}

        with self._lock:
            if time.time() < self._pin_lockout_until:
                rem = int(self._pin_lockout_until - time.time())
                return {
                    "success": False, "ok": False, "locked_out": True,
                    "remaining_seconds": rem,
                    "message": f"Khóa tạm — chờ {rem}s",
                }

        for u in self.users.values():
            if u.get("status") != "approved":
                continue
            if u.get("pin_hash") and _check_pin(u["pin_hash"], pin):
                with self._lock:
                    self._pin_fails = 0
                    self._pin_lockout_until = 0
                self.add_login_log(u["username"], "pin", "THÀNH CÔNG", "PIN kiosk")
                self.unlock(by=u["username"], method="pin")
                return {
                    "success": True, "ok": True,
                    "user_name": u["username"],
                    "role": u.get("role", "member"),
                }

        with self._lock:
            self._pin_fails += 1
            left = MAX_PIN_ATTEMPTS - self._pin_fails
            if self._pin_fails >= MAX_PIN_ATTEMPTS:
                self._pin_lockout_until = time.time() + PIN_LOCKOUT_SECONDS
                self._pin_fails = 0
                self.add_login_log("?", "pin", "KHÓA TẠM", f"Sai {MAX_PIN_ATTEMPTS} lần")
                self.add_security_alert(
                    "Dò mã PIN", "warning",
                    f"Sai PIN {MAX_PIN_ATTEMPTS} lần — khóa {PIN_LOCKOUT_SECONDS}s",
                )
                return {
                    "success": False, "ok": False, "locked_out": True,
                    "remaining_seconds": PIN_LOCKOUT_SECONDS,
                    "message": f"Sai PIN quá nhiều. Khóa {PIN_LOCKOUT_SECONDS}s",
                }
        self.add_login_log("?", "pin", "THẤT BẠI", f"Còn {left} lần")
        return {
            "success": False, "ok": False,
            "message": f"Sai PIN. Còn {left} lần thử.",
            "attempts_left": left,
        }

    def rfid_auth(self, rfid_uid: str) -> dict:
        uid = _norm_rfid(rfid_uid)
        if not uid:
            return {"success": False, "ok": False, "message": "Thiếu UID"}
        for u in self.users.values():
            if u.get("status") != "approved":
                continue
            if _norm_rfid(u.get("rfid_uid")) == uid:
                self.add_login_log(u["username"], "rfid", "THÀNH CÔNG", "RFID ESP32/kiosk")
                self.unlock(by=u["username"], method="rfid")
                self._rfid_status = {
                    "state": "recognized",
                    "user_name": u["username"],
                    "rfid_uid": uid,
                }
                return {
                    "success": True, "ok": True,
                    "user_name": u["username"],
                    "role": u.get("role", "member"),
                }
        self.add_login_log("?", "rfid", "THẤT BẠI", f"UID {uid}")
        self._rfid_status = {"state": "failed", "user_name": None, "rfid_uid": uid}
        return {"success": False, "ok": False, "message": "Thẻ không hợp lệ"}

    def fingerprint_auth(self, fingerprint_id: str) -> dict:
        fid = _norm_fp(fingerprint_id)
        if not fid:
            return {"success": False, "ok": False, "message": "Thiếu mã vân tay"}
        for u in self.users.values():
            if u.get("status") != "approved":
                continue
            if _norm_fp(u.get("fingerprint_id")) == fid:
                self.add_login_log(u["username"], "fingerprint", "THÀNH CÔNG", "FP ESP32/kiosk")
                self.unlock(by=u["username"], method="fingerprint")
                self._fp_status = {
                    "state": "recognized",
                    "user_name": u["username"],
                    "fingerprint_id": fid,
                }
                return {
                    "success": True, "ok": True,
                    "user_name": u["username"],
                    "role": u.get("role", "member"),
                }
        self.add_login_log("?", "fingerprint", "THẤT BẠI", f"ID {fid}")
        self._fp_status = {"state": "failed", "user_name": None, "fingerprint_id": fid}
        return {"success": False, "ok": False, "message": "Vân tay không hợp lệ"}

    def fingerprint_status(self) -> dict:
        return dict(self._fp_status)

    def rfid_status(self) -> dict:
        return dict(self._rfid_status)

    def fire_alert(self, source: str = "esp32", sensor: str = "mq2", value=None) -> dict:
        """Canh bao chay / khoi tu ESP32 — gui Telegram ngay."""
        extra = f" (gia tri={value})" if value is not None else ""
        msg = (
            f"🔥 CẢNH BÁO CHÁY / KHÓI\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Nguồn: {source}\n"
            f"Cảm biến: {sensor}{extra}\n"
            f"🕐 {_now_str()}\n"
            f"📍 Smart Key Kiosk — kiểm tra ngay!"
        )
        self.add_security_alert(
            "Báo cháy",
            "critical",
            f"Phát hiện cháy/khói từ {source} ({sensor}){extra}",
            notify_telegram=True,
        )
        # force telegram even if filter
        self._telegram_send(msg)
        self.add_login_log(source, "fire_sensor", "CẢNH BÁO", f"{sensor}{extra}")
        return {"ok": True, "success": True, "message": "Đã gửi cảnh báo cháy"}

    def esp32_event(self, kind: str, payload: dict = None) -> dict:
        """Dieu huong su kien tu ESP32: rfid | fingerprint | fire | doorbell."""
        payload = payload or {}
        kind = (kind or "").lower().strip()
        if kind in ("rfid", "rfid_auth"):
            return self.rfid_auth(payload.get("rfid_uid") or payload.get("uid") or "")
        if kind in ("fingerprint", "fp", "fingerprint_auth"):
            return self.fingerprint_auth(
                str(payload.get("fingerprint_id") or payload.get("id") or "")
            )
        if kind in ("fire", "smoke", "mq2", "gas"):
            return self.fire_alert(
                source=payload.get("source") or "esp32",
                sensor=payload.get("sensor") or kind,
                value=payload.get("value"),
            )
        if kind in ("bell", "doorbell"):
            return self.doorbell(by=payload.get("by") or "esp32")
        return {"ok": False, "message": f"kind không hỗ trợ: {kind}"}


    def change_pin(self, username: str, old_pin: str, new_pin: str) -> dict:
        u = self._find_user(username)
        if not u:
            return {"ok": False, "message": "Không tìm thấy user"}
        if not u.get("pin_hash"):
            return {"ok": False, "message": "Chưa đặt PIN — dùng enroll CLI"}
        if not _check_pin(u["pin_hash"], old_pin or ""):
            return {"ok": False, "message": "PIN cũ sai"}
        new_pin = (new_pin or "").strip()
        if not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
            return {"ok": False, "message": "PIN mới phải 4-8 chữ số"}
        key = self._find_user_key(username)
        with self._lock:
            self.users[key]["pin_hash"] = _hash_pin(new_pin)
            self._save_users()
        self.add_login_log(username, "pin", "THAY ĐỔI", "Đổi PIN")
        return {"ok": True, "message": "Đổi PIN thành công"}

    # ===================== Face =====================

    def face_list(self) -> dict:
        faces = {name: len(embs) for name, embs in self.face_db.items()}
        return {
            "faces": faces,
            "deepface_ready": self.face.deepface_ready or self.face.mode in ("sface", "deepface"),
            "mode": self.face.mode,
            "ok": True,
        }

    def _save_faces(self):
        """Luu pickle + metadata JSON (de kiem tra / backup)."""
        _save_pickle(FACE_DB_PATH, self.face_db)
        try:
            self._rebuild_face_index()
        except Exception:
            pass
        meta = {
            "model_runtime": getattr(self.face, "mode", "unknown"),
            "updated_at": _now_str(),
            "people": {},
        }
        for name, embs in self.face_db.items():
            dims = []
            for e in embs:
                try:
                    dims.append(int(np.asarray(e).flatten().shape[0]))
                except Exception:
                    dims.append(-1)
            meta["people"][name] = {
                "samples": len(embs),
                "dims": dims,
                "dim_unique": sorted(set(dims)),
            }
        meta_path = os.path.join(BASE_DIR, "face_database_meta.json")
        _save_json(meta_path, meta)


    def _rebuild_face_index(self):
        """Cache embedding L2-norm — so khop chi can dot product."""
        idx = {}
        mode_dim = {"sface": 128, "deepface": 512, "simple": 64}.get(self.face.mode)
        for name, embs in (self.face_db or {}).items():
            vecs = []
            for e in embs:
                try:
                    v = np.asarray(e, dtype=np.float32).flatten()
                    if mode_dim and v.shape[0] != mode_dim:
                        continue
                    n = float(np.linalg.norm(v))
                    if n > 1e-8:
                        v = (v / n).astype(np.float32)
                    vecs.append(v)
                except Exception:
                    continue
            if vecs:
                idx[name] = vecs
        self._face_index = idx

    def face_db_info(self) -> dict:

        """Thong tin database khuon mat — debug khi quet khong duoc."""
        people = {}
        all_dims = set()
        for name, embs in self.face_db.items():
            dims = []
            for e in embs:
                try:
                    d = int(np.asarray(e).flatten().shape[0])
                    dims.append(d)
                    all_dims.add(d)
                except Exception:
                    dims.append(-1)
            people[name] = {"samples": len(embs), "dims": dims}
        runtime_dim = {
            "sface": 128,
            "deepface": 512,
            "simple": 64,
        }.get(self.face.mode, None)
        compatible = True
        if runtime_dim and all_dims and runtime_dim not in all_dims:
            compatible = False
        return {
            "ok": True,
            "path": FACE_DB_PATH,
            "meta_path": os.path.join(BASE_DIR, "face_database_meta.json"),
            "face_mode": self.face.mode,
            "similarity_threshold": FACE_SIMILARITY,
            "people": people,
            "count_people": len(people),
            "embedding_dims_in_db": sorted(all_dims),
            "runtime_expected_dim": runtime_dim,
            "compatible": compatible,
            "hint": (
                None if compatible else
                f"DB dim {sorted(all_dims)} != model {self.face.mode} (dim~{runtime_dim}). "
                f"Hay xoa DB cu va enroll lai, hoac doi FACE_MODEL trong .env cho khop."
            ),
        }

    def delete_face(self, name: str) -> dict:
        name = _norm_name(name)
        with self._lock:
            if name not in self.face_db:
                return {"ok": False, "message": f"Không có mặt: {name}"}
            del self.face_db[name]
            self._save_faces()
        return {"ok": True, "message": f"Đã xóa khuôn mặt {name}"}

    def clear_face_db(self) -> dict:
        with self._lock:
            n = len(self.face_db)
            self.face_db = {}
            self._save_faces()
        return {"ok": True, "message": f"Đã xóa toàn bộ face DB ({n} người)"}

    def purge_incompatible_faces(self) -> dict:
        """Xoa mau embedding khong khop so chieu model hien tai."""
        expected = {"sface": 128, "deepface": 512, "simple": 64}.get(self.face.mode)
        if not expected:
            return {"ok": False, "message": f"Không xác định dim cho mode={self.face.mode}"}
        removed = 0
        with self._lock:
            new_db = {}
            for name, embs in self.face_db.items():
                keep = []
                for e in embs:
                    try:
                        if int(np.asarray(e).flatten().shape[0]) == expected:
                            keep.append(e)
                        else:
                            removed += 1
                    except Exception:
                        removed += 1
                if keep:
                    new_db[name] = keep
            self.face_db = new_db
            self._save_faces()
        return {
            "ok": True,
            "removed_samples": removed,
            "people_left": list(self.face_db.keys()),
            "expected_dim": expected,
        }

    def register_face(self, name: str, frame_bgr) -> dict:
        name = _norm_name(name)
        if not name:
            return {"ok": False, "message": "Thiếu tên"}
        if frame_bgr is None:
            return {"ok": False, "message": "Thiếu frame"}
        if self.face.mode in ("none",):
            return {
                "ok": False,
                "message": "Face engine chưa sẵn sàng (thiếu OpenCV/model). Cài opencv-python hoặc deepface.",
            }
        box = self.face.detect_largest_face(frame_bgr)
        q = self.face.face_quality(frame_bgr, box=box)
        if not q.get("ok"):
            return {
                "ok": False,
                "message": f"Chất lượng chưa đủ: {q.get('reason')} — lại gần, đủ sáng, đứng yên",
                "quality": q,
            }
        emb = self.face.embed(frame_bgr, box=box)
        if emb is None:
            return {"ok": False, "message": "Không thấy khuôn mặt rõ — lại gần camera, đủ sáng"}
        emb = np.asarray(emb, dtype=np.float32).flatten()
        with self._lock:
            if name not in self.face_db:
                self.face_db[name] = []
            # bo mau cu khac dim
            same = []
            for e in self.face_db[name]:
                try:
                    if int(np.asarray(e).flatten().shape[0]) == int(emb.shape[0]):
                        same.append(e)
                except Exception:
                    pass
            same.append(emb.tolist())
            self.face_db[name] = same[-15:]  # toi da 15 mau / nguoi
            self._save_faces()
            if name not in self.users:
                self.users[name] = {
                    "username": name,
                    "status": "approved",
                    "role": "member",
                    "created_at": _now_str(),
                }
                self._save_users()
        info = self.face_db_info()
        return {
            "ok": True,
            "count": len(self.face_db[name]),
            "name": name,
            "dim": int(emb.shape[0]),
            "face_mode": self.face.mode,
            "message": f"Đã lưu mẫu mặt #{len(self.face_db[name])} cho {name} (dim={emb.shape[0]}, model={self.face.mode})",
            "db_compatible": info.get("compatible"),
        }

    def face_recognize(self, frame_bgr, unlock: bool = True) -> dict:
        """
        Trả về format kiosk_ui mong đợi:
          state: idle|scanning|recognized|stranger|error
          name, score, message, ms
        """
        t0 = time.time()
        if frame_bgr is None:
            self._scan = {"state": "scanning", "name": None, "score": 0, "message": "Chờ camera…"}
            return {**self._scan, "ms": 0}

        fr = frame_bgr
        try:
            h, w = fr.shape[:2]
            if w > FACE_INFER_MAX_WIDTH:
                sc = FACE_INFER_MAX_WIDTH / float(w)
                fr = cv2.resize(
                    fr, (FACE_INFER_MAX_WIDTH, max(1, int(h * sc))),
                    interpolation=cv2.INTER_AREA,
                )
        except Exception:
            fr = frame_bgr

        sel = self.face.select_face(fr)
        box = sel.get("box")
        reason = sel.get("reason") or ""
        nfaces = int(sel.get("face_count") or 0)

        if box is None:
            self._on_presence(nfaces > 0)
            msg = "Không thấy mặt"
            if reason == "multi_face":
                msg = "Nhiều khuôn mặt — chỉ đứng một người"
            elif reason in ("off_center", "multi_off_center"):
                msg = "Đưa mặt vào giữa khung hình"
            elif reason == "not_frontal":
                msg = "Nhìn thẳng vào camera"
            self._scan = {
                "state": "scanning", "name": None, "score": 0,
                "message": msg, "face_count": nfaces,
            }
            return {**self._scan, "ms": int((time.time() - t0) * 1000)}

        x, y, bw, bh = box
        min_need = max(40, FACE_MIN_SIZE * FACE_INFER_MAX_WIDTH // 640)
        if min(bw, bh) < min_need:
            self._scan = {
                "state": "scanning", "name": None, "score": 0,
                "message": "Lại gần camera",
            }
            return {**self._scan, "ms": int((time.time() - t0) * 1000)}

        emb = self.face.embed(fr, box=box)

        if emb is None:
            self._on_presence(True, frame_bgr=frame_bgr)
            self._scan = {"state": "scanning", "name": None, "score": 0, "message": "Không trích xuất được đặc trưng"}
            return {**self._scan, "ms": int((time.time() - t0) * 1000)}

        emb = FaceEngine._l2_normalize(emb)
        emb_dim = int(emb.shape[0])
        dim_skipped = 0
        person_scores = {}
        with self._lock:
            index = getattr(self, "_face_index", None) or {}
            raw_items = None if index else list(self.face_db.items())
        if raw_items is not None:
            for name, embs in raw_items:
                scores = []
                for db_e in embs:
                    db_arr = np.asarray(db_e, dtype=np.float32).flatten()
                    if db_arr.shape[0] != emb_dim:
                        dim_skipped += 1
                        continue
                    sc = self.face.cosine(emb, db_arr)
                    if sc >= 0:
                        scores.append(sc)
                if scores:
                    scores.sort(reverse=True)
                    k = max(1, min(FACE_TOP_K, len(scores)))
                    person_scores[name] = float(sum(scores[:k]) / k)
        else:
            for name, vecs in index.items():
                scores = []
                for v in vecs:
                    if v.shape[0] != emb_dim:
                        dim_skipped += 1
                        continue
                    scores.append(float(np.dot(emb, v)))
                if scores:
                    scores.sort(reverse=True)
                    k = max(1, min(FACE_TOP_K, len(scores)))
                    person_scores[name] = float(sum(scores[:k]) / k)

        if dim_skipped and not getattr(self, "_dim_warn_printed", False):
            self._dim_warn_printed = True
            print(
                f"[FACE] Canh bao: {dim_skipped} mau trong DB co so chieu khac "
                f"(query={emb_dim}). Hay enroll lai mat bang dung model hien tai "
                f"(face_mode={self.face.mode})."
            )

        ranked = sorted(person_scores.items(), key=lambda x: x[1], reverse=True)
        best_name = ranked[0][0] if ranked else None
        best_score = ranked[0][1] if ranked else -1.0
        second_score = ranked[1][1] if len(ranked) > 1 else -1.0

        ms = int((time.time() - t0) * 1000)
        thr = FACE_SIMILARITY
        if self.face.mode == "simple":
            thr = 0.92
        elif self.face.mode == "deepface":
            thr = max(thr, 0.50)

        self._last_presence_frame = frame_bgr

        # Margin: top1 phai cao hon top2 ro ret (tranh nham 2 nguoi giong)
        margin_ok = True
        if best_name and second_score >= 0 and FACE_MATCH_MARGIN > 0:
            if (best_score - second_score) < FACE_MATCH_MARGIN and best_score < (thr + 0.08):
                margin_ok = False

        if best_name is None or best_score < thr or not margin_ok:
            self._scan = {
                "state": "stranger",
                "name": None,
                "score": round(float(best_score), 3),
                "message": "Người lạ",
            }
            self._on_presence(True, frame_bgr=frame_bgr)
            self._maybe_stranger_alert(best_score, frame_bgr=frame_bgr)
            return {**self._scan, "ms": ms}

        greet = f"Xin chào {best_name}"
        if nfaces >= 2:
            greet += " (ưu tiên mặt giữa)"
        self._scan = {
            "state": "recognized",
            "name": best_name,
            "user_name": best_name,
            "score": round(float(best_score), 3),
            "message": greet,
            "face_count": nfaces,
        }
        self._on_presence(True, frame_bgr=frame_bgr)

        if unlock and FACE_AUTO_UNLOCK:
            last = self._last_face_unlock.get(best_name, 0)
            if time.time() - last >= FACE_UNLOCK_COOLDOWN:
                self._last_face_unlock[best_name] = time.time()
                self.unlock(by=best_name, method="face")
                self.add_login_log(best_name, "face", "THÀNH CÔNG", f"score={best_score:.3f}")

        return {**self._scan, "ms": ms}

    def scan_status(self) -> dict:
        return dict(self._scan)

    def _maybe_stranger_alert(self, score: float, frame_bgr=None):
        # Mac dinh KHONG gui Telegram ngay — chi ghi log khi STRANGER_INSTANT_ALERT=1
        # Canh bao co anh duoc gui o _on_presence (dung lau).
        if not STRANGER_INSTANT_ALERT:
            return
        now = time.time()
        if now - self._last_stranger_alert < STRANGER_ALERT_COOLDOWN:
            return
        self._last_stranger_alert = now
        self.add_security_alert(
            "Người lạ",
            "critical",
            f"Phát hiện khuôn mặt không khớp (score={score:.2f})",
            frame_bgr=frame_bgr,
        )

    # ===================== Prolonged presence =====================

    def _on_presence(self, present: bool, frame_bgr=None):
        # Dem thoi gian. UI gui Telegram; backend chi log (tranh double).
        now = time.time()
        if present:
            if frame_bgr is not None:
                self._last_presence_frame = frame_bgr
            if self._presence_since is None:
                self._presence_since = now
            elif now - self._presence_since >= PROLONGED_SECONDS:
                if now - self._last_prolonged_alert < PROLONGED_COOLDOWN:
                    return
                self._last_prolonged_alert = now
                self._last_stranger_alert = now
                secs = int(now - self._presence_since)
                state = self._scan.get("state")
                who = self._scan.get("name") or "người lạ / chưa nhận diện"
                if state == "recognized":
                    self.add_security_alert(
                        "Đối tượng ở lâu",
                        "warning",
                        f"{who} đứng trước camera khoảng {secs}s",
                        frame_bgr=None,
                        notify_telegram=False,
                    )
                else:
                    img = frame_bgr if frame_bgr is not None else getattr(
                        self, "_last_presence_frame", None
                    )
                    self.add_security_alert(
                        "Người lạ đứng lâu",
                        "critical",
                        f"Người lạ đứng trước camera khoảng {secs}s",
                        frame_bgr=img,
                        notify_telegram=False,
                    )
                self._presence_since = now
        else:
            self._presence_since = None

    # ===================== MFA =====================

    def mfa_start(self, methods: list) -> dict:
        methods = [m for m in (methods or []) if m]
        if len(methods) != 2:
            return {"ok": False, "message": "Chọn đúng 2 phương thức MFA"}
        # factor types
        factors = {
            "telegram_otp": "HAVE", "rfid": "HAVE",
            "pin": "KNOW",
            "face": "ARE", "fingerprint": "ARE",
        }
        fset = {factors.get(m) for m in methods}
        if None in fset or len(fset) < 2:
            return {"ok": False, "message": "2 phương thức phải khác nhóm yếu tố (HAVE/KNOW/ARE)"}
        sid = str(uuid.uuid4())
        self._mfa[sid] = {
            "methods": methods,
            "done": set(),
            "user": None,
            "created": time.time(),
            "exp": time.time() + MFA_SESSION_TTL,
        }
        return {
            "ok": True,
            "session_id": sid,
            "methods": methods,
            "message": "MFA session started",
        }

    def mfa_status(self, session_id: str) -> dict:
        s = self._mfa.get(session_id or "")
        if not s:
            return {"ok": False, "state": "invalid", "message": "Session không tồn tại"}
        if time.time() > s["exp"]:
            self._mfa.pop(session_id, None)
            return {"ok": False, "state": "expired", "message": "Hết hạn"}
        remaining = [m for m in s["methods"] if m not in s["done"]]
        completed = list(s["done"])
        if len(s["done"]) >= 2:
            return {
                "ok": True, "state": "completed",
                "user_name": s.get("user"),
                "completed": completed, "remaining": [],
            }
        return {
            "ok": True, "state": "pending",
            "user_name": s.get("user"),
            "completed": completed, "remaining": remaining,
            "methods": s["methods"],
        }

    def mfa_cancel(self, session_id: str) -> dict:
        self._mfa.pop(session_id or "", None)
        return {"ok": True}

    def mfa_request_otp(self, session_id: str, username: str = "") -> dict:
        # Single OTP or MFA: gửi OTP tới user có telegram_chat_id
        code = "".join(random.choices(string.digits, k=6))
        target_user = None
        if username:
            target_user = self._find_user(username)
        if target_user is None:
            # gửi tới mọi user có chat_id (đơn giản cho kiosk)
            candidates = [
                u for u in self.users.values()
                if u.get("status") == "approved" and u.get("telegram_chat_id")
            ]
            if len(candidates) == 1:
                target_user = candidates[0]
            elif not candidates:
                return {"ok": False, "message": "Không có user nào gắn Telegram chat_id"}
            else:
                # nếu nhiều user — bắt buộc username
                if not username:
                    return {"ok": False, "message": "Nhập username để nhận OTP"}
                return {"ok": False, "message": "Không tìm thấy user"}

        chat_id = str(target_user.get("telegram_chat_id") or "")
        if not chat_id:
            return {"ok": False, "message": "User chưa gắn telegram_chat_id"}

        uname = target_user["username"]
        self._otp[uname] = {"code": code, "exp": time.time() + OTP_TTL}
        sent = self._telegram_otp_send(chat_id, code, uname)
        if not sent:
            # Dev fallback: in ra console
            print(f"[OTP] {uname} → {code} (Telegram gửi thất bại — dùng mã console)")
        return {
            "ok": True,
            "message": "Đã gửi OTP qua Telegram" if sent else "Gửi Telegram thất bại — xem console",
            "session_id": session_id,
            "username": uname,
        }

    def mfa_verify(self, session_id: str, method: str, payload: dict) -> dict:
        s = self._mfa.get(session_id or "")
        if not s:
            return {"ok": False, "success": False, "message": "Session invalid"}
        if time.time() > s["exp"]:
            self._mfa.pop(session_id, None)
            return {"ok": False, "success": False, "message": "Session hết hạn"}
        method = (method or "").lower()
        if method not in s["methods"]:
            return {"ok": False, "success": False, "message": f"Method {method} không thuộc session"}
        if method in s["done"]:
            return {"ok": True, "success": True, "message": "Đã xác thực method này", "already": True}

        ok = False
        user_name = s.get("user")
        payload = payload or {}

        if method == "pin":
            r = self._verify_pin_only(payload.get("pin") or "")
            ok = r.get("ok")
            user_name = r.get("user_name") or user_name
        elif method == "rfid":
            r = self._verify_rfid_only(payload.get("rfid_uid") or "")
            ok = r.get("ok")
            user_name = r.get("user_name") or user_name
        elif method == "fingerprint":
            r = self._verify_fp_only(payload.get("fingerprint_id") or "")
            ok = r.get("ok")
            user_name = r.get("user_name") or user_name
        elif method == "face":
            # UI đã gọi face_recognize; payload có thể có name
            name = payload.get("name") or payload.get("user_name")
            if name and name in self.face_db:
                ok = True
                user_name = name
            elif self._scan.get("state") == "recognized" and self._scan.get("name"):
                ok = True
                user_name = self._scan.get("name")
            else:
                ok = False
        elif method == "telegram_otp":
            r = self._verify_otp(payload.get("username") or user_name or "", payload.get("otp") or payload.get("code") or "")
            ok = r.get("ok")
            user_name = r.get("user_name") or user_name
        else:
            return {"ok": False, "success": False, "message": f"Method không hỗ trợ: {method}"}

        if not ok:
            self.add_login_log(user_name or "?", f"mfa/{method}", "THẤT BẠI", "")
            return {"ok": False, "success": False, "message": f"Xác thực {method} thất bại"}

        # Consistency: nếu đã có user, method sau phải cùng user
        if s.get("user") and user_name and s["user"] != user_name:
            return {"ok": False, "success": False, "message": "Không khớp user của bước MFA trước"}

        s["done"].add(method)
        if user_name:
            s["user"] = user_name

        if len(s["done"]) >= 2:
            self.unlock(by=s["user"] or "mfa", method="mfa")
            self.add_login_log(s["user"] or "?", "mfa", "THÀNH CÔNG", "+".join(sorted(s["done"])))
            return {
                "ok": True, "success": True, "completed": True,
                "user_name": s["user"],
                "message": "MFA hoàn tất",
            }

        return {
            "ok": True, "success": True, "completed": False,
            "user_name": s.get("user"),
            "remaining": [m for m in s["methods"] if m not in s["done"]],
            "message": f"Đã xác thực {method}",
        }

    def _verify_pin_only(self, pin: str) -> dict:
        pin = (pin or "").strip()
        for u in self.users.values():
            if u.get("status") == "approved" and u.get("pin_hash") and _check_pin(u["pin_hash"], pin):
                return {"ok": True, "user_name": u["username"]}
        return {"ok": False}

    def _verify_rfid_only(self, uid: str) -> dict:
        uid = _norm_rfid(uid)
        for u in self.users.values():
            if u.get("status") == "approved" and _norm_rfid(u.get("rfid_uid")) == uid:
                return {"ok": True, "user_name": u["username"]}
        return {"ok": False}

    def _verify_fp_only(self, fid: str) -> dict:
        fid = _norm_fp(fid)
        for u in self.users.values():
            if u.get("status") == "approved" and _norm_fp(u.get("fingerprint_id")) == fid:
                return {"ok": True, "user_name": u["username"]}
        return {"ok": False}

    def _verify_otp(self, username: str, code: str) -> dict:
        code = (code or "").strip()
        username = _norm_name(username)
        # nếu không có username, thử mọi OTP còn hạn
        if username and username in self._otp:
            rec = self._otp[username]
            if time.time() <= rec["exp"] and rec["code"] == code:
                self._otp.pop(username, None)
                return {"ok": True, "user_name": username}
        else:
            for uname, rec in list(self._otp.items()):
                if time.time() <= rec["exp"] and rec["code"] == code:
                    self._otp.pop(uname, None)
                    return {"ok": True, "user_name": uname}
        return {"ok": False}

    # ===================== MQTT (optional) =====================

    def _init_mqtt(self):
        try:
            from mqtt_kiosk_hook import KioskMqttHook
            self._mqtt = KioskMqttHook(
                unlock_fn=lambda: self.unlock(by="mqtt", method="remote"),
                lock_fn=lambda: self.lock(by="mqtt", method="remote"),
                get_status_fn=self.door_status,
                source="kiosk",
            )
            self._mqtt.start()
            print("[LOCAL] MQTT hook started")
        except Exception as e:
            print(f"[LOCAL] MQTT init fail (optional): {e}")
            self._mqtt = None

    def _mqtt_publish_door(self):
        if self._mqtt:
            try:
                self._mqtt.publish_status()
            except Exception:
                pass

    def _mqtt_publish_log(self, entry: dict):
        if self._mqtt:
            try:
                self._mqtt.on_auth_success(
                    entry.get("identifier", "?"),
                    entry.get("method", ""),
                    note=f"{entry.get('result')} {entry.get('note', '')}".strip(),
                ) if "THÀNH" in str(entry.get("result", "")).upper() or "SUCCESS" in str(entry.get("result", "")).upper() else self._mqtt.on_auth_fail(
                    entry.get("method", ""),
                    note=str(entry.get("note", "")),
                )
            except Exception:
                pass

    def _mqtt_publish_alert(self, entry: dict):
        if self._mqtt:
            try:
                self._mqtt.on_alert(
                    entry.get("severity", "notice"),
                    entry.get("type", "alert"),
                    entry.get("message", ""),
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level API (enroll_face_cli + kiosk_ui)
# ---------------------------------------------------------------------------

backend = LocalBackend()

def list_faces():
    return backend.face_list()


def register_face(name, frame):
    return backend.register_face(name, frame)

def face_db_info():
    return backend.face_db_info()

def delete_face(name):
    return backend.delete_face(name)

def clear_face_db():
    return backend.clear_face_db()

def purge_incompatible_faces():
    return backend.purge_incompatible_faces()



# ===================== HTTP API cho ESP32 =====================
# ESP32 POST toi http://<IP_Pi>:5055/api/...
ESP32_API_PORT = int(os.environ.get("ESP32_API_PORT", "5055"))

def start_esp32_api_server(backend_instance=None):
    """Thread HTTP nho de ESP32 goi vao (RFID / van tay / bao chay)."""
    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json as _json
    except Exception as e:
        print(f"[ESP32-API] Khong start duoc: {e}")
        return None

    be = backend_instance

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[ESP32-API] {args[0] if args else fmt}")

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return _json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return {}

        def _send(self, code, obj):
            body = _json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/health"):
                return self._send(200, {"ok": True, "service": "kiosk-esp32-api"})
            if path == "/api/door_status" and be:
                return self._send(200, be.door_status())
            if path == "/api/system_health" and be:
                return self._send(200, be.system_health())
            self._send(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if not be:
                return self._send(503, {"ok": False, "message": "backend chua san sang"})
            path = self.path.split("?")[0]
            body = self._read_json()
            if path == "/api/rfid_auth":
                return self._send(200, be.rfid_auth(body.get("rfid_uid") or body.get("uid") or ""))
            if path == "/api/fingerprint_auth":
                return self._send(
                    200,
                    be.fingerprint_auth(
                        str(body.get("fingerprint_id") or body.get("id") or "")
                    ),
                )
            if path in ("/api/fire_alert", "/api/smoke_alert"):
                return self._send(
                    200,
                    be.fire_alert(
                        source=body.get("source") or "esp32",
                        sensor=body.get("sensor") or "mq2",
                        value=body.get("value"),
                    ),
                )
            if path == "/api/doorbell":
                return self._send(200, be.doorbell(by=body.get("by") or "esp32"))
            if path == "/api/esp32":
                return self._send(
                    200,
                    be.esp32_event(body.get("kind") or body.get("type") or "", body),
                )
            if path == "/api/door_control":
                return self._send(200, be.door_control(body.get("action") or ""))
            self._send(404, {"ok": False, "message": f"unknown {path}"})

    def _run():
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", ESP32_API_PORT), Handler)
            print(f"[ESP32-API] Listening 0.0.0.0:{ESP32_API_PORT} "
                  f"(rfid_auth / fingerprint_auth / fire_alert)")
            httpd.serve_forever()
        except Exception as e:
            print(f"[ESP32-API] Loi: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def set_user_credentials(name, pin=None, fingerprint_id=None, rfid_uid=None, telegram_chat_id=None):
    return backend.set_user_credentials(
        name, pin=pin, fingerprint_id=fingerprint_id,
        rfid_uid=rfid_uid, telegram_chat_id=telegram_chat_id,
    )


def face_recognize(frame, unlock=True):
    return backend.face_recognize(frame, unlock=unlock)

# Flag for kiosk_ui
LOCAL_OK = True


# Start ESP32 HTTP API (sau khi ham da define)
try:
    start_esp32_api_server(backend)
except Exception as _e:
    print(f"[ESP32-API] start fail: {_e}")
