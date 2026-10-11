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

# ---- 9. saved face tracking survives boot (regression 2026-10-10) -------------------------------
# The only check here that RUNS the UI's JavaScript. boot() restored a saved autoframe:true, then
# called applyView() just to draw the crop box — and applyView() treated that as a manual touch:
# it switched tracking off, sent set_view (Pipeline.set_zoom disengages auto-framing) and the next
# save wrote autoframe:false. Static checks can't see call ORDER, so this executes the real script
# under node with a fake DOM and a recording fake api. Needs node on PATH, else SKIP.
_BOOT_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const calls = [];
function el(id) {
  const s = new Set();
  return { id, style: {}, dataset: {}, value: '', checked: false, textContent: '', innerHTML: '',
    classList: { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c),
                 toggle: (c, f) => ((f === undefined ? !s.has(c) : f) ? s.add(c) : s.delete(c)) },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeAttribute() {},
    setAttribute() {}, focus() {}, click() {}, querySelectorAll: () => [], querySelector: () => null,
    getBoundingClientRect: () => ({ width: 400, height: 225, left: 0, top: 0 }) };
}
const els = {};
const document = { getElementById: id => els[id] || (els[id] = el(id)), createElement: t => el(t),
  querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, body: el('body') };
const cfg = JSON.parse(process.argv[3]);
const reports = [];
const api = new Proxy({}, { get(t, p) {
  if (p === 'then') return undefined;
  return (...a) => {
    calls.push(String(p));
    if (p === 'report_js_error') reports.push(String(a[0]));
    if (p === 'options') return Promise.resolve({ config: cfg, resolutions: ['1920x1080'],
      fps: ['60', '30'], rotations: ['0', '90', '180', '270'], decoders: ['CPU'], bitrates: [12],
      autostart: false, preview_url: null });
    if (p === 'status') return Promise.resolve({ state: 'idle', fps: 0, frames: 0, dropped: 0,
      msg: '', auto: true, z: cfg.zoom, cx: cfg.pan[0], cy: cfg.pan[1], lost: false, sz_obs: 1 });
    return Promise.resolve(undefined);
  };
} });
const ctx = { document, console, navigator: { userAgent: 'node' },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: () => 0 };
ctx.window = Object.assign(ctx, { pywebview: { api }, addEventListener() {},
  removeEventListener() {}, innerWidth: 1200, innerHeight: 800, devicePixelRatio: 1, location: {} });
vm.createContext(ctx);
vm.runInContext(src, ctx);
(async () => {
  for (let i = 0; i < 50; i++) await new Promise(r => setImmediate(r));
  const boot = calls.slice();
  const st = vm.runInContext('({afOn: afOn, snapAf: snap().autoframe})', ctx);
  vm.runInContext('pvWheel({preventDefault(){}, deltaY: -1})', ctx);
  const after = vm.runInContext('({afOn: afOn})', ctx);
  console.log(JSON.stringify({ reports, boot, afOn: st.afOn, snapAf: st.snapAf,
    wheelAfOn: after.afOn, wheelSetView: calls.slice(boot.length).includes('set_view') }));
})().catch(e => console.log(JSON.stringify({ error: String(e && e.stack || e) })));
"""
import json as _json
import os as _os
import shutil as _shutil
import subprocess as _sp
import tempfile as _tf

_node = _shutil.which("node")
if not _node:
    print("SKIP  saved face tracking survives boot  (node not on PATH)")
else:
    _script = re.findall(r"<script[^>]*>(.*?)</script>", webui.HTML, re.S)[-1]
    _cfg = dict(m.DEFAULTS, autoframe=True, zoom=1.6, pan=[0.4, 0.45])
    with _tf.TemporaryDirectory() as _d:
        _ui, _h = _os.path.join(_d, "ui.js"), _os.path.join(_d, "harness.js")
        open(_ui, "w", encoding="utf-8").write(_script)
        open(_h, "w", encoding="utf-8").write(_BOOT_HARNESS)
        _p = _sp.run([_node, _h, _ui, _json.dumps(_cfg)], capture_output=True, text=True, timeout=60)
    try:
        _r = _json.loads((_p.stdout or "").strip().splitlines()[-1])
    except Exception:
        _r = {"error": (_p.stdout + _p.stderr)[-400:]}
    check("boot runs under the harness", "error" not in _r and "boot ok" in _r.get("reports", []),
          str(_r.get("error") or _r.get("reports")))
    check("boot does not send set_view (a manual-touch hand-back)", "set_view" not in _r.get("boot", []),
          f"boot calls={_r.get('boot')}")
    check("saved face tracking is still ON after boot", _r.get("afOn") is True)
    check("the config boot would save keeps autoframe:true", _r.get("snapAf") is True)
    check("a real scroll on the preview still hands control back",
          _r.get("wheelAfOn") is False and _r.get("wheelSetView") is True)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
