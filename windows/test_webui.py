#!/usr/bin/env python3
"""Self-checks for the web UI. Run: python test_webui.py   (exits 1 on failure)

WHY THIS FILE EXISTS. A JS -> Python call that does not exist fails SILENTLY inside pywebview: the
promise rejects, the catch swallows it, and the control just quietly does nothing. That is the same
shape of bug as the arm() regression (2026-08-27) — no crash, no log, a feature simply dead. So the
first check reads every `api.X(` out of the HTML and asserts X is a real method on Api.

No pywebview, no phone and no window needed: everything here is static analysis plus a fake Pipeline.
"""
import re
import sys

import numpy as np

import op3t_webcam as m
import webui

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


# ---- 1. every api.* the HTML calls actually exists ---------------------------------------------
called = set(re.findall(r"\bapi\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", webui.HTML))
missing = sorted(c for c in called if not callable(getattr(webui.Api, c, None)))
check("every api.* called by the UI exists on Api", not missing,
      f"called={len(called)} missing={missing}")

# ...and nothing on Api is dead weight. Not a hard failure — options/status are called by name and
# a few methods exist for the headless path — but it catches a rename that left an orphan behind.
public = {n for n in dir(webui.Api) if not n.startswith("_")}
unused = sorted(public - called)
check("no unreachable Api methods", not unused, f"unused={unused}")

# ---- 2. every element the JS touches is in the markup ------------------------------------------
ids_used = set(re.findall(r"\$\('([a-zA-Z0-9_]+)'\)", webui.HTML))
ids_used |= set(re.findall(r"getElementById\('([a-zA-Z0-9_]+)'\)", webui.HTML))
ids_have = set(re.findall(r"\bid=\"([a-zA-Z0-9_]+)\"", webui.HTML))
# szoff/szauto/szon are built by concatenation ('sz'+m), so name them explicitly
ids_used |= {"szoff", "szauto", "szon"}
ghosts = sorted(i for i in ids_used if i not in ids_have and i != "zapp")   # zapp is injected by JS
check("every element id the JS reaches exists", not ghosts, f"ghosts={ghosts}")

# ---- 3. the config round-trips ------------------------------------------------------------------
# snap() is what the UI persists; load_config() drops any key that is not in DEFAULTS, so a field
# the UI sends but DEFAULTS does not know about is silently thrown away on the next launch.
snap_keys = set(re.findall(r"([a-z_]+):\s*(?:Number\(|store\.|\$\(|szMode|afOn|RES)",
                           webui.HTML.split("function snap()")[1].split("}")[0]))
unknown = sorted(k for k in snap_keys if k not in m.DEFAULTS)
check("every key the UI saves survives load_config", not unknown,
      f"saved={len(snap_keys)} not-in-DEFAULTS={unknown}")
check("tightness is persisted", "tightness" in m.DEFAULTS)

# ---- 4. sliders cannot silently save on every pixel of a drag ----------------------------------
# The old UI called api.save() (a JSON write to disk) from oninput on four sliders. At ~60 events a
# second per drag that is a Python thread and a disk write per event, on the GIL the frame thread
# needs. Every oninput must go through the throttle/debounce helpers.
for el in ("z", "ev", "focus", "szx"):
    body = webui.HTML.split(f"$('{el}').oninput=")[1].split(";\n")[0]
    check(f"slider '{el}' does not call api.save directly", "api.save" not in body)
check("saves are debounced", "function saveSoon()" in webui.HTML and "600" in webui.HTML)
check("live sends are throttled", webui.HTML.count("throttle(") >= 4)

# ---- 5. the preview encoder ---------------------------------------------------------------------
if m.cv2 is None:
    print("SKIP  cv2 missing — JPEG preview path not testable here")
else:
    W, H = 1920, 1080
    nv12 = np.vstack([np.full((H, W), 128, np.uint8), np.full((H // 2, W), 128, np.uint8)])
    jpg = m._nv12_preview_jpeg(nv12, W, H)
    check("preview encodes to JPEG", isinstance(jpg, bytes) and jpg[:2] == b"\xff\xd8",
          f"{len(jpg) if jpg else 0} bytes")
    # A 640x360 JPEG must be far smaller than the 324 KB base64 BMP it replaces, or the whole point
    # of moving off the bridge is lost.
    check("preview payload is small", len(jpg) < 120_000, f"{len(jpg)/1024:.0f} KB")
    dec = m.cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
    check("preview decodes at the requested size",
          dec is not None and dec.shape[1] == m.PREVIEW_W and dec.shape[0] % 2 == 0,
          f"shape={None if dec is None else dec.shape}")
    # Odd/awkward frame sizes must not blow up the NV12 repack.
    for w2, h2 in ((1280, 720), (640, 480)):
        f2 = np.vstack([np.full((h2, w2), 128, np.uint8), np.full((h2 // 2, w2), 128, np.uint8)])
        try:
            ok = isinstance(m._nv12_preview_jpeg(f2, w2, h2), bytes)
        except Exception as e:
            ok, w2 = False, f"{w2} ({e})"
        check(f"preview handles {w2}x{h2}", ok)

# ---- 6. preview rate is per format --------------------------------------------------------------
# 30 Hz is affordable for the 4.3 ms JPEG path and NOT for the 8.6 ms numpy path that tkinter uses.
check("jpeg preview targets 30 fps", m.PREVIEW_FPS == 30, f"{m.PREVIEW_FPS}")
# The JPEG gate is frame-COUNT based on purpose: a wall-clock gate loses frames to bursty arrivals
# (MEASURED 21.7 fps against a 30 fps target). Every Nth frame cannot drift.
for src, want in ((30, 1), (60, 2)):
    check(f"{src} fps in -> preview builds every {want} frame(s)",
          max(1, round(src / m.PREVIEW_FPS)) == want)
check("ppm preview stays slow for tkinter", m.PREVIEW_PPM_INTERVAL_S >= 0.1,
      f"{m.PREVIEW_PPM_INTERVAL_S}")

# ---- 7. the MJPEG server binds loopback only ----------------------------------------------------


class _FakePipe:
    _preview_jpg = None
    _mjpeg_stop = __import__("threading").Event()


srv, port = m.start_mjpeg(_FakePipe())
try:
    check("mjpeg server binds 127.0.0.1 only", srv.server_address[0] == "127.0.0.1",
          f"{srv.server_address}")
    check("mjpeg server got a port", port > 0, str(port))
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=3)
        served = False
    except urllib.error.HTTPError as e:
        served = e.code == 404
    except Exception:
        served = False
    check("mjpeg server serves only the preview path", served)
finally:
    _FakePipe._mjpeg_stop.set()
    srv.shutdown()


# ---- 7b. nothing heavy hangs off the Api where pywebview can crawl it ---------------------------
# pywebview builds the JS bridge by recursing into every public attribute of the js_api object. The
# tray (a live WinForms form), the camera host and the auto-start brain hang off Api; crawling the form
# stalled the bridge and the panel booted with "api.options is not a function" (MEASURED 2026-10-04).
# _serializable = False is pywebview's own opt-out.
for cls in (m.VcamHost, m.AutoCam, webui.Tray):
    check(f"{cls.__name__} opts out of pywebview's js_api crawl",
          getattr(cls, "_serializable", True) is False)

# ---- 8. Api.options() must be callable AND JSON-serialisable -----------------------------------
# boot() awaits api.options() before it wires a single handler. If that call raises, or returns
# something pywebview cannot serialise, the promise never settles: boot hangs forever, every control
# stays dead, and NOTHING is logged — the window just sits there looking fine. That is exactly how
# the 2026-08-28 panel shipped broken, so it is a permanent check now.
_ctx = {"host": "127.0.0.1", "port": 8080, "Pipeline": m.Pipeline,
        "RESOLUTIONS": m.RESOLUTIONS, "FPS_OPTS": m.FPS_OPTS, "ROTATIONS": m.ROTATIONS,
        "DECODERS": m.DECODERS, "load_config": m.load_config, "save_config": lambda c: None,
        "adb_forward": lambda p: None, "shutdown_phone": lambda p: None,
        "start_mjpeg": m.start_mjpeg, "log_js_error": lambda s: None}
try:
    _api = webui.Api(_ctx)
    check("Api() constructs", True)
except Exception as e:
    _api = None
    check("Api() constructs", False, repr(e))

if _api is not None:
    import json
    for name, call in (("options", lambda: _api.options()), ("status", lambda: _api.status())):
        try:
            val = call()
            json.dumps(val)
            ok, detail = True, ""
        except Exception as e:
            ok, detail = False, repr(e)
        check(f"Api.{name}() returns JSON-serialisable data", ok, detail)
    # every field boot() reads off options() must actually be present
    o = None
    try:
        o = _api.options()
    except Exception:
        pass
    if o is not None:
        for k in ("resolutions", "fps", "rotations", "decoders", "bitrates", "config"):
            check(f"options() provides '{k}'", k in o and o[k] is not None)
        for k in ("zoom", "tightness", "sensor_zoom", "sensor_zoom_x", "autoframe", "ev",
                  "bitrate", "focus", "pan"):
            check(f"options().config provides '{k}'", k in o.get("config", {}))

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
