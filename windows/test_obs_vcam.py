#!/usr/bin/env python3
"""Self-checks for obs_vcam.py, the app's own OBS Virtual Camera writer.
Run: python windows/test_obs_vcam.py   (exits 1 on failure)

1-4 check the shared-memory layout and the frame cycle against what a working producer (pyvirtualcam
0.15.0) was seen to write on 2026-10-05. 5 sends frames through OBS Studio's real filter and reads
them back with ffmpeg, so it needs OBS Studio and ffmpeg (else SKIP). Everything needs nothing else
producing the virtual camera right now (else SKIP).
"""
import ctypes
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

import obs_vcam as v
import op3t_webcam as m

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return str(e) or True
    except Exception as e:
        return f"wrong exception {type(e).__name__}: {e}"
    return False


class Reader:
    """What the filter does: open the section read-only and look."""

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                    ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        self.k, self.h = k, m._section_open()
        self.p = k.MapViewOfFile(self.h, 0x0004, 0, 0, 0) if self.h else None    # FILE_MAP_READ

    def u32(self, n=24):
        return [int(x) for x in np.frombuffer(ctypes.string_at(self.p, 4 * n), "<u4")]

    def at(self, off, n):
        return np.frombuffer(ctypes.string_at(self.p + off, n), np.uint8)

    def close(self):
        if self.p:
            self.k.UnmapViewOfFile(self.p)
        m._section_close(self.h)


def nv12(w, h, y, uv):
    f = np.empty((h * 3 // 2, w), np.uint8)
    f[:h], f[h:] = y, uv
    return f


if not v.installed():
    print("SKIP  OBS Studio is not installed: its virtual-camera filter is not registered")
    sys.exit(0)
if m._section_open():
    print("SKIP  something else is producing the OBS virtual camera right now")
    sys.exit(0)

# ---- 1. layout: byte for byte what pyvirtualcam 0.15.0 wrote for the same camera ----------------------
check("slot layout 1280x720", v.layout(1280, 720) == ([96, 1382528, 2764960], 4147392),
      f"{v.layout(1280, 720)}")
check("slot layout 640x480", v.layout(640, 480)[0] == [96, 460928, 921760], f"{v.layout(640, 480)}")

cam = v.ObsVirtualCamera(1280, 720, 30)
r = Reader()
seen = r.u32()
want = [0, 0, 1, 96, 1382528, 2764960, 0, 1280, 720, 0, 333333, 0] + [0] * 12
check("a new camera's header matches the reference, word for word", seen == want, f"{seen[:12]}")

# ---- 2. the frame cycle ----------------------------------------------------------------------------------
stamps, ok_slots = [], True
for k in range(1, 5):
    cam.send(nv12(1280, 720, 10 * k, 100 + k))
    hdr = r.u32(3)
    off = [96, 1382528, 2764960][k % 3]
    frame = r.at(off + 32, 1280 * 720 * 3 // 2)
    ok_slots &= (hdr == [k, k, 2] and frame[0] == 10 * k and frame[1280 * 720 - 1] == 10 * k
                 and frame[1280 * 720] == 100 + k and frame[-1] == 100 + k)
    stamps.append(int(r.at(off, 8).view("<u8")[0]))
check("each frame lands in slot write_idx % 3, then read_idx follows, state READY", ok_slots)
now = time.perf_counter_ns()
check("slot timestamps are perf_counter ns, increasing",
      all(b > a for a, b in zip(stamps, stamps[1:])) and 0 <= now - stamps[-1] < 5e9, f"{stamps}")

# ---- 3. what send() refuses ------------------------------------------------------------------------------
check("wrong shape is refused", raises(ValueError, lambda: cam.send(np.zeros((720, 1280), np.uint8))))
check("wrong dtype is refused",
      raises(ValueError, lambda: cam.send(np.zeros((1080, 1280), np.uint16))))
check("an odd size is refused", raises(ValueError, lambda: v.ObsVirtualCamera(641, 480, 30)))

# ---- 4. never two producers; close leaves STOPPING for readers ---------------------------------------
msg = raises(RuntimeError, lambda: v.ObsVirtualCamera(1280, 720, 30))
check("a second camera is refused while one exists", msg and "in use" in str(msg), f"{msg}")
cam.close()
check("close() marks the section STOPPING for readers still on it", r.u32(3)[2] == 3, f"{r.u32(3)}")
msg = raises(RuntimeError, lambda: v.ObsVirtualCamera(640, 480, 30))
check("...and a new camera waits until the last reader lets go", bool(msg), f"{msg}")
r.close()
check("closed and unread: the section is gone", m._section_open() is None)
cam.close()
check("close() twice is harmless", True)
check("send() after close() is an error, not a crash",
      raises(RuntimeError, lambda: cam.send(nv12(1280, 720, 0, 128))))

cam = v.ObsVirtualCamera(64, 48, 60)
r = Reader()
check("60 fps -> interval 166666 x 100 ns, as the reference wrote", r.u32(12)[10] == 166666,
      f"{r.u32(12)[10]}")
check("the writer holds exactly one handle (+ this reader's)", m._own_handles(r.h) == 2,
      f"own={m._own_handles(r.h)}")
r.close()
cam.close()

# ---- 5. end to end through OBS Studio's real filter --------------------------------------------------
ff = shutil.which("ffmpeg")
if not ff:
    print("SKIP  ffmpeg not on PATH — round trip through the real filter not checked")
else:
    W, H = 640, 480
    a = np.empty((H, W), np.uint8)
    a[:, :W // 2], a[:, W // 2:] = 40, 200                # left dark, right bright
    frames = [nv12(W, H, a, 128), nv12(W, H, a[:, ::-1], 128)]
    cam = v.ObsVirtualCamera(W, H, 30)
    probe = m._section_open()
    stop, users = threading.Event(), []

    def feed():
        t0 = time.monotonic()
        while not stop.is_set():
            cam.send(frames[0 if time.monotonic() - t0 < 2.5 else 1])   # swap halves after 2.5 s
            users.append(m._section_handles(probe) - 2)
            time.sleep(1 / 30)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    time.sleep(0.3)
    idle_users = max(users) if users else -1
    # Scaled to 64x48 NV12: Y passes through untouched whatever format the filter hands over.
    p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "dshow",
                        "-i", "video=OBS Virtual Camera", "-t", "5", "-s", "64x48",
                        "-pix_fmt", "nv12", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=30)
    time.sleep(0.5)
    stop.set()
    t.join()
    after_users = users[-1]
    cam.close()
    m._section_close(probe)
    n = len(p.stdout) // (64 * 48 * 3 // 2)
    check("ffmpeg captured from the OBS Virtual Camera", p.returncode == 0 and n > 30,
          f"rc={p.returncode} frames={n} {p.stderr[-300:]!r}")
    if n:
        ys = [np.frombuffer(p.stdout, np.uint8, 64 * 48, i * 64 * 48 * 3 // 2).reshape(48, 64)
              for i in range(n)]
        lr = [(float(y[:, :16].mean()), float(y[:, 48:].mean())) for y in ys]
        hit = any(abs(lo - 40) < 8 and abs(hi - 200) < 8 for lo, hi in lr)
        check("our frames come out of the filter (left 40, right 200)", hit,
              f"first={lr[0]} last={lr[-1]}")
        check("...live: the swap made 2.5 s in shows up too",
              abs(lr[-1][0] - 200) < 8 and abs(lr[-1][1] - 40) < 8, f"last={lr[-1]}")
    check("the capture counted as one app with the camera on",
          idle_users == 0 and max(users) == 1, f"before={idle_users} peak={max(users)}")
    check("...and stopped counting when ffmpeg let go", after_users == 0, f"after={after_users}")

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
