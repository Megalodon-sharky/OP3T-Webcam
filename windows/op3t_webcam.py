#!/usr/bin/env python3
"""
OP3T Webcam — Windows receiver + control GUI.

Pipeline (all heavy lifting is reused; this file is wiring + UI):
  phone TCP 8080  --adb forward-->  localhost:8080
      -> on connect, PC sends "WIDTHxHEIGHT@FPS\n"; phone encodes to that
      -> raw H.264 Annex-B bytes
      -> ffmpeg decodes (GPU or CPU), passthrough timing, applies orientation
      -> a reader thread keeps only the NEWEST decoded frame (drops stale ones)
      -> pyvirtualcam pushes that frame into the OBS Virtual Camera
      -> any app (Discord/Zoom/Teams/OBS) sees "OBS Virtual Camera"

Why the reader drops frames: if the PC is briefly busy, decoded frames must NOT
queue up — a queue turns into seconds of latency that never drains. Always showing
the newest frame keeps glass-to-glass latency bounded (this is what scrcpy does).

Live control travels back up the same socket:
  "ZOOM <ratio> <cx> <cy>", "FOCUS", "NR <0|1>" (phone-side noise reduction).

Prereqs (install once): adb, ffmpeg, OBS Studio (vcam backend),
  pip install pyvirtualcam numpy   (tkinter ships with Python)

Run:
  pythonw op3t_webcam.py           # GUI (no console). Or double-click "OP3T Webcam.vbs".
  python  op3t_webcam.py --test    # vcam test pattern, no phone
  python  op3t_webcam.py --headless --width 1280 --height 720 --fps 60
"""
import argparse
import contextlib
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import pyvirtualcam

# Optional. GUARDED because latency_probe.py imports this module and must keep working on a machine
# without OpenCV — and because the numpy path below is a correct fallback, not a stub.
try:
    import cv2
    # OpenCV defaults to one thread per core (MEASURED: 8 here). ffmpeg's decode runs at HIGH
    # priority per the existing lag fix, so letting cv2 grab every core makes them fight.
    cv2.setNumThreads(2)
except Exception:
    cv2 = None


def tool_path(name):
    """Path to a bundled tool (ffmpeg.exe/adb.exe) when frozen by PyInstaller, else just the
    name so PATH is used in dev. PyInstaller onefile unpacks bundled files under sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, "tools", name)
        if os.path.exists(cand):
            return cand
    return name


FFMPEG = tool_path("ffmpeg.exe")
ADB = tool_path("adb.exe")
# MEASURED 2026-10-04: an adb server started without this binds UDP :::5353 for mDNS discovery
# (wireless debugging), and Windows Firewall asks about any program that listens beyond loopback. The
# exe unpacks adb.exe into a fresh _MEIxxxxx folder on EVERY launch and firewall rules are keyed on the
# full path, so that "allow adb.exe?" prompt came back on every single launch — the dev box had piled
# up 182 rules, one per _MEI folder. With ADB_MDNS=0 the server opens nothing but 127.0.0.1:5037. The
# server inherits it from whichever adb client starts it, so setting it here covers every call;
# setdefault so an explicit choice still wins. Cost: no wireless-debugging discovery on a server this
# app started, which it never uses.
os.environ.setdefault("ADB_MDNS", "0")

# The phone ALWAYS encodes and sends 1080p. Only its CAPTURE size varies, and the phone picks that
# from the frame rate on its own: 4K sensor readout at 30 fps, 1080p at 60 (the OP3T's H.264 encoder
# caps 4K at 30 anyway, and 4K capture cannot sustain 60). The 4K frame is downscaled to 1080p on the
# phone's Adreno GPU before it ever reaches the encoder, so the PC's decode cost is IDENTICAL either
# way while 30 fps gains supersampled detail (4K -> 1080p downscale = less noise, more real detail).
# Sub-1080p output sizes were removed on purpose: they traded real quality for a PC saving that the
# measurements did not show (decode+pipe is ~40 ms at 1080p60 and the drops were never PC-side).
RESOLUTIONS = ["1920x1080"]
FPS_OPTS = ["60", "30"]
ROTATIONS = ["0", "90", "180", "270"]
DECODERS = {  # label -> ffmpeg input args before "-i pipe:0"
    # MEASURED on this box (2026-07, 1080p60, NV12 output): Intel QSV WINS — decodes on the idle Intel
    # iGPU, so ~0 NVIDIA-3D and ~0 CPU once NV12 killed the colour convert; it's the DEFAULT. Needs the
    # larger probe + "-async_depth 1" + no "-fflags nobuffer" (handled in _run, else it buffers seconds or
    # deadlocks). cuvid = real NVIDIA decode but lights the GPU 3D engine (CUDA). d3d11va = SILENT CPU
    # fallback here (adapter 0 is a virtual-display fake -> hwaccel init fails). CPU = watchdog safety net.
    "GPU (NVIDIA)": ["-c:v", "h264_cuvid"],
    # -async_depth 1: QSV's MFX decoder defaults to a deep async surface pipeline (buffers many frames ->
    # multi-SECOND latency here). Depth 1 = emit each frame as decoded, the QSV low-latency lever.
    # -extra_hw_frames 2: async_depth alone does NOT bound the MFX surface pool — libavcodec still
    # allocates a deep pool of hardware frames and QSV walks it before recycling, which shows up as
    # pure latency. MEASURED (latency_probe.py, 1080p60): steady-state decode+pipe 78 ms -> 40 ms.
    "GPU (Intel QSV)": ["-c:v", "h264_qsv", "-async_depth", "1", "-extra_hw_frames", "2"],
    "GPU (d3d11va)": ["-hwaccel", "d3d11va", "-f", "h264"],
    # -threads 1: libavcodec H.264 defaults to frame-threading = core count, which delays output by
    # (N-1) frames (~200ms on an 8-core box @30fps). low_delay does NOT disable it. Pin to 1 so the
    # CPU path (and the silent watchdog fallback) emits each frame immediately. Baseline single-slice
    # frames can't slice-thread anyway, so zero throughput cost.
    "CPU": ["-f", "h264", "-threads", "1"],
}
DEFAULT_DECODER = "GPU (Intel QSV)"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — keep ffmpeg/adb from flashing a console
HIGH_PRIORITY = 0x00000080  # HIGH_PRIORITY_CLASS — keep the decoder fed when a screenshare/video
                            # fights for the CPU/GPU. This is THE lag fix for "lags when I share my
                            # screen": the OS stops starving ffmpeg's decode thread under contention.
DECODE_WATCHDOG_S = 4.0  # if a hwaccel decoder emits no frame in this long, it's hung -> fall back to CPU
# How often the preview image is rebuilt, and how big. The web UI now PULLS the preview as MJPEG at
# this rate (the browser's own <img>, no bridge call), so a build is no longer wasted work.
#
# MEASURED 2026-08-28 on this box, 1080p NV12, median of 40 (probe_preview.py):
#   numpy stride-decimate -> RGB -> BMP -> base64   8.56 ms   324 KB   384x216   <- what shipped before
#   cv2 shrink-planes -> convert small -> JPEG q72   4.30 ms    38 KB   640x360   <- this
#   ...same at 960x540                               5.69 ms    99 KB
# Cheaper AND 2.8x the pixels: the old path did the YUV->RGB in Python integer math over the whole
# 2 MP frame, threw away 24 of every 25 pixels with no averaging (hence the aliasing), then shipped
# it uncompressed. cv2 does the work in C that releases the GIL.
#
# 640 not 960 because the build runs INSIDE _send_loop: at 60 fps the budget is 16.67 ms and
# _crop_scale already takes up to 11.2 ms of it. 11.2 + 4.3 fits; 11.2 + 5.7 does not.
# The JPEG preview is gated on FRAME COUNT, not the clock. Frames do not arrive evenly: the reader
# keeps only the newest and the sender picks them up in bursts (that is what `dropped` counts), so
# two frames can land 5 ms apart. A wall-clock gate then skips the second one and the preview runs
# well under its target — MEASURED 19.7 fps at a 33 ms gate and 21.7 at 30 ms, chasing 30.
# Every Nth frame is immune to that: 30 fps in -> every frame, 60 fps in -> every other.
PREVIEW_FPS = 30                # target for the JPEG / web UI preview
PREVIEW_PPM_INTERVAL_S = 0.12   # numpy+PPM / tkinter — 8.6 ms a build, and tkinter repaints at 10 Hz
PREVIEW_W = 640      # height follows the frame's aspect
PREVIEW_Q = 72       # JPEG quality; 82 costs 40% more bytes for no visible gain at this size

APP_ID = "com.op3t.webcam"  # launched on the phone via adb so you never touch it
# Face rectangles for auto-framing arrive on their OWN port. Socket 8080's phone->PC direction is a
# raw Annex-B byte stream relayed verbatim into ffmpeg's stdin, so text cannot share it.
FACE_PORT = 8081

# ---------------------------------------------------------------- settings persistence

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "OP3T Webcam")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULTS = {
    "resolution": "1920x1080", "fps": "60", "rotation": "0", "decoder": DEFAULT_DECODER,
    "flip_h": False, "flip_v": False, "denoise": False, "zoom": 1.0, "pan": [0.5, 0.5],
    "ev": 0, "bitrate": 12,   # ev = exposure-comp steps; bitrate = quality in Mbps (phone clamps)
    "focus": 0.0,             # manual focus, 0..1 of the lens range. 0 = autofocus (the Refocus button)
    "autoframe": False,       # face-tracking auto zoom+pan. Persisted: it always starts at z=1.0 and
                              # only engages once a face is actually seen, so there is no surprise.
    "sensor_zoom": "off",     # off | auto | on — who does the magnification, phone sensor or PC crop
    "sensor_zoom_x": 1.0,     # the fixed ratio used by mode "on"
    "tightness": 1.5,         # auto-framing: how much of the frame your face fills. Persisted apart
                              # from `zoom` because the UI slider means one or the other, never both.
    "autocam": True,          # start/stop the stream when an app turns the virtual camera on/off
                              # (see VcamHost). Off = the old behaviour: only Start starts it.
}
SENSOR_MODES = ("off", "auto", "on")


JS_LOG_PATH = os.path.join(CONFIG_DIR, "js_error.log")


def log_js_error(msg):
    """The web panel has no console anyone will ever see: an exception inside pywebview rejects a
    promise and vanishes. If it happens during boot, EVERY control is left unwired and the window
    looks alive but does nothing — which is exactly how the 2026-08-28 overhaul shipped. So the
    panel reports its own errors here, next to the config."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(JS_LOG_PATH) and os.path.getsize(JS_LOG_PATH) > 200_000:
            os.remove(JS_LOG_PATH)        # a breadcrumb file, not an archive
        with open(JS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    # A saved decoder that no longer exists (or the QSV trap that hangs on this pipe) would strand the
    # app at startup; snap it back to the working default so a stale config can't brick the stream.
    if cfg.get("decoder") not in DECODERS:
        cfg["decoder"] = DEFAULT_DECODER
    if cfg.get("sensor_zoom") not in SENSOR_MODES:
        cfg["sensor_zoom"] = "off"
    try:
        cfg["sensor_zoom_x"] = min(max(float(cfg.get("sensor_zoom_x", 1.0)), 1.0), AF_SENSOR_MAX)
    except (TypeError, ValueError):
        cfg["sensor_zoom_x"] = 1.0
    return cfg


def save_config(cfg):
    """Atomic write so a crash mid-save never leaves a half-written config."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- adb

def _adb(*args):
    subprocess.run([ADB, *args], creationflags=NO_WINDOW, capture_output=True, timeout=10)


def _adb_out(*args):
    try:
        r = subprocess.run([ADB, *args], creationflags=NO_WINDOW, capture_output=True,
                           timeout=10, text=True)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _ensure_online():
    """This phone's adb link periodically drops to 'offline' over USB (power management / cable),
    which makes every connect refuse. If we see no healthy 'device' line, kick it back: reconnect
    offline transports, and as a last resort bounce the adb server. Cheap no-op when already online."""
    out = _adb_out("devices")
    if "\tdevice" in out:
        return
    _adb("reconnect", "offline")
    if "\tdevice" not in _adb_out("devices"):
        _adb("kill-server")
        _adb("start-server")
        _adb("reconnect", "offline")


def adb_forward(port):
    """USB tunnel + wake the phone + open the app — no .bat/console, no touching the phone."""
    try:
        _ensure_online()                                   # self-heal a dropped/offline USB link
        _adb("forward", f"tcp:{port}", f"tcp:{port}")
        _adb("forward", f"tcp:{FACE_PORT}", f"tcp:{FACE_PORT}")   # auto-framing face rects
        # Phone is usually asleep/locked: `am start` alone delivers to a screen that stays OFF,
        # so wake the display and drop a non-secure keyguard first, THEN launch.
        _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        _adb("shell", "wm", "dismiss-keyguard")
        _adb("shell", "am", "start", "-n", f"{APP_ID}/.MainActivity")
    except Exception:
        pass


def park_phone(port):
    """Close the phone app and put the screen to sleep, so the phone isn't left awake with a warm
    camera after you're done. KEYCODE_SLEEP (not POWER) because POWER *toggles* — on an already dark
    screen it would switch the display back ON. Sleep locks the phone when a secure lock screen is set.

    Leaves the adb server running: this is also what runs when an app turns the camera off while the
    PC app stays in the tray, and the next start should not pay for an adb cold start."""
    try:
        _adb("shell", "am", "force-stop", APP_ID)
        _adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
    except Exception:
        pass
    try:
        _adb("forward", "--remove", f"tcp:{port}")
        _adb("forward", "--remove", f"tcp:{FACE_PORT}")   # or stale forwards pile up across runs
    except Exception:
        pass


def shutdown_phone(port):
    """Run when the PC app QUITS — deliberately NOT on Stop, so pressing Stop still leaves the phone
    ready for another Start.

    park_phone(), then kill the adb server. That is the fix for "Failed to remove temporary directory
    _MEIxxxxx": the bundled adb.exe starts a background adb *server* that keeps running after the app
    exits, holding tools\\adb.EXE open inside PyInstaller's unpack dir, so the cleanup can't delete it.
    Killing the server releases that handle. adb restarts itself on demand next launch.
    """
    park_phone(port)
    try:
        _adb("kill-server")      # must be LAST: everything above needs the server alive
    except Exception:
        pass


# needs_filter() and build_filter() lived here and were DEAD: zero call sites, _run hardcodes
# `vf = None`, and the `if vf:` branch never fired. Worse, needs_filter's docstring claimed "flips are
# PC-side again", the OPPOSITE of live behaviour — flips go to the phone as XFORM and are applied in
# GlFlipRenderer. Anyone reasoning about where the transform happens from that comment would derive
# the face-coordinate mapping backwards, so the stale code was removed rather than left as a trap.


def _crop_rect(w, h, z, cx, cy):
    """Even-aligned crop window (w/z x h/z) centred on (cx,cy) in 0..1 frame coords. Even alignment is
    required because NV12 chroma is subsampled 2x2 — an odd left/top/size would split a chroma pair.
    Aspect preserved (square crop fraction on 16:9 stays 16:9), so no distortion on the scale-back-up."""
    cw = max(2, min(w, round(w / z))); cw -= cw % 2
    ch = max(2, min(h, round(h / z))); ch -= ch % 2
    left = int(min(max(round(cx * w - cw / 2), 0), w - cw)); left -= left % 2
    top = int(min(max(round(cy * h - ch / 2), 0), h - ch)); top -= top % 2
    return left, top, cw, ch


def _crop_scale(nv12, w, h, z, cx, cy):
    """PC-side digital zoom — the Camo crop box, in NV12. The phone streams the WHOLE frame; we crop
    here so the preview can show what's OUTSIDE the crop. `nv12` is the raw decoder frame as (h*3//2, w):
    a Y plane (h x w) then a half-res interleaved UV plane (h//2 x w). We crop+nearest-scale BOTH planes
    back to the fixed w x h canvas with chained np.take (pure numpy, no cv2/PIL; contiguous copy out).
    NV12 avoids the per-frame NV12->BGR colour convert that was pinning the CPU (QSV) / GPU-3D (cuvid).

    Order matters: slice the crop ROWS first, then gather columns, then gather rows. The old
    row-then-column order made the first .take materialise a full 2 MB (h x w) intermediate and then
    fancy-index it along axis 1 — the expensive direction. Doing it this way the gathers only ever
    touch the cropped region. MEASURED at 1080p: 20.7 ms -> 10.6 ms at z=2.0, 27.3 -> 10.8 at z=1.5,
    13.3 -> 4.3 at z=4.0, output bit-identical (np.array_equal True) at every z/pan tested."""
    left, top, cw, ch = _crop_rect(w, h, z, cx, cy)
    if cv2 is not None:
        # Same crop rect, same nearest-neighbour resample, MEASURED 5-8x cheaper: at 1080p, z=1.2
        # costs 1.18 ms against the numpy path's 9.47 ms (z=1.5: 1.17 vs 7.69; z=2.0: 1.14 vs 6.07).
        # That matters far more now that auto-framing keeps the pipeline in this branch on EVERY
        # frame instead of almost never.
        #
        # NOT bit-identical to the numpy fallback, and the honest reason is worth recording: MEASURED
        # over a sweep of z = 1.05..4.00 in 0.01 steps, 82 of 296 values differ. It is purely a
        # sampling-PHASE difference — OpenCV and the numpy index arithmetic disagree about which of
        # two adjacent source pixels a given output pixel lands on. Both are valid nearest-neighbour
        # resamples of the same crop rect; neither is sharper or softer. (The differing-byte counts
        # look alarming only because the parity test feeds random noise, where adjacent pixels are
        # uncorrelated; on real video the two outputs are indistinguishable.)
        #
        # INTER_LINEAR would be better quality than either for the same ~1.1 ms, but it is left off
        # deliberately so that installing or removing OpenCV changes SPEED only, never how the
        # picture looks.
        out = np.empty((h * 3 // 2, w), np.uint8)
        cv2.resize(nv12[top:top + ch, left:left + cw], (w, h),
                   dst=out[:h], interpolation=cv2.INTER_NEAREST)
        uv = nv12[h:].reshape(h // 2, w // 2, 2)[top // 2: top // 2 + ch // 2,
                                                 left // 2: left // 2 + cw // 2]
        cv2.resize(uv, (w // 2, h // 2), dst=out[h:].reshape(h // 2, w // 2, 2),
                   interpolation=cv2.INTER_NEAREST)
        return out
    Y = nv12[:h]
    UV = nv12[h:].reshape(h // 2, w // 2, 2)            # (rows, cols, [U,V]) at half res
    out = np.empty((h * 3 // 2, w), np.uint8)
    ys = np.arange(h) * ch // h                        # relative: the row slice already applied top
    xs = left + (np.arange(w) * cw // w)
    Y[top:top + ch].take(xs, 1, mode="clip").take(ys, 0, mode="clip", out=out[:h])
    uys = np.arange(h // 2) * (ch // 2) // (h // 2)
    uxs = (left // 2) + (np.arange(w // 2) * (cw // 2) // (w // 2))
    UVo = UV[top // 2: top // 2 + ch // 2].take(uxs, 1, mode="clip").take(uys, 0, mode="clip")
    out[h:] = UVo.reshape(h // 2, w)
    return out


# BT.601 limited-range YUV->RGB, fixed-point (coeff * 65536) to avoid float cost on the preview path.
_C_R_V, _C_G_U, _C_G_V, _C_B_U = 91881, 22554, 46802, 116130


def _nv12_preview_rgb(nv12, w, h, stride):
    """Small RGB preview from an NV12 frame (preview shows the FULL frame; the UI draws the crop box on
    it). Downsample by `stride`, upsample the half-res chroma by nearest, convert YUV->RGB in int math.
    Runs only when preview is on and on every other frame, so the extra convert is on a ~360px image."""
    Y = nv12[:h][::stride, ::stride].astype(np.int32)  # (ph, pw); int32 so the fixed-point mul can't overflow
    ph, pw = Y.shape
    UV = nv12[h:].reshape(h // 2, w // 2, 2)
    uy = np.arange(ph) * (h // 2) // ph
    ux = np.arange(pw) * (w // 2) // pw
    U = UV[:, :, 0].take(uy, 0).take(ux, 1).astype(np.int32) - 128
    V = UV[:, :, 1].take(uy, 0).take(ux, 1).astype(np.int32) - 128
    R = Y + ((_C_R_V * V) >> 16)
    G = Y - ((_C_G_U * U) >> 16) - ((_C_G_V * V) >> 16)
    B = Y + ((_C_B_U * U) >> 16)
    rgb = np.clip(np.stack([R, G, B], axis=2), 0, 255).astype(np.uint8)
    return pw, ph, rgb.tobytes()


def _nv12_preview_jpeg(nv12, w, h, out_w=PREVIEW_W, q=PREVIEW_Q):
    """The web UI's preview frame: JPEG bytes, ready to push down the MJPEG socket.

    SHRINK THE PLANES FIRST, then convert. Converting the full 2 MP frame and resizing after costs
    ~7 ms (MEASURED); resizing Y and UV separately and converting the small image costs 4.3 ms at
    640x360. INTER_AREA averages instead of decimating, which is what kills the aliasing the old
    `[::stride, ::stride]` preview had.

    Returns None if cv2 is unavailable — the caller falls back to _nv12_preview_rgb + BMP."""
    if cv2 is None:
        return None
    out_h = int(h * out_w / w) // 2 * 2                       # NV12 needs even dimensions
    y = cv2.resize(nv12[:h], (out_w, out_h), interpolation=cv2.INTER_AREA)
    uv = cv2.resize(nv12[h:].reshape(h // 2, w // 2, 2), (out_w // 2, out_h // 2),
                    interpolation=cv2.INTER_AREA)
    packed = np.vstack([y, uv.reshape(out_h // 2, out_w)])    # back into NV12 layout
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_NV12),
                           [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes() if ok else None


def start_mjpeg(pipe):
    """Serve the preview to the web UI as MJPEG on 127.0.0.1. Returns (server, port).

    WHY NOT THE pywebview BRIDGE: every js_api call spawns a Python thread and copies its result
    string several times while holding the GIL, competing with the thread that owes the vcam a frame
    every 16.67 ms. That cost is per CALL, so a 30 fps preview over the bridge would be 30 of them a
    second — the same contention that already forced backdrop-filter out and slowed the status poll.
    The browser pulls MJPEG on its own connection instead: no bridge call, and no base64 (33% fewer
    bytes on the wire).

    Loopback only, ephemeral port, and it serves exactly one path."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass                     # the default logger writes a stderr line PER FRAME

        def do_GET(self):
            if self.path.split("?")[0] != "/preview.mjpg":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while not pipe._mjpeg_stop.is_set():
                    jpg = pipe._preview_jpg
                    # identity, not equality: _send_loop rebinds the attribute on every build, so
                    # `is` tells us "new frame" without comparing 38 KB of bytes.
                    if jpg is None or jpg is last:
                        time.sleep(0.004)
                        continue
                    last = jpg
                    self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except Exception:
                pass                 # browser closed the connection; not an error worth surfacing

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- auto-framing (face tracking)
#
# The phone's ISP detects faces for free (STATISTICS_FACE_DETECT_MODE_SIMPLE; MEASURED available on
# this device: availableFaceDetectModes [OFF, SIMPLE], maxFaceCount 10, no faceIds and no landmarks)
# and ships RAW active-array rectangles over tcp:8081. Everything below — geometry, control law,
# subject lock — is PC-side ON PURPOSE, so a mapping fix is a Python edit and not an APK rebuild.
#
# WHY THE PHONE DOES NOT JUST FRAME ITSELF: android.scaler.croppingType is CENTER_ONLY on this HAL
# (MEASURED). The sensor crop can zoom but can never PAN, so "follow me left/right" is impossible
# on-device. The PC crop box (_crop_scale) is the only thing that can actually track.
#
# COST. _crop_scale is MONOTONE DECREASING in zoom (MEASURED on a quiet box, 1080p NV12, medians):
# z=1.002 11.22 ms | 1.20 9.94 | 1.50 7.86 | 2.00 6.13 | 4.00 3.70. Auto-framing therefore gives up
# the zoom<=1.001 passthrough (worth ~10 ms/frame) for as long as it is engaged. That is affordable
# because the reader keeps only the newest frame (_reader) and _send_loop counts the rest, so an
# overrun costs delivered fps and bumps `dropped` — it never accumulates latency.
#
# The crop origin is quantised to 2 SOURCE pixels at every zoom (_crop_rect, required for NV12
# chroma pairing), so the output translates in 2*z-pixel jumps and a slow creeping pan ALWAYS
# stair-steps. The cure is behavioural — hold still, move decisively, hold still — which is why the
# dead zone is generous and the settle times are long. Shrinking either makes it visibly WORSE.

# CALIBRATED ON DEVICE, not guessed. 30 s / 299 samples at a normal seating distance gave a mapped
# face height of 0.1453 of frame height, and 0.22 put that at z = 1.51 with headroom to zoom further.
#
# RESCALED 2026-08-22 with the face_to_frame FOV-crop-axis fix: the mapped face height is now the
# array-Y extent divided by 0.749 instead of left alone, so the SAME physical face measures 1/0.749 =
# 1.335x taller. Every tightness constant is multiplied by that factor, which makes the zoom the user
# actually gets identical to before the fix — only the horizontal aim changes. 0.22 -> 0.294.
AF_TIGHTNESS    = 0.294  # face-box height as a fraction of OUTPUT height
AF_EYE_IN_FACE  = 0.28   # eye line inside the detector box (0 = top). MEASURED off the debug overlay:
                         # this HAL's box runs forehead-to-chin and the eyes sit ~28% down it.
AF_EYE_LINE_OUT = 0.36   # where that eye line sits in the output -> ~14% headroom, distance-independent

AF_ZOOM_MAX = 2.00   # well under the manual 4.0: z=2.0 already sources only 960x540 = 25% of 1080p,
                     # nearest-neighbour upscaled. A distant user is framed loose, not upscaled to mush.
AF_ZOOM_MIN = 1.25   # below this the framing change is invisible but still costs ~10 ms/frame,
AF_ENGAGE_Z = 1.25   # where sitting at exactly 1.0 costs nothing. Hysteresis so box-size noise
AF_RELEASE_Z = 1.12  # cannot toggle the crop path on and off.
AF_SNAP_Z   = 1.010  # a spring never REACHES 1.0; without this snap the pipeline parks at z~1.002
                     # and pays 11.22 ms/frame forever instead of 0.

AF_PAN_SETTLE_S    = 0.55
AF_ZOOM_SETTLE_S   = 1.10   # 2x pan, never shared: z* = TIGHTNESS/fh passes box-SIZE noise straight
AF_ENGAGE_SETTLE_S = 0.45   # through, and looming is a far stronger nausea trigger than translation.
AF_RETURN_SETTLE_S = 2.50
AF_PAN_VMAX     = 0.45      # output-widths/s
AF_ZOOM_VMAX_LN = 0.45      # ln-units/s

AF_PAN_ENTER  = 0.055  # ~106 output px: above seated sway/typing/breathing, below a deliberate lean.
AF_PAN_EXIT   = 0.018  # ~35 px = 8.6 crop-origin quanta at z=2, so it cannot limit-cycle.
                       # MEASURED sitting still, 299 samples: cx sigma 0.0063 (3-sigma 0.019, well
                       # inside the dead zone) but cy sigma 0.0207 (3-sigma 0.062, which would EXCEED
                       # 0.055). The aspect weighting is what saves it: cy is multiplied by 0.5625
                       # before the hypot, giving 0.035. Remove that weighting and the frame twitches
                       # vertically on every breath.
AF_ZOOM_ENTER = 0.14   # log-space: reads directly as a 14% apparent-size change. MEASURED box-height
AF_ZOOM_EXIT  = 0.045  # sigma sitting still was EXACTLY 0.0 over 299 samples — this HAL quantises the
                       # box size, so there is no zoom jitter to suppress and 0.14 has ample margin.
                       # (The flip side: box size changes in coarse STEPS, which is what the 1.10 s
                       # zoom settle is smoothing.)
AF_PAN_REARM_S  = 0.35
AF_ZOOM_REARM_S = 1.50

AF_LOST_HOLD_S    = 1.20   # hold framing through a head turn or a blink
AF_REACQUIRE_N    = 3
AF_RETURN_HOME_S  = 2.50
AF_LOCK_STEAL_S   = 1.50   # a rival face must win continuously this long to take the lock
AF_LOCK_SIZE_GATE = 2.00
AF_MIN_SCORE      = 40     # HAL scores are 1..100 (MEASURED range on this device: 55-91)
# While auto-framing is on the Zoom slider means FRAMING TIGHTNESS instead, so 1.0..4.0 maps onto a
# face-height fraction. Reusing the slider costs no new widget and no new config key — and the tkinter
# window is already 83 px over its own height.
# Scaled by the same 1.335 as AF_TIGHTNESS (0.155 -> 0.207) so a saved Zoom value keeps producing the
# zoom it produced before the FOV-crop-axis fix. Slider 1.42 is AF_TIGHTNESS; it is NOT 2.0, and has
# not been since AF_TIGHTNESS was first recalibrated.
AF_FRAC_PER_X = 0.207
AF_TIGHT_MIN, AF_TIGHT_MAX = 0.12, 0.60
AF_MAX_DT, AF_DISCONT_DT = 0.25, 0.50

# ---- sensor (phone-side) zoom -------------------------------------------------------------------
# The PC crop upscales an already-downscaled 1080p frame. The phone can crop the SENSOR instead
# (SCALER_CROP_REGION) and scale that region into the same stream, which keeps real detail as long as
# the crop stays at least as wide as the output.
#
# MEASURED on device: the HAL honours ZOOM exactly from 1.00 to 4.00 and always centres the crop
# (croppingType CENTER_ONLY) -- request 2.40 and the read-back is 1939x1455 at (1365,1027) inside a
# 4648-wide array. 4648 / 1920 = 2.42, so 2.40 is the last ratio that is still information-lossless.
#
# NOT MEASURED, state it honestly: whether that extra sensor detail actually SURVIVES this sensor's
# 4K readout. Two A/B attempts (variance-of-Laplacian, sensor 2x vs PC 2x at matched FOV) disagreed
# with each other because focus state and subject motion moved more than the effect did. So this
# ships as a user-visible switch, not as an automatic quality claim.
AF_SENSOR_MAX   = 2.40
# NEVER SEND EXACTLY 1.0 ONCE THE FEATURE IS IN USE. MEASURED, and it is the whole reason sensor mode
# switching used to freeze the picture and drop the connection:
#   ZOOM 1.0 returns the sensor to its full-FOV mode. The NEXT departure from that mode stalls the
#   phone for ~6.2 s — it stops sending altogether (byte gap, not a decoder gap), the HAL logs
#   "Did not find matching stream to update index" and "remosaicClient ... call remosaic_init first",
#   and the 4.0 s DECODE_WATCHDOG_S then tears the whole pipeline down and reconnects.
#   The first departure of a session is always clean; every one after a return to 1.0 is not.
# MEASURED with a 1.02 floor instead: 1.5 -> 1.02 -> 1.5 -> 1.02 -> 1.8 -> 1.02 -> 2.4, all seven
# changes clean, max inter-frame gap 119 ms, zero watchdog trips. Ratio never mattered — 2.40 is fine.
# 1.02 costs 2% of the frame, which is invisible; the 6.2 s freeze was not.
AF_SENSOR_MIN   = 1.02
AF_SENSOR_STEP  = 0.20   # dead band on the requested ratio; below this, do not disturb the phone
AF_SENSOR_DWELL = 4.0    # min seconds between changes. Every change re-issues the repeating request,
                         # which costs an exposure/focus blip and a fresh IDR, and lands up to
                         # pipelineMaxDepth = 8 frames later (MEASURED).
AF_SENSOR_SLACK = 1.50   # hand magnification BACK once the PC crop has this much headroom spare

_SETTLE_K = 4.74              # (1+u)e^-u = 0.05  ->  omega = _SETTLE_K / settle_seconds
_ASPECT_Y = 1080.0 / 1920.0   # cy spans 1080 px, cx spans 1920: weight cy before any hypot


def _spring(x, v, target, omega, dt, vmax):
    """Exact critically damped step. Stable for ANY dt — _send_loop DROPS frames, so dt is not 1/fps
    even at a fixed frame rate. C1-continuous across a target change (velocity carries), which is why
    the lost-face return, the manual handoff and mid-move re-acquisition need no special cases.

    Critical damping bars OSCILLATION, not overshoot: the slew clamp below can set a velocity aimed
    hard at the target, which produces a small single overshoot. It is self-correcting."""
    a = x - target
    b = v + omega * a
    e = math.exp(-omega * dt)
    xn = target + (a + b * dt) * e
    vn = (b - omega * (a + b * dt)) * e
    step, lim = xn - x, vmax * dt
    if step > lim:
        xn, vn = x + lim, vmax
    elif step < -lim:
        xn, vn = x - lim, -vmax
    return xn, vn


def _face_to_out(u, v, eff, flip_h, flip_v):
    """(u, v) normalised in the FOV-cropped active array -> normalised OUTPUT, top-left origin.

    BOTH STEPS ARE MEASURED, not derived — the naive derivation was wrong and the overlay caught it.

    Base (eff=0, i.e. rotation 270 in the UI): the camera buffer is TRANSPOSED and x-mirrored with
    respect to active-array coordinates, which is what SENSOR_ORIENTATION=90 means in practice. So
    array-Y becomes output-X (mirrored) and array-X becomes output-Y. Verified by drawing the mapped
    box on a captured frame: it lands on the face, while the identity mapping lands on empty wall.

    Then rotate CCW once per 90 deg of eff. Verified at eff=90 (rotation 0, the shipped default AND
    the user's saved setting): CCW lands on the face, CW lands on the wall above it.

    THE ROTATION IS IN NORMALISED COORDINATES. At eff=90 and eff=270 (UI rotation 0 and 180) that is
    axis-preserving, and together with the array-Y FOV crop in face_to_frame the complete mapping is
    ISOTROPIC — MEASURED at rot=0: a sensor rect of aspect 0.748 maps to 0.75 in output pixels.

    At eff=0 and eff=180 (UI rotation 270 and 90) the normalised rotation transposes, which multiplies
    the mapped box's aspect by 5.6 (MEASURED). That is very likely wrong as well, and it is NOT fixed
    here: it has never been drawn on a frame, nobody runs those rotations, and the right answer depends
    on how GlFlipRenderer fits a rotated 16:9 image back into a 16:9 canvas. Measure before touching.

    Do NOT "fix" this by rotating in square units with a cover-crop. That was tried and put the box on
    the subject's chest at 2.3x size — but the defect it was chasing was the FOV crop AXIS in
    face_to_frame, not the rotation, so its failure proved nothing about isotropy."""
    x, y = 1.0 - v, u
    for _ in range((eff // 90) % 4):
        x, y = y, 1.0 - x          # CCW about the centre, y pointing DOWN (numpy row order)
    if flip_h:
        x = 1.0 - x
    if flip_v:
        y = 1.0 - y
    return x, y


def face_to_frame(rect, crop, cap_w, cap_h, eff, flip_h, flip_v):
    """One HAL face rect in ACTIVE-ARRAY pixels -> (cx, top_y, w, h) normalised to the encoded frame,
    which is exactly the space _crop_rect consumes.

    `crop` is the crop region READ BACK from the CaptureResult, never the requested one: croppingType
    is CENTER_ONLY on this HAL, so the HAL rewrites what was asked for.

    THE FOV CROP IS MANDATORY. The active array is 4:3 (MEASURED 4656x3496) and the capture stream is
    16:9, so part of the sensor never reaches the encoder and a face reported there is real but
    INVISIBLE — skip this and the tracker chases faces the user cannot see. The crop falls on the
    array's Y extent, keeping (crop aspect)/(stream aspect) = (4656/3496)/(3840/2160) = 0.749 of it.
    The array's X extent is transmitted in FULL; ~25% of its Y extent never is.

    THIS SHIPPED THE OTHER WAY ROUND FOR ONE RELEASE and that was the "my face sits off to one side"
    bug. Dividing the face's X offset by 0.749 instead of its Y offset makes the tracker aim the crop
    1.335x FURTHER from centre than the face actually is, so the face lands on the OPPOSITE side of
    centre by an error that grows with both the offset and the zoom (MEASURED: a face 13% off centre
    at z=2.0 landed ~127 output px off centre).

    MEASURED with the mapped box drawn on the decoded frame it belongs to (rot=0, 2026-08-22):
      crop (0,0,4656,3496)  cap 3840x2160  face rect (1580,1738,577,771) score 81
        crop on Y (this code) -> 237x317 out px, aspect 0.75  -> lands on the face
        crop on X (the bug)   -> 317x238 out px, aspect 1.33  -> ~60 px left of the face
    ASPECT IS THE TELL, and it is the check that does not need a human in the frame: active-array
    pixels are square and nothing between the sensor and the encoder is anamorphic, so the mapped box
    must keep the sensor rect's aspect (0.748 here). Only the Y crop does.

    A partially visible face legitimately maps outside [0,1]; the CALLER rejects it."""
    fl, ft, fw, fh = rect
    cl, ct, cw, ch = crop
    if cw <= 0 or ch <= 0 or cap_w <= 0 or cap_h <= 0:
        return None
    u0, u1 = (fl - cl) / cw, (fl + fw - cl) / cw          # along active-array X
    v0, v1 = (ft - ct) / ch, (ft + fh - ct) / ch          # along active-array Y
    keep = (cw / ch) / (cap_w / cap_h)                     # fraction of array-Y that survives
    if keep <= 1.0:
        v0 = (v0 - (1 - keep) / 2) / keep
        v1 = (v1 - (1 - keep) / 2) / keep
    else:                                                  # stream needs more X than the array has
        k = 1.0 / keep                                     # -> crop array-X instead
        u0 = (u0 - (1 - k) / 2) / k
        u1 = (u1 - (1 - k) / 2) / k
    ax, ay = _face_to_out(u0, v0, eff, flip_h, flip_v)
    bx, by = _face_to_out(u1, v1, eff, flip_h, flip_v)
    lo_x, hi_x = min(ax, bx), max(ax, bx)                  # eff is a multiple of 90, so the rect
    lo_y, hi_y = min(ay, by), max(ay, by)                  # stays axis-aligned: 2 corners suffice
    return (lo_x + hi_x) * 0.5, lo_y, hi_x - lo_x, hi_y - lo_y


class AutoFramer:
    """submit() runs on the face-link thread at ~10 Hz. step() runs INLINE in _send_loop every frame.
    They share exactly one immutable tuple, swapped atomically, so _crop_scale can never see a torn
    (cx, cy) pair. The controller runs on the SEND thread on purpose: that makes dt the real
    send-loop interval, and keeps the crop's inputs owned by the thread that uses them."""

    def __init__(self):
        self.enabled = False
        self.z, self.cx, self.cy = 1.0, 0.5, 0.5
        self.lz = 0.0                                # zoom is smoothed in LOG space so 1.0->1.2 and
        self.vlz = self.vcx = self.vcy = 0.0         # 2.0->2.4 feel like the same size move
        self.engaged = False
        self.pan_moving = self.zoom_moving = False
        self.t_pan_stop = self.t_zoom_stop = 0.0
        self.t_prev = None
        self.det = None                              # (cx, top_y, fh) — one atomic reference swap
        self.t_last_det = -1e9
        self.hist = []
        self.lock = None
        self.t_rival = 0.0
        self.reacq = 0
        self.tightness = AF_TIGHTNESS
        # What the zoom law ASKED for before AF_ZOOM_MAX clamped it. SensorZoom reads this: it is the
        # only signal that says "the PC crop is pinned and the subject is still too small".
        self.z_want = 1.0
        # --- tier 3, OFF by default, wired by Pipeline ---
        self.refocus_on_move = False   # re-trigger a ONE-SHOT autofocus on a big distance change
        self.on_refocus = None
        self.z_focused = 1.0
        self.t_refocus = -1e9

    # ---- face-link thread ------------------------------------------------------------------
    def submit(self, boxes, now):
        """boxes: [(cx, top_y, w, h, score)] already mapped to normalised OUTPUT coords."""
        boxes = [b for b in boxes
                 if b[4] >= AF_MIN_SCORE and -0.15 < b[0] < 1.15 and b[1] + b[3] > 0.0 and b[1] < 1.0]
        b = self._select(boxes, now)
        if b is None:
            self.reacq = 0
            return
        if now - self.t_last_det > AF_LOST_HOLD_S:   # coming back from a real loss
            self.reacq += 1
            if self.reacq < AF_REACQUIRE_N:
                return
        self.hist.append((b[0], b[1], b[3]))
        del self.hist[:-3]
        cols = list(zip(*self.hist))
        self.det = tuple(sorted(c)[len(c) // 2] for c in cols)   # median-of-3 kills a lone outlier
        self.t_last_det = now

    def _select(self, boxes, now):
        """The HAL supplies no face IDs (MEASURED: faceIds absent) with maxFaceCount 10, so subject
        association is entirely ours. Plain argmax over box height is discontinuous at a size
        crossover and every switch commands a full-frame whip-pan — the artifact that makes
        auto-framing worse than none."""
        if not boxes:
            return None
        if self.lock is None:
            best = max(boxes, key=lambda b: (b[3], -abs(b[0] - 0.5)))
        else:
            lcx, lcy, lh = self.lock
            gated = [b for b in boxes
                     if lh > 0 and 1.0 / AF_LOCK_SIZE_GATE <= b[3] / lh <= AF_LOCK_SIZE_GATE]
            if not gated:
                return None
            def dist(b):
                return math.hypot(b[0] - lcx, (b[1] + b[3] * 0.5 - lcy) * _ASPECT_Y)
            # nearest to the previous subject; ties broken on frame-centre proximity then index —
            # DETERMINISTIC, because a flip-flopping choice is the exact failure being prevented
            best = min(gated, key=lambda b: (round(dist(b) / 0.05), abs(b[0] - 0.5), boxes.index(b)))
            big = max(boxes, key=lambda b: b[3])
            if big is not best and big[3] > best[3] * 1.25:
                if self.t_rival == 0.0:
                    self.t_rival = now
                elif now - self.t_rival >= AF_LOCK_STEAL_S:
                    best, self.t_rival = big, 0.0
            else:
                self.t_rival = 0.0
        self.lock = (best[0], best[1] + best[3] * 0.5, best[3])
        return best

    # ---- send thread, every frame ----------------------------------------------------------
    def step(self, now):
        if self.t_prev is None:
            self.t_prev = now
            return self.z, self.cx, self.cy
        raw_dt, self.t_prev = now - self.t_prev, now
        if raw_dt > AF_DISCONT_DT:                  # long stall: hold position, never leap
            self.vlz = self.vcx = self.vcy = 0.0
            return self.z, self.cx, self.cy
        dt = min(max(raw_dt, 1.0 / 240.0), AF_MAX_DT)

        fresh = self.det is not None and (now - self.t_last_det) <= AF_LOST_HOLD_S
        if fresh:
            fcx, fy, fh = self.det
            z_want = self.z_want = self.tightness / max(fh, 1e-3)
            if not self.engaged and z_want >= AF_ENGAGE_Z:
                self.engaged = True
            elif self.engaged and z_want < AF_RELEASE_Z:
                self.engaged = False
            if not self.engaged:
                z_t, cx_t, cy_t = 1.0, 0.5, 0.5
            else:
                z_t = min(max(z_want, AF_ZOOM_MIN), AF_ZOOM_MAX)
                cx_t = fcx
                # HEADROOM. out_y = 0.5 + (full_y - cy)*z and y grows DOWNWARD, so putting the eyes
                # HIGH in frame means aiming the crop LOWER — cy_t is numerically GREATER than the
                # face centre. Divide by the APPLIED zoom, not the target: they are deliberately
                # desynchronised (pan 0.55 s, zoom 1.10 s), and using the target makes the pan loop
                # chase a frame that is not the one being sent, which shows up as vertical crawl.
                zc = max(self.z, 1.0)
                cy_t = fy + AF_EYE_IN_FACE * fh + (0.5 - AF_EYE_LINE_OUT) / zc
                half = 0.5 / zc
                # Clamp the TARGET too. Near a frame edge the ideal centre is unreachable, and
                # measuring the dead zone against an unreachable target latches pan_moving forever.
                cx_t = min(max(cx_t, half), 1.0 - half)
                cy_t = min(max(cy_t, half), 1.0 - half)
            w_pan = _SETTLE_K / AF_PAN_SETTLE_S
        else:
            z_t, cx_t, cy_t = 1.0, 0.5, 0.5
            self.z_want = 1.0                             # no subject -> nothing to ask the sensor for
            self.engaged = False
            w_pan = _SETTLE_K / AF_RETURN_HOME_S
            self.pan_moving = self.zoom_moving = True     # the return home is not dead-zoned

        crossing = (self.z <= 1.001) != (z_t <= 1.001)
        w_zoom = _SETTLE_K / (AF_ENGAGE_SETTLE_S if crossing else
                              (AF_ZOOM_SETTLE_S if fresh else AF_RETURN_SETTLE_S))

        # Dead zone in OUTPUT units so the thresholds scale with zoom, and ASPECT-WEIGHTED: cx spans
        # 1920 px and cy only 1080, so an unweighted hypot would make vertical hunting start at half
        # the displacement of horizontal and twitch on every nod.
        e_pan = math.hypot(cx_t - self.cx, (cy_t - self.cy) * _ASPECT_Y) * self.z
        e_z = abs(math.log(z_t) - self.lz)
        if self.pan_moving:
            if e_pan < AF_PAN_EXIT:
                self.pan_moving, self.t_pan_stop = False, now
                self.vcx = self.vcy = 0.0
        elif e_pan > AF_PAN_ENTER and now - self.t_pan_stop >= AF_PAN_REARM_S:
            self.pan_moving = True
        if self.zoom_moving:
            if e_z < AF_ZOOM_EXIT:
                self.zoom_moving, self.t_zoom_stop, self.vlz = False, now, 0.0
        elif e_z > AF_ZOOM_ENTER and now - self.t_zoom_stop >= AF_ZOOM_REARM_S:
            self.zoom_moving = True
        if crossing:
            self.zoom_moving = True

        # No velocity extrapolation anywhere: head motion reverses direction constantly and a
        # predictor overshoots on every reversal. ~150 ms of measurement lag against a 550 ms settle
        # is not perceived as lag — it reads as a camera operator reacting.
        if self.pan_moving:
            vmax = AF_PAN_VMAX / max(self.z, 1.0)
            self.cx, self.vcx = _spring(self.cx, self.vcx, cx_t, w_pan, dt, vmax)
            self.cy, self.vcy = _spring(self.cy, self.vcy, cy_t, w_pan, dt, vmax)
        if self.zoom_moving:
            self.lz, self.vlz = _spring(self.lz, self.vlz, math.log(z_t), w_zoom, dt, AF_ZOOM_VMAX_LN)
            self.z = math.exp(self.lz)

        if z_t <= 1.001 and self.z < AF_SNAP_Z:      # restore the free passthrough; see AF_SNAP_Z
            self.z, self.lz, self.vlz = 1.0, 0.0, 0.0
            self.cx = self.cy = 0.5
            self.vcx = self.vcy = 0.0
            self.zoom_moving = self.pan_moving = False
        self.z = min(max(self.z, 1.0), AF_ZOOM_MAX)
        half = 0.5 / self.z
        self.cx = min(max(self.cx, half), 1.0 - half)
        self.cy = min(max(self.cy, half), 1.0 - half)

        # --- tier 3a: one-shot refocus on a large distance change (OFF by default) ---------------
        # Focus is deliberately parked (one autofocus at session start, then the lens stays put)
        # because continuous AF hunts and looks jittery. But the headline behaviour — lean back, zoom
        # in — is exactly the motion that puts the face out of focus. So: re-trigger ONCE, only on a
        # big hysteretic change, with a long minimum interval. Anything looser reintroduces hunting.
        if self.refocus_on_move and self.on_refocus and self.z > 1.0:
            if (abs(math.log(self.z) - math.log(max(self.z_focused, 1e-6))) > 0.35
                    and now - self.t_refocus > 4.0):
                self.z_focused, self.t_refocus = self.z, now
                try:
                    self.on_refocus()
                except Exception:
                    pass

        # SENSOR ZOOM IS NOT HANDLED HERE, and the deleted tier-3b block that used to do it was wrong.
        # face_to_frame normalises the face inside the READ-BACK crop region, so when the sensor
        # zooms, every quantity in this class — fh, cx, cy — is already expressed in the cropped frame
        # the phone is actually sending. self.z is therefore the RESIDUAL PC crop, not a total, and
        # self.cx/cy are already correct for it. The old code divided z by the sensor ratio AND
        # re-expressed the pan on top of that, double-counting both. It shipped default-off, so it
        # never bit anyone. SensorZoom owns the policy now and touches no geometry at all.
        return self.z, self.cx, self.cy

    def disengage(self, keep_current=True):
        """Manual override: leaves the image EXACTLY where it is, so the handoff has no jump.
        Touching a manual control hands control back — the convention for auto-exposure, autofocus
        and cruise control alike."""
        self.enabled = False
        if not keep_current:
            self.z, self.cx, self.cy, self.lz = 1.0, 0.5, 0.5, 0.0
        self.vlz = self.vcx = self.vcy = 0.0

    def arm(self, z, cx, cy):
        """Re-enable from wherever manual left it, at rest, so it eases in rather than snapping."""
        self.z, self.cx, self.cy = z, cx, cy
        self.lz, self.vlz = math.log(max(z, 1.0)), 0.0
        self.vcx = self.vcy = 0.0
        self.engaged = z > AF_RELEASE_Z
        self.pan_moving = self.zoom_moving = False
        self.t_prev = None                            # dt restarts here; a stale t_prev would look
        self.det, self.lock, self.hist = None, None, []   # like a discontinuity and freeze one step
        self.reacq = 0
        self.enabled = True                           # LAST LINE, and the whole point of arm()


class SensorZoom:
    """Decides how much magnification the PHONE should do, so the PC crop stops being the only lever.

    Three modes, which is exactly what the UI exposes:
      "off"  — never touch the phone; the PC crop does everything. Original behaviour.
      "auto" — the sensor picks up ONLY what the PC crop cannot deliver. It sits at 1.0 until the
               zoom law asks for more than AF_ZOOM_MAX, then takes the remainder; walk back toward
               the camera and it hands the magnification back and returns to 1.0.
      "on"   — a fixed ratio the user dials on a slider; the PC crop takes whatever is left.

    IT TOUCHES NO GEOMETRY. See AutoFramer.step: the face mapping normalises inside the read-back
    crop, so the control law is already working in the cropped frame the phone is sending. Sensor
    zoom is transparent to the PC pipeline — the ONLY thing this class does is choose a ratio and
    send it.

    Everything here is deliberately slow. Each change re-issues the repeating request (exposure/focus
    blip, fresh IDR, ~5 s GOP gap before the decoder recovers) and lands up to 8 frames late.

    WHAT IT COSTS: croppingType is CENTER_ONLY (MEASURED) — the sensor crop cannot pan. Magnification
    moved onto the sensor is magnification the PC can no longer pan across. "auto" spends it only when
    the alternative is not zooming at all, which is also when the subject is far away and moving least
    in frame terms."""

    def __init__(self, send):
        self.send = send          # callable(ratio) -> ships "ZOOM <ratio>" to the phone
        self.mode = "off"         # off | auto | on
        self.manual = 1.0         # mode "on"
        self.req = 1.0            # last ratio requested
        self.obs = 1.0            # ratio derived from the read-back crop — the truth, not the request
        self.t_change = -1e9

    def observe(self, arr_w, crop_w):
        """From the face feed's SCALER_CROP_REGION read-back, ~12 Hz and free."""
        if arr_w > 0 and crop_w > 0:
            self.obs = min(max(arr_w / crop_w, 1.0), 8.0)

    def update(self, z_want, now, tracking=True):
        """z_want: what the zoom law asked for BEFORE the AF_ZOOM_MAX clamp, in the current
        (already sensor-cropped) frame. > AF_ZOOM_MAX means the PC crop is pinned and the subject is
        still too small; well under it means there is slack to hand back.

        tracking: False when z_want is the MANUAL slider, not the auto-framer. "auto" then only ever
        hands magnification BACK. The slider lives in the frame the phone sends but never reacts to
        the sensor, so the remainder formula ratchets on it: slider 3.0 sent ZOOM 1.5, then 2.25, and
        the output sat at 6.75x (see test_autoframe.py). Holding rather than parking also keeps the
        hand-back from Auto-frame free of any jump; zooming out below the release threshold still
        returns the full field of view."""
        if self.mode == "off":
            # Park at the floor rather than 1.0 — see AF_SENSOR_MIN. Turning the feature off must not
            # arm the 6.2 s stall for whenever it is turned back on. If it was never used at all
            # (req still 1.0) this stays 1.0 and the phone is never touched.
            want = AF_SENSOR_MIN if self.req > 1.0 else 1.0
        elif self.mode == "on":
            want = min(max(self.manual, AF_SENSOR_MIN), AF_SENSOR_MAX)
        elif z_want > AF_ZOOM_MAX or z_want < AF_ZOOM_MAX / AF_SENSOR_SLACK:
            # One formula for both directions; the two thresholds ARE the hysteresis, so a subject
            # sitting between them never makes the phone re-issue anything.
            want = min(max(self.obs * z_want / AF_ZOOM_MAX, AF_SENSOR_MIN), AF_SENSOR_MAX)
            if not tracking:
                want = min(want, self.req)      # release only; see `tracking` above
        else:
            want = self.req
        band = AF_SENSOR_STEP if self.mode == "auto" else 0.01
        if abs(want - self.req) > band and now - self.t_change >= AF_SENSOR_DWELL:
            self.req, self.t_change = round(want, 2), now
            try:
                self.send(self.req)
            except Exception:
                pass          # the phone link is best-effort; never take the video path down with it


# ---------------------------------------------------------------- virtual camera host + auto-start

# The OBS Virtual Camera is a DirectShow filter that every capturing app loads into its own process.
# It reads frames from a named section created by the producer (pyvirtualcam, here), and it holds a
# handle to that section ONLY while the app is actually capturing. MEASURED 2026-10-04 (Windows 11
# 25H2, OBS 32.1.2, sampled every 10 ms, ffmpeg as the consumer with Discord live alongside it):
#   device enumeration (-list_devices)   never opens it
#   format query (-list_options)         never opens it
#   a capture                            opened 0.24 s after launch, closed the instant it exited
#   Discord with its camera on           exactly one handle, from its capture process
#   pyvirtualcam producing               exactly one handle
# Nothing else works: the queue header carries no reader state (read_idx is written by the WRITER),
# and Windows' camera-privacy records never list virtual-camera use. So the section's system-wide
# handle count, minus our own, is the number of apps that have the camera on.
VCAM_SECTION = "OBSVirtualCamVideo"
VCAM_IDLE_FPS = 10      # black frames while waiting: plenty to keep the filter attached, ~1% of a core
AUTOCAM_ON_S = 0.5      # an app must hold the camera this long before the phone is woken
AUTOCAM_OFF_S = 10.0    # ...and be gone this long before a stream it caused winds down
AUTOCAM_RETRY_S = 5.0   # a stream that died while an app still wants it is retried this often

_win32_cache = []


def _win32():
    """(kernel32, ntdll, kernelbase) with the few signatures used below, built on first use."""
    if not _win32_cache:
        import ctypes
        from ctypes import wintypes as w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = w.HANDLE
        k32.OpenFileMappingW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
        k32.CloseHandle.argtypes = [w.HANDLE]
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryObject.restype = ctypes.c_long
        nt.NtQueryObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG, ctypes.c_void_p]
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        nt.NtQueryInformationProcess.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG,
                                                 ctypes.POINTER(w.ULONG)]
        kb = ctypes.WinDLL("kernelbase")
        kb.CompareObjectHandles.restype = w.BOOL
        kb.CompareObjectHandles.argtypes = [w.HANDLE, w.HANDLE]
        _win32_cache[:] = [k32, nt, kb]
    return _win32_cache


def _section_open(name=VCAM_SECTION):
    """A handle to the named section, or None if nothing produces it. Holding this keeps the section
    alive, so it must be closed before the camera is re-created (pyvirtualcam refuses to create one
    that still exists)."""
    try:
        return _win32()[0].OpenFileMappingW(0x0004, False, name) or None    # FILE_MAP_READ
    except Exception:
        return None


def _section_handles(h):
    """System-wide handle count of the object behind `h` (NtQueryObject, ObjectBasicInformation).
    Kernel object addresses are hidden from user mode on 24H2+, so this count is the cheap way to see
    other processes' handles: no enumeration of every handle on the box."""
    import ctypes
    try:
        buf = ctypes.create_string_buffer(56)       # OBJECT_BASIC_INFORMATION: exactly 56 bytes on x64,
        if _win32()[1].NtQueryObject(h, 0, buf, 56, None):     # any other length is a mismatch
            return 0
        return int.from_bytes(buf.raw[8:12], "little")        # .HandleCount
    except Exception:
        return 0


def _own_handles(h):
    """How many handles THIS process holds to the object behind `h`, `h` included. Counted, not
    assumed: a pyvirtualcam that kept one more handle would otherwise read as an app with the camera
    on, and wake the phone for nobody. None if Windows will not say."""
    import ctypes
    from ctypes import wintypes as w
    try:
        _, nt, kb = _win32()
        size = 1 << 16
        while True:
            buf = ctypes.create_string_buffer(size)
            ret = w.ULONG()
            st = nt.NtQueryInformationProcess(w.HANDLE(-1), 51, buf, size, ctypes.byref(ret))
            st &= 0xFFFFFFFF                          # 51 = ProcessHandleInformation
            if st == 0xC0000004 and size < (1 << 24):     # STATUS_INFO_LENGTH_MISMATCH
                size = max(size * 2, ret.value + 4096)
                continue
            if st:
                return None
            break
        raw = buf.raw
        n = int.from_bytes(raw[:8], "little")
        same = 0
        for i in range(n):                           # PROCESS_HANDLE_TABLE_ENTRY_INFO: 40 bytes each
            hv = int.from_bytes(raw[16 + 40 * i:24 + 40 * i], "little")
            if hv == h or kb.CompareObjectHandles(hv, h):
                same += 1
        return same
    except Exception:
        return None


def _section_close(h):
    if h:
        try:
            _win32()[0].CloseHandle(h)
        except Exception:
            pass


class VcamHost:
    """Owns the one pyvirtualcam.Camera for the whole run, so the virtual camera can exist BEFORE any
    stream does — the only way to notice an app turning it on (see VCAM_SECTION).

    Between streams it feeds black frames at VCAM_IDLE_FPS. A Pipeline borrows the SAME camera for its
    session (session()) and hands it back after, so an app that turned the camera on early never sees
    it vanish and come back; only a new frame rate re-creates it. With idling off the camera lives
    exactly as long as a session, as it did before this class existed."""

    _serializable = False     # keep pywebview's js_api crawler out (see webui.Tray)

    def __init__(self):
        self.cam = None
        self.spec = None          # (w, h, fps) of self.cam
        self.error = ""
        self.own = 2              # our handles to the section; re-counted on every _open
        self._want = None         # spec to keep alive between streams; None = no idle camera
        self._busy = False        # a Pipeline is sending; idle frames stand aside
        self._probe = None
        self._black = None
        self._lock = threading.Lock()   # one send() at a time across the idle loop and a takeover
        self._quit = threading.Event()
        self._thread = None
        self._retry_at = 0.0

    def idle(self, w, h, fps):
        """Keep a camera alive between streams (auto-start on). Cheap to call again."""
        self._want = (w, h, fps)
        if self._thread is None:
            self._thread = threading.Thread(target=self._idle_loop, daemon=True)
            self._thread.start()

    def no_idle(self):
        """Back to a camera only while streaming (auto-start off)."""
        self._want = None
        with self._lock:
            if not self._busy:
                self._close()

    def consumers(self):
        """How many apps have the virtual camera on right now; 0 while no camera exists."""
        p = self._probe
        n = _section_handles(p) if p else 0
        return max(0, n - self.own) if n else 0

    @contextlib.contextmanager
    def session(self, w, h, fps):
        """The Pipeline's camera for one session: the idle one when the spec matches, else a new one."""
        with self._lock:
            if self.cam is None or self.spec != (w, h, fps):
                if not self._open(w, h, fps):
                    raise RuntimeError(self.error)
            if self._want is not None:
                self._want = (w, h, fps)    # idle at what was streamed last, or the next idle re-creates
            self._busy = True
            cam = self.cam
        try:
            yield cam
        finally:
            with self._lock:
                self._busy = False
                if self._want is None:
                    self._close()

    def close(self):
        self._quit.set()
        with self._lock:
            self._close()

    def _idle_loop(self):
        while not self._quit.wait(1.0 / VCAM_IDLE_FPS):
            with self._lock:
                if self._busy or self._want is None:
                    continue
                if self.cam is None or self.spec != self._want:
                    # Something else may own the camera (OBS's own, or another copy of this app):
                    # ask again every few seconds, not every frame.
                    if time.monotonic() < self._retry_at:
                        continue
                    if not self._open(*self._want, wait=0.0):
                        self._retry_at = time.monotonic() + 5.0
                        continue
                try:
                    self.cam.send(self._black)
                except Exception as e:
                    self.error = str(e)

    def _open(self, w, h, fps, wait=3.0):
        """(Re)create the camera; caller holds _lock. A camera that is going away lingers until every
        app that mapped it lets go — the filter does that within a frame of seeing it stop — and
        pyvirtualcam refuses to create one while it lingers, hence the short retry."""
        self._close()
        deadline = time.monotonic() + wait
        while True:
            try:
                self.cam = pyvirtualcam.Camera(width=w, height=h, fps=fps,
                                               fmt=pyvirtualcam.PixelFormat.NV12)
                break
            except Exception as e:
                if time.monotonic() >= deadline:
                    self.error = f"virtual camera unavailable ({e})"
                    return False
                time.sleep(0.1)
        black = np.empty((h * 3 // 2, w), np.uint8)
        black[:h] = 16            # NV12 video-range black: Y=16, U=V=128
        black[h:] = 128
        self.spec, self._black, self.error = (w, h, fps), black, ""
        self._probe = _section_open()
        own = _own_handles(self._probe) if self._probe else None
        self.own = own if own else 2   # MEASURED fallback: pyvirtualcam's one + the probe
        return True

    def _close(self):
        _section_close(self._probe)        # first: our own handle would keep the section alive
        self._probe = None
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:
                pass
        self.cam = self.spec = None


class AutoCam:
    """When to start and stop the stream, from how many apps have the camera on. Pure logic — fed a
    count and a clock — so the timing is testable without a camera or a phone.

    - An app must hold the camera AUTOCAM_ON_S before the phone is woken.
    - Only a stream THIS started is ever stopped by it (a manual Start belongs to the user), and only
      after every app has been gone AUTOCAM_OFF_S, so flicking the camera during a call does not
      bounce the phone.
    - A manual Stop while an app still has the camera on is respected until that app lets go.
    - A stream that died under an app that still wants it (phone unplugged, say) is retried every
      AUTOCAM_RETRY_S."""

    _serializable = False     # keep pywebview's js_api crawler out (see webui.Tray)

    def __init__(self, on_s=AUTOCAM_ON_S, off_s=AUTOCAM_OFF_S, retry_s=AUTOCAM_RETRY_S):
        self.enabled = True
        self.on_s, self.off_s, self.retry_s = on_s, off_s, retry_s
        self.active = False       # debounced: some app has the camera on
        self.owns = False         # the running stream was started here
        self.held = False         # the user stopped it while an app still had the camera on
        self._edge = None         # when the raw count last crossed zero, not yet debounced
        self._last_start = -1e9

    def step(self, users, now, streaming):
        """users: apps with the camera on. streaming: a session is running or connecting.
        Returns "start", "stop" or None."""
        present = users > 0
        if present == self.active:
            self._edge = None
        else:
            if self._edge is None:
                self._edge = now
            if now - self._edge >= (self.on_s if present else self.off_s):
                self.active, self._edge = present, None
                if not present:
                    self.held = False
        if not self.enabled:
            return None
        if (self.active and not streaming and not self.held
                and now - self._last_start >= self.retry_s):
            self.owns, self._last_start = True, now
            return "start"
        if not self.active and streaming and self.owns:
            self.owns = False
            return "stop"
        return None

    def user_start(self):
        self.owns = False

    def user_stop(self):
        self.owns = False
        self.held = self.active


def _spawn_ffmpeg(cmd, frame_bytes):
    """Start ffmpeg with an stdout pipe big enough to hold a whole frame. Returns (proc, read_stream).

    MEASURED: `subprocess.PIPE` is a 4096-byte Windows pipe (confirmed with GetNamedPipeInfo), while
    ffmpeg flushes the rawvideo muxer in 32 KiB AVIO chunks — so one 1080p NV12 frame costs exactly
    95 blocking pipe round-trips, ~5.8 ms of pure hand-off per frame. Throughput is NOT the issue:
    the same 4 KiB pipe measured 412 MB/s, far above the 186.6 MB/s that 1080p60 needs. It is the
    per-frame round-trip count. A pipe larger than one frame collapses those 95 reads into ~1.
    Falls back to subprocess.PIPE if the Win32 path is unavailable (non-Windows, or a locked-down box).
    """
    try:
        import msvcrt
        import _winapi
        rh, wh = _winapi.CreatePipe(None, max(frame_bytes * 2, 1 << 20))
        wfd = msvcrt.open_osfhandle(wh, 0)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=wfd,
                                    creationflags=NO_WINDOW | HIGH_PRIORITY)
        finally:
            os.close(wfd)      # drop the parent's copy of the write end or the reader never sees EOF
        return proc, open(msvcrt.open_osfhandle(rh, os.O_RDONLY), "rb")
    except Exception:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                creationflags=NO_WINDOW | HIGH_PRIORITY)
        return proc, proc.stdout


class Pipeline:
    """Owns one streaming session: socket -> ffmpeg -> (drop stale) -> vcam.
    Restart for any setting that changes the ffmpeg command or the negotiated stream."""

    def __init__(self):
        self._stop = False
        self._sock = None
        self._ff = None
        self._ffout = None       # read end of ffmpeg's stdout (see _spawn_ffmpeg)
        self.focus = 0.0         # manual focus 0..1 of lens range; 0 = autofocus. Re-sent on connect.
        self.thread = None
        self.zoom = 1.0          # desired zoom; re-sent after each (re)connect
        self.pan = [0.5, 0.5]
        self.denoise = False     # phone-side noise reduction; re-sent after each (re)connect
        self.torch = False       # rear LED torch; re-sent after each (re)connect
        self.ev = 0              # exposure-comp steps; re-sent after each (re)connect
        self.bitrate = 12        # live quality in Mbps; re-sent after each (re)connect
        self.flip_h = False      # phone-side GPU flips; re-sent after each (re)connect
        self.flip_v = False
        self.vcam = None          # VcamHost when the web UI runs one; None = own camera per session
        self.preview_on = False
        # Auto-framing. `af` owns the smoothed view; `auto_view` is the (z, cx, cy) actually applied
        # to the last frame, which is what BOTH previews must draw so the crop box cannot lie.
        self.af = AutoFramer()
        self.af.on_refocus = lambda: self.send_control("FOCUS")   # inert unless refocus_on_move is set
        # Phone-side zoom. Defaults to "off", so with no config change the phone is never touched.
        self.sz = SensorZoom(lambda zs: self.send_control(f"ZOOM {zs:.2f}"))
        self.auto_view = (1.0, 0.5, 0.5)
        self.face_boxes = []      # newest mapped boxes, for the preview overlay only
        self.face_seen = 0.0      # time.monotonic() of the last accepted detection (UI state)
        self.rot = 0              # current rotation, needed by the face mapping
        self._preview_ppm = None  # latest small RGB frame as PPM bytes (tkinter GUI builds the image)
        self._preview_rgb = None  # (w, h, rgb_bytes) — only the cv2-less BMP fallback still uses this
        self._preview_jpg = None  # latest preview as JPEG bytes, pushed by start_mjpeg
        # Which preview the send thread builds. The two UIs want different things and building both
        # would double the cost on the frame thread: tkinter needs PPM (tk.PhotoImage eats it
        # directly), the web UI wants JPEG. webui sets this to "jpeg"; tkinter leaves it alone.
        self.preview_fmt = "ppm"
        self._mjpeg_stop = threading.Event()
        # newest decoded frame + a monotonically increasing id so the sender knows when it's new
        self._latest = None
        self._latest_id = 0
        self._frame_evt = threading.Event()   # reader signals a new frame -> sender wakes instantly
        # live status, read by the GUI
        self.state = "idle"      # idle | connecting | streaming | error
        self._cpu_fallback = False   # set once if a hwaccel decoder stalls and we drop to CPU
        self.fps = 0.0
        self.frames = 0
        self.dropped = 0
        self.msg = ""

    def start(self, host, port, w, h, fps, rot, decoder, flip_h=False, flip_v=False, denoise=False):
        self.stop()
        self._stop = False
        self.denoise = denoise
        self.state, self.frames, self.dropped, self.fps, self.msg = "connecting", 0, 0, 0.0, ""
        self._latest, self._latest_id = None, 0
        self._cpu_fallback = False
        self.thread = threading.Thread(
            target=self._run, args=(host, port, w, h, fps, rot, decoder, flip_h, flip_v), daemon=True)
        self.thread.start()

    def stop(self):
        self._stop = True
        if self._sock:
            try: self._sock.close()
            except OSError: pass
        if self._ff:
            try: self._ff.terminate()
            except Exception: pass
        if self._ffout is not None:
            # _spawn_ffmpeg may hand back a pipe we own rather than proc.stdout, so close it
            # explicitly or the handle leaks across restarts.
            try: self._ffout.close()
            except Exception: pass
            self._ffout = None
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.state = "idle"

    def send_control(self, line):
        s = self._sock
        if s:
            try: s.sendall((line + "\n").encode())
            except OSError: pass

    def set_zoom(self, z, cx, cy):
        # PC-side crop box now — store only, no socket round-trip. The phone always streams the FULL
        # frame; _crop_scale applies this in the send loop, so pan/zoom is instant (no phone latency).
        #
        # Any manual zoom/pan hands control back from auto-framing, KEEPING the current framing so the
        # image does not jump at the handoff. Done here rather than in each UI handler because every
        # manual path — tkinter slider, drag, scroll, double-click reset, and the web UI's set_view —
        # routes through this one function.
        if self.af.enabled:
            self.af.disengage(keep_current=True)
        self.zoom, self.pan = z, [cx, cy]

    def set_denoise(self, on):
        self.denoise = on
        self.send_control(f"NR {1 if on else 0}")

    def set_torch(self, on):
        self.torch = on
        self.send_control(f"FLASH {1 if on else 0}")

    def set_ev(self, steps):
        self.ev = int(steps)
        self.send_control(f"EV {self.ev}")

    def set_bitrate(self, mbps):
        self.bitrate = int(mbps)
        self.send_control(f"BITRATE {self.bitrate}")

    def set_focus(self, f):
        """Manual focus, 0..1 of the lens range (0 = autofocus, 1 = closest). The phone maps this onto
        LENS_FOCUS_DISTANCE in diopters. Live over the open socket, no restart."""
        self.focus = max(0.0, min(1.0, float(f)))
        self.send_control(f"FOCUSDIST {self.focus:.3f}")

    def set_transform(self, rot, h, v):
        self.flip_h, self.flip_v = h, v
        self.send_control(f"XFORM {rot} {1 if h else 0} {1 if v else 0}")

    def set_sensor_zoom(self, mode, manual=None):
        """Who does the magnification. Takes effect on the next send-loop frame; SensorZoom's dwell
        timer still applies, so flipping the switch is not instant by design — see AF_SENSOR_DWELL."""
        if mode in SENSOR_MODES:
            self.sz.mode = mode
        if manual is not None:
            try:
                self.sz.manual = min(max(float(manual), AF_SENSOR_MIN), AF_SENSOR_MAX)
            except (TypeError, ValueError):
                pass
        if self.sz.mode != "auto":       # an explicit choice should not wait out a dwell from auto
            self.sz.t_change = -1e9

    def _run(self, host, port, w, h, fps, rot, decoder, flip_h=False, flip_v=False):
        sock = None
        kick = 0.0                    # when to next run adb_forward (wake, launch, forward) while waiting
        while not self._stop:
            try:
                sock = socket.create_connection((host, port), timeout=2)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock     # set first, so stop() can break a handshake that is waiting
                self._send_config(sock, w, h, fps, rot, flip_h, flip_v)
                # adb forward ACCEPTS on the PC even when nothing listens on the phone yet, then drops
                # the connection once it cannot reach the device: a session that "connects" and is
                # over at once. MEASURED 2026-10-05: forward set, phone app not running -> connect OK,
                # then EOF. Auto-start launches the phone app just before this, so a connection only
                # counts once video arrives.
                if self._phone_answered(sock):
                    break
                raise OSError("dropped before any video")
            except OSError:
                if sock is not None:
                    try: sock.close()
                    except OSError: pass
                sock = self._sock = None
                self.msg = "waiting for phone (open the app, plug in USB)"
                # First miss: the phone app is probably just not running (auto-start parks it between
                # calls, and launching no longer wakes it) — wake, launch, forward right away. Then
                # every 4 s, for a link that went 'offline': self-heal + re-launch.
                if time.monotonic() >= kick:
                    adb_forward(port)
                    kick = time.monotonic() + 4.0
                time.sleep(0.5)
        if self._stop or sock is None:
            return

        # PC does ZERO image processing. Rotation, flips and denoise all happen on the phone GPU/ISP;
        # ffmpeg here only DECODES the H.264 stream to raw frames for the virtual camera. No -vf.
        vf = None
        in_args = DECODERS[decoder]
        # Low-latency demux flags. "-flags low_delay" (decoder emits ASAP, no reorder wait) is safe for
        # every decoder. We deliberately do NOT use "-fflags nobuffer": on this MediaCodec Annex-B stream
        # it makes the H.264 parser flush ~2 frames then STALL (the README warns against it) — a periodic
        # stall that reads as lag, and it hits every non-QSV decoder identically (matches "CPU and NVIDIA
        # lag the same"). QSV additionally needs a larger probe to size its HW surface pool up front.
        # -fpsprobesize 0: with no container timebase on a raw Annex-B pipe, avformat_find_stream_info
        # otherwise waits for its default 20-frame fps probe before the first frame can come out. That
        # is what makes the QSV path (which needs the big probesize to size its surface pool) take
        # ~1.1-1.4 s to first frame versus ~55 ms for the CPU path. The frame rate is already known —
        # we negotiated it in the config line — so the probe buys nothing.
        # NOTE: "-analyzeduration 0" was a no-op (0 is the default, and find_stream_info reinterprets
        # a zero as the 5 s default), so it is dropped rather than kept as false reassurance.
        if decoder == "GPU (Intel QSV)":
            lat = ["-flags", "low_delay", "-fpsprobesize", "0", "-probesize", "262144"]
        else:
            lat = ["-flags", "low_delay", "-fpsprobesize", "0", "-probesize", "32"]
        cmd = [FFMPEG, "-loglevel", "error", *lat, *in_args, "-i", "pipe:0"]
        if vf:
            cmd += ["-vf", vf]
        # MEASURED (windows/latency_probe.py, 1080p60): without -flush_packets the rawvideo muxer's
        # AVIO write buffer (32 KiB) keeps the TAIL of every frame. A 1080p NV12 frame is 3110400 B =
        # 94 full 32 KiB flushes + 30208 B left sitting in the buffer, so _reader's blocking full-frame
        # read cannot complete until the NEXT frame pushes that remainder out -> a whole frame of pure
        # latency, on EVERY decoder (which is why CPU/QSV/cuvid all lagged identically). flush_packets
        # defaults to -1 ("auto"), and auto does NOT flush on a pipe. Measured median decode+pipe
        # latency 40.1 ms -> 22.6 ms at 60 fps, i.e. exactly one frame time recovered.
        cmd += ["-fps_mode", "passthrough",       # pass frames as decoded; no CFR re-pacing -> less lag
                "-flush_packets", "1",            # emit each decoded frame whole, immediately
                "-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1"]   # NV12 = decoder-native, NO colour convert
        frame_bytes = w * h * 3 // 2
        self._ff, self._ffout = _spawn_ffmpeg(cmd, frame_bytes)

        threading.Thread(target=self._pump, args=(sock, self._ff), daemon=True).start()
        threading.Thread(target=self._reader, args=(self._ffout, frame_bytes), daemon=True).start()

        # Decoder watchdog: some hwaccels (notably Intel QSV on this box) HANG on the raw pipe and
        # never emit a frame instead of erroring out, leaving the app stuck at "0 frames". If no frame
        # has decoded within DECODE_WATCHDOG_S, fall back to plain CPU decode (which always works) by
        # tearing down and reconnecting with the "CPU" decoder. Done at most once per session.
        if decoder != "CPU" and not self._cpu_fallback:
            id0 = self._latest_id
            deadline = time.monotonic() + DECODE_WATCHDOG_S
            while not self._stop and time.monotonic() < deadline:
                if self._latest_id != id0:
                    break                              # frames flowing — decoder is fine
                if self._ff.poll() is not None:
                    break                              # ffmpeg exited; _reader will handle EOF
                time.sleep(0.05)
            else:
                pass
            if not self._stop and self._latest_id == id0:
                self.msg = f"{decoder} produced no frames — falling back to CPU decode"
                self._cpu_fallback = True
                try: self._ff.terminate()
                except Exception: pass
                # close the READ end too, or the recursive _run below rebinds self._ffout and this
                # pipe handle leaks for the life of the process (once per forced fallback)
                try:
                    if self._ffout is not None: self._ffout.close()
                except Exception: pass
                self._ffout = None
                try: sock.close()
                except OSError: pass
                self._sock = None
                return self._run(host, port, w, h, fps, rot, "CPU", flip_h, flip_v)

        self._send_loop(w, h, fps)

    def _send_config(self, sock, w, h, fps, rot, flip_h, flip_v):
        sock.sendall(f"{w}x{h}@{fps}\n".encode())
        # No ZOOM to the phone — it streams the full frame; zoom/pan is the PC-side crop box.
        sock.sendall(f"NR {1 if self.denoise else 0}\n".encode())
        sock.sendall(f"EV {self.ev}\n".encode())
        sock.sendall(f"BITRATE {self.bitrate}\n".encode())
        if self.focus > 0:
            sock.sendall(f"FOCUSDIST {self.focus:.3f}\n".encode())   # 0 = leave it on autofocus
        if self.torch:
            sock.sendall(f"FLASH 1\n".encode())
        sock.sendall(f"XFORM {rot} {1 if flip_h else 0} {1 if flip_v else 0}\n".encode())
        # the face mapping needs the SAME transform the phone is applying
        self.rot, self.flip_h, self.flip_v = rot, flip_h, flip_v
        # A fresh session always starts at sensor zoom 1.0. Clearing this is what stops a
        # reconnect from believing a ratio it requested before the phone restarted.
        self.sz.req = self.sz.obs = 1.0
        self.sz.t_change = -1e9

    def _phone_answered(self, sock, wait=8.0):
        """True once the phone has sent its first byte of video, False if the connection was dropped
        first (adb forward's fake accept, see _run). Peeked, so _pump still reads that byte. A phone
        silent for `wait` seconds gets the benefit of the doubt: the decoder watchdog and the reader
        take it from there, as they always did."""
        deadline = time.monotonic() + wait
        sock.settimeout(0.25)
        try:
            while not self._stop and time.monotonic() < deadline:
                try:
                    return sock.recv(1, socket.MSG_PEEK) != b""
                except socket.timeout:
                    pass
            return not self._stop
        finally:
            sock.settimeout(2)        # what create_connection set; _pump's recv still relies on it

    def _reader(self, ffout, frame_bytes):
        """Decode->display decoupler. Reads complete frames as fast as ffmpeg emits them and keeps
        only the NEWEST. If the sender/vcam is behind, older frames are overwritten = dropped.
        `ffout` is the read end from _spawn_ffmpeg (a wide pipe, so ~1 read per frame not 95)."""
        assert ffout is not None
        while not self._stop:
            buf = ffout.read(frame_bytes)            # blocks until a full frame (or EOF)
            if len(buf) < frame_bytes:
                self.msg = "stream ended (phone disconnected)"
                self._latest_id += 1                 # nudge sender so it can notice and exit
                self._frame_evt.set()
                return
            self._latest = buf
            self._latest_id += 1
            self._frame_evt.set()                    # wake the sender immediately (no poll latency)

    def _face_loop(self, host):
        """Reads the phone's face feed on FACE_PORT and hands mapped boxes to the AutoFramer.

        Deliberately best-effort: the phone may not have the face build installed, nothing may ever
        listen, or the socket may die mid-session. Every one of those cases must leave the video path
        completely untouched, so everything here is wrapped and simply retries.

        ANCHORING: this thread is started by _send_loop, not alongside _pump/_reader — _run RETURNS
        its recursive CPU-watchdog fallback, so a thread started there would be duplicated by the
        fallback, and unlike _pump/_reader it has no fd for the fallback to close. _send_loop runs
        exactly once per session on every path, and stop() joins the thread it runs on."""
        arr = None
        while not self._stop:
            s = None
            try:
                s = socket.create_connection((host, FACE_PORT), timeout=2)
                s.settimeout(1.0)
                f = s.makefile("rb")
                while not self._stop:
                    line = f.readline()
                    if not line:
                        break
                    p = line.split()
                    if not p:
                        continue
                    if p[0] == b"A" and len(p) >= 6:
                        arr = (int(p[1]), int(p[2]), int(p[3]), int(p[4]))
                        continue
                    if p[0] != b"F" or len(p) < 9 or arr is None:
                        continue
                    crop = (int(p[2]), int(p[3]), int(p[4]), int(p[5]))
                    # What the sensor crop ACTUALLY is, not what was asked for: the HAL rewrites the
                    # request (CENTER_ONLY, quantised) and applies it up to 8 frames late.
                    self.sz.observe(arr[2], crop[2])
                    cap_w, cap_h, n = int(p[6]), int(p[7]), int(p[8])
                    boxes = []
                    for _ in range(n):
                        q = f.readline().split()
                        if len(q) < 5:
                            break
                        rect = (int(q[0]), int(q[1]), int(q[2]), int(q[3]))
                        m = face_to_frame(rect, crop, cap_w, cap_h,
                                          (self.rot + 90) % 360, self.flip_h, self.flip_v)
                        if m:
                            boxes.append((m[0], m[1], m[2], m[3], int(q[4])))
                    self.face_boxes = boxes           # preview overlay only; one atomic rebind
                    now = time.monotonic()
                    if boxes:
                        self.face_seen = now
                    self.af.submit(boxes, now)
            except (OSError, ValueError):
                pass                                  # phone/app not ready -> back off and retry
            finally:
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            for _ in range(20):                       # ~2 s, but respond to stop() promptly
                if self._stop:
                    return
                time.sleep(0.1)

    def _send_loop(self, w, h, fps):
        pv_stride = max(1, w // 360)                 # preview ~360px wide (numpy/PPM path only)
        pv_every = max(1, round(fps / PREVIEW_FPS))  # build the JPEG preview every Nth frame
        sent_id = 0
        pv_t = 0.0                                   # last preview build (PPM path; see PREVIEW_FPS)
        face_thread = threading.Thread(target=self._face_loop, args=("127.0.0.1",), daemon=True)
        face_thread.start()
        try:
            # The web UI keeps the camera alive across sessions (VcamHost) so auto-start can see an
            # app turn it on; headless and the tkinter panel still own one per session.
            vcam = (self.vcam.session(w, h, fps) if self.vcam is not None else
                    pyvirtualcam.Camera(width=w, height=h, fps=fps, fmt=pyvirtualcam.PixelFormat.NV12))
            with vcam as cam:
                self.state, self.msg = "streaming", cam.device
                t0, c0 = time.monotonic(), 0
                while not self._stop:
                    if self._latest_id == sent_id:
                        self._frame_evt.wait(0.1)    # block until reader signals a new frame
                        self._frame_evt.clear()
                        continue
                    if self._latest is None:         # reader hit EOF
                        break
                    # count everything the reader produced since we last looked; anything beyond
                    # the one frame we actually send was dropped to keep latency low.
                    self.dropped += (self._latest_id - sent_id - 1)
                    sent_id = self._latest_id
                    buf = self._latest
                    if buf is None or len(buf) < w * h * 3 // 2:
                        break
                    full = np.frombuffer(buf, np.uint8).reshape(h * 3 // 2, w)   # NV12 (Y + half-res UV)
                    # Preview is the FULL frame (so the crop box shows what's outside it) — built BEFORE
                    # cropping. NV12->RGB only here, on the small downsampled image.
                    pv_now = time.monotonic()
                    # Rate is per FORMAT. The JPEG path is cheap enough (4.3 ms) to feed a 30 fps
                    # stream and is gated on frame count so bursty arrivals cannot rob it; the numpy
                    # PPM path costs 8.6 ms — 258 ms/s at 30 fps on the thread that owes the vcam a
                    # frame — and tkinter only repaints at 10 Hz anyway, so it keeps the clock gate.
                    pv_due = ((sent_id % pv_every) == 0 if self.preview_fmt == "jpeg"
                              else pv_now - pv_t >= PREVIEW_PPM_INTERVAL_S)
                    if self.preview_on and pv_due:
                        pv_t = pv_now
                        jpg = _nv12_preview_jpeg(full, w, h) if self.preview_fmt == "jpeg" else None
                        if jpg is not None:
                            self._preview_jpg = jpg      # rebound, never mutated: start_mjpeg uses `is`
                        else:
                            # tkinter, or a box with no cv2 — the original numpy path, unchanged.
                            pw, ph, rgb = _nv12_preview_rgb(full, w, h, pv_stride)
                            self._preview_ppm = b"P6\n%d %d\n255\n" % (pw, ph) + rgb
                            self._preview_rgb = (pw, ph, rgb)
                    # Output = PC-side crop box in NV12. ONE immutable-tuple read: the old code did
                    # four attribute loads of three attributes (self.zoom twice — once in the test,
                    # once as the argument), so the fast-path DECISION and the zoom actually APPLIED
                    # could come from different updates. Passing the full frame through when not
                    # zoomed keeps the zero-cost path exactly as it was.
                    tnow = time.monotonic()
                    tracking = self.af.enabled   # read ONCE: the UI thread can disengage mid-frame
                    if tracking:
                        view = self.af.step(tnow)
                        z_want = self.af.z_want
                    else:
                        p = self.pan
                        view = (self.zoom, p[0], p[1])
                        z_want = self.zoom       # manual: the slider is the demand, but see SensorZoom.update
                    # Phone-side zoom decision. Cheap (a compare and a clock read on most frames) and
                    # it deliberately runs on THIS thread, so the ratio it picks is the one belonging
                    # to the frame being sent. It returns nothing: see SensorZoom, it moves no pixels.
                    self.sz.update(z_want, tnow, tracking)
                    self.auto_view = view            # both previews draw THIS, never their own copy
                    z, cx, cy = view
                    out = full if z <= 1.001 else _crop_scale(full, w, h, z, cx, cy)
                    cam.send(out)
                    self.frames += 1
                    now = time.monotonic()
                    if now - t0 >= 1.0:
                        self.fps = (self.frames - c0) / (now - t0)
                        t0, c0 = now, self.frames
        except Exception as e:
            self.state, self.msg = "error", str(e)
        finally:
            try: self._ff.terminate()
            except Exception: pass
            try:
                if self._sock: self._sock.close()
            except OSError: pass
            # Read _stop BEFORE releasing the face thread: a session that ended on its own (phone
            # unplugged) must still fall back to "idle", and setting _stop first would swallow that.
            ended_by_user = self._stop
            self._stop = True              # release _face_loop even when the session ended by itself
            face_thread.join(timeout=2)
            if not ended_by_user:
                self.state = "idle"

    def _pump(self, sock, ff):
        ffin = ff.stdin
        assert ffin is not None
        try:
            while not self._stop:
                data = sock.recv(65536)
                if not data:
                    break
                ffin.write(data)
                ffin.flush()            # push immediately — buffering here = bursty decode = choppy
        except (OSError, BrokenPipeError, ValueError):
            pass
        finally:
            try: ffin.close()
            except (OSError, ValueError): pass


# ---------------------------------------------------------------- GUI

# Catppuccin Mocha — calm, modern dark palette.
BG, BG2, CARD = "#181825", "#1e1e2e", "#313244"
FG, SUB, MUTED = "#cdd6f4", "#a6adc8", "#6c7086"
ACC, ACC_FG = "#89b4fa", "#11111b"
OK, WARN, ERR = "#a6e3a1", "#f9e2af", "#f38ba8"


def run_gui(host, port):
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    adb_forward(port)
    pipe = Pipeline()
    pipe.zoom = float(cfg["zoom"])
    pipe.pan = list(cfg["pan"])
    pipe.denoise = bool(cfg["denoise"])
    pipe.ev = int(cfg["ev"])
    pipe.bitrate = int(cfg["bitrate"])

    root = tk.Tk()
    root.title("OP3T Webcam")
    root.configure(bg=BG)
    root.geometry("440x812")
    root.resizable(False, False)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG,
                    arrowcolor=ACC, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                    selectbackground=CARD, selectforeground=FG, padding=6)
    style.map("TCombobox",
              fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACC)
    root.option_add("*TCombobox*Listbox.selectForeground", ACC_FG)

    # ---- header
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", pady=(16, 6))
    tk.Label(head, text="OP3T Webcam", bg=BG, fg=FG, font=("Segoe UI Semibold", 17)).pack()
    tk.Label(head, text="OnePlus 3T  →  OBS Virtual Camera", bg=BG, fg=MUTED,
             font=("Segoe UI", 9)).pack()

    # ---- settings card
    def card(title):
        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(wrap, text=title.upper(), bg=BG, fg=MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=4, pady=(0, 4))
        c = tk.Frame(wrap, bg=CARD)
        c.pack(fill="x")
        return c

    settings = card("Stream")
    res_v = tk.StringVar(value=cfg["resolution"])
    fps_v = tk.StringVar(value=cfg["fps"])
    rot_v = tk.StringVar(value=cfg["rotation"])
    dec_v = tk.StringVar(value=cfg["decoder"])
    fliph_v = tk.BooleanVar(value=cfg["flip_h"])
    flipv_v = tk.BooleanVar(value=cfg["flip_v"])
    denoise_v = tk.BooleanVar(value=cfg["denoise"])
    flash_v = tk.BooleanVar(value=False)   # torch — never persisted (no surprise relight)
    auto_v = tk.BooleanVar(value=cfg["autoframe"])
    sensor_v = tk.StringVar(value=cfg["sensor_zoom"])
    sensor_x = tk.DoubleVar(value=cfg["sensor_zoom_x"])

    def row(parent, label, var, values, r):
        tk.Label(parent, text=label, bg=CARD, fg=SUB, font=("Segoe UI", 10)).grid(
            row=r, column=0, sticky="w", padx=14, pady=8)
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=17)
        cb.grid(row=r, column=1, padx=14, pady=8)
        cb.bind("<<ComboboxSelected>>", lambda e: (root.focus(), on_change()))
        return cb

    row(settings, "Resolution", res_v, RESOLUTIONS, 0)
    fps_cb = row(settings, "Frame rate", fps_v, FPS_OPTS, 1)
    row(settings, "Orientation", rot_v, ROTATIONS, 2)
    row(settings, "Decoder", dec_v, list(DECODERS.keys()), 3)

    # ---- image card (zoom + refocus + flips + denoise)
    img = card("Image")

    zrow = tk.Frame(img, bg=CARD)
    zrow.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
    img.grid_columnconfigure(0, weight=1)
    zlbl = tk.Label(zrow, text="Zoom 1.0x", bg=CARD, fg=SUB, font=("Segoe UI", 10), width=9, anchor="w")
    zlbl.pack(side="left")

    def send_zoom():
        z = float(zoom.get())
        if z <= 1.05:                       # no zoom -> recenter, full frame
            pan[0], pan[1] = 0.5, 0.5
        pipe.set_zoom(z, pan[0], pan[1])
        save_now()

    def on_zoom(v):
        # With auto-framing on this slider means FRAMING TIGHTNESS, not zoom — the controller owns
        # the zoom. Changing the label is the only cue the user gets that the control changed
        # meaning, so it is not optional. Note this must NOT fall through to send_zoom(), which would
        # disengage auto-framing on every drag.
        if auto_v.get():
            pipe.af.tightness = min(AF_TIGHT_MAX, max(AF_TIGHT_MIN, float(v) * AF_FRAC_PER_X))
            zlbl.config(text=f"Frame {float(v):.1f}x")
            save_now()
            return
        zlbl.config(text=f"Zoom {float(v):.1f}x")
        send_zoom()

    # PC-side crop box, so zoom is not bounded by the sensor at all — allow up to 4x. (The sensor's
    # own SCALER_AVAILABLE_MAX_DIGITAL_ZOOM is 4.0 here, MEASURED; the 1.9 figure that used to be in
    # this comment and in the README was never right.)
    zoom = tk.Scale(zrow, from_=1.0, to=4.0, resolution=0.1, orient="horizontal",
                    command=on_zoom, bg=CARD, fg=FG, troughcolor=BG2, highlightthickness=0,
                    activebackground=ACC, sliderrelief="flat", showvalue=False, length=210)
    zoom.set(float(cfg["zoom"]))
    zoom.pack(side="left", fill="x", expand=True, padx=8)
    tk.Button(zrow, text="Refocus", command=lambda: pipe.send_control("FOCUS"),
              bg=BG2, fg=FG, activebackground=ACC, activeforeground=ACC_FG, relief="flat",
              font=("Segoe UI", 9), cursor="hand2", padx=10).pack(side="right")

    frow = tk.Frame(img, bg=CARD)
    frow.grid(row=1, column=0, sticky="w", padx=14, pady=(2, 10))

    def chk(text, var):
        # flips + denoise are live phone-side controls -> on_toggle (no stream restart, no crash risk).
        tk.Checkbutton(frow, text=text, variable=var, command=lambda: on_toggle(),
                       bg=CARD, fg=FG, selectcolor=BG2, activebackground=CARD, activeforeground=ACC,
                       font=("Segoe UI", 10), cursor="hand2", highlightthickness=0, bd=0).pack(
            side="left", padx=(0, 16))

    chk("Flip H", fliph_v)
    chk("Flip V", flipv_v)
    chk("Denoise", denoise_v)          # was "Denoise (phone)" — shortened to fit Auto-frame on this row

    def on_flash():
        # torch: live phone-side, no restart.
        if running["on"]:
            pipe.set_torch(flash_v.get())

    tk.Checkbutton(frow, text="Flash", variable=flash_v, command=on_flash,
                   bg=CARD, fg=FG, selectcolor=BG2, activebackground=CARD, activeforeground=ACC,
                   font=("Segoe UI", 10), cursor="hand2", highlightthickness=0, bd=0).pack(
        side="left", padx=(0, 10))

    def on_autoframe():
        # Auto-framing is entirely PC-side: no phone verb, no stream restart. Arming from the CURRENT
        # framing means engaging never snaps — it eases in from wherever manual left the crop box.
        if auto_v.get():
            pipe.af.arm(float(zoom.get()), pan[0], pan[1])
            # the slider now means tightness, so show the CURRENT tightness rather than silently
            # reinterpreting whatever zoom value happened to be there
            s = round(pipe.af.tightness / AF_FRAC_PER_X, 1)
            zoom.set(s)
            zlbl.config(text=f"Frame {s:.1f}x")
        else:
            pipe.af.disengage(keep_current=True)
            # hand the settled framing back to the manual controls so the image does not jump
            z, cx, cy = pipe.auto_view
            zoom.set(round(z, 1))
            zlbl.config(text=f"Zoom {z:.1f}x")
            pan[0], pan[1] = cx, cy
        save_now()

    tk.Checkbutton(frow, text="Auto-frame", variable=auto_v, command=on_autoframe,
                   bg=CARD, fg=FG, selectcolor=BG2, activebackground=CARD, activeforeground=ACC,
                   font=("Segoe UI", 10), cursor="hand2", highlightthickness=0, bd=0).pack(
        side="left", padx=(0, 10))

    # Sensor zoom. Rides the SAME row as the checkboxes on purpose — this window is already 83 px
    # over its own height and a new row would push the Start button off small screens.
    def on_sensor(*_):
        pipe.set_sensor_zoom(sensor_v.get(), sensor_x.get())
        sx.config(state="normal" if sensor_v.get() == "on" else "disabled")
        save_now()

    tk.Label(frow, text="Sensor", bg=CARD, fg=SUB, font=("Segoe UI", 10)).pack(side="left")
    ttk.Combobox(frow, textvariable=sensor_v, values=list(SENSOR_MODES), state="readonly",
                 width=5).pack(side="left", padx=(6, 6))
    sx = tk.Spinbox(frow, from_=1.0, to=AF_SENSOR_MAX, increment=0.1, width=4,
                    textvariable=sensor_x, command=on_sensor, bg=BG2, fg=FG, relief="flat",
                    font=("Segoe UI", 10), justify="center")
    sx.pack(side="left")
    sx.config(state="normal" if cfg["sensor_zoom"] == "on" else "disabled")
    sensor_v.trace_add("write", on_sensor)

    # Quality (live encoder bitrate, Mbps) — also a lag lever when the PC is busy.
    qrow = tk.Frame(img, bg=CARD)
    qrow.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 2))
    qlbl = tk.Label(qrow, text=f"Quality {cfg['bitrate']}", bg=CARD, fg=SUB, font=("Segoe UI", 10),
                    width=9, anchor="w")
    qlbl.pack(side="left")

    def on_quality(v):
        qlbl.config(text=f"Quality {int(float(v))}")
        pipe.set_bitrate(int(float(v)))
        save_now()

    quality = tk.Scale(qrow, from_=4, to=20, resolution=1, orient="horizontal", command=on_quality,
                       bg=CARD, fg=FG, troughcolor=BG2, highlightthickness=0, activebackground=ACC,
                       sliderrelief="flat", showvalue=False, length=260)
    quality.set(int(cfg["bitrate"]))
    quality.pack(side="left", fill="x", expand=True, padx=8)

    # Exposure compensation (AE steps; phone clamps to its sensor range).
    erow = tk.Frame(img, bg=CARD)
    erow.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 10))
    elbl = tk.Label(erow, text=f"Exposure {cfg['ev']}", bg=CARD, fg=SUB, font=("Segoe UI", 10),
                    width=9, anchor="w")
    elbl.pack(side="left")

    def on_exposure(v):
        elbl.config(text=f"Exposure {int(float(v))}")
        pipe.set_ev(int(float(v)))
        save_now()

    exposure = tk.Scale(erow, from_=-6, to=6, resolution=1, orient="horizontal", command=on_exposure,
                        bg=CARD, fg=FG, troughcolor=BG2, highlightthickness=0, activebackground=ACC,
                        sliderrelief="flat", showvalue=False, length=260)
    exposure.set(int(cfg["ev"]))
    exposure.pack(side="left", fill="x", expand=True, padx=8)

    pan = list(cfg["pan"])

    # ---- preview (always visible; big and easy to grab)
    pv_wrap = card("Preview")
    PV_W, PV_H = 408, 230
    preview_box = tk.Canvas(pv_wrap, bg="#000000", width=PV_W, height=PV_H, highlightthickness=0,
                            cursor="fleur")
    preview_box.pack(padx=2, pady=2)
    preview_box.create_text(PV_W // 2, PV_H // 2, text="preview off (lower latency) — toggle below",
                            fill=MUTED, font=("Segoe UI", 9))
    prev_img: dict[str, object] = {"ref": None}

    drag = {"x": 0, "y": 0}
    pvdim = {"w": PV_W, "h": PV_H}   # actual displayed preview-image size (set each frame in refresh)

    def on_press(e):
        drag["x"], drag["y"] = e.x, e.y

    def on_drag(e):
        z = float(zoom.get())
        if z <= 1.05:
            return
        # Move the crop box WITH the cursor (Camo feel): cursor delta in image px -> frame fraction.
        dx = (e.x - drag["x"]) / pvdim["w"]
        dy = (e.y - drag["y"]) / pvdim["h"]
        drag["x"], drag["y"] = e.x, e.y
        lo, hi = 0.5 / z, 1 - 0.5 / z                  # keep crop centre inside the frame
        pan[0] = min(max(pan[0] + dx, lo), hi)
        pan[1] = min(max(pan[1] + dy, lo), hi)
        send_zoom()

    def reset_view():
        zoom.set(1.0)
        pan[0], pan[1] = 0.5, 0.5
        send_zoom()

    def on_wheel(e):
        # scroll over the preview = zoom (Camo-style: scroll to size the crop, drag to pan it).
        step = 0.1 if e.delta > 0 else -0.1
        z = min(4.0, max(1.0, round(float(zoom.get()) + step, 1)))
        zoom.set(z)            # triggers on_zoom -> send_zoom

    preview_box.bind("<Button-1>", on_press)
    preview_box.bind("<B1-Motion>", on_drag)
    preview_box.bind("<Double-Button-1>", lambda e: reset_view())
    preview_box.bind("<MouseWheel>", on_wheel)

    # ---- status + actions
    status = tk.Label(root, text="● idle", bg=BG, fg=SUB, font=("Segoe UI", 11))
    status.pack(pady=(12, 0))
    sub = tk.Label(root, text="", bg=BG, fg=MUTED, font=("Consolas", 9))
    sub.pack()

    btns = tk.Frame(root, bg=BG)
    btns.pack(pady=12)
    running = {"on": False}

    def cur():
        w, h = map(int, res_v.get().split("x"))
        return (host, port, w, h, int(fps_v.get()), int(rot_v.get()), dec_v.get(),
                fliph_v.get(), flipv_v.get(), denoise_v.get())

    def snapshot():
        return {
            "resolution": res_v.get(), "fps": fps_v.get(), "rotation": rot_v.get(),
            "decoder": dec_v.get(), "flip_h": fliph_v.get(), "flip_v": flipv_v.get(),
            "denoise": denoise_v.get(), "zoom": round(float(zoom.get()), 2), "pan": [round(pan[0], 3), round(pan[1], 3)],
            "ev": int(exposure.get()), "bitrate": int(quality.get()),
            # `focus` was MISSING here, so every tkinter save silently deleted it from config.json
            # (it is in DEFAULTS but was never written back). Same trap would hit `autoframe`.
            "focus": round(float(pipe.focus), 3), "autoframe": auto_v.get(),
            "sensor_zoom": sensor_v.get(), "sensor_zoom_x": round(float(sensor_x.get()), 2),
        }

    def save_now():
        save_config(snapshot())

    def sync_fps():
        # Output is always 1080p now and the phone chooses its own capture size from the frame rate
        # (4K at 30, 1080p at 60), so there is no longer a resolution/fps pairing to police here.
        fps_cb.config(values=FPS_OPTS)

    # Debounce restarts: rapid combobox/checkbox changes used to thrash the phone's camera
    # (open/close race) and crash the app. Coalesce them into ONE restart after a short pause.
    restart_job = {"id": None}

    def do_restart():
        restart_job["id"] = None
        if running["on"]:
            pipe.start(*cur())

    def on_toggle():
        # checkboxes: apply live over the open socket, no restart (denoise + GPU flips on the phone).
        save_now()
        if running["on"]:
            pipe.set_denoise(denoise_v.get())
            pipe.set_transform(int(rot_v.get()), fliph_v.get(), flipv_v.get())

    def on_change():
        # comboboxes: these change the ffmpeg command / negotiated stream -> debounced restart.
        sync_fps()
        save_now()
        if restart_job["id"]:
            root.after_cancel(restart_job["id"])
        restart_job["id"] = root.after(400, do_restart)

    def start():
        running["on"] = True
        pipe.zoom = float(zoom.get())
        pipe.pan = list(pan)
        if auto_v.get():
            pipe.af.arm(float(zoom.get()), pan[0], pan[1])
        pipe.set_sensor_zoom(sensor_v.get(), sensor_x.get())
        pipe.ev = int(exposure.get())
        pipe.bitrate = int(quality.get())
        pipe.torch = bool(flash_v.get())
        pipe.start(*cur())
        btn.config(text="Stop", bg=ERR, fg=ACC_FG)

    def stop():
        running["on"] = False
        if restart_job["id"]:
            root.after_cancel(restart_job["id"]); restart_job["id"] = None
        pipe.stop()
        btn.config(text="Start", bg=ACC, fg=ACC_FG)

    def toggle():
        stop() if running["on"] else start()

    btn = tk.Button(btns, text="Start", command=toggle, bg=ACC, fg=ACC_FG,
                    activebackground=ACC, relief="flat", font=("Segoe UI Semibold", 13),
                    width=13, height=1, cursor="hand2")
    btn.pack(side="left", padx=6)

    def toggle_preview():
        # Building the preview runs on the send thread; off = a little less work = a little less lag.
        pipe.preview_on = not pipe.preview_on
        if pipe.preview_on:
            pbtn.config(text="Preview: On", bg=ACC, fg=ACC_FG)
        else:
            pbtn.config(text="Preview: Off", bg=BG2, fg=FG)
            preview_box.delete("all")
            preview_box.create_text(PV_W // 2, PV_H // 2, text="preview off (lower latency)",
                                    fill=MUTED, font=("Segoe UI", 9))

    pbtn = tk.Button(btns, text="Preview: Off", command=toggle_preview, bg=BG2, fg=FG,
                     activebackground=ACC, activeforeground=ACC_FG, relief="flat",
                     font=("Segoe UI Semibold", 11), width=11, height=1, cursor="hand2")
    pbtn.pack(side="left", padx=6)

    tk.Label(root, text='Pick "OBS Virtual Camera" in your meeting app.',
             bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(side="bottom", pady=10)

    def refresh():
        colors = {"idle": SUB, "connecting": ACC, "streaming": OK, "error": ERR}
        status.config(text=f"● {pipe.state}", fg=colors.get(pipe.state, SUB))
        # Manual input (drag/scroll/slider) disengages auto inside Pipeline.set_zoom, so the checkbox
        # has to follow, or it would claim to be on while nothing is tracking.
        if auto_v.get() and not pipe.af.enabled and running["on"]:
            auto_v.set(False)
            z0, cx0, cy0 = pipe.auto_view
            zoom.set(round(z0, 1))
            pan[0], pan[1] = cx0, cy0
        if pipe.state == "streaming":
            af_txt = ""
            if pipe.af.enabled:
                lost = time.monotonic() - pipe.face_seen > AF_LOST_HOLD_S
                af_txt = "   auto: no face" if lost else "   auto: tracking"
            sub.config(text=f"{pipe.fps:4.1f} fps   {pipe.frames} frames   "
                            f"{pipe.dropped} dropped{af_txt}")
        else:
            sub.config(text=pipe.msg)
        if pipe.preview_on and pipe._preview_ppm:
            try:
                im = tk.PhotoImage(data=pipe._preview_ppm)
                prev_img["ref"] = im
                preview_box.delete("all")
                preview_box.create_image(PV_W // 2, PV_H // 2, image=im)
                pw, ph = im.width(), im.height()
                pvdim["w"], pvdim["h"] = pw, ph
                il, it = (PV_W - pw) / 2, (PV_H - ph) / 2
                auto_on = pipe.af.enabled
                # Draw the APPLIED view, never a UI-local copy: with auto-framing on, the crop box
                # would otherwise sit motionless while the output pans — a preview that lies.
                if auto_on:
                    z, px, py = pipe.auto_view
                    lost = time.monotonic() - pipe.face_seen > AF_LOST_HOLD_S
                    col = WARN if lost else OK
                    hint = ("auto-frame: no face — holding" if lost
                            else "auto-frame on · drag or scroll to take over")
                    # the raw detection, so "why isn't it following me" is diagnosable (bad light,
                    # side profile, too far) instead of a mystery
                    for b in pipe.face_boxes:
                        fx, fy, fw_, fh_ = b[0], b[1], b[2], b[3]
                        preview_box.create_rectangle(
                            il + (fx - fw_ / 2) * pw, it + fy * ph,
                            il + (fx + fw_ / 2) * pw, it + (fy + fh_) * ph,
                            outline=SUB, width=1, dash=(3, 3))
                else:
                    z, px, py = float(zoom.get()), pan[0], pan[1]
                    col = ACC
                    hint = "drag box to pan · scroll to zoom · double-click reset"
                if z > 1.05 or auto_on:
                    # Camo-style crop box drawn over the FULL-frame preview: shows exactly what the
                    # vcam outputs and what's cropped out. Drag = pan, scroll = zoom.
                    bw, bh = pw / z, ph / z
                    bl = il + (px - 0.5 / z) * pw
                    bt = it + (py - 0.5 / z) * ph
                    preview_box.create_rectangle(bl, bt, bl + bw, bt + bh, outline=col, width=2)
                    preview_box.create_text(PV_W // 2, PV_H - 10, text=hint,
                                            fill=col, font=("Segoe UI", 8))
            except Exception:
                pass
        root.after(100, refresh)

    refresh()

    def on_close():
        save_now()
        pipe.stop()
        shutdown_phone(port)      # closing the app (not Stop) puts the phone to sleep
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


# ---------------------------------------------------------------- test + headless

def run_test(w, h, fps):
    print(f"TEST MODE: {w}x{h}@{fps} moving pattern -> OBS Virtual Camera. Ctrl+C to stop.")
    with pyvirtualcam.Camera(width=w, height=h, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR) as cam:
        print(f"virtual camera live: {cam.device}  (pick this in Discord)")
        frame = np.zeros((h, w, 3), np.uint8)
        x = np.arange(w, dtype=np.int32)
        i = 0
        while True:
            frame[:, :, 0] = ((x + i) % 256).astype(np.uint8)
            frame[:, :, 1] = ((x + i * 2) % 256).astype(np.uint8)
            frame[:, :, 2] = ((x - i) % 256).astype(np.uint8)
            cam.send(frame)
            cam.sleep_until_next_frame()
            i += 3


def run_headless(host, port, w, h, fps, rot, decoder, autoframe=False,
                 sensor_zoom="off", sensor_zoom_x=1.0):
    adb_forward(port)
    pipe = Pipeline()
    # headless never calls load_config, so the `autoframe` config key does nothing here — the flag is
    # the only way in, and the effective view is printed so the feature is observable without a GUI.
    if autoframe:
        pipe.af.arm(1.0, 0.5, 0.5)
    pipe.set_sensor_zoom(sensor_zoom, sensor_zoom_x)
    pipe.start(host, port, w, h, fps, rot, decoder)
    print(f"headless {w}x{h}@{fps} rot={rot} dec={decoder} autoframe={autoframe} "
          f"sensor={sensor_zoom}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            z, cx, cy = pipe.auto_view
            view = f"  z={z:.2f} pan=({cx:.3f},{cy:.3f})" if autoframe else ""
            if pipe.sz.mode != "off":
                view += f" sensor={pipe.sz.obs:.2f}x(req {pipe.sz.req:.2f})"
            print(f"\r{pipe.state}: {pipe.fps:4.1f} fps  {pipe.frames} frames  "
                  f"{pipe.dropped} dropped{view}  {pipe.msg}   ", end="")
    except KeyboardInterrupt:
        pipe.stop()
        shutdown_phone(port)      # same as closing the GUI: sleep the phone, release adb


def _raise_own_priority():
    """Nudge this receiver above normal so the reader/sender threads keep pace with a busy desktop
    (screenshare/video). ffmpeg itself runs HIGH; this keeps the frame hand-off from stalling."""
    try:
        import ctypes
        ABOVE_NORMAL = 0x00008000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), ABOVE_NORMAL)
    except Exception:
        pass


# ---------------------------------------------------------------- start with Windows / one instance

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "OP3T Webcam"


def autostart_command():
    """What Windows runs at login: this exe, or this script under pythonw, straight into the tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}" --tray'


def get_autostart(key=RUN_KEY):
    """The login entry as stored, or "" when there is none."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return str(winreg.QueryValueEx(k, RUN_VALUE)[0] or "")
    except OSError:
        return ""


def set_autostart(on, key=RUN_KEY):
    """Add or remove the per-user login entry (HKCU, so no admin prompt). Returns the new state."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
    return bool(get_autostart(key))


def _single_instance(show_existing):
    """Claim the one-copy-per-user slot. Returns this copy's "show yourself" event handle, None when
    another copy already runs (which is then asked to show its window, unless show_existing is False:
    a login launch must not pop anything open), or 0 when Windows would not say — run anyway.

    One copy matters now that the app lives in the tray: two would fight over the one virtual camera,
    and double-clicking the exe should bring the running one forward, not start a second."""
    try:
        import ctypes
        from ctypes import wintypes as w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = k32.CreateEventW.restype = w.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
        k32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
        k32.SetEvent.argtypes = [w.HANDLE]
        mutex = k32.CreateMutexW(None, False, "Local\\OP3TWebcam.Instance")
        first = ctypes.get_last_error() != 183              # ERROR_ALREADY_EXISTS
        show = k32.CreateEventW(None, False, False, "Local\\OP3TWebcam.Show")    # auto-reset
        if not first:
            if show_existing and show:
                k32.SetEvent(show)
            return None
        _single_instance.held = mutex                      # owned for the life of the process
        return show or 0
    except Exception:
        return 0


def main():
    _raise_own_priority()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--decoder", default=DEFAULT_DECODER, choices=list(DECODERS.keys()))
    ap.add_argument("--test", action="store_true", help="vcam test pattern, no phone")
    ap.add_argument("--headless", action="store_true", help="run pipeline without the GUI")
    ap.add_argument("--autoframe", action="store_true", help="face-tracking auto zoom+pan (headless)")
    ap.add_argument("--sensor-zoom", default="off", choices=list(SENSOR_MODES),
                    help="who magnifies: off = PC crop only, auto = phone takes over once the PC "
                         "crop is pinned, on = fixed phone ratio (--sensor-zoom-x)")
    ap.add_argument("--sensor-zoom-x", type=float, default=1.0,
                    help=f"fixed sensor ratio for --sensor-zoom on (1.0..{AF_SENSOR_MAX})")
    ap.add_argument("--tray", action="store_true",
                    help="start hidden in the notification area (what Start with Windows runs)")
    args = ap.parse_args()

    if args.test:
        run_test(args.width, args.height, args.fps)
    elif args.headless:
        run_headless(args.host, args.port, args.width, args.height, args.fps, args.rotation,
                     args.decoder, args.autoframe, args.sensor_zoom, args.sensor_zoom_x)
    else:
        show_event = _single_instance(show_existing=not args.tray)
        if show_event is None:
            return                     # the running copy was asked to show itself
        # Liquid-glass web UI (pywebview). Falls back to the tkinter GUI if it's unavailable.
        ctx = {"host": args.host, "port": args.port, "Pipeline": Pipeline,
               "RESOLUTIONS": RESOLUTIONS, "FPS_OPTS": FPS_OPTS, "ROTATIONS": ROTATIONS,
               "DECODERS": DECODERS,
               "load_config": load_config, "save_config": save_config, "adb_forward": adb_forward,
               "shutdown_phone": shutdown_phone, "park_phone": park_phone,
               "start_mjpeg": start_mjpeg, "log_js_error": log_js_error,
               "VcamHost": VcamHost, "AutoCam": AutoCam, "tray": args.tray, "show_event": show_event,
               "get_autostart": get_autostart, "set_autostart": set_autostart,
               "autostart_command": autostart_command}
        try:
            import webui
            log_js_error("webui.run: entering")
            if webui.run(ctx):
                return
            log_js_error("webui.run: returned False (pywebview missing) -> tkinter")
        except Exception as e:
            # A frozen build has no console, so this used to vanish and the app would silently
            # come up as the tkinter fallback instead.
            import traceback
            log_js_error("web UI unavailable -> tkinter: " + traceback.format_exc()[-1500:])
            print("web UI unavailable, using tkinter:", e)
        run_gui(args.host, args.port)


if __name__ == "__main__":
    main()
