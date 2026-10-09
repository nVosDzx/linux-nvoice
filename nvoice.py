"""
nVoice (Linux) — local push-to-talk dictation.

Hold Ctrl+Alt, speak, release. The transcript is pasted at your cursor.
Everything runs offline: Whisper (sherpa-onnx) for speech-to-text, and an
optional local Ollama model to clean up the transcript.

Usage:
    nvoice              start in the background (tray icon); the terminal is free
    nvoice --no-llm     skip LLM cleanup (also: -n, --nollm, -nollm)
    nvoice --foreground stay attached to the terminal and print the log (-f)
    nvoice --quit       stop the running instance

Works on Wayland and X11: hotkeys are read from /dev/input and the paste
keystroke is sent through /dev/uinput, so your user needs access to both
(setup.sh handles that).
"""

import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "nvoice.lock"
LOG_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "nvoice" / "nvoice.log"
ICON_PATH = SCRIPT_DIR / "icons" / "nvoice.png"
ICON_TRAY_PATH = SCRIPT_DIR / "icons" / "nvoice-tray.png"
ICON_ACTIVE_PATH = SCRIPT_DIR / "icons" / "nvoice-active.png"
ICON_PROCESSING_PATH = SCRIPT_DIR / "icons" / "nvoice-processing.png"
STARTED_MARKER = "nVoice started."


# ---------- Launcher (runs before the heavy imports, so `nvoice` returns fast) ----------

def parse_args():
    parser = argparse.ArgumentParser(description="nVoice — local push-to-talk dictation")
    parser.add_argument("-n", "--no-llm", "--nollm", "-nollm", dest="no_llm",
                        action="store_true", help="skip LLM cleanup of transcripts")
    parser.add_argument("-f", "--foreground", action="store_true",
                        help="stay attached to the terminal and print the log")
    parser.add_argument("--quit", action="store_true", help="stop the running nVoice")
    return parser.parse_args()


def running_pid():
    """PID of the running nVoice, or None. The instance holds a flock on LOCK_PATH."""
    with open(LOCK_PATH, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            f.seek(0)
            pid = f.read().strip()
            return int(pid) if pid.isdigit() else -1


def launch_in_background():
    """Start a detached copy with --foreground (its output goes to LOG_PATH),
    wait until it reports it started (or died), then return to the shell."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w") as log_file:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--foreground", *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # own session: closing the terminal doesn't kill it
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print(LOG_PATH.read_text().strip(), file=sys.stderr)
            sys.exit(child.returncode or 1)
        if STARTED_MARKER in LOG_PATH.read_text():
            break
        time.sleep(0.1)
    print("nVoice is running in the background. Look for its tray icon.\n"
          "Hold Ctrl+Alt to talk. Quit from the tray menu, with Ctrl+Alt+Q, or `nvoice --quit`.\n"
          f"Log: {LOG_PATH}")
    sys.exit(0)


if __name__ == "__main__":
    ARGS = parse_args()
    if ARGS.quit:
        pid = running_pid()
        if not pid or pid < 0:
            sys.exit("nVoice is not running.")
        os.kill(pid, signal.SIGTERM)
        print("nVoice stopped.")
        sys.exit(0)
    if running_pid():
        sys.exit("nVoice is already running (look for the tray icon). Stop it with: nvoice --quit")
    if not ARGS.foreground:
        launch_in_background()


# ---------- The app itself ----------

# Run the Qt overlay through XWayland: native Wayland windows can't position
# themselves or stay on top, which the floating pill needs.
if os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import collections
import json
import math
import queue
import random
import re
import selectors
import threading
import urllib.request

import numpy as np
import sounddevice as sd
import yaml
import evdev
from evdev import ecodes
import sherpa_onnx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QImage, QPainter
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

with open(SCRIPT_DIR / "config.yaml") as f:
    config = yaml.safe_load(f)

HOTKEY_CFG = config["hotkey"]
AUDIO_CFG = config["audio"]
STT_CFG = config["stt"]
PASTE_CFG = config["paste"]
LLM_CFG = config["llm"]
UI_CFG = config.get("ui", {})

SAMPLERATE = AUDIO_CFG["samplerate"]
CHANNELS = AUDIO_CFG["channels"]

UINPUT_NAME = "nvoice-virtual-keyboard"

# ui_queue carries commands from worker threads to the Qt thread;
# level_queue carries mic RMS levels for the bar animation.
ui_queue = queue.Queue()
level_queue = queue.Queue()


def log(msg):
    print(msg, flush=True)


def notify(msg):
    """Desktop notification, for errors the user can't see in a detached process."""
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "nVoice", "-i", str(ICON_PATH), "nVoice", msg])


def fatal(msg):
    log(f"ERROR: {msg}")
    notify(msg)
    sys.exit(1)


def rms_to_db(rms):
    return 20.0 * np.log10(max(rms, 1e-6))


# ---------- UI Config ----------
PILL_WIDTH = 260
PILL_HEIGHT = 45
SUPERSAMPLE = 4
FPS_MS = 33

GLASS_TOP = "#3A3A3C"
GLASS_BOTTOM = "#1C1C1E"
GLASS_OPACITY = float(UI_CFG.get("glass_opacity", 0.55))  # 1.0 = solid like the Windows version
GLASS_BLUR = bool(UI_CFG.get("blur", True))
# Palette: neutral graphite glass, one cool accent per state.
LISTEN_TOP = "#FFFFFF"       # listening bars: white fading to sky blue...
LISTEN_BOTTOM = "#64D2FF"
LISTEN_GLOW = "#0A84FF"      # ...with an azure glow
PROCESS_TOP = "#E0DFFF"      # transcribing bars: soft lavender to indigo
PROCESS_BOTTOM = "#7D7AFF"

BAR_COUNT = 20
IDLE_AMPLITUDE = 0.16

NOISE_GATE_DB = -55.0
LOUD_RANGE_DB = 15.0
ATTACK = 0.6
RELEASE = 0.22
DB_FLOOR = -90.0


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend_rgb(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def blend_hex(c1, c2, t):
    return blend_rgb(_hex_to_rgb(c1), _hex_to_rgb(c2), t)


def load_font(size):
    for name in ("NotoSans-Regular.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


# ---------- Floating Pill ----------

class VoicePill(QWidget):
    """Frameless, always-on-top, never-focused overlay. Frames are drawn with
    PIL at SUPERSAMPLE x resolution and downscaled, as in the Windows version."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(PILL_WIDTH, PILL_HEIGHT)

        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.x() + (geo.width() - PILL_WIDTH) // 2,
                  geo.y() + geo.height() - PILL_HEIGHT - 80)

        self.ss = SUPERSAMPLE
        self.ssw = PILL_WIDTH * self.ss
        self.ssh = PILL_HEIGHT * self.ss

        spacing = (PILL_WIDTH - 80) / BAR_COUNT
        self.bar_x_positions = [20 + i * spacing + spacing / 2 for i in range(BAR_COUNT)]
        self.bar_width = max(2.0, spacing * 0.35)

        self.shape_mask = Image.new("L", (self.ssw, self.ssh), 0)
        ImageDraw.Draw(self.shape_mask).rounded_rectangle(
            [0, 0, self.ssw - 1, self.ssh - 1], radius=self.ssh / 2, fill=255)
        self.base_layer = self._build_base_layer()

        self.listen_gradient = self._vertical_gradient(LISTEN_TOP, LISTEN_BOTTOM)
        self.process_gradient = self._vertical_gradient(PROCESS_TOP, PROCESS_BOTTOM)

        self.icon_mic = self._make_mic_glyph()
        self.icon_box = (PILL_WIDTH - 45, 5, PILL_WIDTH - 20, PILL_HEIGHT - 5)
        self.font = load_font(10 * self.ss)

        self.bar_current = [IDLE_AMPLITUDE] * BAR_COUNT
        self.smoothed_db = DB_FLOOR
        self.state = "hidden"  # hidden | loading | recording | processing
        self.on_state = lambda state: None  # set by the tray icon
        self.frame = None
        self.drag_offset = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(FPS_MS)

        if GLASS_BLUR:
            self._request_background_blur()

    def _request_background_blur(self):
        """Ask KWin (KDE) to blur what's behind the pill, for a frosted-glass look.
        Uses the _KDE_NET_WM_BLUR_BEHIND_REGION X11 property, with the capsule
        shape given as one rectangle per pixel row. Other desktops ignore it and
        just show the translucent tint."""
        if QApplication.platformName() != "xcb":
            return
        try:
            from Xlib import Xatom, display
            dpr = self.devicePixelRatioF()
            w, h = round(PILL_WIDTH * dpr), round(PILL_HEIGHT * dpr)
            r = h / 2
            rects = []
            for y in range(h):
                dy = abs(y + 0.5 - r)
                inset = math.ceil(r - math.sqrt(max(0.0, r * r - dy * dy)))
                rects += [inset, y, w - 2 * inset, 1]
            disp = display.Display()
            win = disp.create_resource_object("window", int(self.winId()))
            win.change_property(disp.intern_atom("_KDE_NET_WM_BLUR_BEHIND_REGION"),
                                Xatom.CARDINAL, 32, rects)
            disp.flush()
            disp.close()
        except Exception as e:
            log(f"(background blur unavailable: {e})")

    # --- window behaviour ---

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None

    def paintEvent(self, event):
        if self.frame is not None:
            QPainter(self).drawImage(QPoint(0, 0), self.frame)

    def _set_state(self, new_state):
        self.state = new_state
        self.on_state(new_state)
        if new_state == "hidden":
            self.hide()
        else:
            if new_state == "recording":
                self.smoothed_db = DB_FLOOR
            if not self.isVisible():
                self.show()
                self.raise_()

    def _tick(self):
        try:
            while True:
                cmd = ui_queue.get_nowait()
                if cmd == "quit":
                    QApplication.quit()
                    return
                self._set_state(cmd)
        except queue.Empty:
            pass
        if self.state != "hidden":
            self._render()

    # --- drawing ---

    def _vertical_gradient(self, top, bottom):
        w = int(self.bar_width * self.ss) + 2
        img = Image.new("RGBA", (w, self.ssh), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for y in range(self.ssh):
            d.line([(0, y), (w, y)], fill=blend_hex(top, bottom, y / self.ssh) + (255,))
        return img

    def _make_mic_glyph(self):
        """A small line-art microphone, drawn in white to sit quietly on the glass."""
        s = self.ss
        img = Image.new("RGBA", (12 * s, 16 * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ink = (255, 255, 255, 200)
        stroke = int(1.4 * s)
        d.rounded_rectangle([3.5 * s, 0.5 * s, 8.5 * s, 10 * s], radius=2.5 * s, fill=ink)
        d.arc([1 * s, 3 * s, 11 * s, 13 * s], start=0, end=180, fill=ink, width=stroke)
        d.line([(6 * s, 13 * s), (6 * s, 15.5 * s)], fill=ink, width=stroke)
        return img

    def _build_base_layer(self):
        ss = self.ss
        w, h = self.ssw, self.ssh
        r = h / 2

        mask = self.shape_mask
        t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]

        # Tint: lighter and more see-through at the top, denser at the bottom.
        top_c = np.array(_hex_to_rgb(GLASS_TOP) + (255 * GLASS_OPACITY * 0.8,), dtype=np.float32)
        bot_c = np.array(_hex_to_rgb(GLASS_BOTTOM) + (255 * GLASS_OPACITY,), dtype=np.float32)
        row = top_c[None, :] + (bot_c - top_c) * t
        grad = np.repeat(row[:, None, :], w, axis=1)
        gradient_img = Image.fromarray(grad.astype(np.uint8), "RGBA")

        big = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        big.paste(gradient_img, (0, 0), mask)

        # Glass rim: a thin bright edge, strongest along the top (light catching the edge).
        inner = Image.new("L", (w, h), 0)
        rim_w = 1.3 * ss
        ImageDraw.Draw(inner).rounded_rectangle(
            [rim_w, rim_w, w - 1 - rim_w, h - 1 - rim_w], radius=r - rim_w, fill=255)
        rim = ImageChops.subtract(mask, inner)
        fade = Image.fromarray((np.repeat(130 - 105 * t, w, axis=1)).astype(np.uint8), "L")
        rim_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        rim_layer.putalpha(ImageChops.multiply(rim, fade))
        big.alpha_composite(rim_layer)

        sheen = Image.new("L", (w, h), 0)
        band_h = h * 0.55
        ImageDraw.Draw(sheen).polygon(
            [(-w * 0.1, h * 0.05), (w * 0.55, -h * 0.1),
             (w * 0.75, band_h * 0.5), (w * 0.05, band_h * 0.8)],
            fill=45,
        )
        sheen = sheen.filter(ImageFilter.GaussianBlur(radius=2 * ss))
        sheen_mask = Image.composite(sheen, Image.new("L", (w, h), 0), mask)
        sheen_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        sheen_layer.putalpha(sheen_mask)
        big.alpha_composite(sheen_layer)
        return big

    def _bar_box(self, i, width):
        ss = self.ss
        half_h = max(2, self.bar_current[i] * (PILL_HEIGHT - 16) / 2)
        w_px = int(width * ss)
        x = int(self.bar_x_positions[i] * ss)
        return [x - w_px // 2, int((PILL_HEIGHT / 2 - half_h) * ss),
                x + w_px // 2, int((PILL_HEIGHT / 2 + half_h) * ss)], max(1, w_px // 2)

    def _draw_text(self, draw, text, alpha):
        bbox = draw.textbbox((0, 0), text, font=self.font)
        tx = (self.ssw - (bbox[2] - bbox[0])) / 2
        ty = (self.ssh - (bbox[3] - bbox[1])) / 2 - bbox[1]
        draw.text((tx, ty), text, fill=(255, 255, 255, alpha), font=self.font)

    def _paste_bar(self, work, box, radius, gradient):
        x0, y0, x1, y1 = box
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius=radius, fill=255)
        work.paste(gradient.crop((0, y0, x1 - x0, y1)), (x0, y0), mask)

    def _draw_processing(self, work):
        t = time.time()
        for i in range(BAR_COUNT):
            wave_val = math.sin(i / BAR_COUNT * math.pi * 3 - t * 6) * 0.5 + 0.5
            target = 0.2 + wave_val * 0.6
            self.bar_current[i] += (target - self.bar_current[i]) * 0.4
            self._paste_bar(work, *self._bar_box(i, self.bar_width), self.process_gradient)

    def _draw_recording(self, work):
        latest = None
        try:
            while True:
                latest = level_queue.get_nowait()
        except queue.Empty:
            pass

        if latest is not None:
            latest_db = rms_to_db(latest)
            k = ATTACK if latest_db > self.smoothed_db else RELEASE
            self.smoothed_db += (latest_db - self.smoothed_db) * k

        is_idle = self.smoothed_db < NOISE_GATE_DB
        norm = 0.0
        if not is_idle:
            norm = max(0.0, min(1.0, (self.smoothed_db - NOISE_GATE_DB) / LOUD_RANGE_DB))

        glow_color = _hex_to_rgb(LISTEN_GLOW)
        t = time.time()
        # Glows go on their own layer and are blended over the glass afterwards.
        # Drawing them straight onto `work` would *replace* its pixels with
        # semi-transparent ones and punch see-through holes around the bars.
        glow_layer = Image.new("RGBA", work.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_layer)
        bars = []

        for i in range(BAR_COUNT):
            wave = (math.sin(i * 0.5 - t * 7) * 0.4 + 0.6) * (math.sin(i * 1.1 + t * 4) * 0.2 + 0.8)
            if is_idle:
                target, ease = IDLE_AMPLITUDE * wave, 0.2
            else:
                target = (IDLE_AMPLITUDE * 0.5 + norm ** 0.5 * 1.1) * wave * random.uniform(0.95, 1.05)
                ease = 0.4
            self.bar_current[i] += (target - self.bar_current[i]) * ease

            if not is_idle:
                for width_mult, alpha in ((2.6, 40), (1.7, 80)):
                    box, radius = self._bar_box(i, self.bar_width * width_mult)
                    glow_draw.rounded_rectangle(box, radius=radius, fill=glow_color + (alpha,))

            bars.append(self._bar_box(i, self.bar_width))

        work.alpha_composite(glow_layer)
        for box, radius in bars:
            self._paste_bar(work, box, radius, self.listen_gradient)

    def _render(self):
        work = self.base_layer.copy()
        draw = ImageDraw.Draw(work, "RGBA")  # blend semi-transparent ink over the glass

        if self.state == "loading":
            pulse = (math.sin(time.time() * 3) + 1) / 2
            self._draw_text(draw, "Loading model…", int(100 + 155 * pulse))
        else:
            if self.state == "processing":
                self._draw_processing(work)
            else:
                self._draw_recording(work)
            x1, y1, x2, y2 = self.icon_box
            work.alpha_composite(self.icon_mic, (
                int((x1 + x2) / 2 * self.ss - self.icon_mic.width / 2),
                int((y1 + y2) / 2 * self.ss - self.icon_mic.height / 2)))

        work.putalpha(ImageChops.multiply(work.split()[3], self.shape_mask))  # clip to the capsule

        dpr = self.devicePixelRatioF()
        out_w, out_h = round(PILL_WIDTH * dpr), round(PILL_HEIGHT * dpr)
        final = work.resize((out_w, out_h), Image.LANCZOS)
        frame = QImage(final.tobytes("raw", "RGBA"), out_w, out_h, QImage.Format_RGBA8888).copy()
        frame.setDevicePixelRatio(dpr)
        self.frame = frame
        self.update()


# ---------- Audio ----------

class Recorder:
    """Captures mic audio. With always_on, the stream stays open and the last
    preroll_ms are kept in a ring buffer, so speech that starts at the same
    moment as the keypress isn't clipped."""

    def __init__(self):
        self.lock = threading.Lock()
        self.recording = False
        self.chunks = []
        self.preroll = collections.deque()
        self.preroll_samples = 0
        self.preroll_max = int(SAMPLERATE * AUDIO_CFG["preroll_ms"] / 1000)
        self.always_on = AUDIO_CFG["always_on"]
        self.started_at = 0.0
        self.stream = None
        if self.always_on:
            self._open()

    def _open(self):
        self.stream = sd.InputStream(samplerate=SAMPLERATE, channels=CHANNELS,
                                     dtype="float32", callback=self._callback)
        self.stream.start()

    def _close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _callback(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        with self.lock:
            if self.recording:
                self.chunks.append(block)
                level_queue.put(float(np.sqrt(np.mean(block ** 2))))
            elif self.always_on:
                self.preroll.append(block)
                self.preroll_samples += len(block)
                while self.preroll and self.preroll_samples - len(self.preroll[0]) >= self.preroll_max:
                    self.preroll_samples -= len(self.preroll.popleft())

    def start(self):
        with self.lock:
            self.chunks = list(self.preroll)
            self.preroll.clear()
            self.preroll_samples = 0
            self.recording = True
            self.started_at = time.monotonic()
        if not self.always_on:
            self._open()

    def stop(self):
        """Returns (audio, seconds_held)."""
        with self.lock:
            self.recording = False
            chunks, self.chunks = self.chunks, []
            held = time.monotonic() - self.started_at
        if not self.always_on:
            self._close()
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return audio, held


# ---------- Speech-to-text ----------

class Transcriber:
    MAX_CHUNK_S = 28  # Whisper only sees 30s windows; split longer dictation

    def __init__(self):
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(SCRIPT_DIR / STT_CFG["encoder"]),
            decoder=str(SCRIPT_DIR / STT_CFG["decoder"]),
            tokens=str(SCRIPT_DIR / STT_CFG["tokens"]),
            language="en",
            task="transcribe",
            num_threads=STT_CFG["num_threads"],
            provider=STT_CFG["provider"],
        )

    def transcribe(self, audio):
        step = self.MAX_CHUNK_S * SAMPLERATE
        parts = []
        for i in range(0, len(audio), step):
            stream = self.recognizer.create_stream()
            stream.accept_waveform(SAMPLERATE, audio[i:i + step])
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            # Whisper labels non-speech as e.g. "[BLANK_AUDIO]" or "(wind blowing)"
            text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text).strip()
            if text:
                parts.append(text)
        return " ".join(parts)


# ---------- Optional LLM cleanup ----------

def ollama_reachable():
    try:
        urllib.request.urlopen(LLM_CFG["base_url"], timeout=2)
        return True
    except Exception:
        return False


def cleanup_transcript(raw_text):
    payload = {
        "model": LLM_CFG["model"],
        "prompt": raw_text,
        "system": LLM_CFG["system_prompt"],
        "stream": False,
        "options": {"temperature": LLM_CFG["temperature"], "num_ctx": 2048},
    }
    req = urllib.request.Request(
        f"{LLM_CFG['base_url']}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["response"].strip() or raw_text
    except Exception as e:
        log(f">> LLM cleanup failed ({e}), using raw transcript.")
        return raw_text


# ---------- Paste ----------

KEY_NAMES = {"ctrl": ecodes.KEY_LEFTCTRL, "shift": ecodes.KEY_LEFTSHIFT,
             "alt": ecodes.KEY_LEFTALT, "super": ecodes.KEY_LEFTMETA}


def parse_combo(combo):
    return [KEY_NAMES.get(k) or ecodes.ecodes[f"KEY_{k.upper()}"]
            for k in combo.lower().replace(" ", "").split("+")]


if os.environ.get("WAYLAND_DISPLAY"):
    CLIP_GET = ["wl-paste", "--no-newline", "--type", "text"]
    CLIP_SET = ["wl-copy"]
else:
    CLIP_GET = ["xclip", "-selection", "clipboard", "-o"]
    CLIP_SET = ["xclip", "-selection", "clipboard", "-i"]


class Paster:
    """Puts text on the clipboard (wl-copy on Wayland, xclip on X11) and sends
    the paste combo through a virtual uinput keyboard, which works in every app."""

    def __init__(self):
        self.combo = parse_combo(PASTE_CFG["combo"])
        try:
            self.ui = evdev.UInput({ecodes.EV_KEY: self.combo}, name=UINPUT_NAME)
        except (PermissionError, OSError) as e:
            self.ui = None
            log(f"WARNING: can't open /dev/uinput ({e}). Text will be copied to the "
                f"clipboard but not pasted. Run setup.sh, then reboot.")
            notify("Can't send the paste keystroke, so text will only be copied to the "
                   "clipboard. Run setup.sh, then reboot.")

    def _get_clipboard(self):
        try:
            r = subprocess.run(CLIP_GET, capture_output=True, timeout=1)
            return r.stdout if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _set_clipboard(self, data: bytes):
        subprocess.run(CLIP_SET, input=data, check=True, timeout=2)

    def paste(self, text):
        original = self._get_clipboard() if PASTE_CFG["restore_clipboard"] else None
        self._set_clipboard(text.encode())
        if not self.ui:
            log(">> Copied to clipboard (paste with Ctrl+V).\n")
            return
        time.sleep(0.05)
        for key in self.combo:
            self.ui.write(ecodes.EV_KEY, key, 1)
            self.ui.syn()
        for key in reversed(self.combo):
            self.ui.write(ecodes.EV_KEY, key, 0)
            self.ui.syn()
        if original is not None:
            time.sleep(PASTE_CFG["restore_delay_ms"] / 1000)
            self._set_clipboard(original)
        log(">> Pasted.\n")


# ---------- Global hotkey (evdev) ----------

MODIFIER_GROUPS = {
    "ctrl": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
    "alt": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
    "shift": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
    "super": {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA},
}
ALL_MODIFIERS = set().union(*MODIFIER_GROUPS.values())


class HotkeyListener(threading.Thread):
    """Reads every keyboard under /dev/input directly, so the chord is seen no
    matter which window has focus (pynput can't do this on Wayland)."""

    RESCAN_S = 3  # pick up keyboards that are plugged in / wake up later

    def __init__(self, on_start, on_stop, on_cancel, on_quit):
        super().__init__(daemon=True)
        self.on_start, self.on_stop = on_start, on_stop
        self.on_cancel, self.on_quit = on_cancel, on_quit
        self.required = [MODIFIER_GROUPS[k] for k in HOTKEY_CFG["hold"]]
        self.held = set()
        self.chord_active = False
        self.spoiled = False  # another key was pressed during the chord
        self.selector = selectors.DefaultSelector()
        self.devices = {}  # path -> InputDevice

    def scan(self):
        for path in evdev.list_devices():
            if path in self.devices:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if dev.name != UINPUT_NAME and ecodes.KEY_A in keys and ecodes.KEY_LEFTCTRL in keys:
                self.devices[path] = dev
                self.selector.register(dev, selectors.EVENT_READ)
            else:
                dev.close()
        return len(self.devices)

    def _drop(self, dev):
        self.selector.unregister(dev)
        self.devices.pop(dev.path, None)
        dev.close()

    def modifiers_released(self):
        return not (self.held & ALL_MODIFIERS)

    def run(self):
        last_scan = time.monotonic()
        while True:
            for key, _ in self.selector.select(timeout=1):
                dev = key.fileobj
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY and ev.value != 2:
                            self._handle(ev.code, ev.value == 1)
                except OSError:
                    self._drop(dev)  # unplugged
            if time.monotonic() - last_scan > self.RESCAN_S:
                self.scan()
                last_scan = time.monotonic()

    def _handle(self, code, down):
        if down:
            self.held.add(code)
        else:
            self.held.discard(code)

        if code not in ALL_MODIFIERS:
            if down and self.chord_active and not self.spoiled:
                self.spoiled = True
                if code == ecodes.KEY_Q:
                    self.on_quit()
                else:
                    self.on_cancel()
            return

        chord_held = all(group & self.held for group in self.required)
        if chord_held and not self.chord_active:
            self.chord_active = True
            self.spoiled = False
            self.on_start()
        elif self.chord_active and not chord_held:
            self.chord_active = False
            if not self.spoiled:
                self.on_stop()


# ---------- App ----------

class NVoice:
    def __init__(self, use_llm):
        self.use_llm = use_llm
        self.transcriber = None
        self.recorder = Recorder()
        self.paster = Paster()
        self.jobs = queue.Queue()
        self.hotkeys = HotkeyListener(self.start, self.stop, self.cancel, self.quit)

    def load_model(self):
        t0 = time.monotonic()
        try:
            self.transcriber = Transcriber()
        except Exception as e:
            log(f"ERROR: failed to load STT model: {e}")
            notify(f"Failed to load the speech model: {e}")
            ui_queue.put("quit")
            return
        log(f"STT model loaded in {time.monotonic() - t0:.1f}s. Ready — hold "
            f"{'+'.join(HOTKEY_CFG['hold'])} to talk, ctrl+alt+q to quit.\n")
        ui_queue.put("hidden")
        threading.Thread(target=self.worker, daemon=True).start()

    def start(self):
        if not self.transcriber:
            log(">> Model still loading, please wait...")
            return
        self.recorder.start()
        ui_queue.put("recording")
        log(">> Recording...")

    def stop(self):
        if not self.transcriber:
            return
        audio, held = self.recorder.stop()
        if held * 1000 < HOTKEY_CFG["min_duration_ms"]:
            log(">> Too short, ignored.")
            ui_queue.put("hidden")
            return
        ui_queue.put("processing")
        self.jobs.put(audio)

    def cancel(self):
        if not self.transcriber:
            return
        self.recorder.stop()
        ui_queue.put("hidden")
        log(">> Cancelled (another key was pressed).")

    def quit(self):
        log("Ctrl+Alt+Q pressed, exiting.")
        ui_queue.put("quit")

    def worker(self):
        while True:
            audio = self.jobs.get()
            try:
                self.process(audio)
            except Exception as e:
                log(f">> Error: {e}")
            if self.jobs.empty():
                ui_queue.put("hidden")

    def process(self, audio):
        peak_db = rms_to_db(np.sqrt(np.mean(audio ** 2))) if len(audio) else DB_FLOOR
        if peak_db < AUDIO_CFG["silence_db"]:
            log(f">> Silence ({peak_db:.0f} dB), skipping.")
            return

        t0 = time.monotonic()
        text = self.transcriber.transcribe(audio)
        log(f">> Raw ({time.monotonic() - t0:.2f}s): {text}")
        if not text:
            return

        if self.use_llm and len(text.split()) >= LLM_CFG["skip_under_words"]:
            text = cleanup_transcript(text)
            log(f">> Cleaned: {text}")

        # The chord's other modifier may still be physically down; pasting now
        # would send e.g. Alt+Ctrl+V instead of Ctrl+V.
        deadline = time.monotonic() + 2
        while not self.hotkeys.modifiers_released() and time.monotonic() < deadline:
            time.sleep(0.02)

        self.paster.paste(text)


class Tray(QSystemTrayIcon):
    """Tray icon with a small menu. Turns orange while recording/transcribing."""

    def __init__(self, nvoice):
        super().__init__()
        self.nvoice = nvoice
        self.icon_idle = QIcon(str(ICON_TRAY_PATH))
        self.icon_active = QIcon(str(ICON_ACTIVE_PATH))
        self.icon_processing = QIcon(str(ICON_PROCESSING_PATH))
        self.setIcon(self.icon_idle)
        self.setToolTip("nVoice: hold Ctrl+Alt to talk")

        menu = QMenu()
        self.status_action = menu.addAction("Loading model…")
        self.status_action.setEnabled(False)
        menu.addSeparator()
        self.llm_action = QAction("LLM cleanup (Ollama)", menu, checkable=True)
        self.llm_action.setChecked(nvoice.use_llm)
        self.llm_action.toggled.connect(self._toggle_llm)
        menu.addAction(self.llm_action)
        menu.addAction("Open settings (config.yaml)").triggered.connect(
            lambda: subprocess.Popen(["xdg-open", str(SCRIPT_DIR / "config.yaml")]))
        menu.addAction("Open log").triggered.connect(
            lambda: subprocess.Popen(["xdg-open", str(LOG_PATH)]))
        menu.addSeparator()
        menu.addAction("Quit nVoice").triggered.connect(QApplication.quit)
        self.menu = menu  # keep a reference, Qt doesn't own it
        self.setContextMenu(menu)

    def set_state(self, state):
        self.setIcon({"recording": self.icon_active,
                      "processing": self.icon_processing}.get(state, self.icon_idle))
        if state == "loading":
            self.status_action.setText("Loading model…")
        elif state == "recording":
            self.status_action.setText("Listening…")
        elif state == "processing":
            self.status_action.setText("Transcribing…")
        else:
            self.status_action.setText(f"Ready: hold {'+'.join(HOTKEY_CFG['hold']).title()} to talk")

    def _toggle_llm(self, checked):
        if checked and not ollama_reachable():
            self.showMessage("nVoice", f"Ollama isn't running at {LLM_CFG['base_url']}. "
                             "Start it, then try again.", self.icon_idle)
            self.llm_action.setChecked(False)
            return
        self.nvoice.use_llm = checked
        log(f">> LLM cleanup {'enabled' if checked else 'disabled'}.")


def hold_instance_lock():
    lock_file = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("nVoice is already running.")
    lock_file.truncate(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def main():
    lock = hold_instance_lock()  # noqa: F841 (held for process lifetime)

    use_llm = LLM_CFG["enabled"] and not ARGS.no_llm
    if ARGS.no_llm:
        log(">> LLM cleanup disabled (--no-llm).")
    elif use_llm and not ollama_reachable():
        log(f">> Ollama not reachable at {LLM_CFG['base_url']}, pasting raw transcripts.")
        use_llm = False

    app_qt = QApplication(sys.argv)
    app_qt.setApplicationName("nVoice")
    app_qt.setDesktopFileName("nvoice")
    app_qt.setWindowIcon(QIcon(str(ICON_PATH)))
    app_qt.setQuitOnLastWindowClosed(False)
    for sig in (signal.SIGINT, signal.SIGTERM):  # Ctrl+C, `nvoice --quit`
        signal.signal(sig, lambda *_: QApplication.quit())

    pill = VoicePill()

    try:
        nvoice = NVoice(use_llm)
    except sd.PortAudioError as e:
        fatal(f"Can't open the microphone: {e}")

    if nvoice.hotkeys.scan() == 0:
        fatal("No readable keyboards in /dev/input. Run setup.sh, then reboot.")
    nvoice.hotkeys.start()

    tray = Tray(nvoice)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        log("WARNING: no system tray found (on GNOME, install the AppIndicator extension). "
            "Quit with Ctrl+Alt+Q or `nvoice --quit`.")
    pill.on_state = tray.set_state
    ui_queue.put("loading")

    log(f"{STARTED_MARKER} (STT: {Path(STT_CFG['encoder']).parent.name}, "
        f"LLM cleanup: {'on' if use_llm else 'off'}, pid {os.getpid()})")
    threading.Thread(target=nvoice.load_model, daemon=True).start()

    app_qt.exec()
    log("nVoice stopped.")
    os._exit(0)  # don't wait on the audio / evdev threads


if __name__ == "__main__":
    main()
