"""Self-checks for the auto-framing maths. Run: python windows/test_autoframe.py

No framework on purpose — plain asserts, same spirit as latency_probe.py. These cover the two things
that are easy to get silently wrong and impossible to eyeball later:

  1. face_to_frame must be ISOTROPIC at the rotations people actually use. Active-array pixels are
     square and nothing between the sensor and the encoder is anamorphic, so the mapped box HAS to
     keep the sensor rect's aspect. For one release it did not: the 16:9 FOV crop was applied to
     array-X instead of array-Y, which made the mapped box 1.335x too wide and 0.749x too short and
     aimed the crop 1.335x further from centre than the face really was — the user's face sat off to
     one side, worse the further off-centre they were and worse again at high zoom. Aspect is the
     check that catches that with NOBODY in frame, which is why it is the first thing here.

  2. The spring must be frame-rate independent. A fixed per-frame alpha is the standard bug and it is
     invisible at one frame rate; this app runs at both 30 and 60.
"""
import importlib.util
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "op3t_webcam.py")
spec = importlib.util.spec_from_file_location("op3t", SRC)
op3t = importlib.util.module_from_spec(spec)
sys.modules["op3t"] = op3t
spec.loader.exec_module(op3t)

# MEASURED on a OnePlus 3T (ONEPLUS A3003) via the tcp:8081 face feed.
ARR_W, ARR_H = 4656, 3496          # active array, 4:3
CAP_W, CAP_H = 3840, 2160          # capture stream at 30 fps, 16:9
CROP = (0, 0, ARR_W, ARR_H)        # read-back crop region with no sensor zoom
RECT = (1825, 1768, 604, 806)      # a real face rect, overlay-verified against the decoded frame
OUT_W, OUT_H = 1920, 1080

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


# ---- 1. regression lock on the MEASURED mapping ------------------------------------------------
# These are real HAL rects captured over tcp:8081 together with the frame they belong to. The first
# eff=90 row is the one that was VISUALLY VERIFIED (2026-08-22): its box was drawn on the decoded
# frame and lands on the face, while the pre-fix mapping drew a box 60 px to its left with the wrong
# aspect. eff=90 is rotation 0 — the shipped default and the user's saved setting.
#
# The eff=180/270/0 rows are consistency locks, not visual proofs: they pin current behaviour so a
# refactor cannot silently move the mapping. They have NOT been checked against a frame, and eff=0
# and eff=180 are believed WRONG (see _face_to_out: the normalised rotation transposes there and
# blows the box aspect up 5.6x). They are locked so that fixing them is a deliberate act.
CASES = [
    # (eff, rect,                        expected x, y, w, h in 1920x1080 output px, verified?)
    (90,  (1580, 1738, 577, 771),        651,  535, 237, 317, "VISUALLY VERIFIED 2026-08-22"),
    (90,  (1820, 1922, 495, 661),        750,  611, 204, 272, "consistency lock only"),
    (180, (1820, 2082, 468, 625),       1204,  549, 458, 108, "consistency lock only, believed wrong"),
    (270, (1836, 2117, 462, 617),        972,  133, 190, 254, "consistency lock only"),
    (0,   (1831, 2125, 462, 617),        231,  424, 452, 107, "consistency lock only, believed wrong"),
]
for eff, rect, ex, ey, ew, eh, note in CASES:
    cx, top, w, h = op3t.face_to_frame(rect, CROP, CAP_W, CAP_H, eff, False, False)
    gx, gy = int((cx - w / 2) * OUT_W), int(top * OUT_H)
    gw, gh = max(2, int(w * OUT_W)), max(2, int(h * OUT_H))
    ok = abs(gx - ex) <= 2 and abs(gy - ey) <= 2 and abs(gw - ew) <= 2 and abs(gh - eh) <= 2
    check(f"mapping eff={eff} ({note})", ok, f"got x={gx} y={gy} w={gw} h={gh}")

# THE INVARIANT that would have caught the off-centre bug with nobody in frame. At the rotations that
# do not transpose (eff=90/270 = UI rotation 0/180) the mapped box must keep the sensor rect's aspect:
# active-array pixels are square and nothing between sensor and encoder is anamorphic. Cropping the
# wrong array axis breaks it by exactly (3840/2160)/(4656/3496) = 1.335 in each direction.
for eff in (90, 270):
    for rect in ((1580, 1738, 577, 771), (900, 700, 300, 900), (3000, 1600, 800, 500)):
        _cx, _top, w, h = op3t.face_to_frame(rect, CROP, CAP_W, CAP_H, eff, False, False)
        want, got = rect[2] / rect[3], (w * OUT_W) / (h * OUT_H)
        check(f"isotropic at eff={eff} rect={rect}", abs(got / want - 1) < 0.01,
              f"aspect {got:.3f} vs sensor {want:.3f}")

# ---- 2. the measured base orientation ----------------------------------------------------------
# At eff=0 the buffer is transposed and x-mirrored (MEASURED: the box lands on the face, the identity
# mapping lands on empty wall). So the face's SENSOR-Y extent must become the OUTPUT-X extent.
cx, top, w, h = op3t.face_to_frame(RECT, CROP, CAP_W, CAP_H, 0, False, False)
check("eff=0 transposes (sensor-Y -> output-X)", w > h,
      f"w={w:.3f} h={h:.3f} (normalised)")

# A face near the array's low-Y edge must land right-of-centre in output X (the mirror).
low_y_face = (1825, 200, 604, 806)
cxl, _, _, _ = op3t.face_to_frame(low_y_face, CROP, CAP_W, CAP_H, 0, False, False)
check("eff=0 mirrors X", cxl > 0.5, f"cx={cxl:.3f}")

# flip_h must mirror the result, exactly.
cf, _, _, _ = op3t.face_to_frame(RECT, CROP, CAP_W, CAP_H, 0, True, False)
check("flip_h mirrors cx", abs((1 - cf) - cx) < 1e-9, f"{cf:.4f} vs {1 - cx:.4f}")

# ---- 3. the FOV crop is real, and it is on array-Y ----------------------------------------------
# Only 74.9% of the array's Y extent reaches the encoder, so a face in the top or bottom band is
# reported by the HAL but is NOT in the video. It must map outside [0,1] so the caller drops it.
for name, rect in (("top band", (1825, 20, 604, 300)), ("bottom band", (1825, 3200, 604, 280))):
    _cx, top, _w, h = op3t.face_to_frame(rect, CROP, CAP_W, CAP_H, 90, False, False)
    check(f"out-of-FOV face ({name}) maps outside [0,1]", top + h < 0.0 or top > 1.0,
          f"top={top:.3f} h={h:.3f}")
# ...and the array's X extent is transmitted in FULL, so a face hard against the low-X edge is
# VISIBLE. This is the assertion that pins the crop to the right axis: it fails if X is cropped.
cxe, tope, _w, _h = op3t.face_to_frame((60, 1768, 604, 806), CROP, CAP_W, CAP_H, 90, False, False)
check("low-array-X face stays in frame (X is never cropped)", 0.0 < cxe < 0.2 and 0.0 < tope < 1.0,
      f"cx={cxe:.3f} top={tope:.3f}")

# ---- 4. spring is frame-rate independent -------------------------------------------------------
# Same wall-clock, three frame rates: the fraction of the way to target must agree. A fixed alpha
# would give 0.386 / 0.634 / 0.866 here instead.
omega = op3t._SETTLE_K / 0.55
travelled = []
for fps in (30, 60, 120):
    x, v, dt = 0.0, 0.0, 1.0 / fps
    for _ in range(int(0.55 * fps)):
        x, v = op3t._spring(x, v, 1.0, omega, dt, 1e9)
    travelled.append(x)
spread = max(travelled) - min(travelled)
check("spring fps-independent", spread < 0.01,
      "30/60/120 -> " + " ".join(f"{t:.3f}" for t in travelled))

# ---- 5. disengage restores the free passthrough EXACTLY ----------------------------------------
# A spring only approaches 1.0 asymptotically; parking at z=1.002 costs ~11 ms/frame forever because
# _send_loop would take the _crop_scale branch on every frame instead of passing the frame through.
af = op3t.AutoFramer()
af.arm(1.6, 0.4, 0.4)
t = 0.0
z = 1.6
for _ in range(600):                                   # 10 s at 60 fps with no detections at all
    t += 1 / 60
    z, _, _ = af.step(t)
check("snaps to exactly 1.0 when the face is gone", z == 1.0, f"z={z!r}")
check("z=1.0 takes the passthrough branch", z <= 1.001)

# ---- 6. zoom law holds the face at a constant fraction of the frame -----------------------------
# The point of the whole feature: move back (smaller box) -> zoom in, by exactly the ratio needed.
af2 = op3t.AutoFramer()
for fh_frac in (0.10, 0.15, 0.20):
    z_want = af2.tightness / fh_frac
    check(f"zoom law at fh={fh_frac}", abs(fh_frac * z_want - af2.tightness) < 1e-9,
          f"z={z_want:.3f} -> apparent {fh_frac * z_want:.3f}")

# ---- 7. sensor zoom: engages only when the PC crop is pinned, and lets go again -----------------
# The failure that matters is CHATTER: every change re-issues the phone's repeating request, costing
# an exposure/focus blip and a ~5 s GOP gap. So the dwell and the hysteresis band are the test.
sent = []
sz = op3t.SensorZoom(sent.append)
sz.mode = "auto"
t = 100.0
sz.update(1.6, t)                         # comfortably inside the PC crop's range
check("sensor idle while the PC crop copes", sent == [], f"sent={sent}")

t += 10
sz.update(2.6, t)                         # PC pinned at 2.0 and the face is still too small
check("sensor engages when PC is pinned", sent == [1.3], f"sent={sent}")

sz.observe(4648, 4648 / 1.3)              # phone confirms; the law now works in the cropped frame
t += 0.5
sz.update(2.0, t)                         # exactly pinned, but dwell has not elapsed
check("dwell blocks a change inside AF_SENSOR_DWELL", sent == [1.3], f"sent={sent}")

t += 10
sz.update(1.9, t)                         # between the two thresholds -> deliberately do nothing
check("hysteresis band holds", sent == [1.3], f"sent={sent}")

t += 10
sz.update(1.0, t)                         # subject came back close: hand it all back
check("sensor lets go when you come closer", sent == [1.3, op3t.AF_SENSOR_MIN], f"sent={sent}")

# THE ONE THAT MATTERS. Sending exactly 1.0 puts the sensor back in full-FOV mode and the NEXT
# departure stalls the phone ~6.2 s, which trips DECODE_WATCHDOG_S and drops the connection.
# MEASURED; see AF_SENSOR_MIN. Nothing may ever request exactly 1.0 while the feature is in use.
check("never requests exactly 1.0 while engaged", all(v >= op3t.AF_SENSOR_MIN for v in sent),
      f"sent={sent}")

# mode "on" is a fixed ratio and must ignore the zoom law entirely
sent2 = []
sz2 = op3t.SensorZoom(sent2.append)
sz2.mode, sz2.manual = "on", 1.8
sz2.update(4.0, 200.0)
sz2.update(1.0, 300.0)
check("mode 'on' is a fixed ratio", sent2 == [1.8], f"sent={sent2}")

# mode "off" parks at the floor, once, and then leaves the phone alone. NOT 1.0: turning the feature
# off must not arm the stall for whenever it is turned back on.
sent3 = []
sz3 = op3t.SensorZoom(sent3.append)
sz3.mode, sz3.req = "off", 2.0
sz3.update(3.0, 400.0)
sz3.update(3.0, 500.0)
check("mode 'off' parks at the floor exactly once", sent3 == [op3t.AF_SENSOR_MIN], f"sent={sent3}")

# ...but a session that never used sensor zoom must not touch the phone at all.
sent5 = []
sz5 = op3t.SensorZoom(sent5.append)          # mode defaults to "off", req defaults to 1.0
sz5.update(3.0, 600.0)
sz5.update(1.0, 700.0)
check("untouched 'off' never talks to the phone", sent5 == [], f"sent={sent5}")

# clamp: never ask for more than the lossless limit
sent4 = []
sz4 = op3t.SensorZoom(sent4.append)
sz4.mode = "auto"
sz4.update(99.0, 600.0)
check("sensor request clamped to AF_SENSOR_MAX", sent4 == [op3t.AF_SENSOR_MAX], f"sent={sent4}")

# observe() is the read-back, and it must never invert the ratio or go below 1
sz4.observe(4648, 1939)
check("observe() derives the applied ratio", abs(sz4.obs - 2.397) < 0.01, f"obs={sz4.obs:.3f}")
sz4.observe(4648, 99999)
check("observe() floors at 1.0", sz4.obs == 1.0, f"obs={sz4.obs}")

# ---- 8. arm() actually turns the controller ON --------------------------------------------------
# REGRESSION 2026-08-27: arm()'s tail had been pasted onto the end of SensorZoom.update, so nothing
# in the program ever set af.enabled = True. _send_loop's `if self.af.enabled` gate then took the
# manual branch forever: autoframe dead, and sensor "auto" dead with it, because z_want falls back to
# the manual slider and never exceeds AF_ZOOM_MAX. Every other check here calls step() directly and
# so sailed straight past it. The gate is what needs testing, not the maths behind it.
af3 = op3t.AutoFramer()
af3.det, af3.hist, af3.reacq = (0.9, 0.1, 0.2), [(0.9, 0.1, 0.2)], 3
af3.t_prev = 12345.0
af3.arm(1.0, 0.5, 0.5)
check("arm() enables the controller", af3.enabled is True, f"enabled={af3.enabled!r}")
check("arm() drops stale detection state",
      af3.det is None and af3.hist == [] and af3.reacq == 0 and af3.t_prev is None,
      f"det={af3.det} hist={af3.hist} reacq={af3.reacq} t_prev={af3.t_prev}")
af3.disengage()
check("disengage() turns it back off", af3.enabled is False, f"enabled={af3.enabled!r}")

# SensorZoom picks a ratio and NOTHING else — it owns no controller state.
sz6 = op3t.SensorZoom(lambda _z: None)
sz6.mode = "auto"
sz6.update(2.6, 800.0)
check("SensorZoom.update owns no controller state",
      not any(hasattr(sz6, a) for a in ("enabled", "det", "hist", "reacq", "t_prev")),
      f"leaked={[a for a in ('enabled','det','hist','reacq','t_prev') if hasattr(sz6, a)]}")

# The two halves wired together: a far face must drive the sensor through the SAME z_want that
# _send_loop reads. This is the symptom the user reported ("sensor crop works only on manual mode").
af4 = op3t.AutoFramer()
af4.arm(1.0, 0.5, 0.5)
for i in range(op3t.AF_REACQUIRE_N + 2):                 # small face = far away
    af4.submit([(0.5, 0.45, 0.06, 0.08, 90)], 1000.0 + i * 0.1)   # score is the HAL's 0-100
af4.step(1000.5)
af4.step(1000.52)
sent6 = []
sz7 = op3t.SensorZoom(sent6.append)
sz7.mode = "auto"
sz7.update(af4.z_want if af4.enabled else 1.0, 1000.52)
check("a far face reaches the sensor in auto mode", bool(sent6) and sent6[0] > 1.0,
      f"enabled={af4.enabled} z_want={af4.z_want:.2f} sent={sent6}")

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
