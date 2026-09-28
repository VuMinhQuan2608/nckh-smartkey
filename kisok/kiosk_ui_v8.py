"""
SENTRY AI / SMART KEY - kiosk_ui_v8.py
Dark Premium Smart Access Control kiosk for Raspberry Pi 4 + touchscreen.

v8 — STANDALONE: Kiosk chạy độc lập, KHÔNG cần bật web_server.
  - Auth PIN / RFID / Fingerprint / MFA / OTP local (kiosk_local_backend.py)
  - Khóa cửa GPIO local
  - Telegram bằng token RIÊNG trên Kiosk (.env)
  - Tuỳ chọn đẩy frame lên server nếu USE_SERVER_CAMERA_UPLOAD=1

MFA rule:
  Select exactly 2 methods from DIFFERENT factor types:
  HAVE: telegram_otp, rfid
  KNOW: pin
  ARE:  face, fingerprint
"""

import os
import queue
import threading
import time
import math
import tkinter as tk

# ===== LOAD .env TRƯỚC MỌI THỨ (để Telegram hoạt động) =====
def _load_dotenv():
    try:
        from dotenv import load_dotenv
        if load_dotenv(override=True):
            print("[ENV] Đã load .env bằng python-dotenv")
            return
    except ImportError:
        pass
    # Fallback đọc thủ công
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")
        print("[ENV] Đã load .env thủ công")
    else:
        print("[ENV] Không tìm thấy file .env")

_load_dotenv()
_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_chats = os.environ.get("TELEGRAM_CHAT_IDS", "")
print(f"[ENV] TELEGRAM_BOT_TOKEN = {'***' + _token[-10:] if len(_token) > 10 else '(TRỐNG)'}")
print(f"[ENV] TELEGRAM_CHAT_IDS  = {_chats or '(TRỐNG)'}")
# ========================================================

# Suppress noisy OpenCV / FFmpeg camera probe logs (Windows DSHOW etc.)
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")  # quiet

import cv2
import numpy as np
import requests
from PIL import Image, ImageTk

try:
    cv2.setLogLevel(cv2.LOG_LEVEL_ERROR)
except Exception:
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:
        pass


# ============================================================
# CONFIG — STANDALONE mặc định (không cần web_server)
# ============================================================

# 1 = luôn backend local. 0 = thử server trước, fallback local khi server down.
STANDALONE = os.environ.get("KIOSK_STANDALONE", "1").strip() not in (
    "0", "false", "False", "no",
)

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:5000").rstrip("/")
USE_SERVER_CAMERA_UPLOAD = os.environ.get("USE_SERVER_CAMERA_UPLOAD", "0").strip() in (
    "1", "true", "True", "yes",
)

VIDEO_URL = f"{SERVER_URL}/video_feed"
KIOSK_FRAME_URL = f"{SERVER_URL}/api/kiosk_frame"

STATUS_INTERVAL_MS = 1200
CAMERA_RECONNECT_SECONDS = 2

LOCAL_CAMERA_INDEX = 0
CAPTURE_WIDTH = 640
CAPTURE_HEIGHT = 480
UPLOAD_FPS = 10
JPEG_QUALITY = 70
CAMERA_FALLBACK_TO_SERVER = not STANDALONE

TRANSITION_MS = 160
TRANSITION_FADE_STEPS = 3

OPTIONAL_FINGERPRINT_STATUS_URL = f"{SERVER_URL}/api/fingerprint_status"
OPTIONAL_RFID_STATUS_URL = f"{SERVER_URL}/api/rfid_status"
OPTIONAL_SYSTEM_STATUS_URL = f"{SERVER_URL}/api/system_status"

try:
    from kiosk_local_backend import backend as LOCAL_BACKEND
    LOCAL_OK = True
    print(f"[KIOSK] Local backend OK | STANDALONE={STANDALONE}")
except Exception as _lb_err:
    LOCAL_BACKEND = None
    LOCAL_OK = False
    print(f"[KIOSK] Local backend load fail: {_lb_err}")
    if STANDALONE:
        print("[KIOSK] *** Cần kiosk_local_backend.py cùng thư mục ***")


# ============================================================
# DARK PREMIUM THEME (Smart Key / Tesla / Security panel)
# ============================================================

BG          = "#020B18"      # Deep navy
BG2         = "#06152A"      # Slightly lighter navy
CARD        = "#081B35"      # Panel / card
CARD2       = "#0B2342"      # Secondary panel
PANEL3      = "#102B4D"      # Tertiary
PRIMARY     = "#1688FF"      # Electric blue
PRIMARY2    = "#38A9FF"      # Soft cyan-blue
SUCCESS     = "#18D6B0"      # Cyan-green
SUCCESS_BG  = "#0A2E28"      # Dark success tint
WARN        = "#F5B942"      # Amber
WARN_BG     = "#2E2410"      # Dark warn tint
DANGER      = "#FF4D67"      # Red
DANGER_BG   = "#2E1018"      # Dark danger tint
TEXT        = "#F4F8FF"      # Near white
DIM         = "#8EA9C7"      # Muted blue-gray
DIM2        = "#6F89A8"      # Dimmer
BORDER      = "#164A78"      # Blue border
BORDER2     = "#1A5A90"      # Brighter border
SELECT_BG   = "#0D2F5C"      # Selected card bg
SELECT_BD   = "#1688FF"      # Selected border
ICON_BG     = "#0D294A"      # Icon well
SHADOW      = "#041020"
WHITE       = "#FFFFFF"
BLACK       = "#020B18"
GLOW        = "#1688FF"


# ============================================================
# MICRO-ANIMATION HELPER (Canvas + after, Pi-friendly)
# - 15–25 FPS max, only while active
# - cancel job when done / screen changes
# ============================================================

class MicroAnim:
    """Lightweight animation jobs bound to a Tk root. Auto-stop on cancel."""

    def __init__(self, root):
        self.root = root
        self._jobs = {}  # name -> after id

    def cancel(self, name=None):
        if name is None:
            for j in list(self._jobs.values()):
                try:
                    self.root.after_cancel(j)
                except Exception:
                    pass
            self._jobs.clear()
            return
        j = self._jobs.pop(name, None)
        if j is not None:
            try:
                self.root.after_cancel(j)
            except Exception:
                pass

    def _schedule(self, name, delay_ms, fn):
        self.cancel(name)

        def _run():
            self._jobs.pop(name, None)
            try:
                fn()
            except Exception:
                pass

        self._jobs[name] = self.root.after(delay_ms, _run)

    def ring_expand(self, canvas, color=SUCCESS, steps=6, size=72, on_done=None):
        """Access Granted: expanding ring then check. ~20 FPS, ~300ms."""
        self.cancel("ring")
        step = [0]

        def tick():
            Icons.clear(canvas)
            s = size / 48.0
            progress = step[0] / max(steps - 1, 1)
            r = int(8 + progress * 16)
            canvas.create_oval(
                size // 2 - r, size // 2 - r,
                size // 2 + r, size // 2 + r,
                outline=color, width=max(2, int(2.5 * s)),
            )
            if step[0] >= steps - 1:
                Icons.check(canvas, color, size)
                if on_done:
                    on_done()
                return
            step[0] += 1
            self._schedule("ring", 50, tick)

        tick()

    def shake(self, widget, times=3, offset=4, on_done=None):
        """Access Denied: horizontal shake. Stops after times*2 frames."""
        self.cancel("shake")
        state = {"n": 0, "dir": 1}
        try:
            info = widget.place_info()
            base_x = float(info.get("relx", 0.5))
            using_place = bool(info)
        except Exception:
            using_place = False
            base_x = 0.5

        def tick():
            if state["n"] >= times * 2:
                try:
                    if using_place:
                        widget.place_configure(relx=base_x)
                except Exception:
                    pass
                if on_done:
                    on_done()
                return
            dx = state["dir"] * offset
            state["dir"] *= -1
            state["n"] += 1
            try:
                if using_place:
                    # slight relx nudge
                    widget.place_configure(relx=base_x + dx * 0.002)
            except Exception:
                pass
            self._schedule("shake", 40, tick)

        tick()

    def pulse_dot(self, label, color_a=SUCCESS, color_b="#0E8A6E", period_ms=1200):
        """Security status pulse – toggles fg color. Cancel with cancel('pulse')."""
        self.cancel("pulse")

        def tick():
            try:
                if not label.winfo_exists():
                    return
                cur = label.cget("fg")
                label.config(fg=color_b if cur == color_a else color_a)
            except Exception:
                return
            self._schedule("pulse", period_ms, tick)

        tick()

    def key_flash(self, btn, flash_bg=PRIMARY, normal_bg=None, ms=120):
        """PIN key press highlight then restore."""
        if normal_bg is None:
            try:
                normal_bg = btn.cget("bg")
            except Exception:
                normal_bg = CARD2
        try:
            btn.config(bg=flash_bg)
        except Exception:
            return

        def restore():
            try:
                if btn.winfo_exists():
                    btn.config(bg=normal_bg)
            except Exception:
                pass

        self.root.after(ms, restore)

    def lock_transition(self, canvas, unlock=True, size=64, on_done=None):
        """Lock → Unlock (or reverse) in a few frames."""
        self.cancel("lock")
        frames = [
            (Icons.lock_closed, DANGER),
            (Icons.lock_closed, WARN),
            (Icons.lock_open if unlock else Icons.lock_closed,
             SUCCESS if unlock else DANGER),
        ]
        if not unlock:
            frames = [
                (Icons.lock_open, SUCCESS),
                (Icons.lock_open, WARN),
                (Icons.lock_closed, DANGER),
            ]
        idx = [0]

        def tick():
            fn, col = frames[idx[0]]
            try:
                fn(canvas, col, size)
            except Exception:
                pass
            if idx[0] >= len(frames) - 1:
                if on_done:
                    on_done()
                return
            idx[0] += 1
            self._schedule("lock", 80, tick)

        tick()

    def scan_line(self, parent, width=560, height=360, x=40, color=PRIMARY,
                  name="scanline", fps=20):
        """Face scan line bouncing top↔bottom. Call cancel(name) to stop."""
        self.cancel(name)
        line = tk.Frame(parent, bg=color, height=2)
        state = {"y": 24, "dir": 1}
        delay = max(33, int(1000 / fps))

        def tick():
            try:
                if not parent.winfo_exists():
                    return
                line.place(x=x, y=state["y"], width=width, height=2)
                line.lift()
            except Exception:
                return
            state["y"] += state["dir"] * 8
            if state["y"] >= height - 24 or state["y"] <= 24:
                state["dir"] *= -1
            self._schedule(name, delay, tick)

        # store line ref so we can destroy later
        self._scan_line_widget = line
        tick()
        return line

    def focus_brackets(self, canvas, color=PRIMARY, size_w=640, size_h=380,
                       name="focus", fps=12):
        """4 corner brackets breathe in/out on face camera overlay."""
        self.cancel(name)
        phase = [0]
        delay = max(40, int(1000 / fps))

        def draw(expand):
            canvas.delete("bracket")
            L = 18 + expand
            t = 3
            # NW
            canvas.create_line(12, 12 + L, 12, 12, fill=color, width=t, tags="bracket")
            canvas.create_line(12, 12, 12 + L, 12, fill=color, width=t, tags="bracket")
            # NE
            canvas.create_line(size_w - 12, 12, size_w - 12 - L, 12, fill=color, width=t, tags="bracket")
            canvas.create_line(size_w - 12, 12, size_w - 12, 12 + L, fill=color, width=t, tags="bracket")
            # SW
            canvas.create_line(12, size_h - 12, 12, size_h - 12 - L, fill=color, width=t, tags="bracket")
            canvas.create_line(12, size_h - 12, 12 + L, size_h - 12, fill=color, width=t, tags="bracket")
            # SE
            canvas.create_line(size_w - 12, size_h - 12, size_w - 12 - L, size_h - 12, fill=color, width=t, tags="bracket")
            canvas.create_line(size_w - 12, size_h - 12, size_w - 12, size_h - 12 - L, fill=color, width=t, tags="bracket")

        def tick():
            try:
                if not canvas.winfo_exists():
                    return
            except Exception:
                return
            expand = int(4 * abs((phase[0] % 20) - 10) / 10)  # 0..4
            draw(expand)
            phase[0] += 1
            self._schedule(name, delay, tick)

        try:
            canvas.place(x=0, y=0)
            canvas.lift()
        except Exception:
            pass
        tick()

    def rfid_waves(self, canvas, color=PRIMARY, size=72, name="rfid_wave"):
        """RFID arcs expand in phases, loop until cancel."""
        self.cancel(name)
        phase = [0]

        def tick():
            try:
                if not canvas.winfo_exists():
                    return
            except Exception:
                return
            Icons.clear(canvas)
            s = size / 48.0
            # card body
            canvas.create_rectangle(6 * s, 12 * s, 34 * s, 36 * s,
                                    outline=color, width=max(2, int(2 * s)))
            canvas.create_rectangle(10 * s, 18 * s, 18 * s, 26 * s,
                                    outline=color, width=max(1, int(1.5 * s)))
            # expanding waves (phase selects how many arcs)
            n = 1 + (phase[0] % 3)
            for i in range(n):
                r = 8 + i * 6
                canvas.create_arc(
                    28 * s - r * s, 16 * s - r * s,
                    28 * s + r * s, 32 * s + r * s,
                    start=-50, extent=100, style="arc",
                    outline=color, width=max(1, int(1.5 * s)),
                )
            phase[0] += 1
            self._schedule(name, 400, tick)

        tick()

    def fingerprint_scan(self, canvas, color=PRIMARY, size=72, name="fp_scan"):
        """Fingerprint with moving scan band (y marker)."""
        self.cancel(name)
        y = [10]
        direction = [1]

        def tick():
            try:
                if not canvas.winfo_exists():
                    return
            except Exception:
                return
            Icons.fingerprint(canvas, color, size)
            # overlay scan band
            s = size / 48.0
            yy = y[0] * s
            canvas.create_line(8 * s, yy, 40 * s, yy, fill=SUCCESS, width=max(2, int(2 * s)))
            y[0] += direction[0] * 3
            if y[0] >= 40 or y[0] <= 8:
                direction[0] *= -1
            self._schedule(name, 50, tick)

        tick()

    def telegram_send(self, canvas, color=PRIMARY, size=64, on_done=None):
        """Paper plane slides right then restores. ~400ms."""
        self.cancel("tg_send")
        frames = [0, 4, 8, 12, 8, 0]
        idx = [0]

        def tick():
            try:
                if not canvas.winfo_exists():
                    return
            except Exception:
                return
            Icons.clear(canvas)
            dx = frames[idx[0]]
            s = size / 48.0
            pts = [
                (8 + dx) * s, 24 * s,
                (40 + dx) * s, 10 * s,
                (28 + dx) * s, 38 * s,
                (22 + dx) * s, 30 * s,
                (16 + dx) * s, 36 * s,
            ]
            canvas.create_polygon(pts, fill=color, outline=color)
            if idx[0] >= len(frames) - 1:
                Icons.telegram(canvas, color, size)
                if on_done:
                    on_done()
                return
            idx[0] += 1
            self._schedule("tg_send", 60, tick)

        tick()

    def loading_dots(self, label, base_text="Đang xác thực", name="loading"):
        """AUTHENTICATING . .. ... loop until cancel."""
        self.cancel(name)
        n = [0]

        def tick():
            try:
                if not label.winfo_exists():
                    return
            except Exception:
                return
            dots = "." * (1 + n[0] % 3)
            label.config(text=f"{base_text}{dots}")
            n[0] += 1
            self._schedule(name, 400, tick)

        tick()


# ============================================================
# VECTOR ICON RENDERER (Canvas drawing – no emoji dependency)
# ============================================================

class Icons:
    """Consistent vector icons drawn on Canvas. Size ~48–64 px recommended."""

    @staticmethod
    def clear(c):
        c.delete("all")

    @staticmethod
    def telegram(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        # paper plane body
        pts = [
            8*s, 24*s,
            40*s, 10*s,
            28*s, 38*s,
            22*s, 30*s,
            16*s, 36*s,
        ]
        c.create_polygon(pts, fill=color, outline=color, smooth=False)
        # wing line
        c.create_line(22*s, 30*s, 40*s, 10*s, fill=WHITE, width=max(1, int(1.5*s)))
        # signal arcs
        c.create_arc(34*s, 6*s, 46*s, 18*s, start=40, extent=100,
                     style="arc", outline=color, width=max(1, int(1.5*s)))
        c.create_arc(38*s, 2*s, 50*s, 14*s, start=40, extent=100,
                     style="arc", outline=color, width=max(1, int(1.2*s)))

    @staticmethod
    def rfid(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        # card body
        c.create_rounded_rectangle = getattr(c, "create_rounded_rectangle", None)
        c.create_rectangle(6*s, 12*s, 34*s, 36*s, outline=color, width=max(2, int(2*s)), fill="")
        # chip
        c.create_rectangle(10*s, 18*s, 18*s, 26*s, outline=color, width=max(1, int(1.5*s)))
        c.create_line(18*s, 22*s, 22*s, 22*s, fill=color, width=max(1, int(1.5*s)))
        # waves
        for i, r in enumerate([8, 14, 20]):
            c.create_arc(
                28*s - r*s, 16*s - r*s,
                28*s + r*s, 32*s + r*s,
                start=-50, extent=100, style="arc",
                outline=color, width=max(1, int(1.5*s)),
            )

    @staticmethod
    def pin(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        # keypad frame
        c.create_rectangle(10*s, 8*s, 38*s, 40*s, outline=color, width=max(2, int(2*s)))
        # 3x3 dots (keys)
        for row in range(3):
            for col in range(3):
                x = 16*s + col * 8*s
                y = 14*s + row * 8*s
                c.create_oval(x-2*s, y-2*s, x+2*s, y+2*s, fill=color, outline="")
        # PIN dots row at bottom
        for i in range(4):
            x = 14*s + i * 6*s
            c.create_oval(x, 36*s, x+3*s, 39*s, fill=color, outline="")

    @staticmethod
    def face(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        # outer frame (scan brackets)
        thick = max(2, int(2.2*s))
        L = 10*s
        # NW
        c.create_line(6*s, 6*s+L, 6*s, 6*s, fill=color, width=thick)
        c.create_line(6*s, 6*s, 6*s+L, 6*s, fill=color, width=thick)
        # NE
        c.create_line(42*s, 6*s, 42*s-L, 6*s, fill=color, width=thick)
        c.create_line(42*s, 6*s, 42*s, 6*s+L, fill=color, width=thick)
        # SW
        c.create_line(6*s, 42*s, 6*s, 42*s-L, fill=color, width=thick)
        c.create_line(6*s, 42*s, 6*s+L, 42*s, fill=color, width=thick)
        # SE
        c.create_line(42*s, 42*s, 42*s-L, 42*s, fill=color, width=thick)
        c.create_line(42*s, 42*s, 42*s, 42*s-L, fill=color, width=thick)
        # face oval
        c.create_oval(14*s, 12*s, 34*s, 36*s, outline=color, width=max(1, int(1.8*s)))
        # eyes
        c.create_oval(18*s, 20*s, 22*s, 24*s, fill=color, outline="")
        c.create_oval(26*s, 20*s, 30*s, 24*s, fill=color, outline="")
        # smile
        c.create_arc(18*s, 22*s, 30*s, 32*s, start=200, extent=140,
                     style="arc", outline=color, width=max(1, int(1.5*s)))

    @staticmethod
    def fingerprint(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        cx, cy = 24*s, 24*s
        # concentric fingerprint arcs
        for r, start, extent in [
            (6, 40, 280),
            (10, 20, 300),
            (14, 50, 260),
            (18, 30, 280),
            (22, 60, 240),
        ]:
            c.create_arc(
                cx-r, cy-r, cx+r, cy+r,
                start=start, extent=extent, style="arc",
                outline=color, width=max(1, int(1.6*s)),
            )
        # center
        c.create_oval(cx-2*s, cy-2*s, cx+2*s, cy+2*s, fill=color, outline="")

    @staticmethod
    def lock_closed(c, color=DANGER, size=48):
        Icons.clear(c)
        s = size / 48.0
        # body
        c.create_rectangle(12*s, 22*s, 36*s, 40*s, outline=color, width=max(2, int(2.2*s)), fill="")
        # shackle
        c.create_arc(16*s, 8*s, 32*s, 28*s, start=0, extent=180,
                     style="arc", outline=color, width=max(2, int(2.5*s)))
        # keyhole
        c.create_oval(21*s, 28*s, 27*s, 34*s, fill=color, outline="")
        c.create_rectangle(22.5*s, 32*s, 25.5*s, 37*s, fill=color, outline="")

    @staticmethod
    def lock_open(c, color=SUCCESS, size=48):
        Icons.clear(c)
        s = size / 48.0
        c.create_rectangle(12*s, 22*s, 36*s, 40*s, outline=color, width=max(2, int(2.2*s)), fill="")
        # open shackle (shifted left)
        c.create_arc(8*s, 8*s, 24*s, 28*s, start=0, extent=180,
                     style="arc", outline=color, width=max(2, int(2.5*s)))
        c.create_oval(21*s, 28*s, 27*s, 34*s, fill=color, outline="")
        c.create_rectangle(22.5*s, 32*s, 25.5*s, 37*s, fill=color, outline="")

    @staticmethod
    def camera(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        c.create_rectangle(6*s, 14*s, 38*s, 36*s, outline=color, width=max(2, int(2*s)))
        c.create_oval(16*s, 18*s, 28*s, 30*s, outline=color, width=max(2, int(2*s)))
        c.create_oval(20*s, 22*s, 24*s, 26*s, fill=color, outline="")
        # flash / top
        c.create_rectangle(28*s, 10*s, 34*s, 14*s, outline=color, width=max(1, int(1.5*s)))

    @staticmethod
    def server(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        for i, y0 in enumerate([8, 20, 32]):
            c.create_rectangle(10*s, y0*s, 38*s, (y0+10)*s, outline=color, width=max(1, int(1.8*s)))
            c.create_oval(14*s, (y0+3)*s, 18*s, (y0+7)*s, fill=SUCCESS if i < 2 else WARN, outline="")
            c.create_line(22*s, (y0+5)*s, 34*s, (y0+5)*s, fill=color, width=max(1, int(1.2*s)))

    @staticmethod
    def network(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        nodes = [(12, 24), (24, 12), (36, 24), (24, 36), (24, 24)]
        for x, y in nodes:
            c.create_oval((x-4)*s, (y-4)*s, (x+4)*s, (y+4)*s, fill=color, outline="")
        pairs = [(0, 4), (1, 4), (2, 4), (3, 4), (0, 1), (1, 2), (2, 3), (3, 0)]
        for a, b in pairs:
            x1, y1 = nodes[a]
            x2, y2 = nodes[b]
            c.create_line(x1*s, y1*s, x2*s, y2*s, fill=color, width=max(1, int(1.3*s)))

    @staticmethod
    def shield(c, color=SUCCESS, size=48):
        Icons.clear(c)
        s = size / 48.0
        pts = [24*s, 6*s, 40*s, 12*s, 40*s, 26*s, 24*s, 42*s, 8*s, 26*s, 8*s, 12*s]
        c.create_polygon(pts, outline=color, width=max(2, int(2*s)), fill="")
        # check
        c.create_line(16*s, 24*s, 22*s, 30*s, fill=color, width=max(2, int(2.5*s)))
        c.create_line(22*s, 30*s, 34*s, 16*s, fill=color, width=max(2, int(2.5*s)))

    @staticmethod
    def check(c, color=SUCCESS, size=48):
        Icons.clear(c)
        s = size / 48.0
        c.create_oval(6*s, 6*s, 42*s, 42*s, outline=color, width=max(2, int(2.5*s)))
        c.create_line(14*s, 24*s, 22*s, 32*s, fill=color, width=max(3, int(3*s)))
        c.create_line(22*s, 32*s, 36*s, 16*s, fill=color, width=max(3, int(3*s)))

    @staticmethod
    def door(c, color=PRIMARY, size=48):
        Icons.clear(c)
        s = size / 48.0
        c.create_rectangle(12*s, 6*s, 36*s, 42*s, outline=color, width=max(2, int(2.2*s)))
        c.create_oval(28*s, 22*s, 32*s, 26*s, fill=color, outline="")


# ============================================================
# MAIN APP
# ============================================================

class KioskApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SMART KEY — Secure Access Control")
        self.root.configure(bg=BG)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_w}x{screen_h}+0+0")
        try:
            self.root.attributes("-fullscreen", True)
        except Exception:
            pass
        try:
            self.root.state("zoomed")
        except Exception:
            pass
        self.root.bind("<Escape>", self._exit_fullscreen)

        self.http = requests.Session()

        self.running = True
        self.current_screen = ""
        self.logged_in_user = None

        self.pin_value = ""
        self.pin_max_len = 10
        self.change_pin_old = ""
        self._pin_dialog_value = ""

        self.frame_queue = queue.Queue(maxsize=2)
        self.latest_frame_bgr = None  # frame mới nhất cho Face AI local
        self.camera_thread = None
        self.camera_cap = None

        self.status_job = None
        self.clock_job = None
        self.face_poll_job = None
        self.scan_anim_job = None
        self._wave_job = None
        self._pulse_job = None
        self.anim = MicroAnim(self.root)  # Pi-friendly micro-animations

        self.last_scan_state = "idle"
        self.last_scan_name = None
        self.last_scan_score = 0
        self.last_door_data = {}
        self.last_system_data = {}
        self._scan_line_y = 0
        self._scan_direction = 1

        self._transitioning = False
        self._transition_job = None

        self._unlock_popup = None
        self._prev_door_locked = True
        self._prev_scan_state = "idle"
        self._last_recog_popup_time = 0
        self._last_face_unlock_time = 0

        # MFA
        self.mfa_mode = False
        self.mfa_session_id = None
        self.mfa_session = None
        self.mfa_selected = []
        self.mfa_vars = {}
        self.mfa_step_info = ""

        # Realtime connection flags (updated by camera + status poller)
        self._last_frame_time = 0.0
        self._server_ok = False
        self._network_ok = False
        self._camera_ok = False
        self._system_ok = False
        self._status_poll_job = None

        self.access_mode = "mfa"  # single | mfa | open
        self.access_mode_label = "Đăng nhập 2 lần (MFA)"
        self.single_selected_method = None

        # --- Cảnh báo người lạ đứng lâu + gửi ảnh Telegram ---
        self._stranger_since = None
        self._last_stranger_alert_at = 0.0
        self._stranger_monitor_job = None
        self._stranger_infer_busy = False
        self.PROLONGED_ALERT_SECONDS = float(
            os.environ.get("PROLONGED_ALERT_SECONDS", "60")
        )
        self.STRANGER_ALERT_COOLDOWN = float(
            os.environ.get("STRANGER_ALERT_COOLDOWN", "120")
        )
        self.PROLONGED_ALERT_COOLDOWN = float(
            os.environ.get("PROLONGED_ALERT_COOLDOWN", "120")
        )
        self.STRANGER_UI_TELEGRAM = os.environ.get(
            "STRANGER_UI_TELEGRAM", "1"
        ).strip().lower() in ("1", "true", "yes", "on")

        self._build_base_ui()
        self._start_camera_thread()
        self._refresh_camera()
        self._load_access_mode_and_home()
        self._update_clock()
        self._poll_connection_status()
        self._start_stranger_monitor()
        self._start_battery_watch()

    # ========================================================
    # BASE SHELL
    # ========================================================

    def _exit_fullscreen(self, _event=None):
        try:
            self.root.attributes("-fullscreen", False)
        except Exception:
            pass

    def _build_base_ui(self):
        # Header
        self.header = tk.Frame(self.root, bg=CARD, height=72,
                               highlightthickness=1, highlightbackground=BORDER)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        brand = tk.Frame(self.header, bg=CARD)
        brand.pack(side="left", padx=20, pady=8)

        icon_c = tk.Canvas(brand, width=40, height=40, bg=CARD, highlightthickness=0)
        icon_c.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        Icons.shield(icon_c, PRIMARY, 40)

        tk.Label(brand, text="SMART KEY", font=("Arial", 18, "bold"),
                 bg=CARD, fg=TEXT).grid(row=0, column=1, sticky="w")
        tk.Label(brand, text="SECURE ACCESS CONTROL", font=("Arial", 9),
                 bg=CARD, fg=DIM).grid(row=1, column=1, sticky="w")

        right = tk.Frame(self.header, bg=CARD)
        right.pack(side="right", padx=20)
        self.header_status = tk.Label(
            right, text="●  SYSTEM ONLINE", font=("Arial", 10, "bold"),
            bg=CARD, fg=SUCCESS,
        )
        self.header_status.pack(side="right", padx=(12, 0))
        tk.Label(right, text="│", font=("Arial", 12), bg=CARD, fg=BORDER).pack(
            side="right", padx=8,
        )
        self.clock_label = tk.Label(
            right, text="", font=("Arial", 12, "bold"), bg=CARD, fg=DIM,
        )
        self.clock_label.pack(side="right")

        # Camera panel (shown only on face screen)
        self.camera_outer = tk.Frame(self.root, bg=BG)
        self.camera_panel = tk.Frame(
            self.camera_outer, bg=BLACK,
            highlightthickness=2, highlightbackground=BORDER2,
            width=640, height=380,
        )
        self.camera_panel.pack()
        self.camera_panel.pack_propagate(False)

        self.video_label = tk.Label(
            self.camera_panel,
            text="Camera starting…",
            font=("Arial", 14), bg=BLACK, fg=DIM2, justify="center",
        )
        self.video_label.pack(fill="both", expand=True)

        # Face focus corners — small canvases at 4 corners (do NOT cover video)
        self._corner_brackets = []
        self.scan_overlay = None  # unused full-cover overlay (kept for compat)

        # Content
        self.content_frame = tk.Frame(self.root, bg=BG)
        self.content_frame.pack(fill="both", expand=True, padx=24, pady=(8, 4))

        self.transition_overlay = tk.Frame(self.root, bg=BG)
        self.transition_label = tk.Label(
            self.transition_overlay, text="", font=("Arial", 13), bg=BG, fg=DIM,
        )
        self.transition_label.place(relx=0.5, rely=0.5, anchor="center")

        # Footer status bar
        self.footer = tk.Frame(self.root, bg=CARD, height=44,
                               highlightthickness=1, highlightbackground=BORDER)
        self.footer.pack(fill="x", side="bottom")
        self.footer.pack_propagate(False)

        fl = tk.Frame(self.footer, bg=CARD)
        fl.pack(side="left", padx=20)
        self.footer_camera = tk.Label(
            fl, text="● CAMERA  ONLINE", font=("Arial", 9, "bold"),
            bg=CARD, fg=SUCCESS,
        )
        self.footer_camera.pack(side="left", padx=(0, 18))
        _srv_label = "● LOCAL  ONLINE" if STANDALONE else "● SERVER  ONLINE"
        self.footer_server = tk.Label(
            fl, text=_srv_label, font=("Arial", 9, "bold"),
            bg=CARD, fg=SUCCESS,
        )
        self.footer_server.pack(side="left", padx=(0, 18))
        self.footer_network = tk.Label(
            fl, text="● NETWORK  ONLINE", font=("Arial", 9, "bold"),
            bg=CARD, fg=SUCCESS,
        )
        self.footer_network.pack(side="left")

        tk.Label(
            self.footer, text="SMART KEY  ·  REALTIME SECURITY",
            font=("Arial", 9), bg=CARD, fg=DIM,
        ).pack(side="right", padx=20)

    def _set_indicator(self, label, online, online_text, offline_text):
        """Update a status label to reflect real on/off state."""
        try:
            if not label or not label.winfo_exists():
                return
            if online:
                label.config(text=online_text, fg=SUCCESS)
            else:
                label.config(text=offline_text, fg=DANGER)
        except Exception:
            pass

    def _apply_status_indicators(self):
        """Paint header + footer from real flags. Pulse only when system OK."""
        cam = self._camera_ok
        srv = self._server_ok
        net = self._network_ok
        sys_ok = cam and srv and net
        self._system_ok = sys_ok

        self._set_indicator(
            self.footer_camera, cam,
            "● CAMERA  ONLINE", "● CAMERA  OFFLINE",
        )
        self._set_indicator(
            self.footer_server, srv,
            "● LOCAL  ONLINE" if STANDALONE else "● SERVER  ONLINE",
            "● LOCAL  OFFLINE" if STANDALONE else "● SERVER  OFFLINE",
        )
        self._set_indicator(
            self.footer_network, net,
            "● NETWORK  ONLINE", "● NETWORK  OFFLINE",
        )

        try:
            if self.header_status.winfo_exists():
                if sys_ok:
                    self.header_status.config(text="●  SYSTEM ONLINE", fg=SUCCESS)
                    # gentle pulse only while healthy
                    self.anim.pulse_dot(
                        self.header_status, SUCCESS, "#0E9A7A", 1600,
                    )
                else:
                    self.anim.cancel("pulse")
                    if not srv or not net:
                        self.header_status.config(
                            text="●  SYSTEM OFFLINE", fg=DANGER,
                        )
                    elif not cam:
                        self.header_status.config(
                            text="●  CAMERA OFFLINE", fg=WARN,
                        )
                    else:
                        self.header_status.config(
                            text="●  SYSTEM DEGRADED", fg=WARN,
                        )
        except Exception:
            pass

    def _poll_connection_status(self):
        """
        Real status every ~2s:
          camera  = received a frame in last 3s
          server  = local backend OK (standalone) hoặc web server reachable
          network = internet (Telegram) khi standalone; LAN server khi hybrid
        """
        if not self.running:
            return

        self._camera_ok = (time.time() - getattr(self, "_last_frame_time", 0)) < 3.0

        def worker():
            backend_ok = False
            net_ok = False
            try:
                status, data = self._request_json(
                    "GET", f"{SERVER_URL}/api/door_status", timeout=2,
                )
                backend_ok = status == 200
            except Exception:
                backend_ok = LOCAL_OK and STANDALONE

            if STANDALONE and LOCAL_OK:
                backend_ok = True
                # Network = có internet? (DNS/Telegram API nhẹ)
                try:
                    r = requests.get("https://api.telegram.org", timeout=2)
                    net_ok = r.status_code < 500
                except Exception:
                    net_ok = False
            else:
                net_ok = backend_ok

            def update():
                self._server_ok = backend_ok
                self._network_ok = net_ok
                self._apply_status_indicators()
                if self.running:
                    self._status_poll_job = self.root.after(
                        2000, self._poll_connection_status,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_camera(self):
        self.camera_outer.pack(padx=24, pady=(4, 0), before=self.content_frame)

    def _hide_camera(self):
        self.camera_outer.pack_forget()
        # stop face-only animations when camera hidden
        try:
            self.anim.cancel("scanline")
            self.anim.cancel("focus")
        except Exception:
            pass
        if getattr(self, "_scan_line", None) is not None:
            try:
                self._scan_line.destroy()
            except Exception:
                pass
            self._scan_line = None
        for c in getattr(self, "_corner_brackets", []):
            try:
                c.destroy()
            except Exception:
                pass
        self._corner_brackets = []

    def _clear_content(self):
        for w in self.content_frame.winfo_children():
            w.destroy()
        for job_name in ("status_job", "face_poll_job", "scan_anim_job", "_wave_job"):
            job = getattr(self, job_name, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_name, None)
        # Stop screen-specific micro-anims (keep global pulse)
        try:
            self.anim.cancel("scanline")
            self.anim.cancel("focus")
            self.anim.cancel("rfid_wave")
            self.anim.cancel("fp_scan")
            self.anim.cancel("tg_send")
            self.anim.cancel("loading")
            self.anim.cancel("ring")
            self.anim.cancel("lock")
            self.anim.cancel("shake")
        except Exception:
            pass

    def _transition_to(self, target_fn, *args, **kwargs):
        if self._transitioning:
            return
        self._transitioning = True
        if self._transition_job:
            try:
                self.root.after_cancel(self._transition_job)
            except Exception:
                pass
        try:
            self.transition_overlay.place(
                in_=self.content_frame, relx=0, rely=0, relwidth=1, relheight=1,
            )
            self.transition_overlay.lift()
        except Exception:
            pass

        def _do():
            try:
                target_fn(*args, **kwargs)
            except Exception:
                pass
            self._transition_job = self.root.after(30, self._end_transition)

        self._transition_job = self.root.after(TRANSITION_MS // 2, _do)

    def _end_transition(self):
        try:
            self.transition_overlay.place_forget()
        except Exception:
            pass
        self._transitioning = False
        self._transition_job = None

    # ---------- UI primitives ----------

    def _label(self, parent, text, size=14, color=TEXT, bold=False, **kwargs):
        return tk.Label(
            parent, text=text,
            font=("Arial", size, "bold" if bold else "normal"),
            bg=kwargs.pop("bg", parent.cget("bg") if parent else BG),
            fg=color, **kwargs,
        )

    def _button(self, parent, text, command, kind="normal", width=16, height=2):
        styles = {
            "success": (SUCCESS, BLACK, "#14B89A"),
            "danger":  (DANGER, WHITE, "#E03A52"),
            "primary": (PRIMARY, WHITE, "#0F6FD0"),
            "warn":    (WARN, BLACK, "#D4A030"),
            "normal":  (CARD2, TEXT, PANEL3),
            "ghost":   (CARD, DIM, BORDER),
        }
        bg, fg, abg = styles.get(kind, styles["normal"])
        btn = tk.Button(
            parent, text=text, command=command,
            font=("Arial", 13, "bold"),
            bg=bg, fg=fg,
            activebackground=abg, activeforeground=fg if kind != "ghost" else TEXT,
            relief="flat", bd=0,
            width=width, height=height,
            padx=12, pady=8,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=BORDER if kind in ("normal", "ghost") else bg,
        )
        return btn

    def _card(self, parent, **kwargs):
        return tk.Frame(
            parent, bg=CARD,
            highlightthickness=1, highlightbackground=BORDER,
            **kwargs,
        )

    # ========================================================
    # HTTP + LOCAL BACKEND ROUTING
    # ========================================================

    def _local_handle(self, method, path, body=None):
        """Map /api/* → kiosk_local_backend. Return (status, data) or None if not local."""
        if not LOCAL_OK or LOCAL_BACKEND is None:
            return None
        body = body or {}
        m = (method or "GET").upper()
        path = (path or "").split("?")[0]

        try:
            if path == "/api/pin_login" and m == "POST":
                return 200, LOCAL_BACKEND.pin_login(body.get("pin") or "")
            if path == "/api/rfid_auth" and m == "POST":
                return 200, LOCAL_BACKEND.rfid_auth(body.get("rfid_uid") or "")
            if path == "/api/fingerprint_auth" and m == "POST":
                return 200, LOCAL_BACKEND.fingerprint_auth(
                    body.get("fingerprint_id") or ""
                )
            if path == "/api/change_pin" and m == "POST":
                user = getattr(self, "logged_in_user", None) or body.get("username") or ""
                return 200, LOCAL_BACKEND.change_pin(
                    user, body.get("old_pin") or "", body.get("new_pin") or "",
                )
            if path == "/api/door_status" and m == "GET":
                return 200, LOCAL_BACKEND.door_status()
            if path == "/api/door_control" and m == "POST":
                return 200, LOCAL_BACKEND.door_control(body.get("action") or "")
            if path == "/api/doorbell" and m == "POST":
                return 200, LOCAL_BACKEND.doorbell(body.get("by") or "kiosk")
            if path in ("/api/fire_alert", "/api/smoke_alert") and m == "POST":
                return 200, LOCAL_BACKEND.fire_alert(
                    source=body.get("source") or "esp32",
                    sensor=body.get("sensor") or "mq2",
                    value=body.get("value"),
                )
            if path == "/api/esp32" and m == "POST":
                return 200, LOCAL_BACKEND.esp32_event(
                    body.get("kind") or body.get("type") or "", body,
                )
            if path == "/api/safety_bolt" and m == "GET":
                return 200, LOCAL_BACKEND.get_safety_bolt()
            if path == "/api/safety_bolt" and m == "POST":
                en = body.get("enabled")
                if en is None:
                    en = body.get("action") in ("on", "safety_on", True, 1, "1")
                return 200, LOCAL_BACKEND.set_safety_bolt(
                    bool(en), by=body.get("by") or "kiosk"
                )
            if path == "/api/system_health" and m == "GET":
                return 200, LOCAL_BACKEND.system_health()
            if path == "/api/access_mode" and m == "GET":
                return 200, LOCAL_BACKEND.get_access_mode()
            if path == "/api/access_mode" and m == "POST":
                return 200, LOCAL_BACKEND.set_access_mode(
                    body.get("mode") or "",
                    body.get("changed_by") or getattr(self, "logged_in_user", None) or "kiosk",
                )
            if path == "/api/login_log" and m == "GET":
                return 200, LOCAL_BACKEND.login_log()
            if path == "/api/security_alerts" and m == "GET":
                return 200, LOCAL_BACKEND.security_alerts()
            if path == "/api/mfa/start" and m == "POST":
                methods = body.get("methods") or body.get("selected_methods") or []
                return 200, LOCAL_BACKEND.mfa_start(methods)
            if path == "/api/mfa/status" and m == "GET":
                # query param handled by caller via body/session
                return 200, LOCAL_BACKEND.mfa_status(body.get("session_id") or "")
            if path == "/api/mfa/request_otp" and m == "POST":
                return 200, LOCAL_BACKEND.mfa_request_otp(
                    body.get("session_id") or "", body.get("username") or "",
                )
            if path == "/api/mfa/verify" and m == "POST":
                method_name = body.get("method") or ""
                payload = {
                    k: v for k, v in body.items()
                    if k not in ("session_id", "method")
                }
                return 200, LOCAL_BACKEND.mfa_verify(
                    body.get("session_id") or "", method_name, payload,
                )
            if path == "/api/mfa/cancel" and m == "POST":
                return 200, LOCAL_BACKEND.mfa_cancel(body.get("session_id") or "")
            if path == "/api/logout" and m == "POST":
                return 200, {"ok": True}
            if path == "/api/telegram/test":
                return 200, LOCAL_BACKEND.telegram_test()
            # Fingerprint / RFID status polling — idle local (ESP32 can POST auth)
            if path == "/api/fingerprint_status" and m == "GET":
                return 200, LOCAL_BACKEND.fingerprint_status()
            if path == "/api/rfid_status" and m == "GET":
                return 200, LOCAL_BACKEND.rfid_status()
            if path == "/api/scan_status" and m == "GET":
                return 200, LOCAL_BACKEND.scan_status()
            if path == "/api/faces" and m == "GET":
                return 200, LOCAL_BACKEND.face_list()
            if path == "/api/face_db_info" and m == "GET":
                return 200, LOCAL_BACKEND.face_db_info()
            if path == "/api/face_delete" and m == "POST":
                return 200, LOCAL_BACKEND.delete_face(body.get("name") or "")
            if path == "/api/face_clear" and m == "POST":
                return 200, LOCAL_BACKEND.clear_face_db()
            if path == "/api/face_purge" and m == "POST":
                return 200, LOCAL_BACKEND.purge_incompatible_faces()
            if path == "/api/register_face" and m == "POST":
                # frame optional — UI gọi face_register trực tiếp với numpy
                return 200, {"ok": False, "message": "Dùng API local face_register với frame"}
            if path == "/api/system_status" and m == "GET":
                d = LOCAL_BACKEND.door_status()
                fl = LOCAL_BACKEND.face_list()
                return 200, {
                    "mq2": "N/A (local)",
                    "network": "Kiosk standalone",
                    "locked": d.get("locked"),
                    "source": "local",
                    "faces": fl.get("faces", {}),
                    "deepface_ready": fl.get("deepface_ready"),
                }
        except Exception as e:
            return 500, {"ok": False, "success": False, "message": str(e)}
        return None

    def _request_json(self, method, url, **kwargs):
        """
        STANDALONE=1 → luôn local backend cho /api/*.
        STANDALONE=0 → thử server; nếu fail thì fallback local.
        """
        body = kwargs.get("json") if isinstance(kwargs.get("json"), dict) else {}
        # Parse path from full URL
        path = url
        if "://" in url:
            try:
                from urllib.parse import urlparse
                path = urlparse(url).path or "/"
            except Exception:
                path = url.split(SERVER_URL)[-1] if SERVER_URL in url else url

        use_local = STANDALONE and LOCAL_OK
        if use_local:
            handled = self._local_handle(method, path, body)
            if handled is not None:
                return handled

        # Optional server call
        try:
            response = self.http.request(
                method, url, timeout=kwargs.pop("timeout", 6), **kwargs,
            )
            try:
                data = response.json()
            except Exception:
                data = {}
            return response.status_code, data
        except Exception as exc:
            # Fallback local when server unreachable
            if LOCAL_OK:
                handled = self._local_handle(method, path, body)
                if handled is not None:
                    return handled
            return 0, {"success": False, "ok": False, "message": str(exc)}

    # ========================================================
    # ACCESS MODE — single | mfa | open
    # ========================================================

    def _load_access_mode_and_home(self):
        """Đọc chế độ đã lưu rồi mở đúng màn hình home."""
        def worker():
            status, data = self._request_json(
                "GET", f"{SERVER_URL}/api/access_mode", timeout=3,
            )

            def update():
                if status == 200 and data.get("ok"):
                    self.access_mode = data.get("mode") or "mfa"
                    self.access_mode_label = data.get("label") or self.access_mode
                self._go_home_by_mode()

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _go_home_by_mode(self):
        """Home theo access_mode hiện tại."""
        mode = getattr(self, "access_mode", "mfa") or "mfa"
        if mode == "open":
            self._transition_to(self._show_open_mode)
        elif mode == "single":
            self._transition_to(self._show_single_auth)
        else:
            self._transition_to(self._show_auth_methods)

    def _back_from_auth(self):
        """Quay lại đúng màn chọn phương thức theo chế độ (single/mfa)."""
        if getattr(self, "mfa_mode", False):
            self._transition_to(self._show_auth_methods)
        elif getattr(self, "access_mode", "mfa") == "single":
            self._transition_to(self._show_single_auth)
        else:
            self._transition_to(self._show_auth_methods)

    def _show_access_mode_settings(self):
        """Màn hình chọn: 1 lần / 2 lần / không cần đăng nhập."""
        # Bat buoc dang nhap moi duoc doi che do
        if not self.logged_in_user:
            self._show_unlock_success_popup(
                title="CẦN ĐĂNG NHẬP",
                subtitle="Đổi chế độ truy cập chỉ dùng sau khi xác thực thành công",
            )
            self.root.after(400, self._go_home_by_mode)
            return

        self._clear_content()
        self.current_screen = "access_mode"
        self._hide_camera()

        self._label(self.content_frame, "CHẾ ĐỘ TRUY CẬP", 10, PRIMARY, True).pack(
            anchor="w", pady=(4, 0),
        )
        self._label(
            self.content_frame,
            "Chọn cách mở cửa trên Kiosk",
            20, TEXT, True,
        ).pack(anchor="w", pady=(2, 4))
        self._label(
            self.content_frame,
            "Cài đặt được lưu lại. Lần sau mở app sẽ dùng chế độ đã chọn.",
            11, DIM,
        ).pack(anchor="w", pady=(0, 10))

        current = getattr(self, "access_mode", "mfa")
        options = [
            (
                "single",
                "①  ĐĂNG NHẬP 1 LẦN",
                "Chỉ cần 1 phương thức (PIN hoặc thẻ hoặc vân tay…)\nrồi mở cửa.",
                PRIMARY,
            ),
            (
                "mfa",
                "②  ĐĂNG NHẬP 2 LẦN (MFA)",
                "Bắt buộc 2 phương thức thuộc 2 loại yếu tố khác nhau.\nAn toàn hơn.",
                PRIMARY2,
            ),
            (
                "open",
                "〇  KHÔNG CẦN ĐĂNG NHẬP",
                "Ai cũng bấm MỞ CỬA được — không xác thực.\nChỉ dùng khi tin tưởng môi trường.",
                WARN,
            ),
        ]

        self._mode_cards = {}
        for mid, title, desc, color in options:
            selected = current == mid
            card = tk.Frame(
                self.content_frame,
                bg=SELECT_BG if selected else CARD,
                highlightthickness=2 if selected else 1,
                highlightbackground=SELECT_BD if selected else BORDER,
                cursor="hand2",
            )
            card.pack(fill="x", pady=6, padx=4)
            row = tk.Frame(card, bg=card.cget("bg"))
            row.pack(fill="x", padx=16, pady=14)
            tk.Label(
                row, text=title, font=("Arial", 15, "bold"),
                bg=row.cget("bg"), fg=SUCCESS if selected else color, anchor="w",
            ).pack(anchor="w")
            tk.Label(
                row, text=desc, font=("Arial", 11),
                bg=row.cget("bg"), fg=DIM, anchor="w", justify="left",
            ).pack(anchor="w", pady=(4, 0))
            if selected:
                tk.Label(
                    row, text="✓  ĐANG DÙNG", font=("Arial", 10, "bold"),
                    bg=row.cget("bg"), fg=SUCCESS, anchor="w",
                ).pack(anchor="w", pady=(6, 0))

            def _pick(event=None, m=mid):
                self._apply_access_mode(m)

            for w in (card, row):
                w.bind("<Button-1>", _pick)
            for child in row.winfo_children():
                child.bind("<Button-1>", _pick)
            self._mode_cards[mid] = card

        self.mode_result = self._label(self.content_frame, "", 12, DIM, True)
        self.mode_result.pack(pady=8)

        bottom = tk.Frame(self.content_frame, bg=BG)
        bottom.pack(fill="x", pady=8)
        self._button(
            bottom, "← QUAY LẠI CONTROL",
            lambda: self._transition_to(
                self._show_control_panel, self.logged_in_user
            ),
            "ghost", width=22, height=1,
        ).pack(side="left")

    def _apply_access_mode(self, mode):
        self.mode_result.config(text="Đang lưu…", fg=WARN)

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/access_mode",
                json={
                    "mode": mode,
                    "changed_by": self.logged_in_user or "kiosk",
                },
                timeout=4,
            )

            def update():
                if status == 200 and data.get("ok"):
                    self.access_mode = data.get("mode") or mode
                    self.access_mode_label = data.get("label") or mode
                    self.mode_result.config(
                        text=f"✓ {data.get('message', 'Đã lưu')}",
                        fg=SUCCESS,
                    )
                    # Sau khi doi mode: ve Control Panel (da dang nhap)
                    self.root.after(
                        1200,
                        lambda: self._transition_to(
                            self._show_control_panel, self.logged_in_user
                        ),
                    )
                else:
                    self.mode_result.config(
                        text=f"✕ {data.get('message', 'Lưu thất bại')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_open_mode(self):
        """Không cần đăng nhập — bấm mở cửa trực tiếp."""
        self._clear_content()
        self.current_screen = "open_mode"
        self._hide_camera()

        self._label(self.content_frame, "CHẾ ĐỘ MỞ TỰ DO", 10, WARN, True).pack(
            pady=(8, 2),
        )
        self._label(
            self.content_frame,
            "Không cần đăng nhập",
            22, TEXT, True,
        ).pack(pady=4)
        self._label(
            self.content_frame,
            "Bấm nút bên dưới để mở cửa. Ai cũng dùng được.",
            12, DIM,
        ).pack(pady=(0, 12))

        warn = tk.Frame(
            self.content_frame, bg=WARN_BG,
            highlightthickness=1, highlightbackground=WARN,
        )
        warn.pack(fill="x", padx=24, pady=8)
        tk.Label(
            warn,
            text="⚠  Chế độ này không xác thực người dùng. Chỉ bật khi môi trường an toàn.",
            font=("Arial", 11), bg=WARN_BG, fg=WARN, wraplength=640, justify="center",
        ).pack(padx=14, pady=12)

        self.open_mode_icon = tk.Canvas(
            self.content_frame, width=80, height=80, bg=BG, highlightthickness=0,
        )
        self.open_mode_icon.pack(pady=10)
        Icons.lock_closed(self.open_mode_icon, DANGER, 80)

        self.open_mode_msg = self._label(
            self.content_frame, "Cửa đang khóa", 14, DIM, True,
        )
        self.open_mode_msg.pack(pady=4)

        self._button(
            self.content_frame, "🔓  MỞ CỬA NGAY",
            self._open_mode_unlock,
            "success", width=22, height=2,
        ).pack(pady=12)
        self._button(
            self.content_frame, "🔒  KHÓA LẠI",
            self._open_mode_lock,
            "danger", width=16, height=1,
        ).pack(pady=4)

        # Chuong ngoai — khach bam khong can dang nhap
        self._pack_public_doorbell(self.content_frame)
        self._poll_door_status_open_mode()

    def _open_mode_unlock(self):
        self.open_mode_msg.config(text="Đang mở…", fg=WARN)

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/door_control",
                json={"action": "unlock"}, timeout=6,
            )

            def update():
                if status == 200 and (data.get("ok") or data.get("success")):
                    self.open_mode_msg.config(text="✓ Đã mở cửa", fg=SUCCESS)
                    try:
                        Icons.lock_open(self.open_mode_icon, SUCCESS, 80)
                        self.anim.ring_expand(self.open_mode_icon, SUCCESS, 6, 80)
                    except Exception:
                        pass
                    self._show_unlock_success_popup(
                        title="ACCESS GRANTED",
                        subtitle="Chế độ không cần đăng nhập",
                    )
                else:
                    self.open_mode_msg.config(
                        text=data.get("message", "Mở cửa thất bại"),
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _open_mode_lock(self):
        def worker():
            self._request_json(
                "POST", f"{SERVER_URL}/api/door_control",
                json={"action": "lock"}, timeout=6,
            )

            def update():
                try:
                    Icons.lock_closed(self.open_mode_icon, DANGER, 80)
                    self.open_mode_msg.config(text="Đã khóa lại", fg=DANGER)
                except Exception:
                    pass

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _poll_door_status_open_mode(self):
        if self.current_screen != "open_mode":
            return

        def worker():
            status, data = self._request_json(
                "GET", f"{SERVER_URL}/api/door_status", timeout=4,
            )
            if status == 200:
                self.last_door_data = data

            def update():
                if self.current_screen != "open_mode":
                    return
                locked = self.last_door_data.get("locked", True)
                try:
                    if locked:
                        Icons.lock_closed(self.open_mode_icon, DANGER, 80)
                        rem = ""
                    else:
                        Icons.lock_open(self.open_mode_icon, SUCCESS, 80)
                        ar = self.last_door_data.get("auto_lock_remaining", 0)
                        rem = f" · tự khóa sau {ar}s" if ar else ""
                    self.open_mode_msg.config(
                        text=("Cửa đang khóa" if locked else "Cửa đang mở") + rem,
                        fg=DANGER if locked else SUCCESS,
                    )
                except Exception:
                    pass
                self.status_job = self.root.after(
                    STATUS_INTERVAL_MS, self._poll_door_status_open_mode,
                )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_single_auth(self):
        """Đăng nhập 1 lần — chọn đúng 1 phương thức rồi xác thực."""
        self._clear_content()
        self.current_screen = "single_select"
        self._hide_camera()
        self.mfa_mode = False
        self.mfa_session_id = None
        self.single_selected_method = None

        hero = tk.Frame(self.content_frame, bg=BG)
        hero.pack(fill="x", pady=(2, 6))
        self._label(hero, "ĐĂNG NHẬP 1 LẦN", 10, PRIMARY, True).pack(anchor="w")
        self._label(
            hero, "Chọn 1 phương thức để mở cửa", 20, TEXT, True,
        ).pack(anchor="w", pady=(2, 0))
        self._label(
            hero,
            f"Chế độ hiện tại: {getattr(self, 'access_mode_label', 'single')}",
            11, DIM,
        ).pack(anchor="w", pady=(2, 0))

        grid = tk.Frame(self.content_frame, bg=BG)
        grid.pack(fill="both", expand=True, pady=4)

        methods = [
            ("pin", "PIN", "Mã PIN cá nhân", Icons.pin, WARN),
            ("rfid", "RFID", "Quét thẻ RFID", Icons.rfid, PRIMARY2),
            ("fingerprint", "Vân tay", "Cảm biến vân tay", Icons.fingerprint, SUCCESS),
            ("face", "Face ID", "Nhận diện khuôn mặt", Icons.face, PRIMARY),
            ("telegram_otp", "Telegram OTP", "Mã OTP qua Telegram", Icons.telegram, PRIMARY),
        ]
        for idx, (mid, title, sub, icon_fn, color) in enumerate(methods):
            r, c = divmod(idx, 3)
            card = tk.Frame(
                grid, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                cursor="hand2",
            )
            card.grid(row=r, column=c, padx=7, pady=7, sticky="nsew")
            grid.grid_columnconfigure(c, weight=1, minsize=200)
            grid.grid_rowconfigure(r, weight=1, minsize=110)

            box = tk.Frame(card, bg=CARD)
            box.pack(fill="both", expand=True, padx=12, pady=12)
            ic = tk.Frame(box, bg=ICON_BG, width=48, height=48)
            ic.pack(side="left", padx=(0, 12))
            ic.pack_propagate(False)
            cv = tk.Canvas(ic, width=44, height=44, bg=ICON_BG, highlightthickness=0)
            cv.pack(expand=True)
            icon_fn(cv, color, 44)
            col = tk.Frame(box, bg=CARD)
            col.pack(side="left", fill="both", expand=True)
            tk.Label(
                col, text=title, font=("Arial", 13, "bold"),
                bg=CARD, fg=TEXT, anchor="w",
            ).pack(anchor="w")
            tk.Label(
                col, text=sub, font=("Arial", 10),
                bg=CARD, fg=DIM, anchor="w",
            ).pack(anchor="w", pady=(2, 0))

            def _go(event=None, m=mid):
                self._start_single_method(m)

            for w in (card, box, ic, cv, col):
                w.bind("<Button-1>", _go)
            for child in col.winfo_children():
                child.bind("<Button-1>", _go)

        # Doi che do truy cap chi co SAU khi dang nhap (Control Panel)
        self._pack_public_doorbell(self.content_frame)

    def _start_single_method(self, method):
        """Chạy đúng 1 bước xác thực (không MFA session)."""
        self.mfa_mode = False
        self.mfa_session_id = None
        self.single_selected_method = method
        self.mfa_step_info = "Đăng nhập 1 lần"
        if method == "pin":
            self._transition_to(self._show_pin)
        elif method == "rfid":
            self._transition_to(self._show_rfid)
        elif method == "fingerprint":
            self._transition_to(self._show_fingerprint)
        elif method == "face":
            self._transition_to(self._start_local_face)
        elif method == "telegram_otp":
            # OTP đơn: tạo MFA session 1 bước giả — dùng request OTP + verify
            # Đơn giản hơn: hiện màn OTP standalone
            self._transition_to(self._show_single_telegram_otp)
        else:
            self._show_single_auth()

    def _show_single_telegram_otp(self):
        """OTP 1 lần (không cần MFA 2 bước)."""
        self._clear_content()
        self.current_screen = "single_otp"
        self._hide_camera()
        self._single_otp_session_id = None

        self._label(self.content_frame, "ĐĂNG NHẬP 1 LẦN", 10, PRIMARY, True).pack(
            pady=(6, 2),
        )
        self.otp_canvas = tk.Canvas(
            self.content_frame, width=64, height=64, bg=BG, highlightthickness=0,
        )
        self.otp_canvas.pack(pady=6)
        Icons.telegram(self.otp_canvas, PRIMARY, 64)

        self._label(self.content_frame, "TELEGRAM OTP", 20, TEXT, True).pack()
        self._label(
            self.content_frame, "Nhận mã 6 số qua Telegram rồi nhập bên dưới", 11, DIM,
        ).pack(pady=(0, 8))

        self.single_otp_status = self._label(
            self.content_frame, "Đang gửi mã OTP…", 12, WARN, True,
        )
        self.single_otp_status.pack(pady=4)

        self._button(
            self.content_frame, "GỬI LẠI MÃ OTP",
            self._single_request_otp,
            "primary", width=16, height=1,
        ).pack(pady=6)

        entry_box = self._card(self.content_frame)
        entry_box.pack(pady=6)
        self.single_otp_entry = tk.Entry(
            entry_box, font=("Arial", 20), width=10, justify="center",
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.single_otp_entry.pack(padx=14, pady=10, ipady=6)
        try:
            self.single_otp_entry.focus_set()
        except Exception:
            pass

        self._button(
            self.content_frame, "XÁC THỰC",
            self._single_verify_otp,
            "success", width=16, height=1,
        ).pack(pady=8)
        self._button(
            self.content_frame, "← QUAY LẠI",
            lambda: self._transition_to(self._show_single_auth),
            "ghost", width=14, height=1,
        ).pack(pady=4)

        # Tự động gửi OTP ngay khi vào màn hình (không cần bấm trước)
        self.root.after(400, self._single_request_otp)

    def _single_request_otp(self):
        """Gửi OTP cho chế độ Đăng nhập 1 lần."""
        try:
            self.single_otp_status.config(text="Đang gửi mã OTP…", fg=WARN)
        except Exception:
            pass

        def worker():
            # Tạo session MFA (telegram_otp + pin) rồi chỉ dùng bước OTP
            st, data = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/start",
                json={"methods": ["telegram_otp", "pin"]}, timeout=8,
            )
            if not (st == 200 and data.get("ok")):
                msg = (data or {}).get("message", "Không tạo được phiên OTP")
                self.root.after(0, lambda: self.single_otp_status.config(
                    text=f"✕ {msg}", fg=DANGER,
                ))
                return

            sess = data.get("session") or data or {}
            # Hỗ trợ nhiều tên key khác nhau từ backend
            sid = (
                sess.get("mfa_session_id")
                or sess.get("session_id")
                or data.get("mfa_session_id")
                or data.get("session_id")
            )
            if not sid:
                self.root.after(0, lambda: self.single_otp_status.config(
                    text="✕ Backend không trả session_id", fg=DANGER,
                ))
                return

            self._single_otp_session_id = sid

            st2, data2 = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/request_otp",
                json={"session_id": sid}, timeout=15,
            )

            def update():
                if st2 == 200 and (data2 or {}).get("ok"):
                    self.single_otp_status.config(
                        text="✓ Đã gửi OTP — kiểm tra Telegram rồi nhập mã",
                        fg=SUCCESS,
                    )
                    try:
                        self.anim.telegram_send(self.otp_canvas, PRIMARY, 64)
                    except Exception:
                        pass
                else:
                    msg = (data2 or {}).get("message", "Gửi OTP thất bại")
                    # Vẫn giữ session_id để user có thể thử lại / dùng mã console
                    self.single_otp_status.config(text=f"⚠ {msg}", fg=WARN)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _single_verify_otp(self):
        """Xác thực OTP ở chế độ Đăng nhập 1 lần → OTP đúng là mở cửa."""
        otp = (self.single_otp_entry.get() or "").strip()
        sid = getattr(self, "_single_otp_session_id", None)

        if not otp:
            self.single_otp_status.config(text="Nhập mã OTP 6 số", fg=WARN)
            return

        if not sid:
            # Tự động gửi lại nếu chưa có session
            self.single_otp_status.config(
                text="Chưa có phiên — đang gửi OTP lại…", fg=WARN,
            )
            self._single_request_otp()
            return

        self.single_otp_status.config(text="Đang xác thực…", fg=WARN)

        def worker():
            st, data = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/verify",
                json={"session_id": sid, "method": "telegram_otp", "otp": otp},
                timeout=10,
            )
            data = data or {}

            def update():
                # Coi là thành công khi:
                # - ok = True
                # - hoặc status chứa VERIFIED / SUCCESS
                # - hoặc message không nói "sai" / "fail"
                ok = data.get("ok") is True
                status_str = str(data.get("status", "")).upper()
                msg = str(data.get("message", "")).lower()

                factor_ok = (
                    ok
                    or "VERIFIED" in status_str
                    or "SUCCESS" in status_str
                    or "GRANTED" in status_str
                    or (st == 200 and "sai" not in msg and "fail" not in msg
                        and "invalid" not in msg and "expired" not in msg
                        and "hết hạn" not in msg and data.get("ok") is not False)
                )

                if factor_ok:
                    # Single mode: OTP đúng là đủ → mở cửa ngay
                    try:
                        self._request_json(
                            "POST", f"{SERVER_URL}/api/door_control",
                            json={"action": "unlock"}, timeout=6,
                        )
                    except Exception:
                        pass
                    name = data.get("user_name") or data.get("username") or "admin"
                    self.single_otp_status.config(
                        text=f"✓ ACCESS GRANTED — {name}",
                        fg=SUCCESS,
                    )
                    try:
                        self._show_unlock_success_popup(
                            title="ACCESS GRANTED",
                            subtitle="Telegram OTP (1 lần)",
                        )
                    except Exception:
                        pass
                    self.root.after(
                        1500,
                        lambda: self._transition_to(self._show_control_panel, name),
                    )
                else:
                    err = data.get("message") or "OTP sai hoặc đã hết hạn"
                    self.single_otp_status.config(text=f"✕ {err}", fg=DANGER)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # MFA — factor types must differ
    # ========================================================

    MFA_FACTOR_TYPE = {
        "telegram_otp": "HAVE",
        "rfid": "HAVE",
        "pin": "KNOW",
        "face": "ARE",
        "fingerprint": "ARE",
    }
    MFA_TYPE_LABEL = {
        "HAVE": "Something you have",
        "KNOW": "Something you know",
        "ARE": "Something you are",
    }
    MFA_METHOD_META = {
        "telegram_otp": ("Telegram OTP", "Mã OTP qua Telegram", "HAVE"),
        "rfid": ("RFID Card", "Quét thẻ RFID", "HAVE"),
        "pin": ("PIN", "Mã PIN cá nhân", "KNOW"),
        "face": ("Face ID", "Nhận diện khuôn mặt", "ARE"),
        "fingerprint": ("Fingerprint", "Vân tay sinh trắc", "ARE"),
    }
    MFA_ICON_FN = {
        "telegram_otp": Icons.telegram,
        "rfid": Icons.rfid,
        "pin": Icons.pin,
        "face": Icons.face,
        "fingerprint": Icons.fingerprint,
    }

    def _show_auth_methods(self):
        self._clear_content()
        self.current_screen = "mfa_select"
        self._hide_camera()
        self.mfa_selected = []
        self.mfa_session_id = None
        self.mfa_session = None
        self.mfa_vars = {}
        self.mfa_mode = False
        self._mfa_filter = None          # None | HAVE | KNOW | ARE
        self._mfa_card_refs = {}         # mid -> card widget meta
        self._factor_chip_refs = {}      # type -> (chip_frame, label)

        # Hero row
        hero = tk.Frame(self.content_frame, bg=BG)
        hero.pack(fill="x", pady=(2, 4))
        left = tk.Frame(hero, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        self._label(left, "SECURE ENTRY", 10, PRIMARY, True).pack(anchor="w")
        self._label(left, "Xác thực đa yếu tố", 22, TEXT, True).pack(anchor="w", pady=(2, 0))
        self._label(
            left,
            "Chọn đúng 2 phương thức thuộc 2 loại yếu tố khác nhau, rồi bấm TIẾP TỤC.",
            11, DIM,
        ).pack(anchor="w", pady=(2, 0))

        # Progress pill 0/2
        prog = tk.Frame(hero, bg=CARD2, highlightthickness=1, highlightbackground=BORDER)
        prog.pack(side="right", padx=8, pady=4)
        self.mfa_count_label = tk.Label(
            prog, text="○  0 / 2", font=("Arial", 14, "bold"),
            bg=CARD2, fg=DIM, padx=18, pady=12,
        )
        self.mfa_count_label.pack()

        self.mfa_error_label = self._label(self.content_frame, "", 11, DANGER, True)
        self.mfa_error_label.pack(pady=(0, 2))

        # Factor filter pills — clickable
        hint = tk.Frame(self.content_frame, bg=BG)
        hint.pack(fill="x", pady=(2, 10))
        self._label(hint, "Lọc theo loại:", 9, DIM2).pack(side="left", padx=(0, 10))
        for lab in ("HAVE", "KNOW", "ARE"):
            chip = tk.Frame(
                hint, bg=CARD2, highlightthickness=1, highlightbackground=BORDER,
                cursor="hand2",
            )
            chip.pack(side="left", padx=(0, 8))
            lbl = tk.Label(
                chip, text=lab, font=("Arial", 10, "bold"),
                bg=CARD2, fg=DIM, padx=14, pady=6, cursor="hand2",
            )
            lbl.pack()
            self._factor_chip_refs[lab] = (chip, lbl)

            def _on_filter(event=None, t=lab):
                self._mfa_apply_filter(t)

            chip.bind("<Button-1>", _on_filter)
            lbl.bind("<Button-1>", _on_filter)

        # "Tất cả" reset filter
        all_chip = tk.Frame(
            hint, bg=CARD2, highlightthickness=1, highlightbackground=BORDER,
            cursor="hand2",
        )
        all_chip.pack(side="left", padx=(4, 0))
        all_lbl = tk.Label(
            all_chip, text="TẤT CẢ", font=("Arial", 10, "bold"),
            bg=CARD2, fg=DIM, padx=12, pady=6, cursor="hand2",
        )
        all_lbl.pack()
        self._factor_chip_refs["ALL"] = (all_chip, all_lbl)

        def _on_all(_=None):
            self._mfa_apply_filter(None)

        all_chip.bind("<Button-1>", _on_all)
        all_lbl.bind("<Button-1>", _on_all)

        # Grid of 5 method cards
        grid = tk.Frame(self.content_frame, bg=BG)
        grid.pack(fill="both", expand=True, pady=2)

        methods = ["telegram_otp", "rfid", "pin", "face", "fingerprint"]
        colors = [PRIMARY, PRIMARY2, WARN, PRIMARY, SUCCESS]
        for idx, mid in enumerate(methods):
            r, c = divmod(idx, 3)
            card = self._build_method_card(grid, mid, colors[idx])
            card.grid(row=r, column=c, padx=7, pady=7, sticky="nsew")
            grid.grid_columnconfigure(c, weight=1, minsize=210)
            grid.grid_rowconfigure(r, weight=1, minsize=120)

        # Continue
        bottom = tk.Frame(self.content_frame, bg=BG)
        bottom.pack(fill="x", pady=(12, 2))
        self.mfa_continue_btn = self._button(
            bottom, "TIẾP TỤC XÁC THỰC  →", self._mfa_continue,
            "primary", width=26, height=2,
        )
        self.mfa_continue_btn.pack(side="left", padx=(0, 10))
        try:
            self.mfa_continue_btn.config(state="disabled")
        except Exception:
            pass

        # Doi che do truy cap chi co SAU khi dang nhap (Control Panel)
        self._button(
            bottom, "🔔 CHUÔNG",
            self._ring_doorbell,
            "warn", width=12, height=2,
        ).pack(side="left", padx=(0, 8))

        if self.logged_in_user:
            self._button(
                bottom, "← QUAY LẠI",
                lambda: self._transition_to(self._show_control_panel),
                "ghost", width=12, height=2,
            ).pack(side="left")

        # Default: highlight ALL as active filter
        self._mfa_apply_filter(None)

    def _mfa_apply_filter(self, factor_type):
        """Highlight factor pill + dim non-matching cards. None = show all."""
        self._mfa_filter = factor_type
        active = {"HAVE": PRIMARY, "KNOW": PRIMARY2, "ARE": SUCCESS}

        for key, (chip, lbl) in getattr(self, "_factor_chip_refs", {}).items():
            if key == "ALL":
                on = factor_type is None
                chip.config(
                    highlightbackground=PRIMARY if on else BORDER,
                    highlightthickness=2 if on else 1,
                    bg=SELECT_BG if on else CARD2,
                )
                lbl.config(bg=SELECT_BG if on else CARD2, fg=TEXT if on else DIM)
            else:
                on = factor_type == key
                ac = active.get(key, PRIMARY)
                chip.config(
                    highlightbackground=ac if on else BORDER,
                    highlightthickness=2 if on else 1,
                    bg=SELECT_BG if on else CARD2,
                )
                lbl.config(bg=SELECT_BG if on else CARD2, fg=ac if on else DIM)

        # Dim / restore method cards
        for mid, meta in getattr(self, "_mfa_card_refs", {}).items():
            ftype = self.MFA_FACTOR_TYPE.get(mid)
            chosen = self.mfa_vars.get(mid) and self.mfa_vars[mid].get()
            card = meta["card"]
            match = factor_type is None or ftype == factor_type
            if chosen:
                # keep selected look
                card.config(highlightbackground=SELECT_BD, highlightthickness=2)
            elif match:
                card.config(highlightbackground=BORDER, highlightthickness=1)
                try:
                    card.config(bg=CARD)
                except Exception:
                    pass
            else:
                # dim non-matching
                card.config(highlightbackground="#0A1A30", highlightthickness=1)

    def _build_method_card(self, parent, mid, color):
        """Horizontal dark glass card matching reference layout."""
        title, sub, ftype = self.MFA_METHOD_META[mid]
        var = tk.BooleanVar(value=False)
        self.mfa_vars[mid] = var

        card = tk.Frame(
            parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
            cursor="hand2",
        )

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="both", expand=True, padx=14, pady=14)

        # Icon well — colored soft background
        icon_box = tk.Frame(
            row, bg=ICON_BG, width=56, height=56,
            highlightthickness=1, highlightbackground=BORDER,
        )
        icon_box.pack(side="left", padx=(0, 14))
        icon_box.pack_propagate(False)
        canvas = tk.Canvas(icon_box, width=48, height=48, bg=ICON_BG, highlightthickness=0)
        canvas.pack(expand=True)
        self.MFA_ICON_FN[mid](canvas, color, 48)

        # Text column
        col = tk.Frame(row, bg=CARD)
        col.pack(side="left", fill="both", expand=True)

        title_lbl = tk.Label(
            col, text=title, font=("Arial", 14, "bold"),
            bg=CARD, fg=TEXT, anchor="w",
        )
        title_lbl.pack(anchor="w")
        sub_lbl = tk.Label(
            col, text=sub, font=("Arial", 10),
            bg=CARD, fg=DIM, anchor="w",
        )
        sub_lbl.pack(anchor="w", pady=(2, 6))

        # Factor badge
        ftype_colors = {"HAVE": PRIMARY, "KNOW": PRIMARY2, "ARE": SUCCESS}
        ffg = ftype_colors.get(ftype, DIM)
        pill = tk.Frame(col, bg=ICON_BG, highlightthickness=1, highlightbackground=BORDER)
        pill.pack(anchor="w")
        type_lbl = tk.Label(
            pill, text=ftype, font=("Arial", 8, "bold"),
            bg=ICON_BG, fg=ffg, padx=10, pady=2,
        )
        type_lbl.pack()

        state = tk.Label(
            col, text="○  CHƯA CHỌN", font=("Arial", 10, "bold"),
            bg=CARD, fg=DIM, anchor="w",
        )
        state.pack(anchor="w", pady=(8, 0))

        # Store refs for filter + visual update
        self._mfa_card_refs[mid] = {
            "card": card, "row": row, "col": col, "icon_box": icon_box,
            "canvas": canvas, "state": state, "color": color,
            "pill": pill, "type_lbl": type_lbl, "title_lbl": title_lbl,
            "sub_lbl": sub_lbl, "ftype": ftype,
        }

        def _paint(chosen):
            bg = SELECT_BG if chosen else CARD
            ibg = SELECT_BG if chosen else ICON_BG
            card.config(
                highlightbackground=SELECT_BD if chosen else BORDER,
                highlightthickness=2 if chosen else 1,
                bg=bg,
            )
            for w in (row, col, title_lbl, sub_lbl, state):
                try:
                    w.config(bg=bg)
                except Exception:
                    pass
            try:
                icon_box.config(bg=ibg)
                canvas.config(bg=ibg)
            except Exception:
                pass
            state.config(
                text="✓  ĐÃ CHỌN" if chosen else "○  CHƯA CHỌN",
                fg=SUCCESS if chosen else DIM,
                bg=bg,
            )
            # redraw icon brighter when selected
            try:
                self.MFA_ICON_FN[mid](canvas, SUCCESS if chosen else color, 48)
            except Exception:
                pass

        def toggle(_=None):
            var.set(not var.get())
            self._mfa_on_toggle(mid)
            # re-apply visual for this card after validation may have unselected it
            _paint(var.get())
            # re-apply filter dimming
            if getattr(self, "_mfa_filter", None) is not None:
                self._mfa_apply_filter(self._mfa_filter)

        for w in (card, row, icon_box, canvas, col, state, type_lbl, pill, title_lbl, sub_lbl):
            w.bind("<Button-1>", toggle)
        return card

    def _mfa_on_toggle(self, method_id):
        selected = [m for m, v in self.mfa_vars.items() if v.get()]
        self.mfa_error_label.config(text="")

        if len(selected) > 2:
            self.mfa_vars[method_id].set(False)
            selected = [m for m, v in self.mfa_vars.items() if v.get()]
            self.mfa_error_label.config(text="Chỉ được chọn đúng 2 phương thức.")

        # Enforce different factor types
        if len(selected) == 2:
            t1 = self.MFA_FACTOR_TYPE.get(selected[0])
            t2 = self.MFA_FACTOR_TYPE.get(selected[1])
            if t1 == t2:
                self.mfa_vars[method_id].set(False)
                selected = [m for m, v in self.mfa_vars.items() if v.get()]
                self.mfa_error_label.config(
                    text=f"Hai phương thức phải thuộc 2 loại khác nhau (không cùng {t1})."
                )

        selected = [m for m, v in self.mfa_vars.items() if v.get()]
        self.mfa_selected = selected
        n = len(selected)
        mark = "●" if n == 2 else ("◐" if n == 1 else "○")
        self.mfa_count_label.config(
            text=f"{mark}  {n} / 2",
            fg=SUCCESS if n == 2 else (PRIMARY2 if n == 1 else DIM),
        )
        btn = getattr(self, "mfa_continue_btn", None)
        if btn is not None:
            try:
                if btn.winfo_exists():
                    btn.config(state="normal" if n == 2 else "disabled")
            except Exception:
                pass

    def _mfa_continue(self):
        selected = [m for m, v in self.mfa_vars.items() if v.get()]
        if len(selected) != 2:
            self.mfa_error_label.config(text="Vui lòng chọn đúng 2 phương thức.")
            return
        t1 = self.MFA_FACTOR_TYPE.get(selected[0])
        t2 = self.MFA_FACTOR_TYPE.get(selected[1])
        if t1 == t2:
            self.mfa_error_label.config(
                text="Hai phương thức phải thuộc 2 loại yếu tố khác nhau."
            )
            return

        self.mfa_error_label.config(text="Đang kiểm tra với máy chủ…", fg=WARN)

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/start",
                json={"methods": selected}, timeout=6,
            )

            def update():
                if not (status == 200 and data.get("ok") and data.get("valid")):
                    self.mfa_error_label.config(
                        text=data.get("message", "MFA không hợp lệ."),
                        fg=DANGER,
                    )
                    return
                self.mfa_session = data.get("session") or {}
                self.mfa_session_id = self.mfa_session.get("mfa_session_id")
                self._mfa_run_current_step()

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _mfa_run_current_step(self):
        sess = self.mfa_session or {}
        method = sess.get("current_method")
        step = sess.get("current_step", 1)
        total = sess.get("total_steps", 2)
        if not method:
            self._show_auth_methods()
            return
        self.mfa_mode = True
        self.mfa_step_info = f"Yếu tố {step}/{total}"

        if method == "pin":
            self._transition_to(self._show_pin)
        elif method == "rfid":
            self._transition_to(self._show_rfid)
        elif method == "fingerprint":
            self._transition_to(self._show_fingerprint)
        elif method == "face":
            self._transition_to(self._start_local_face)
        elif method == "telegram_otp":
            self._transition_to(self._show_telegram_otp)
        else:
            self._show_auth_methods()

    def _mfa_verify(self, method, payload: dict):
        body = {"session_id": self.mfa_session_id, "method": method}
        body.update(payload or {})

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/verify",
                json=body, timeout=8,
            )

            def update():
                if status == 200 and data.get("ok"):
                    self.mfa_session = data.get("session") or self.mfa_session
                    if data.get("mfa_success"):
                        name = data.get("user_name") or ""
                        self.logged_in_user = name
                        self.mfa_mode = False
                        self._show_unlock_success_popup(
                            title="ACCESS GRANTED",
                            subtitle=(
                                f"MFA 2/2 — Xin chào {name}!"
                                if name else "MFA thành công — Mời vào!"
                            ),
                        )
                        self.root.after(
                                500,
                            lambda: self._transition_to(self._show_control_panel, name),
                        )
                    else:
                        next_label = data.get("next_label") or "yếu tố tiếp theo"
                        self._show_unlock_success_popup(
                            title="YẾU TỐ 1/2 ĐÃ XÁC THỰC",
                            subtitle=f"Tiếp theo: {next_label}",
                        )
                        self.root.after(1200, self._mfa_run_current_step)
                else:
                    msg = data.get("message", "Xác thực thất bại.")
                    for attr in (
                        "pin_result", "rfid_status", "fingerprint_status",
                        "otp_result", "local_face_detail",
                    ):
                        w = getattr(self, attr, None)
                        if w is not None:
                            try:
                                if w.winfo_exists():
                                    w.config(text=f"✕ {msg}", fg=DANGER)
                            except Exception:
                                pass

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # TELEGRAM OTP
    # ========================================================

    def _show_telegram_otp(self):
        self._clear_content()
        self.current_screen = "telegram_otp"
        self._hide_camera()

        step = getattr(self, "mfa_step_info", "MFA")
        self._label(self.content_frame, step, 11, PRIMARY, True).pack(pady=(6, 2))

        # Icon
        ic = tk.Canvas(self.content_frame, width=64, height=64, bg=BG, highlightthickness=0)
        ic.pack(pady=4)
        Icons.telegram(ic, PRIMARY, 64)

        self._label(self.content_frame, "TELEGRAM OTP", 20, TEXT, True).pack(pady=2)
        self._label(
            self.content_frame,
            "Nhấn GỬI OTP — kiểm tra Telegram — nhập mã 6 số",
            11, DIM,
        ).pack(pady=(0, 8))

        self._button(
            self.content_frame, "GỬI OTP QUA TELEGRAM",
            self._mfa_request_otp, "primary", width=24, height=1,
        ).pack(pady=6)

        box = self._card(self.content_frame)
        box.pack(pady=8)
        self.otp_entry = tk.Entry(
            box, font=("Arial", 22), width=10, justify="center",
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.otp_entry.pack(padx=16, pady=12, ipady=8)

        self._button(
            self.content_frame, "XÁC THỰC OTP",
            self._mfa_submit_otp, "success", width=18, height=1,
        ).pack(pady=6)

        self.otp_result = self._label(self.content_frame, "", 12, DIM, True)
        self.otp_result.pack(pady=6)

        self._button(
            self.content_frame, "← HỦY MFA",
            self._back_from_auth,
            "ghost", width=14, height=1,
        ).pack(pady=8)

    def _mfa_request_otp(self):
        self.otp_result.config(text="Đang gửi OTP…", fg=WARN)
        self.anim.loading_dots(self.otp_result, "Đang gửi OTP", "loading")
        # Paper-plane send micro-anim on any telegram canvas in screen
        for w in self.content_frame.winfo_children():
            if isinstance(w, tk.Canvas):
                try:
                    self.anim.telegram_send(w, PRIMARY, 64)
                except Exception:
                    pass
                break

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/mfa/request_otp",
                json={"session_id": self.mfa_session_id}, timeout=10,
            )

            def update():
                self.anim.cancel("loading")
                if status == 200 and data.get("ok"):
                    self.otp_result.config(
                        text="✓ Đã gửi OTP qua Telegram (60 giây).",
                        fg=SUCCESS,
                    )
                else:
                    self.otp_result.config(
                        text=f"✕ {data.get('message', 'Gửi OTP thất bại')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _mfa_submit_otp(self):
        otp = self.otp_entry.get().strip()
        if not otp:
            self.otp_result.config(text="Vui lòng nhập mã OTP.", fg=WARN)
            return
        self.otp_result.config(text="Đang xác thực…", fg=WARN)
        self._mfa_verify("telegram_otp", {"otp": otp})

    # ========================================================
    # FACE RECOGNITION
    # ========================================================

    def _start_local_face(self):
        self._show_face_info_only()

    def _show_face_info_only(self):
        self._clear_content()
        self.current_screen = "face_local"
        self._prev_scan_state = "idle"
        self._last_recog_popup_time = 0
        self.last_scan_state = "idle"
        self.last_scan_name = None
        self._face_nav_done = False
        self._face_armed_at = time.time() + 0.4
        self._mfa_face_sent = False
        self._last_face_infer_at = 0.0
        self._show_camera()
        self._place_face_corners()

        # Header bar
        bar = self._card(self.content_frame)
        bar.pack(fill="x", pady=(8, 4), padx=20)
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=14, pady=10)

        ic = tk.Canvas(inner, width=40, height=40, bg=CARD, highlightthickness=0)
        ic.pack(side="left", padx=(0, 12))
        Icons.face(ic, PRIMARY, 40)
        self.face_icon_canvas = ic

        tb = tk.Frame(inner, bg=CARD)
        tb.pack(side="left", fill="x", expand=True)
        self.local_face_status = tk.Label(
            tb, text="VUI LÒNG NHÌN VÀO CAMERA",
            font=("Arial", 15, "bold"), bg=CARD, fg=TEXT, anchor="w",
        )
        self.local_face_status.pack(anchor="w")
        self.local_face_detail = tk.Label(
            tb, text="AI chạy trực tiếp trên Kiosk — nhìn thẳng camera",
            font=("Arial", 11), bg=CARD, fg=DIM, anchor="w",
        )
        self.local_face_detail.pack(anchor="w")

        self.face_lock_banner = tk.Label(
            self.content_frame,
            text="🔒  Đang kiểm tra trạng thái khóa…",
            font=("Arial", 13, "bold"),
            bg=CARD2, fg=DIM,
            highlightthickness=1, highlightbackground=BORDER,
            pady=10,
        )
        self.face_lock_banner.pack(fill="x", pady=(0, 6), padx=20)

        self._button(
            self.content_frame, "← QUAY LẠI",
            self._back_from_auth,
            "ghost", width=14, height=1,
        ).pack(pady=8)

        self._start_scan_animation()
        self._poll_scan_status()

    def _place_face_corners(self):
        """4 L-shaped corners over camera — does not block video feed."""
        # Remove old
        for c in getattr(self, "_corner_brackets", []):
            try:
                c.destroy()
            except Exception:
                pass
        self._corner_brackets = []
        size = 36
        positions = [
            (0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se"),
        ]
        for relx, rely, anchor in positions:
            c = tk.Canvas(
                self.camera_panel, width=size, height=size,
                bg=BLACK, highlightthickness=0,
            )
            # draw L
            t = 3
            col = PRIMARY2
            if anchor == "nw":
                c.create_line(2, 2, 2, size - 4, fill=col, width=t)
                c.create_line(2, 2, size - 4, 2, fill=col, width=t)
            elif anchor == "ne":
                c.create_line(size - 2, 2, size - 2, size - 4, fill=col, width=t)
                c.create_line(size - 2, 2, 4, 2, fill=col, width=t)
            elif anchor == "sw":
                c.create_line(2, size - 2, 2, 4, fill=col, width=t)
                c.create_line(2, size - 2, size - 4, size - 2, fill=col, width=t)
            else:
                c.create_line(size - 2, size - 2, size - 2, 4, fill=col, width=t)
                c.create_line(size - 2, size - 2, 4, size - 2, fill=col, width=t)
            c.place(relx=relx, rely=rely, anchor=anchor,
                    x=(-4 if relx else 4), y=(-4 if rely else 4))
            self._corner_brackets.append(c)

    def _start_scan_animation(self):
        """Face scan line only — corners are static (no full-screen black overlay)."""
        if self.current_screen != "face_local":
            self.anim.cancel("scanline")
            self.anim.cancel("focus")
            return
        # Scan line (~18 FPS) — thin frame, does not cover video
        self._scan_line = self.anim.scan_line(
            self.camera_panel, width=560, height=360, x=40,
            color=PRIMARY, name="scanline", fps=18,
        )

    # ========================================================
    # CẢNH BÁO NGƯỜI LẠ ĐỨNG LÂU → CHỤP ẢNH → GỬI TELEGRAM
    # ========================================================


    def _start_battery_watch(self):
        """Kiem tra pin yeu dinh ky (neu co UPS/BAT)."""
        self._battery_watch_tick()

    def _battery_watch_tick(self):
        if not self.running:
            return
        def worker():
            try:
                if LOCAL_OK and LOCAL_BACKEND is not None:
                    LOCAL_BACKEND.check_battery_alert()
            except Exception as e:
                print(f"[BATTERY] {e}")
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(300000, self._battery_watch_tick)  # 5 phut

    def _start_stranger_monitor(self):
        """Bắt đầu vòng lặp giám sát người lạ (chạy nền)."""
        print(
            f"[STRANGER] Monitor bật | threshold={self.PROLONGED_ALERT_SECONDS}s "
            f"| cooldown={self.PROLONGED_ALERT_COOLDOWN}s"
        )
        self._stranger_monitor_tick()

    def _stranger_monitor_tick(self):
        """Mỗi 2 giây: kiểm tra có stranger không, đếm thời gian, gửi cảnh báo."""
        if not self.running:
            return
        try:
            self._stranger_monitor_check()
        except Exception as e:
            print(f"[STRANGER] Lỗi monitor: {e}")
        if self.running:
            self._stranger_monitor_job = self.root.after(
                2000, self._stranger_monitor_tick
            )

    def _stranger_monitor_check(self):
        """
        Logic:
        - Nếu đang ở màn face_local và last_scan_state == stranger → dùng luôn
        - Ngược lại: chạy face_recognize nhẹ trên frame hiện tại (nền)
        - Người lạ liên tục >= PROLONGED_ALERT_SECONDS → chụp + gửi Telegram
        """
        now = time.time()

        # Cooldown chi ap dung luc GUI tin (_stranger_mark)
        is_stranger = False

        # 1) Dùng kết quả face screen nếu đang mở
        if self.current_screen == "face_local" and self.last_scan_state == "stranger":
            is_stranger = True
        else:
            # 2) Nhận diện nền (không khóa cửa) — chỉ khi camera OK và không bận
            if (
                self._camera_ok
                and LOCAL_OK
                and LOCAL_BACKEND is not None
                and not self._stranger_infer_busy
                and not getattr(self, "mfa_mode", False)
            ):
                self._stranger_infer_busy = True

                def bg_infer():
                    try:
                        frame = self._grab_current_frame()
                        if frame is None:
                            return
                        h, w = frame.shape[:2]
                        if w > 320:
                            scale = 320.0 / w
                            frame = cv2.resize(frame, (320, int(h * scale)))
                        data = LOCAL_BACKEND.face_recognize(frame, unlock=False)
                        raw = str(data.get("state", "")).lower()
                        stranger = raw in (
                            "stranger", "unknown", "failed", "error",
                        )
                        # Có face nhưng không phải user đã đăng ký
                        if stranger or (
                            data.get("face_detected") and not data.get("matched")
                        ):
                            self.root.after(0, lambda: self._stranger_mark(True))
                        else:
                            # recognized / idle / no face
                            if raw in ("recognized", "matched", "success"):
                                self.root.after(0, lambda: self._stranger_mark(False))
                            elif raw in ("idle", "scanning", "") and not data.get(
                                "face_detected"
                            ):
                                self.root.after(0, lambda: self._stranger_mark(False))
                    except Exception as e:
                        print(f"[STRANGER] infer error: {e}")
                    finally:
                        self._stranger_infer_busy = False

                threading.Thread(target=bg_infer, daemon=True).start()
                # Dùng trạng thái cũ trong lúc infer
                is_stranger = self._stranger_since is not None
            else:
                is_stranger = (
                    self.current_screen == "face_local"
                    and self.last_scan_state == "stranger"
                )

        self._stranger_mark(is_stranger)

    def _stranger_mark(self, is_stranger):
        """Cập nhật timer người lạ và kích hoạt cảnh báo nếu đủ lâu."""
        now = time.time()
        if is_stranger:
            if self._stranger_since is None:
                self._stranger_since = now
                print("[STRANGER] Phát hiện người lạ — bắt đầu đếm giờ")
            elapsed = now - self._stranger_since
            if elapsed >= self.PROLONGED_ALERT_SECONDS:
                # 1 tin / PROLONGED_ALERT_COOLDOWN — khong spam
                cooldown = max(float(self.PROLONGED_ALERT_COOLDOWN), 60.0)
                if now - self._last_stranger_alert_at >= cooldown:
                    self._last_stranger_alert_at = now
                    self._stranger_since = now  # reset dem cho lan sau
                    if getattr(self, "STRANGER_UI_TELEGRAM", True):
                        print(
                            f"[STRANGER] Da dung {int(elapsed)}s → Telegram "
                            f"(lan sau sau {int(cooldown)}s)"
                        )
                        threading.Thread(
                            target=self._send_stranger_alert_telegram,
                            args=(int(elapsed),),
                            daemon=True,
                        ).start()
                    else:
                        print(f"[STRANGER] Da dung {int(elapsed)}s (UI Telegram tat)")
        else:
            if self._stranger_since is not None:
                print("[STRANGER] Người lạ đã rời / nhận diện được — reset timer")
            self._stranger_since = None

    def _send_stranger_alert_telegram(self, elapsed_seconds):
        """Chụp frame hiện tại + gửi ảnh + tin nhắn cảnh báo qua Telegram."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids_raw = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
        if not token or not chat_ids_raw:
            print("[STRANGER] Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_IDS")
            return

        frame = self._grab_current_frame()
        photo_bytes = None
        if frame is not None:
            try:
                # Resize vừa phải để gửi nhanh
                h, w = frame.shape[:2]
                if w > 640:
                    scale = 640.0 / w
                    frame = cv2.resize(frame, (640, int(h * scale)))
                ok, buf = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                )
                if ok:
                    photo_bytes = buf.tobytes()
            except Exception as e:
                print(f"[STRANGER] Encode ảnh lỗi: {e}")

        ts = time.strftime("%d/%m/%Y %H:%M:%S")
        caption = (
            f"⚠️ CẢNH BÁO NGƯỜI LẠ\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👤 Phát hiện người lạ trước camera\n"
            f"⏱ Đứng khoảng {elapsed_seconds} giây\n"
            f"🕐 {ts}\n"
            f"📍 Smart Key Kiosk"
        )

        chat_ids = [
            c.strip()
            for c in chat_ids_raw.replace(";", ",").split(",")
            if c.strip()
        ]

        for chat_id in chat_ids:
            try:
                if photo_bytes:
                    # Gửi ảnh kèm caption
                    url = f"https://api.telegram.org/bot{token}/sendPhoto"
                    files = {"photo": ("stranger.jpg", photo_bytes, "image/jpeg")}
                    data = {"chat_id": chat_id, "caption": caption}
                    r = requests.post(url, data=data, files=files, timeout=20)
                else:
                    # Không có ảnh → chỉ gửi text
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    r = requests.post(
                        url,
                        json={"chat_id": chat_id, "text": caption},
                        timeout=15,
                    )
                result = r.json() if r.content else {}
                if result.get("ok"):
                    print(f"[STRANGER] Đã gửi cảnh báo tới {chat_id}")
                else:
                    print(
                        f"[STRANGER] Gửi thất bại {chat_id}: "
                        f"{result.get('description', r.text[:120])}"
                    )
            except Exception as e:
                print(f"[STRANGER] Exception gửi Telegram: {e}")

    def _grab_current_frame(self):
        """Lấy frame BGR mới nhất (cập nhật bởi _refresh_camera)."""
        try:
            frame = getattr(self, "latest_frame_bgr", None)
            if frame is not None:
                return frame.copy()
        except Exception:
            pass
        return None

    def _poll_scan_status(self):
        """Nhận diện khuôn mặt LOCAL trên Kiosk (không gọi web)."""
        if self.current_screen not in ("face_local", "alerts", "system"):
            return

        if self.current_screen != "face_local":
            # Chỉ poll door cho màn khác
            def worker_door():
                door_status, door_data = self._request_json(
                    "GET", f"{SERVER_URL}/api/door_status", timeout=4,
                )
                if door_status == 200:
                    self.last_door_data = door_data
                self.root.after(0, self._update_scan_status)

            threading.Thread(target=worker_door, daemon=True).start()
            return

        # Face local AI
        if time.time() < getattr(self, "_face_armed_at", 0):
            self.last_scan_state = "scanning"
            self.root.after(0, self._update_scan_status)
            return

        def worker():
            frame = self._grab_current_frame()
            data = {}
            # Pi 4: ~2.5s/lần (SFace thường 0.3–1.5s sau preload)
            now = time.time()
            last_inf = getattr(self, "_last_face_infer_at", 0)
            interval = float(os.environ.get("FACE_INFER_INTERVAL", "1.0"))
            skip_infer = (now - last_inf) < interval

            if LOCAL_OK and LOCAL_BACKEND is not None and frame is not None and not skip_infer:
                try:
                    self._last_face_infer_at = now
                    h, w = frame.shape[:2]
                    if w > 400:
                        scale = 400.0 / w
                        frame = cv2.resize(frame, (400, int(h * scale)))
                    unlock = not getattr(self, "mfa_mode", False)
                    data = LOCAL_BACKEND.face_recognize(frame, unlock=unlock)
                    if data.get("ms"):
                        data["message"] = (
                            (data.get("message") or "") + f" ({data['ms']}ms)"
                        ).strip()
                except Exception as e:
                    data = {"state": "error", "message": str(e)}
            elif skip_infer and self.last_scan_state:
                data = {
                    "state": self.last_scan_state,
                    "name": self.last_scan_name,
                    "score": self.last_scan_score,
                    "message": getattr(self, "_last_face_msg", ""),
                }
            elif frame is None:
                data = {"state": "scanning", "message": "Chờ camera…"}
            else:
                st, data = self._request_json(
                    "GET", f"{SERVER_URL}/api/scan_status", timeout=4,
                )

            raw_state = str(data.get("state", "idle")).lower()
            name = data.get("name") or data.get("user_name")
            score = data.get("score", 0)
            msg = data.get("message") or ""
            if raw_state in ("recognized", "matched", "success"):
                self.last_scan_state = "recognized"
            elif raw_state in ("stranger", "unknown", "failed"):
                self.last_scan_state = "stranger"
            elif raw_state in ("scanning", "detecting"):
                self.last_scan_state = "scanning"
            elif raw_state == "error":
                self.last_scan_state = "stranger"
            else:
                self.last_scan_state = raw_state or "idle"
            self.last_scan_name = name
            self.last_scan_score = score
            self._last_face_msg = msg

            door_status, door_data = self._request_json(
                "GET", f"{SERVER_URL}/api/door_status", timeout=3,
            )
            if door_status == 200:
                self.last_door_data = door_data

            # Cap nhat UI + neu recognized → popup + Control Panel ngay
            def on_main(
                st=self.last_scan_state,
                nm=name,
                sc=score,
                message=msg,
            ):
                self._update_scan_status()
                if st == "recognized" and nm:
                    self._handle_face_access_granted(nm, sc)

            self.root.after(0, on_main)

        threading.Thread(target=worker, daemon=True).start()


    def _handle_face_access_granted(self, name, score=0):
        """Popup ACCESS GRANTED + vao Control Panel (chi 1 lan)."""
        if getattr(self, "current_screen", "") != "face_local":
            return
        if getattr(self, "_face_nav_done", False):
            return
        if time.time() < getattr(self, "_face_armed_at", 0):
            # thu lai sau khi arm
            delay = max(50, int((self._face_armed_at - time.time()) * 1000) + 50)
            self.root.after(
                delay,
                lambda: self._handle_face_access_granted(name, score),
            )
            return

        self._face_nav_done = True
        self.logged_in_user = name
        self.last_scan_state = "recognized"
        self.last_scan_name = name
        try:
            self.last_scan_score = float(score or 0)
        except Exception:
            self.last_scan_score = 0

        print(f"[FACE] ACCESS GRANTED user={name} score={score} → Control Panel")

        if getattr(self, "mfa_mode", False) and self.mfa_session_id:
            if not getattr(self, "_mfa_face_sent", False):
                self._mfa_face_sent = True
                self._show_unlock_success_popup(
                    title="✓ KHUÔN MẶT HỢP LỆ",
                    subtitle=f"Xin chào {name}!",
                )
                self.root.after(150, lambda: self._mfa_verify("face", {"name": name}))
            return

        self._show_unlock_success_popup(
            title="✓ ACCESS GRANTED",
            subtitle=f"Xin chào {name}! — Mời vào",
        )
        is_locked = self.last_door_data.get("locked", True)
        if is_locked:
            self._request_face_unlock(name)

        def go():
            if self.current_screen == "face_local":
                self._transition_to(self._show_control_panel, name)

        self.root.after(250, go)

    def _update_scan_status(self):
        state = self.last_scan_state
        name = self.last_scan_name
        prev_state = getattr(self, "_prev_scan_state", "idle")
        is_locked = self.last_door_data.get("locked", True)

        if state == "recognized":
            title = f"✓ ACCESS GRANTED — {name}" if name else "✓ ACCESS GRANTED"
            title_color = SUCCESS
            detail = f"Điểm khớp: {self.last_scan_score}" if self.last_scan_score else "Đã xác nhận danh tính"
            if hasattr(self, "face_icon_canvas"):
                try:
                    Icons.check(self.face_icon_canvas, SUCCESS, 40)
                except Exception:
                    pass
        elif state == "stranger":
            title, title_color = "✕ KHÔNG NHẬN DIỆN ĐƯỢC", DANGER
            detail = "Khuôn mặt không khớp. Vui lòng thử lại."
        elif state == "scanning":
            title, title_color = "ĐANG XÁC THỰC KHUÔN MẶT…", WARN
            detail = getattr(self, "_last_face_msg", None) or "Giữ yên — hệ thống đang xử lý."
        else:
            title, title_color = "VUI LÒNG NHÌN VÀO CAMERA", TEXT
            detail = "Camera Ready — chờ nhận diện"

        if hasattr(self, "local_face_status") and self.local_face_status.winfo_exists():
            self.local_face_status.config(text=title, fg=title_color)
            self.local_face_detail.config(text=detail)

        if hasattr(self, "face_lock_banner") and self.face_lock_banner.winfo_exists():
            if state == "recognized" and not is_locked:
                remaining = self.last_door_data.get("auto_lock_remaining", 0)
                extra = f"  ·  Tự khóa sau {remaining}s" if remaining > 0 else ""
                self.face_lock_banner.config(
                    text=f"✓  ĐÃ MỞ CỬA — MỜI VÀO!{extra}",
                    fg=SUCCESS, bg=SUCCESS_BG, highlightbackground=SUCCESS,
                )
            elif state == "recognized":
                self.face_lock_banner.config(
                    text="✓  ĐĂNG NHẬP THÀNH CÔNG — Đang mở cửa…",
                    fg=SUCCESS, bg=SUCCESS_BG, highlightbackground=SUCCESS,
                )
            elif is_locked:
                self.face_lock_banner.config(
                    text="🔒  CỬA ĐANG KHÓA",
                    fg=DIM, bg=CARD2, highlightbackground=BORDER,
                )
            else:
                remaining = self.last_door_data.get("auto_lock_remaining", 0)
                extra = f"  ·  Tự khóa sau {remaining}s" if remaining > 0 else ""
                self.face_lock_banner.config(
                    text=f"🔓  CỬA ĐANG MỞ{extra}",
                    fg=SUCCESS, bg=SUCCESS_BG, highlightbackground=SUCCESS,
                )



        self._prev_scan_state = state
        self._prev_door_locked = is_locked

        if self.current_screen in ("face_local", "alerts", "system"):
            interval = 600 if self.current_screen == "face_local" else STATUS_INTERVAL_MS
            self.status_job = self.root.after(interval, self._poll_scan_status)

    def _request_face_unlock(self, name=None):
        now = time.time()
        if now - getattr(self, "_last_face_unlock_time", 0) < 5.0:
            return
        self._last_face_unlock_time = now

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/door_control",
                json={"action": "unlock"}, timeout=8,
            )

            def update():
                if status == 200 and (data.get("ok") or data.get("success")):
                    if hasattr(self, "face_lock_banner") and self.face_lock_banner.winfo_exists():
                        self.face_lock_banner.config(
                            text="✓  ĐÃ MỞ CỬA THÀNH CÔNG — MỜI VÀO!",
                            fg=SUCCESS, bg=SUCCESS_BG, highlightbackground=SUCCESS,
                        )
                    self.last_door_data["locked"] = False
                else:
                    if hasattr(self, "face_lock_banner") and self.face_lock_banner.winfo_exists():
                        self.face_lock_banner.config(
                            text="⚠ Nhận diện OK nhưng mở cửa thất bại",
                            fg=WARN, bg=WARN_BG, highlightbackground=WARN,
                        )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # PIN
    # ========================================================

    def _show_pin(self):
        self._clear_content()
        self.current_screen = "pin"
        self.pin_value = ""
        self._hide_camera()

        step = getattr(self, "mfa_step_info", "")
        if step:
            self._label(self.content_frame, step, 11, PRIMARY, True).pack(pady=(6, 2))

        ic = tk.Canvas(self.content_frame, width=64, height=64, bg=BG, highlightthickness=0)
        ic.pack(pady=4)
        Icons.pin(ic, WARN, 64)

        self._label(self.content_frame, "NHẬP MÃ PIN", 20, TEXT, True).pack(pady=(2, 2))
        self._label(self.content_frame, "Nhập mã PIN đã đăng ký trên hệ thống", 11, DIM).pack(pady=(0, 6))

        pin_box = self._card(self.content_frame)
        pin_box.pack(pady=6)
        self.pin_display = self._label(pin_box, " ", 28, PRIMARY, True, width=14, bg=CARD)
        self.pin_display.pack(padx=20, pady=12)

        keypad = tk.Frame(self.content_frame, bg=BG)
        keypad.pack(pady=4)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "XÓA", "0", "NHẬP"]
        self.pin_keypad_buttons = []
        for i, key in enumerate(keys):
            r, c = divmod(i, 3)
            kind = "success" if key == "NHẬP" else ("danger" if key == "XÓA" else "normal")
            btn = self._button(
                keypad, key, lambda k=key: self._pin_key(k),
                kind, width=7, height=2,
            )
            btn.grid(row=r, column=c, padx=5, pady=5)
            self.pin_keypad_buttons.append(btn)

        self.pin_result = self._label(self.content_frame, "", 13, DIM, True)
        self.pin_result.pack(pady=6)

        self._button(
            self.content_frame, "← QUAY LẠI",
            self._back_from_auth,
            "ghost", width=14, height=1,
        ).pack(pady=4)

    def _pin_key(self, key):
        # Flash the pressed key (micro feedback)
        try:
            keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "XÓA", "0", "NHẬP"]
            if key in keys and hasattr(self, "pin_keypad_buttons"):
                idx = keys.index(key)
                btn = self.pin_keypad_buttons[idx]
                flash = SUCCESS if key == "NHẬP" else (DANGER if key == "XÓA" else PRIMARY)
                self.anim.key_flash(btn, flash_bg=flash, ms=100)
        except Exception:
            pass

        if key == "XÓA":
            self.pin_value = self.pin_value[:-1]
        elif key == "NHẬP":
            self._submit_pin()
            return
        elif len(self.pin_value) < self.pin_max_len:
            self.pin_value += key
        self.pin_display.config(
            text="●" * len(self.pin_value) if self.pin_value else " "
        )

    def _submit_pin(self):
        if not self.pin_value:
            self.pin_result.config(text="Vui lòng nhập mã PIN.", fg=WARN)
            return
        pin = self.pin_value
        self.pin_value = ""
        self.pin_display.config(text=" ")
        self.pin_result.config(text="Đang xác thực…", fg=WARN)

        if getattr(self, "mfa_mode", False) and self.mfa_session_id:
            self._mfa_verify("pin", {"pin": pin})
            return

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/pin_login",
                json={"pin": pin}, timeout=6,
            )

            def update():
                success = bool(data.get("success") or data.get("ok"))
                if success:
                    user_name = data.get("user_name", "")
                    self.pin_result.config(
                        text=f"✓ ACCESS GRANTED — Xin chào {user_name}",
                        fg=SUCCESS,
                    )
                    self._show_unlock_success_popup(
                        title="ACCESS GRANTED",
                        subtitle=f"Xin chào {user_name}!" if user_name else "Mời sử dụng hệ thống",
                    )
                    self.root.after(
                            500,
                        lambda: self._transition_to(self._show_control_panel, user_name),
                    )
                elif data.get("locked_out"):
                    remaining = data.get("remaining_seconds", 60)
                    self._set_pin_keypad_enabled(False)
                    self._start_pin_lockout_countdown(remaining)
                else:
                    self.pin_result.config(
                        text=f"✕ {data.get('message', 'SAI MÃ PIN')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _set_pin_keypad_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for btn in getattr(self, "pin_keypad_buttons", []):
            try:
                btn.config(state=state)
            except Exception:
                pass

    def _start_pin_lockout_countdown(self, remaining_seconds):
        if self.current_screen != "pin":
            return
        remaining_seconds = int(remaining_seconds)
        if remaining_seconds <= 0:
            self.pin_result.config(text="", fg=DIM)
            self._set_pin_keypad_enabled(True)
            return
        self.pin_result.config(
            text=f"🔒 ĐÃ KHÓA NHẬP PIN — Thử lại sau {remaining_seconds} giây",
            fg=DANGER,
        )
        self.root.after(1000, lambda: self._start_pin_lockout_countdown(remaining_seconds - 1))

    # ========================================================
    # CHANGE PIN
    # ========================================================

    def _show_change_pin_dialog(self):
        self.current_screen = "change_pin_old"
        self.change_pin_old = ""
        self._pin_dialog_value = ""
        self._render_change_pin_step()

    def _render_change_pin_step(self):
        self._clear_content()
        self._hide_camera()
        is_old = self.current_screen == "change_pin_old"
        title = "NHẬP MÃ PIN HIỆN TẠI" if is_old else "NHẬP MÃ PIN MỚI"
        self._label(self.content_frame, title, 18, TEXT, True).pack(pady=(10, 4))

        pin_box = self._card(self.content_frame)
        pin_box.pack(pady=6)
        self.change_pin_display = self._label(
            pin_box, " ", 24, PRIMARY, True, width=14, bg=CARD,
        )
        self.change_pin_display.pack(padx=20, pady=12)

        keypad = tk.Frame(self.content_frame, bg=BG)
        keypad.pack()
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "XÓA", "0", "NHẬP"]
        for i, key in enumerate(keys):
            r, c = divmod(i, 3)
            kind = "success" if key == "NHẬP" else ("danger" if key == "XÓA" else "normal")
            self._button(
                keypad, key, lambda k=key: self._change_pin_key(k),
                kind, width=7, height=2,
            ).grid(row=r, column=c, padx=5, pady=5)

        self.change_pin_result = self._label(self.content_frame, "", 12, DIM, True)
        self.change_pin_result.pack(pady=8)
        self._button(
            self.content_frame, "← QUAY LẠI BẢNG ĐIỀU KHIỂN",
            lambda: self._transition_to(self._show_control_panel),
            "ghost", width=26, height=1,
        ).pack(pady=4)

    def _change_pin_key(self, key):
        if key == "XÓA":
            self._pin_dialog_value = self._pin_dialog_value[:-1]
        elif key == "NHẬP":
            self._submit_change_pin_step()
            return
        else:
            if len(self._pin_dialog_value) < 10:
                self._pin_dialog_value += key
        self.change_pin_display.config(
            text="●" * len(self._pin_dialog_value) if self._pin_dialog_value else " "
        )

    def _submit_change_pin_step(self):
        if self.current_screen == "change_pin_old":
            if not self._pin_dialog_value:
                self.change_pin_result.config(text="Vui lòng nhập mã PIN hiện tại.", fg=WARN)
                return
            self.change_pin_old = self._pin_dialog_value
            self._pin_dialog_value = ""
            self.current_screen = "change_pin_new"
            self._render_change_pin_step()
            return

        new_pin = self._pin_dialog_value
        if not new_pin:
            self.change_pin_result.config(text="Vui lòng nhập mã PIN mới.", fg=WARN)
            return
        old_pin = self.change_pin_old
        self._pin_dialog_value = ""
        self.change_pin_result.config(text="Đang xử lý…", fg=WARN)

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/change_pin",
                json={"old_pin": old_pin, "new_pin": new_pin}, timeout=6,
            )

            def update():
                if self.current_screen != "change_pin_new":
                    return
                if data.get("ok"):
                    self.change_pin_result.config(
                        text=f"✓ {data.get('message', '')}", fg=SUCCESS,
                    )
                    self.root.after(350, lambda: self._transition_to(self._show_control_panel))
                else:
                    self.change_pin_result.config(
                        text=f"✕ {data.get('message', 'Đổi mã PIN thất bại.')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # FINGERPRINT
    # ========================================================

    def _show_fingerprint(self):
        self._clear_content()
        self.current_screen = "fingerprint"
        self._hide_camera()

        step = getattr(self, "mfa_step_info", "")
        if step:
            self._label(self.content_frame, step, 11, PRIMARY, True).pack(pady=(6, 2))

        self.fp_canvas = tk.Canvas(self.content_frame, width=72, height=72, bg=BG, highlightthickness=0)
        self.fp_canvas.pack(pady=6)
        Icons.fingerprint(self.fp_canvas, PRIMARY, 72)
        self.anim.fingerprint_scan(self.fp_canvas, PRIMARY, 72, name="fp_scan")

        self._label(self.content_frame, "XÁC THỰC VÂN TAY", 20, TEXT, True).pack(pady=2)
        self._label(
            self.content_frame,
            "Đặt ngón tay lên cảm biến  ·  hoặc nhập mã thủ công",
            11, DIM,
        ).pack(pady=(0, 8))

        status_card = self._card(self.content_frame)
        status_card.pack(fill="x", padx=36, pady=4)
        self.fingerprint_status = self._label(
            status_card,
            "Đang chờ cảm biến vân tay…\n(ESP32 / R503 / AS608)",
            13, WARN, True, bg=CARD, wraplength=600, justify="center",
        )
        self.fingerprint_status.pack(padx=14, pady=12)

        self._label(self.content_frame, "— hoặc nhập mã thủ công —", 10, DIM).pack(pady=(6, 2))
        entry_box = self._card(self.content_frame)
        entry_box.pack(pady=4)
        self.fingerprint_entry = tk.Entry(
            entry_box, font=("Arial", 16), width=16, justify="center",
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.fingerprint_entry.pack(padx=14, pady=10, ipady=6)
        self.fingerprint_entry.bind("<Return>", lambda e: self._submit_fingerprint())

        self._button(
            self.content_frame, "XÁC THỰC", self._submit_fingerprint,
            "success", width=16, height=1,
        ).pack(pady=8)
        self._button(
            self.content_frame, "← QUAY LẠI",
            self._back_from_auth,
            "ghost", width=14, height=1,
        ).pack(pady=6)

        self._poll_optional_status(OPTIONAL_FINGERPRINT_STATUS_URL, "fingerprint")

    def _animate_fingerprint_wave(self):
        # legacy hook – MicroAnim.fingerprint_scan owns the loop
        if self.current_screen != "fingerprint":
            self.anim.cancel("fp_scan")

    def _submit_fingerprint(self):
        fingerprint_id = self.fingerprint_entry.get().strip()
        if not fingerprint_id:
            self.fingerprint_status.config(text="Vui lòng nhập mã vân tay.", fg=WARN)
            return
        self.fingerprint_status.config(text="Đang xác thực…", fg=WARN)

        if getattr(self, "mfa_mode", False) and self.mfa_session_id:
            self._mfa_verify("fingerprint", {"fingerprint_id": fingerprint_id})
            return

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/fingerprint_auth",
                json={"fingerprint_id": fingerprint_id}, timeout=6,
            )

            def update():
                success = bool(data.get("success") or data.get("ok"))
                if success:
                    user_name = data.get("user_name", "")
                    self.fingerprint_status.config(
                        text=f"✓ ACCESS GRANTED — Xin chào {user_name}",
                        fg=SUCCESS,
                    )
                    try:
                        self.anim.cancel("fp_scan")
                        Icons.check(self.fp_canvas, SUCCESS, 72)
                    except Exception:
                        pass
                    self._show_unlock_success_popup(
                        title="ACCESS GRANTED",
                        subtitle=f"Xin chào {user_name}!" if user_name else "Mời sử dụng hệ thống",
                    )
                    self.root.after(
                            500,
                        lambda: self._transition_to(self._show_control_panel, user_name),
                    )
                else:
                    self.fingerprint_status.config(
                        text=f"✕ {data.get('message', 'VÂN TAY KHÔNG HỢP LỆ')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # RFID
    # ========================================================

    def _show_rfid(self):
        self._clear_content()
        self.current_screen = "rfid"
        self._hide_camera()

        step = getattr(self, "mfa_step_info", "")
        if step:
            self._label(self.content_frame, step, 11, PRIMARY, True).pack(pady=(6, 2))

        self.rfid_canvas = tk.Canvas(self.content_frame, width=72, height=72, bg=BG, highlightthickness=0)
        self.rfid_canvas.pack(pady=6)
        Icons.rfid(self.rfid_canvas, PRIMARY, 72)
        self.anim.rfid_waves(self.rfid_canvas, PRIMARY, 72, name="rfid_wave")

        self._label(self.content_frame, "XÁC THỰC RFID", 20, TEXT, True).pack(pady=2)
        self._label(
            self.content_frame,
            "Đưa thẻ vào đầu đọc  ·  hoặc nhập UID thủ công",
            11, DIM,
        ).pack(pady=(0, 8))

        status_card = self._card(self.content_frame)
        status_card.pack(fill="x", padx=36, pady=4)
        self.rfid_status = self._label(
            status_card,
            "Đang chờ thẻ RFID…\n(ESP32 / RC522)",
            13, WARN, True, bg=CARD, wraplength=600, justify="center",
        )
        self.rfid_status.pack(padx=14, pady=12)

        self._label(self.content_frame, "— hoặc nhập UID thủ công —", 10, DIM).pack(pady=(6, 2))
        entry_box = self._card(self.content_frame)
        entry_box.pack(pady=4)
        self.rfid_entry = tk.Entry(
            entry_box, font=("Arial", 16), width=16, justify="center",
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.rfid_entry.pack(padx=14, pady=10, ipady=6)
        self.rfid_entry.bind("<Return>", lambda e: self._submit_rfid())

        self._button(
            self.content_frame, "XÁC THỰC", self._submit_rfid,
            "success", width=16, height=1,
        ).pack(pady=8)
        self._button(
            self.content_frame, "← QUAY LẠI",
            self._back_from_auth,
            "ghost", width=14, height=1,
        ).pack(pady=6)

        self._poll_optional_status(OPTIONAL_RFID_STATUS_URL, "rfid")

    def _animate_rfid_wave(self):
        # legacy hook – MicroAnim.rfid_waves owns the loop
        if self.current_screen != "rfid":
            self.anim.cancel("rfid_wave")

    def _submit_rfid(self):
        rfid_uid = self.rfid_entry.get().strip()
        if not rfid_uid:
            self.rfid_status.config(text="Vui lòng nhập mã UID thẻ.", fg=WARN)
            return
        self.rfid_status.config(text="Đang xác thực…", fg=WARN)

        if getattr(self, "mfa_mode", False) and self.mfa_session_id:
            self._mfa_verify("rfid", {"rfid_uid": rfid_uid})
            return

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/rfid_auth",
                json={"rfid_uid": rfid_uid}, timeout=6,
            )

            def update():
                success = bool(data.get("success") or data.get("ok"))
                if success:
                    user_name = data.get("user_name", "")
                    self.rfid_status.config(
                        text=f"✓ ACCESS GRANTED — Xin chào {user_name}",
                        fg=SUCCESS,
                    )
                    try:
                        self.anim.cancel("rfid_wave")
                        Icons.check(self.rfid_canvas, SUCCESS, 72)
                    except Exception:
                        pass
                    self._show_unlock_success_popup(
                        title="ACCESS GRANTED",
                        subtitle=f"Xin chào {user_name}!" if user_name else "Mời sử dụng hệ thống",
                    )
                    self.root.after(
                            500,
                        lambda: self._transition_to(self._show_control_panel, user_name),
                    )
                else:
                    self.rfid_status.config(
                        text=f"✕ {data.get('message', 'THẺ KHÔNG HỢP LỆ')}",
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _poll_optional_status(self, url, kind):
        if self.current_screen not in ("fingerprint", "rfid"):
            return

        def worker():
            status, data = self._request_json("GET", url, timeout=3)

            def update():
                if self.current_screen not in ("fingerprint", "rfid"):
                    return
                if kind == "fingerprint":
                    label = getattr(self, "fingerprint_status", None)
                    waiting_msg = "Đang chờ cảm biến vân tay…\n(ESP32 / R503 / AS608)"
                else:
                    label = getattr(self, "rfid_status", None)
                    waiting_msg = "Đang chờ thẻ RFID…\n(ESP32 / RC522)"
                if label is None or not label.winfo_exists():
                    return
                if status == 200:
                    state = str(data.get("state", data.get("status", ""))).lower()
                    user_name = data.get("user_name") or data.get("name") or ""
                    if state in ("recognized", "success", "valid", "matched", "ok"):
                        label.config(
                            text=f"✓ ACCESS GRANTED"
                                 + (f" — {user_name}" if user_name else ""),
                            fg=SUCCESS,
                        )
                        self._show_unlock_success_popup(
                            title="ACCESS GRANTED",
                            subtitle=f"Xin chào {user_name}!" if user_name else "Mời sử dụng hệ thống",
                        )
                        self.root.after(
                            1500,
                            lambda: self._transition_to(
                                self._show_control_panel, user_name or None
                            ),
                        )
                        return
                    elif state in ("failed", "invalid", "stranger", "rejected"):
                        label.config(text="✕ XÁC THỰC THẤT BẠI — Thử lại", fg=DANGER)
                    else:
                        label.config(text=data.get("message", waiting_msg), fg=WARN)
                else:
                    label.config(text=waiting_msg, fg=WARN)
                self.root.after(1000, lambda: self._poll_optional_status(url, kind))

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # CONTROL PANEL / DASHBOARD
    # ========================================================



    def _show_enroll_fp_rfid(self):
        """Dang ky ma van tay + UID RFID cho user dang login."""
        self._clear_content()
        self.current_screen = "enroll_fp_rfid"
        self._hide_camera()

        top = tk.Frame(self.content_frame, bg=BG)
        top.pack(fill="x", pady=(4, 8))
        self._label(top, "ĐĂNG KÝ VÂN TAY & RFID", 10, PRIMARY, True).pack(anchor="w")
        tk.Label(
            top,
            text="Gán mã cảm biến ESP32 cho tài khoản đang đăng nhập",
            font=("Arial", 12), bg=BG, fg=DIM,
        ).pack(anchor="w")

        user = self.logged_in_user or ""
        # Current values
        cur_fp, cur_rfid = "", ""
        try:
            if LOCAL_OK and LOCAL_BACKEND is not None:
                u = None
                for name, data in (LOCAL_BACKEND.users or {}).items():
                    if name == user or data.get("username") == user:
                        u = data
                        break
                if u:
                    cur_fp = str(u.get("fingerprint_id") or "")
                    cur_rfid = str(u.get("rfid_uid") or "")
        except Exception:
            pass

        card = self._card(self.content_frame)
        card.pack(fill="x", padx=12, pady=8)
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=18, pady=16)

        tk.Label(
            body, text=f"Tài khoản: {user or '(chưa login)'}",
            font=("Arial", 14, "bold"), bg=CARD, fg=TEXT,
        ).pack(anchor="w", pady=(0, 12))

        # Fingerprint
        tk.Label(
            body, text="Mã vân tay (fingerprint_id từ ESP32 / R503)",
            font=("Arial", 11), bg=CARD, fg=DIM,
        ).pack(anchor="w")
        self.enroll_fp_var = tk.StringVar(value=cur_fp)
        tk.Entry(
            body, textvariable=self.enroll_fp_var, font=("Arial", 16),
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        ).pack(fill="x", ipady=8, pady=(4, 12))

        # RFID
        tk.Label(
            body, text="UID thẻ RFID (vd A1B2C3D4 — in hoa, không khoảng trắng)",
            font=("Arial", 11), bg=CARD, fg=DIM,
        ).pack(anchor="w")
        self.enroll_rfid_var = tk.StringVar(value=cur_rfid)
        tk.Entry(
            body, textvariable=self.enroll_rfid_var, font=("Arial", 16),
            bg=CARD2, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
        ).pack(fill="x", ipady=8, pady=(4, 8))

        self.enroll_cred_msg = tk.Label(
            body,
            text="Để trống ô nào = không đổi ô đó. ESP32 sẽ gửi đúng mã này khi quét.",
            font=("Arial", 11), bg=CARD, fg=DIM, wraplength=520, justify="left",
        )
        self.enroll_cred_msg.pack(anchor="w", pady=(8, 0))

        actions = tk.Frame(self.content_frame, bg=BG)
        actions.pack(fill="x", pady=14, padx=12)
        self._button(
            actions, "💾  LƯU",
            self._save_enroll_fp_rfid,
            "success", width=14, height=2,
        ).pack(side="left", padx=4)
        self._button(
            actions, "XÓA VÂN TAY",
            lambda: self._clear_one_cred("fp"),
            "danger", width=14, height=2,
        ).pack(side="left", padx=4)
        self._button(
            actions, "XÓA RFID",
            lambda: self._clear_one_cred("rfid"),
            "danger", width=12, height=2,
        ).pack(side="left", padx=4)
        self._button(
            actions, "← QUAY LẠI",
            lambda: self._transition_to(
                self._show_control_panel, self.logged_in_user
            ),
            "ghost", width=12, height=2,
        ).pack(side="right", padx=4)

    def _save_enroll_fp_rfid(self):
        user = self.logged_in_user or ""
        if not user:
            self.enroll_cred_msg.config(text="Chưa đăng nhập", fg=DANGER)
            return
        if not LOCAL_OK or LOCAL_BACKEND is None:
            self.enroll_cred_msg.config(text="Backend không sẵn sàng", fg=DANGER)
            return
        try:
            fp = (self.enroll_fp_var.get() or "").strip()
            rfid = (self.enroll_rfid_var.get() or "").strip()
        except Exception:
            fp, rfid = "", ""

        def worker():
            try:
                kwargs = {}
                # empty string means clear if user explicitly wants - we only set if non-empty
                if fp != "":
                    kwargs["fingerprint_id"] = fp
                if rfid != "":
                    kwargs["rfid_uid"] = rfid
                if not kwargs:
                    r = {"ok": False, "message": "Nhập mã vân tay hoặc UID RFID"}
                else:
                    r = LOCAL_BACKEND.set_user_credentials(user, **kwargs)
                    if not isinstance(r, dict):
                        r = {"ok": True, "message": "Đã lưu"}
                    elif r.get("ok") is None:
                        r = {"ok": True, "message": r.get("message") or "Đã lưu"}
            except Exception as e:
                r = {"ok": False, "message": str(e)}

            def update():
                ok = r.get("ok", True)
                self.enroll_cred_msg.config(
                    text=("✓ " if ok else "✕ ") + (r.get("message") or ("Đã lưu" if ok else "Lỗi")),
                    fg=SUCCESS if ok else DANGER,
                )
            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _clear_one_cred(self, kind):
        user = self.logged_in_user or ""
        if not user or not LOCAL_OK or LOCAL_BACKEND is None:
            return

        def worker():
            try:
                if kind == "fp":
                    r = LOCAL_BACKEND.set_user_credentials(user, fingerprint_id="")
                    self.root.after(0, lambda: self.enroll_fp_var.set(""))
                else:
                    r = LOCAL_BACKEND.set_user_credentials(user, rfid_uid="")
                    self.root.after(0, lambda: self.enroll_rfid_var.set(""))
                if not isinstance(r, dict):
                    r = {"ok": True, "message": "Đã xóa"}
            except Exception as e:
                r = {"ok": False, "message": str(e)}

            def update():
                self.enroll_cred_msg.config(
                    text=r.get("message") or "OK",
                    fg=SUCCESS if r.get("ok", True) else DANGER,
                )
            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_enroll_face(self):
        """Dang ky khuon mat truc tiep tren kiosk (can dang nhap)."""
        self._clear_content()
        self.current_screen = "enroll_face"
        self._enroll_count = 0
        self._show_camera()
        try:
            self._place_face_corners()
        except Exception:
            pass

        bar = self._card(self.content_frame)
        bar.pack(fill="x", pady=(8, 4), padx=16)
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)

        tk.Label(
            inner, text="ĐĂNG KÝ KHUÔN MẶT",
            font=("Arial", 16, "bold"), bg=CARD, fg=TEXT, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            inner,
            text="Nhìn thẳng camera → bấm CHỤP MẪU (nên 3–5 góc: thẳng, trái, phải)",
            font=("Arial", 11), bg=CARD, fg=DIM, anchor="w",
        ).pack(anchor="w", pady=(4, 8))

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Tên:", font=("Arial", 12), bg=CARD, fg=TEXT).pack(side="left")
        self.enroll_name_var = tk.StringVar(value=self.logged_in_user or "")
        ent = tk.Entry(
            row, textvariable=self.enroll_name_var,
            font=("Arial", 14), width=18,
            bg=CARD2, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
        )
        ent.pack(side="left", padx=8, ipady=6)

        self.enroll_status = tk.Label(
            inner, text="Chưa có mẫu — bấm CHỤP MẪU",
            font=("Arial", 12, "bold"), bg=CARD, fg=WARN, anchor="w",
        )
        self.enroll_status.pack(anchor="w", pady=(8, 0))

        self.enroll_detail = tk.Label(
            inner, text="", font=("Arial", 10), bg=CARD, fg=DIM, anchor="w", justify="left",
        )
        self.enroll_detail.pack(anchor="w")

        def _load_info():
            try:
                if LOCAL_OK and LOCAL_BACKEND is not None:
                    info = LOCAL_BACKEND.face_db_info()
                    people = info.get("people") or {}
                    mode = info.get("face_mode")
                    lines = [
                        f"Model: {mode} | Ngưỡng: {info.get('similarity_threshold')}"
                    ]
                    if people:
                        lines.append(
                            "Đã có: "
                            + ", ".join(
                                f"{n}({p.get('samples')} mẫu)" for n, p in people.items()
                            )
                        )
                    else:
                        lines.append("Database trống — chưa ai được đăng ký mặt")
                    if info.get("hint"):
                        lines.append("⚠ " + str(info["hint"]))
                    self.enroll_detail.config(text="\n".join(lines))
            except Exception as e:
                self.enroll_detail.config(text=str(e))

        self.root.after(100, _load_info)

        actions = tk.Frame(self.content_frame, bg=BG)
        actions.pack(fill="x", pady=10, padx=16)
        self._button(
            actions, "📷  CHỤP MẪU",
            self._enroll_face_capture,
            "success", width=16, height=2,
        ).pack(side="left", padx=4)
        self._button(
            actions, "🗑 XÓA MẶT NGƯỜI NÀY",
            self._enroll_face_delete,
            "danger", width=18, height=2,
        ).pack(side="left", padx=4)
        self._button(
            actions, "← QUAY LẠI",
            lambda: self._transition_to(
                self._show_control_panel, self.logged_in_user
            ),
            "ghost", width=12, height=2,
        ).pack(side="right", padx=4)

    def _enroll_face_capture(self):
        try:
            name = (self.enroll_name_var.get() or "").strip()
        except Exception:
            name = (self.logged_in_user or "").strip()
        if not name:
            self.enroll_status.config(text="Nhập tên trước khi chụp", fg=DANGER)
            return
        if not LOCAL_OK or LOCAL_BACKEND is None:
            self.enroll_status.config(text="Backend local không sẵn sàng", fg=DANGER)
            return

        frame = getattr(self, "_latest_frame_bgr", None)
        if frame is None:
            self.enroll_status.config(
                text="Chưa có frame camera — đợi 1–2 giây rồi bấm lại",
                fg=WARN,
            )
            return

        self.enroll_status.config(text="Đang lưu mẫu…", fg=WARN)
        fr = frame.copy() if hasattr(frame, "copy") else frame

        def worker():
            try:
                r = LOCAL_BACKEND.register_face(name, fr)
            except Exception as e:
                r = {"ok": False, "message": str(e)}

            def update():
                if r.get("ok"):
                    self._enroll_count = int(r.get("count") or getattr(self, "_enroll_count", 0) + 1)
                    self.enroll_status.config(
                        text=(
                            f"✓ Đã lưu mẫu #{self._enroll_count} cho «{name}» "
                            f"(dim={r.get('dim')}, model={r.get('face_mode')})"
                        ),
                        fg=SUCCESS,
                    )
                    try:
                        info = LOCAL_BACKEND.face_db_info()
                        people = info.get("people") or {}
                        self.enroll_detail.config(
                            text="Đã có: "
                            + ", ".join(
                                f"{n}({p.get('samples')} mẫu)" for n, p in people.items()
                            )
                        )
                    except Exception:
                        pass
                else:
                    self.enroll_status.config(
                        text="✕ " + (r.get("message") or "Lỗi enroll"),
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _enroll_face_delete(self):
        try:
            name = (self.enroll_name_var.get() or "").strip()
        except Exception:
            name = (self.logged_in_user or "").strip()
        if not name or not LOCAL_OK or LOCAL_BACKEND is None:
            return

        def worker():
            r = LOCAL_BACKEND.delete_face(name)

            def update():
                self._enroll_count = 0
                fg = WARN if r.get("ok") else DANGER
                self.enroll_status.config(text=r.get("message", ""), fg=fg)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_control_panel(self, name=None):
        self._clear_content()
        self.current_screen = "control"
        self._hide_camera()
        if name:
            self.logged_in_user = name

        top = tk.Frame(self.content_frame, bg=BG)
        top.pack(fill="x", pady=(0, 6))
        self._label(top, "CONTROL CENTER", 10, PRIMARY, True).pack(anchor="w")
        tk.Label(
            top, text=f"Xin chào, {self.logged_in_user or 'Người dùng'}",
            font=("Arial", 20, "bold"), bg=BG, fg=TEXT,
        ).pack(side="left")
        self._button(
            top, "ĐĂNG XUẤT",
            self._logout,  # logout clears session then shows auth screen immediately
            "danger", width=11, height=1,
        ).pack(side="right", pady=4)

        main = tk.Frame(self.content_frame, bg=BG)
        main.pack(fill="both", expand=True)

        # Door card
        door = self._card(main)
        door.pack(side="left", fill="both", expand=True, padx=(0, 8))

        tk.Label(door, text="DOOR SECURITY", font=("Arial", 10, "bold"),
                 bg=CARD, fg=PRIMARY).pack(anchor="w", padx=18, pady=(14, 2))

        # Lock icon
        self.door_icon_canvas = tk.Canvas(door, width=64, height=64, bg=CARD, highlightthickness=0)
        self.door_icon_canvas.pack(pady=4)
        Icons.lock_closed(self.door_icon_canvas, DANGER, 64)

        self.control_door_label = tk.Label(
            door, text="Đang tải trạng thái khóa…",
            font=("Arial", 18, "bold"), bg=CARD, fg=TEXT, justify="left",
        )
        self.control_door_label.pack(anchor="w", padx=18, pady=(4, 4))
        self.control_msg = tk.Label(
            door, text="Realtime status", font=("Arial", 10),
            bg=CARD, fg=DIM,
        )
        self.control_msg.pack(anchor="w", padx=18)

        actions = tk.Frame(door, bg=CARD)
        actions.pack(fill="x", padx=14, pady=16)
        self._button(
            actions, "🔓  MỞ KHÓA",
            lambda: self._control_door("unlock"),
            "success", width=11, height=2,
        ).pack(side="left", padx=3)
        self._button(
            actions, "🔒  KHÓA",
            lambda: self._control_door("lock"),
            "danger", width=11, height=2,
        ).pack(side="left", padx=3)
        self._button(
            actions, "🔔 CHUÔNG",
            self._ring_doorbell,
            "warn", width=11, height=2,
        ).pack(side="left", padx=3)

        # Side quick access
        side = tk.Frame(main, bg=BG, width=300)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        self._label(side, "QUICK ACCESS", 10, DIM, True).pack(anchor="w", pady=(0, 6))

        items = [
            ("ĐĂNG KÝ KHUÔN MẶT", self._show_enroll_face, "primary"),
            ("ĐĂNG KÝ VÂN TAY / RFID", self._show_enroll_fp_rfid, "primary"),
            ("CHẾ ĐỘ TRUY CẬP", self._show_access_mode_settings, "warn"),
            ("CHỐT AN TOÀN", self._toggle_safety_bolt, "warn"),
            ("TRẠNG THÁI HỆ THỐNG", self._show_system_status, "normal"),
            ("LỊCH SỬ TRUY CẬP", self._show_access_history, "normal"),
            ("CẢNH BÁO", self._show_alerts, "warn"),
            ("ĐỔI MÃ PIN", self._show_change_pin_dialog, "normal"),
        ]
        for text, cmd, kind in items:
            self._button(
                side, text,
                lambda c=cmd: self._transition_to(c),
                kind, width=26, height=1,
            ).pack(fill="x", pady=4)

        self._poll_door_status()


    def _pack_public_doorbell(self, parent, side="bottom"):
        """Chuong ngoai — bat ky ai cung bam duoc, khong can dang nhap."""
        bar = tk.Frame(parent, bg=CARD, highlightthickness=2, highlightbackground=WARN)
        if side == "bottom":
            bar.pack(side="bottom", fill="x", pady=(8, 4), padx=4)
        else:
            bar.pack(fill="x", pady=8)
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)
        tk.Label(
            inner, text="Khách / người ngoài",
            font=("Arial", 10), bg=CARD, fg=DIM,
        ).pack(side="left")
        self._button(
            inner, "🔔  BẤM CHUÔNG",
            self._ring_doorbell,
            "warn", width=18, height=2,
        ).pack(side="right")
        return bar

    def _ring_doorbell(self):
        """Chuong cua — gui Telegram ngay lap tuc."""
        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/doorbell",
                json={"by": self.logged_in_user or "kiosk"},
                timeout=8,
            )
            def update():
                ok = status == 200 and data.get("ok")
                msg = data.get("message", "OK" if ok else "That bai")
                try:
                    if hasattr(self, "control_msg") and self.control_msg.winfo_exists():
                        self.control_msg.config(
                            text=("🔔 " + msg) if ok else ("✕ " + msg),
                            fg=SUCCESS if ok else DANGER,
                        )
                except Exception:
                    pass
                self._show_unlock_success_popup(
                    title="CHUÔNG ĐÃ GỬI" if ok else "CHUÔNG LỖI",
                    subtitle=msg,
                )
            self.root.after(0, update)
        threading.Thread(target=worker, daemon=True).start()

    def _toggle_safety_bolt(self):
        """Bat/tat chot an toan (chan mo tu xa)."""
        def worker():
            st, cur = self._request_json("GET", f"{SERVER_URL}/api/safety_bolt", timeout=5)
            enabled = False
            if st == 200:
                enabled = not bool(cur.get("safety_bolt"))
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/safety_bolt",
                json={
                    "enabled": enabled,
                    "by": self.logged_in_user or "kiosk",
                },
                timeout=5,
            )
            def update():
                if status == 200 and data.get("ok"):
                    on = data.get("safety_bolt")
                    self._show_unlock_success_popup(
                        title="CHỐT AN TOÀN",
                        subtitle=data.get("message") or ("BẬT" if on else "TẮT"),
                    )
                else:
                    self._show_unlock_success_popup(
                        title="LỖI",
                        subtitle=data.get("message", "Không đổi được chốt an toàn"),
                    )
                self.root.after(350, lambda: self._transition_to(
                    self._show_control_panel, self.logged_in_user
                ))
            self.root.after(0, update)
        threading.Thread(target=worker, daemon=True).start()

    def _control_door(self, action):
        self.control_msg.config(text="Đang gửi lệnh…", fg=WARN)
        self.anim.loading_dots(self.control_msg, "Đang gửi lệnh", "loading")

        def worker():
            status, data = self._request_json(
                "POST", f"{SERVER_URL}/api/door_control",
                json={"action": action}, timeout=8,
            )

            def update():
                self.anim.cancel("loading")
                if status == 200 and (data.get("ok") or data.get("success")):
                    if action == "unlock":
                        self.control_msg.config(
                            text="✓  ĐÃ MỞ CỬA THÀNH CÔNG — Mời vào!",
                            fg=SUCCESS,
                        )
                        try:
                            self.anim.lock_transition(
                                self.door_icon_canvas, unlock=True, size=64,
                            )
                        except Exception:
                            pass
                        self._show_unlock_success_popup()
                    else:
                        self.control_msg.config(text="🔒  Đã khóa lại thành công.", fg=SUCCESS)
                        try:
                            self.anim.lock_transition(
                                self.door_icon_canvas, unlock=False, size=64,
                            )
                        except Exception:
                            pass
                else:
                    self.control_msg.config(
                        text=data.get("message", "Lệnh điều khiển thất bại."),
                        fg=DANGER,
                    )

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _show_unlock_success_popup(self, title=None, subtitle=None):
        if getattr(self, "_unlock_popup", None) is not None:
            try:
                self._unlock_popup.destroy()
            except Exception:
                pass
            self._unlock_popup = None

        if title is None:
            title = "ACCESS GRANTED"
        if subtitle is None:
            subtitle = "Mời bạn vào!"

        popup = tk.Frame(
            self.root, bg=CARD,
            highlightthickness=3, highlightbackground=SUCCESS,
            padx=28, pady=8,
        )
        popup.place(relx=0.5, rely=0.36, anchor="center")
        try:
            popup.lift()
            popup.tkraise()
        except Exception:
            pass

        ic = tk.Canvas(popup, width=72, height=72, bg=CARD, highlightthickness=0)
        ic.pack(pady=(12, 4))
        # Expanding ring → check (Access Granted micro-anim)
        is_denied = "DENIED" in (title or "").upper() or "THẤT BẠI" in (title or "").upper()
        if is_denied:
            Icons.clear(ic)
            s = 72 / 48.0
            ic.create_oval(10 * s, 10 * s, 38 * s, 38 * s, outline=DANGER, width=3)
            ic.create_line(18 * s, 18 * s, 30 * s, 30 * s, fill=DANGER, width=3)
            ic.create_line(30 * s, 18 * s, 18 * s, 30 * s, fill=DANGER, width=3)
            try:
                self.anim.shake(popup, times=3, offset=5)
            except Exception:
                pass
        else:
            self.anim.ring_expand(ic, color=SUCCESS, steps=6, size=72)

        tk.Label(popup, text=title, font=("Arial", 18, "bold"),
                 bg=CARD, fg=DANGER if is_denied else SUCCESS).pack(pady=(0, 4))
        tk.Label(popup, text=subtitle, font=("Arial", 13),
                 bg=CARD, fg=TEXT).pack(pady=(0, 16))

        self._unlock_popup = popup

        def _hide():
            try:
                self.anim.cancel("ring")
                self.anim.cancel("shake")
                if self._unlock_popup is popup:
                    popup.destroy()
                    self._unlock_popup = None
            except Exception:
                pass

        self.root.after(3200 if is_denied else 3500, _hide)

    def _poll_door_status(self):
        if self.current_screen not in ("control", "system"):
            return

        def worker():
            status, data = self._request_json(
                "GET", f"{SERVER_URL}/api/door_status", timeout=5,
            )
            if status == 200:
                self.last_door_data = data
            else:
                self.last_door_data = {"locked": True, "door": "unknown", "online": False}
            self.root.after(0, self._update_door_labels)

        threading.Thread(target=worker, daemon=True).start()

    def _update_door_labels(self):
        data = self.last_door_data
        locked = data.get("locked", True)
        door = data.get("door", "unknown")

        lock_text = "🔒  CỬA ĐANG KHÓA" if locked else "🔓  CỬA ĐANG MỞ"
        lock_color = DANGER if locked else SUCCESS

        auto_lock_remaining = data.get("auto_lock_remaining", 0)
        if not locked and data.get("auto_lock_enabled") and auto_lock_remaining > 0:
            lock_text += f"\n⏱ Tự khóa lại sau: {auto_lock_remaining}s"

        door_map = {
            "open": "Cửa: ĐANG MỞ",
            "closed": "Cửa: ĐANG ĐÓNG",
            "unknown": "Cửa: CHƯA CÓ CẢM BIẾN",
        }
        door_text = door_map.get(door, "Cửa: KHÔNG RÕ")
        text = f"{lock_text}\n{door_text}"

        if hasattr(self, "control_door_label") and self.control_door_label.winfo_exists():
            self.control_door_label.config(text=text, fg=lock_color)
        if hasattr(self, "door_icon_canvas") and self.door_icon_canvas.winfo_exists():
            try:
                if locked:
                    Icons.lock_closed(self.door_icon_canvas, DANGER, 64)
                else:
                    Icons.lock_open(self.door_icon_canvas, SUCCESS, 64)
            except Exception:
                pass
        if hasattr(self, "system_lock_label") and self.system_lock_label.winfo_exists():
            self.system_lock_label.config(text=lock_text, fg=lock_color)

        if self.current_screen in ("control", "system"):
            self.status_job = self.root.after(STATUS_INTERVAL_MS, self._poll_door_status)

    # ========================================================
    # SYSTEM STATUS
    # ========================================================

    def _show_system_status(self):
        self._clear_content()
        self.current_screen = "system"
        self._hide_camera()

        self._label(self.content_frame, "TRẠNG THÁI HỆ THỐNG", 20, TEXT, True).pack(pady=(6, 8))

        self.system_grid = tk.Frame(self.content_frame, bg=BG)
        self.system_grid.pack()

        self.system_lock_label = self._status_card(self.system_grid, 0, 0, "KHÓA CỬA", "Đang kiểm tra…", Icons.lock_closed)
        self.system_door_label = self._status_card(self.system_grid, 0, 1, "CỬA", "Đang kiểm tra…", Icons.door)
        self.system_camera_label = self._status_card(self.system_grid, 1, 0, "CAMERA", "Đang hoạt động", Icons.camera)
        self.system_face_label = self._status_card(self.system_grid, 1, 1, "FACE ID", "AI Recognition", Icons.face)
        self.system_fingerprint_label = self._status_card(self.system_grid, 2, 0, "VÂN TAY", "ESP32 / R503", Icons.fingerprint)
        self.system_rfid_label = self._status_card(self.system_grid, 2, 1, "RFID", "ESP32 / RC522", Icons.rfid)
        self.system_mq2_label = self._status_card(self.system_grid, 3, 0, "MQ-2", "Đang chờ dữ liệu", Icons.shield)
        self.system_network_label = self._status_card(self.system_grid, 3, 1, "SERVER", "127.0.0.1:5000", Icons.server)

        self._button(
            self.content_frame, "← QUAY LẠI",
            lambda: self._transition_to(self._show_control_panel),
            "ghost", width=14, height=1,
        ).pack(pady=10)

        self._poll_door_status()
        self._poll_scan_status()
        self._poll_optional_system()

    def _status_card(self, parent, row, col, title, value, icon_fn=None):
        card = tk.Frame(
            parent, bg=CARD, padx=12, pady=10,
            highlightthickness=1, highlightbackground=BORDER,
        )
        card.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
        if icon_fn:
            c = tk.Canvas(card, width=28, height=28, bg=CARD, highlightthickness=0)
            c.pack(anchor="w")
            icon_fn(c, PRIMARY, 28)
        self._label(card, title, 10, DIM, True, bg=CARD).pack(anchor="w", pady=(4, 0))
        value_label = self._label(card, value, 13, TEXT, True, bg=CARD, width=20)
        value_label.pack(pady=(4, 0))
        return value_label

    def _poll_optional_system(self):
        if self.current_screen != "system":
            return

        def worker():
            status, data = self._request_json(
                "GET", OPTIONAL_SYSTEM_STATUS_URL, timeout=3,
            )

            def update():
                if self.current_screen != "system":
                    return
                if status == 200:
                    mq2 = data.get("mq2", data.get("gas", "OK"))
                    network = data.get("network", data.get("wifi", "Connected"))
                    self.system_mq2_label.config(
                        text=str(mq2),
                        fg=DANGER if data.get("alarm") else SUCCESS,
                    )
                    self.system_network_label.config(text=str(network), fg=SUCCESS)
                else:
                    self.system_mq2_label.config(text="ESP32 / MQ-2 chưa trả dữ liệu", fg=WARN)
                    self.system_network_label.config(text="Web server cục bộ", fg=SUCCESS)
                self._update_system_from_door()
                if self.current_screen == "system":
                    self.root.after(1500, self._poll_optional_system)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def _update_system_from_door(self):
        locked = self.last_door_data.get("locked", True)
        door = self.last_door_data.get("door", "unknown")
        self.system_lock_label.config(
            text="ĐÃ KHÓA" if locked else "ĐÃ MỞ",
            fg=DANGER if locked else SUCCESS,
        )
        self.system_door_label.config(
            text={"open": "ĐANG MỞ", "closed": "ĐANG ĐÓNG", "unknown": "KHÔNG RÕ"}.get(door, "KHÔNG RÕ"),
            fg=TEXT,
        )

    # ========================================================
    # ACCESS HISTORY
    # ========================================================

    def _show_access_history(self):
        self._clear_content()
        self.current_screen = "history"
        self._hide_camera()

        self._label(self.content_frame, "LỊCH SỬ TRUY CẬP", 20, TEXT, True).pack(pady=(6, 8))

        # Header row
        hdr = tk.Frame(self.content_frame, bg=CARD2, highlightthickness=1, highlightbackground=BORDER)
        hdr.pack(fill="x", padx=8)
        cols = [("THỜI GIAN", 18), ("NGƯỜI DÙNG", 16), ("PHƯƠNG THỨC", 12), ("KẾT QUẢ", 10), ("GHI CHÚ", 20)]
        for text, w in cols:
            tk.Label(
                hdr, text=text, font=("Arial", 10, "bold"),
                bg=CARD2, fg=DIM, width=w, anchor="w",
            ).pack(side="left", padx=4, pady=8)

        outer = self._card(self.content_frame)
        outer.pack(fill="both", expand=True, padx=8, pady=4)

        # Scrollable list via Canvas + Frame
        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.history_list = tk.Frame(canvas, bg=CARD)
        self.history_list.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.history_list, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._history_canvas = canvas

        bottom = tk.Frame(self.content_frame, bg=BG)
        bottom.pack(pady=8)
        self._button(bottom, "↻ TẢI LẠI", self._load_history, "primary", width=12, height=1).pack(side="left", padx=6)
        self._button(
            bottom, "← QUAY LẠI",
            lambda: self._transition_to(self._show_control_panel),
            "ghost", width=12, height=1,
        ).pack(side="left", padx=6)

        self._load_history()

    def _load_history(self):
        def worker():
            status, data = self._request_json(
                "GET", f"{SERVER_URL}/api/login_log", timeout=6,
            )

            def update():
                if self.current_screen != "history":
                    return
                for w in self.history_list.winfo_children():
                    w.destroy()

                if status != 200:
                    self._label(self.history_list, "Không tải được lịch sử từ server.", 12, DANGER).pack(pady=16)
                    return
                logs = data.get("logs", [])
                if not logs:
                    self._label(self.history_list, "Chưa có sự kiện truy cập.", 12, DIM).pack(pady=16)
                    return

                for i, log in enumerate(logs):
                    bg = CARD if i % 2 == 0 else CARD2
                    row = tk.Frame(self.history_list, bg=bg)
                    row.pack(fill="x", pady=1)

                    result = str(log.get("result", "")).upper()
                    if "SUCCESS" in result or "OK" in result or "GRANTED" in result:
                        r_color, r_icon = SUCCESS, "✓"
                    elif "DENY" in result or "FAIL" in result or "REJECT" in result:
                        r_color, r_icon = DANGER, "✕"
                    else:
                        r_color, r_icon = WARN, "⚠"

                    vals = [
                        (str(log.get("time", ""))[:18], 18, TEXT),
                        (str(log.get("identifier", ""))[:16], 16, TEXT),
                        (str(log.get("method", ""))[:12], 12, DIM),
                        (f"{r_icon} {result[:10]}", 10, r_color),
                        (str(log.get("note", ""))[:22], 20, DIM),
                    ]
                    for text, w, color in vals:
                        tk.Label(
                            row, text=text, font=("Arial", 10),
                            bg=bg, fg=color, width=w, anchor="w",
                        ).pack(side="left", padx=4, pady=6)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # ALERTS
    # ========================================================

    def _show_alerts(self):
        self._clear_content()
        self.current_screen = "alerts"
        self._hide_camera()

        self._label(self.content_frame, "CẢNH BÁO BẢO MẬT", 20, TEXT, True).pack(pady=(6, 8))

        self.alert_list_frame = tk.Frame(self.content_frame, bg=BG)
        self.alert_list_frame.pack(fill="both", expand=True, padx=8)

        self._label(self.alert_list_frame, "Đang tải danh sách cảnh báo…", 12, DIM).pack(pady=16)

        self._button(
            self.content_frame, "← QUAY LẠI",
            lambda: self._transition_to(self._show_control_panel),
            "ghost", width=14, height=1,
        ).pack(pady=8)

        self._poll_alert_status()

    def _poll_alert_status(self):
        if self.current_screen != "alerts":
            return

        def worker():
            status, data = self._request_json(
                "GET", f"{SERVER_URL}/api/security_alerts", timeout=5,
            )

            def update():
                if self.current_screen != "alerts":
                    return
                for w in self.alert_list_frame.winfo_children():
                    w.destroy()

                if status != 200:
                    self._label(
                        self.alert_list_frame, "Không kết nối được máy chủ.", 12, DANGER,
                    ).pack(pady=16)
                    self.status_job = self.root.after(1500, self._poll_alert_status)
                    return

                alerts = data.get("alerts", [])[:8]
                if not alerts:
                    self._label(
                        self.alert_list_frame, "✓ Không có cảnh báo nào.", 13, SUCCESS,
                    ).pack(pady=16)
                else:
                    severity_color = {
                        "critical": DANGER, "warning": WARN, "notice": "#C9A227",
                    }
                    severity_label = {
                        "critical": "CRITICAL",
                        "warning": "WARNING",
                        "notice": "NOTICE",
                    }
                    severity_bg = {
                        "critical": DANGER_BG, "warning": WARN_BG, "notice": CARD2,
                    }
                    for alert in alerts:
                        sev = alert.get("severity", "notice")
                        color = severity_color.get(sev, DIM)
                        bg = severity_bg.get(sev, CARD)
                        row = tk.Frame(
                            self.alert_list_frame, bg=bg,
                            highlightthickness=1, highlightbackground=color,
                        )
                        row.pack(fill="x", pady=3)
                        top = tk.Frame(row, bg=bg)
                        top.pack(fill="x", padx=12, pady=(8, 2))
                        tk.Label(
                            top, text=severity_label.get(sev, sev),
                            font=("Arial", 10, "bold"), bg=bg, fg=color,
                        ).pack(side="left")
                        tk.Label(
                            top, text=alert.get("time", ""), font=("Arial", 10),
                            bg=bg, fg=DIM,
                        ).pack(side="right")
                        tk.Label(
                            row,
                            text=f"{alert.get('type', '')}: {alert.get('message', '')}",
                            font=("Arial", 12), bg=bg, fg=TEXT,
                            wraplength=720, justify="left", anchor="w",
                        ).pack(fill="x", padx=12, pady=(0, 8))

                self.status_job = self.root.after(2000, self._poll_alert_status)

            self.root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # LOGOUT
    # ========================================================

    def _logout(self):
        """Clear session locally and return to home theo access_mode."""
        self.logged_in_user = None
        self.mfa_mode = False
        self.mfa_session_id = None
        self.mfa_session = None
        self.mfa_selected = []
        self.pin_value = ""
        self._transitioning = False
        if self._transition_job:
            try:
                self.root.after_cancel(self._transition_job)
            except Exception:
                pass
            self._transition_job = None

        def worker():
            try:
                self._request_json("POST", f"{SERVER_URL}/api/logout", timeout=5)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()
        self._go_home_by_mode()

    # ========================================================
    # CAMERA
    # ========================================================

    def _start_camera_thread(self):
        self.camera_thread = threading.Thread(target=self._camera_worker, daemon=True)
        self.camera_thread.start()

    def _open_local_camera(self):
        """
        Try local webcam / Pi Camera once per index.
        Only index LOCAL_CAMERA_INDEX and 0 — avoid probing index 1.
        Suppresses OpenCV/FFmpeg stderr spam while probing.
        On failure returns None → worker falls back to server snapshot.
        """
        candidates = []
        for idx in (LOCAL_CAMERA_INDEX, 0):
            if idx not in candidates:
                candidates.append(idx)

        backends = []
        if hasattr(cv2, "CAP_DSHOW"):
            backends.append(cv2.CAP_DSHOW)
        if hasattr(cv2, "CAP_V4L2"):
            backends.append(cv2.CAP_V4L2)
        backends.append(None)

        # Silence native OpenCV / FFmpeg / DirectShow noise during probe
        devnull = None
        old_stderr = None
        try:
            devnull = open(os.devnull, "w")
            old_stderr = os.dup(2)
            os.dup2(devnull.fileno(), 2)
        except Exception:
            old_stderr = None

        try:
            for idx in candidates:
                for backend in backends:
                    cap = None
                    try:
                        if backend is None:
                            cap = cv2.VideoCapture(idx)
                        else:
                            cap = cv2.VideoCapture(idx, backend)
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
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass
                    try:
                        ret, _ = cap.read()
                    except Exception:
                        ret = False
                    if ret:
                        print(f"[KIOSK CAM] Local camera OK index={idx}")
                        return cap
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
        return None

    def _snapshot_looks_like_video(self, stream):
        """Return False if frame is the yellow placeholder from USE_LOCAL_CAMERA=0."""
        try:
            ret, frame = stream.read()
            if not ret or frame is None:
                return False
            # Placeholder is mostly dark with yellow text — low variance / small non-dark area
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean = float(gray.mean())
            std = float(gray.std())
            # Real webcam: higher variance; placeholder: dark + little texture
            if mean < 25 and std < 20:
                return False
            return True
        except Exception:
            return False

    def _open_server_stream(self):
        class _SnapshotStream:
            def __init__(self, http, url):
                self.http = http
                self.url = url
                self._ok = True

            def isOpened(self):
                return self._ok

            def read(self):
                try:
                    r = self.http.get(self.url, timeout=2)
                    if r.status_code != 200 or not r.content:
                        return False, None
                    arr = np.frombuffer(r.content, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        return False, None
                    return True, frame
                except Exception:
                    return False, None

            def release(self):
                self._ok = False

        try:
            url = f"{SERVER_URL}/api/snapshot"
            stream = _SnapshotStream(self.http, url)
            ret, frame = stream.read()
            if ret and frame is not None:
                print("[KIOSK CAM] Using server snapshot /api/snapshot")
                return stream
        except Exception as e:
            print(f"[KIOSK CAM] Snapshot error: {e}")
        return None

    def _upload_frame_to_web(self, frame_bgr):
        try:
            ok, buf = cv2.imencode(
                ".jpg", frame_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
            )
            if not ok:
                return False
            self.http.post(
                KIOSK_FRAME_URL,
                data=buf.tobytes(),
                headers={"Content-Type": "image/jpeg"},
                timeout=1.5,
            )
            return True
        except Exception:
            return False

    def _camera_worker(self):
        """
        web_server USE_LOCAL_CAMERA=0:
          Kiosk MUST open local cam → display → POST /api/kiosk_frame
          Server AI reads those frames. Snapshot from server is only a placeholder.
        """
        upload_interval = 1.0 / max(UPLOAD_FPS, 1)
        last_upload = 0.0
        fail_streak = 0

        while self.running:
            cap = None
            using_server_stream = False
            try:
                # Always try LOCAL first (required when server has no camera)
                cap = self._open_local_camera()

                if cap is None and CAMERA_FALLBACK_TO_SERVER:
                    stream = self._open_server_stream()
                    if stream is not None and self._snapshot_looks_like_video(stream):
                        cap = stream
                        using_server_stream = True
                    elif stream is not None:
                        try:
                            stream.release()
                        except Exception:
                            pass

                if cap is None:
                    self._queue_camera_message(
                        "Không mở được camera local.\n"
                        "• Đóng tab web / app đang giữ webcam\n"
                        "• Rồi chạy lại kiosk_ui_v7.py\n"
                        "• Server cần frame từ /api/kiosk_frame"
                    )
                    time.sleep(CAMERA_RECONNECT_SECONDS)
                    continue

                self.camera_cap = cap
                fail_streak = 0

                while self.running:
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        fail_streak += 1
                        if using_server_stream and fail_streak < 15:
                            time.sleep(0.15)
                            continue
                        break
                    fail_streak = 0
                    try:
                        display = cv2.resize(frame, (640, 380))
                        if self.frame_queue.full():
                            try:
                                self.frame_queue.get_nowait()
                            except queue.Empty:
                                pass
                        self._latest_frame_bgr = frame
                        self.frame_queue.put_nowait(display)
                    except queue.Full:
                        pass

                    if not using_server_stream and USE_SERVER_CAMERA_UPLOAD:
                        now = time.time()
                        if now - last_upload >= upload_interval:
                            last_upload = now
                            up = frame
                            h, w = frame.shape[:2]
                            if w > 640:
                                scale = 640.0 / w
                                up = cv2.resize(frame, (640, int(h * scale)))
                            self._upload_frame_to_web(up)
                    else:
                        time.sleep(0.02 if not using_server_stream else 0.08)

            except Exception as exc:
                self._queue_camera_message(f"Lỗi camera: {exc}")
            finally:
                self.camera_cap = None
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
            if self.running:
                time.sleep(CAMERA_RECONNECT_SECONDS)

    def _queue_camera_message(self, message):
        try:
            self.frame_queue.put_nowait(message)
        except queue.Full:
            pass

    def _refresh_camera(self):
        try:
            item = self.frame_queue.get_nowait()
            if isinstance(item, str):
                self.video_label.config(
                    image="", text=item, fg=DIM2, width=640, height=380,
                )
                self.video_label.image = None
                self.latest_frame_bgr = None
                self._camera_ok = False
            else:
                self.latest_frame_bgr = item  # dùng cho Face AI local
                rgb = cv2.cvtColor(item, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb)
                photo = ImageTk.PhotoImage(image=image)
                self.video_label.config(image=photo, text="", width=640, height=380)
                self.video_label.image = photo
                self._last_frame_time = time.time()
                self._camera_ok = True
        except queue.Empty:
            pass
        if self.running:
            self.root.after(60, self._refresh_camera)

    # ========================================================
    # CLOCK / SHUTDOWN
    # ========================================================

    def _update_clock(self):
        if not self.running:
            return
        self.clock_label.config(text=time.strftime("%d/%m/%Y  %H:%M:%S"))
        self.clock_job = self.root.after(1000, self._update_clock)

    def close(self):
        self.running = False
        try:
            self.anim.cancel()  # stop all micro-anims
        except Exception:
            pass
        if self._status_poll_job:
            try:
                self.root.after_cancel(self._status_poll_job)
            except Exception:
                pass
        if getattr(self, "_stranger_monitor_job", None):
            try:
                self.root.after_cancel(self._stranger_monitor_job)
            except Exception:
                pass
        try:
            if self.camera_cap:
                self.camera_cap.release()
        except Exception:
            pass
        try:
            self.http.close()
        except Exception:
            pass
        self.root.destroy()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = KioskApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
