#!/usr/bin/env python3
"""
Measures the LAST unmeasured stage: obs_vcam write -> DirectShow consumer delivery.

Everything else in the pipeline now has a number:
  * phone capture -> socket      ~66 ms   (CameraStreamer logs "capture->socket", pipelineDepth=5)
  * socket -> decoded frame      ~39 ms   (latency_probe.py, QSV)
...but nothing has ever measured what happens AFTER we hand a frame to the virtual camera. The app's
own GUI preview cannot see this stage (it reads the frame slot directly, and it polls at 150 ms), so
"the preview lags too" was never evidence about it either way.

HOW IT WORKS
  One process, one clock, so no clock-sync problem:
    writer thread : builds an NV12 frame whose Y plane carries a BARCODE of "ms since t0", and sends
                    it through obs_vcam.ObsVirtualCamera at the requested fps.
    reader        : bundled ffmpeg opens "OBS Virtual Camera" as a dshow capture device, downscaled
                    to grey rawvideo on a pipe. For each frame we decode the barcode and subtract:
                        latency = now_ms - barcode_ms
                    = time from our send() to the frame being delivered out the DirectShow path.

  The barcode is 2 sync blocks (known white, known black) + 20 data bits, as big luma blocks in the
  top-left. Big blocks survive the filter's format conversion and the downscale; the sync pair gives
  the decoder its own white/black reference instead of a hard-coded threshold, so it does not care
  whether the consumer hands back full-range or limited-range luma.

WHAT THE NUMBER INCLUDES (read this before quoting it)
  our send() -> OBS filter -> DirectShow graph -> ffmpeg's dshow input buffer -> pipe -> us.
  ffmpeg's own dshow input buffering is inside it, so treat this as an UPPER BOUND on what a
  consumer pays. A real app (Discord/Zoom) may buffer more, never less. What it does bound tightly is
  OUR side: if this reads ~20-40 ms, obs_vcam.py is not where the latency lives.

REQUIREMENTS
  OBS Virtual Camera installed, and NOTHING ELSE producing it right now (close the OP3T Webcam app —
  the virtual camera takes one producer). ffmpeg comes from op3t_webcam.FFMPEG.

Usage:
  python windows/vcam_latency_test.py                      # 1920x1080@60, 10 s
  python windows/vcam_latency_test.py --seconds 20 --fps 30
  python windows/vcam_latency_test.py --width 1280 --height 720
"""
import argparse
import statistics
import subprocess
import sys
import threading
import time

import numpy as np

from op3t_webcam import FFMPEG, NO_WINDOW
from obs_vcam import ObsVirtualCamera, installed

# Barcode geometry, in SOURCE pixels. 22 blocks x 80 px = 1760 px, fits 1920 wide.
BLOCK = 80
SYNC_BITS = 2            # block 0 = white reference, block 1 = black reference
DATA_BITS = 20           # 2^20 ms ~= 17.5 minutes before the counter wraps
N_BLOCKS = SYNC_BITS + DATA_BITS
Y_WHITE, Y_BLACK = 235, 16   # limited-range safe, so no clipping surprises either way


def build_frame(w, h, value):
    """An NV12 frame (h*3//2, w) whose Y plane carries `value` as a barcode in the top-left."""
    frame = np.empty((h * 3 // 2, w), np.uint8)
    frame[:h] = Y_BLACK                      # Y plane: dark field
    frame[h:] = 128                          # UV plane: neutral grey
    band = min(BLOCK, h)
    frame[0:band, 0:BLOCK] = Y_WHITE         # block 0: white reference
    frame[0:band, BLOCK:2 * BLOCK] = Y_BLACK  # block 1: black reference
    for i in range(DATA_BITS):
        if (value >> i) & 1:
            x = (SYNC_BITS + i) * BLOCK
            frame[0:band, x:x + BLOCK] = Y_WHITE
    return frame


def decode_frame(gray, sw, sh, w, h):
    """Decode the barcode from a `sh x sw` grey frame that was downscaled from `h x w`.
    Returns the value, or None if the sync blocks don't look like a barcode."""
    sx = sw / w
    sy = sh / h
    band = min(BLOCK, h)
    cy = int((band / 2) * sy)
    cy = min(max(cy, 1), sh - 2)

    def block_mean(i):
        cx = int(((i + 0.5) * BLOCK) * sx)
        cx = min(max(cx, 1), sw - 2)
        return float(gray[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2].mean())

    hi = block_mean(0)
    lo = block_mean(1)
    if hi - lo < 20:                 # no contrast -> not our frame (black startup frame, or a resize)
        return None
    mid = (hi + lo) / 2
    value = 0
    for i in range(DATA_BITS):
        if block_mean(SYNC_BITS + i) > mid:
            value |= (1 << i)
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--read-width", type=int, default=480,
                    help="downscale the read-back (blocks stay readable; less pipe traffic)")
    ap.add_argument("--legacy-reader", action="store_true",
                    help="use the original reader (no -flush_packets / -fps_mode passthrough) to A/B "
                         "how much of the number was the measuring tool itself")
    ap.add_argument("--filter-fps", type=int, default=0,
                    help="declare THIS rate to the OBS filter while still sending at --fps. The filter "
                         "free-runs its own tick at the declared interval and shows the newest slot, so "
                         "a faster tick shortens the phase wait (avg half an interval) — at the cost of "
                         "consumers seeing that rate. Experiment only.")
    args = ap.parse_args()

    w, h, fps = args.width, args.height, args.fps
    if w < N_BLOCKS * BLOCK:
        print(f"width {w} too small for the barcode ({N_BLOCKS * BLOCK} px needed)")
        return 1
    if not installed():
        print("OBS Virtual Camera is not installed (install OBS Studio).")
        return 1

    sw = args.read_width
    sh = max(2, int(round(sw * h / w)) // 2 * 2)
    frame_bytes = sw * sh

    t0 = time.perf_counter()

    def now_ms():
        return int((time.perf_counter() - t0) * 1000)

    stop = threading.Event()
    sent = {"n": 0}

    try:
        cam = ObsVirtualCamera(w, h, args.filter_fps or fps)   # only sets the header interval
    except Exception as e:
        print(f"could not open the virtual camera: {e}")
        print("Is the OP3T Webcam app still running? The virtual camera takes ONE producer.")
        return 1

    def writer():
        period = 1.0 / fps
        nxt = time.perf_counter()
        while not stop.is_set():
            # stamp as late as possible, so the barcode reflects the moment of send()
            try:
                cam.send(build_frame(w, h, now_ms() & ((1 << DATA_BITS) - 1)))
            except Exception:
                return
            sent["n"] += 1
            nxt += period
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.perf_counter()

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    time.sleep(0.6)                 # let the filter see a READY producer before ffmpeg opens it

    # The reader must not add latency of its own, or the number blames the virtual camera for it.
    # Same two traps the main pipeline already fixed: rawvideo-to-pipe strands the tail of every frame
    # in a 32 KiB AVIO buffer until the NEXT frame pushes it out (-flush_packets 1), and rawvideo's
    # default fps mode is CFR re-pacing, which can hold a frame (-fps_mode passthrough).
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
           "-f", "dshow", "-i", "video=OBS Virtual Camera",
           "-fps_mode", "passthrough", "-flush_packets", "1",
           "-s", f"{sw}x{sh}", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
    if args.legacy_reader:          # the original reader, to A/B how much of the number was the reader
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "dshow", "-i", "video=OBS Virtual Camera",
               "-s", f"{sw}x{sh}", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
    print("reader:", " ".join(cmd[1:]))
    ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=NO_WINDOW)

    lats = []
    bad = 0
    deadline = time.perf_counter() + args.seconds
    try:
        while time.perf_counter() < deadline:
            buf = ff.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            t_recv = now_ms()
            gray = np.frombuffer(buf, np.uint8).reshape(sh, sw)
            v = decode_frame(gray, sw, sh, w, h)
            if v is None:
                bad += 1
                continue
            d = t_recv - v
            if d < 0:                       # counter wrap
                d += (1 << DATA_BITS)
            if 0 <= d < 5000:
                lats.append(d)
            else:
                bad += 1
    finally:
        stop.set()
        wt.join(timeout=2)
        try: ff.terminate()
        except Exception: pass
        err = b""
        try: err = ff.stderr.read() or b""
        except Exception: pass
        cam.close()

    print()
    print("=" * 64)
    print(f"frames sent: {sent['n']}   frames read back: {len(lats) + bad}   undecodable: {bad}")
    if not lats:
        print("NO decodable frames. ffmpeg said:")
        print((err.decode(errors="replace") or "(nothing)").strip()[:800])
        print("If it reports a format/size failure, try --read-width 640, or check that nothing")
        print("else is producing the virtual camera.")
        print("=" * 64)
        return 1

    lats.sort()
    drop = min(len(lats) // 4, 30)          # discard startup: dshow graph build + first-frame costs
    body = lats[drop:] if len(lats) > drop + 5 else lats
    print(f"vcam send -> dshow consumer delivery, ms:")
    print(f"  median {statistics.median(body):6.1f}   mean {statistics.fmean(body):6.1f}   "
          f"min {min(body):6.1f}   p95 {body[int(len(body)*0.95)-1]:6.1f}   max {max(body):6.1f}")
    print(f"  (one frame at {fps} fps = {1000.0/fps:.1f} ms)")
    print()
    print("Includes ffmpeg's own dshow input buffering, so it is an UPPER BOUND on a consumer's cost.")
    print("Pipeline so far: ~66 ms phone + ~39 ms decode+pipe + this stage.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
