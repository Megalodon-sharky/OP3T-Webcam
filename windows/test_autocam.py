#!/usr/bin/env python3
"""Self-checks for auto-start: the AutoCam timing and the VcamHost camera hand-off.
Run: python windows/test_autocam.py   (exits 1 on failure)

No framework, no phone and no real camera needed: AutoCam is pure logic fed a count and a clock, and
VcamHost runs against a fake pyvirtualcam. The one real-Windows check (counting handles on the OBS
section) only runs when something is producing the virtual camera right now, and is read-only.
"""
import sys
import time

import op3t_webcam as m

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def run(ac, script):
    """Feed AutoCam (t, users, streaming) ticks; collect the actions it returns."""
    return [(t, a) for t, users, streaming in script
            for a in [ac.step(users, t, streaming)] if a]


# ---- 1. AutoCam: timing ----------------------------------------------------------------------------
# A capture opens the section in 0.24 s (MEASURED); enumeration never does. Still, a blip must not wake
# the phone, and a camera flicked off and on inside a call must not bounce it.
ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
acts = run(ac, [(0.0, 1, False), (0.3, 0, False), (0.6, 0, False)])
check("a 0.3 s blip does not start the stream", acts == [], f"{acts}")

ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
acts = run(ac, [(0.0, 1, False), (0.25, 1, False), (0.5, 1, False), (0.75, 1, True)])
check("an app holding the camera 0.5 s starts it, once", acts == [(0.5, "start")], f"{acts}")
check("...and the stream is ours", ac.owns)

acts = run(ac, [(1.0, 0, True), (5.0, 0, True), (10.9, 0, True)])
check("no stop inside the 10 s grace", acts == [], f"{acts}")
acts = run(ac, [(11.0, 0, True)])
check("stop once every app has been gone 10 s", acts == [(11.0, "stop")], f"{acts}")

ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
run(ac, [(0.0, 1, False), (0.5, 1, False)])
acts = run(ac, [(3.0, 0, True), (6.0, 1, True), (20.0, 1, True)])
check("camera flicked off and on mid-call: no stop", acts == [], f"{acts}")

# ---- 2. AutoCam: who owns the stream -----------------------------------------------------------------
ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
ac.user_start()                                       # the Start button, nobody watching
acts = run(ac, [(0.0, 1, True), (1.0, 1, True), (2.0, 0, True), (30.0, 0, True)])
check("a manual Start is never auto-stopped", acts == [], f"{acts}")

ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
run(ac, [(0.0, 1, False), (0.5, 1, False)])           # auto-started
ac.user_stop()                                         # ...then the user pressed Stop mid-call
acts = run(ac, [(1.0, 1, False), (10.0, 1, False), (20.0, 1, False)])
check("a manual Stop holds while the app still has the camera", acts == [], f"{acts}")
acts = run(ac, [(21.0, 0, False), (31.5, 0, False), (40.0, 1, False), (40.6, 1, False)])
check("...and is forgotten once the app lets go", acts == [(40.6, "start")], f"{acts}")

ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
run(ac, [(0.0, 1, False), (0.5, 1, False)])           # started at t=0.5
acts = run(ac, [(2.0, 1, False), (5.4, 1, False), (5.5, 1, False)])
check("a stream that died under the app is retried after 5 s, not before",
      acts == [(5.5, "start")], f"{acts}")

ac = m.AutoCam(on_s=0.5, off_s=10.0, retry_s=5.0)
ac.enabled = False
acts = run(ac, [(0.0, 1, False), (1.0, 1, False), (5.0, 1, False)])
check("disabled: never starts", acts == [], f"{acts}")


# ---- 3. VcamHost against a fake camera ---------------------------------------------------------------
class FakeCam:
    made = []

    def __init__(self, width, height, fps, fmt=None):
        self.spec, self.sent, self.closed, self.device = (width, height, fps), 0, False, "fake"
        FakeCam.made.append(self)

    def send(self, frame):
        assert frame.shape == (self.spec[1] * 3 // 2, self.spec[0]), frame.shape
        self.sent += 1

    def close(self):
        self.closed = True


users = {"n": 0}
real_helpers = (m.pyvirtualcam.Camera, m._section_open, m._section_close, m._own_handles,
                m._section_handles, m.VCAM_IDLE_FPS)
m.pyvirtualcam.Camera = FakeCam
m._section_open = lambda name=m.VCAM_SECTION: 123
m._section_close = lambda h: None
m._own_handles = lambda h: 2
m._section_handles = lambda h: 2 + users["n"]
m.VCAM_IDLE_FPS = 50                                    # keep the test quick
try:
    host = m.VcamHost()
    host.idle(64, 36, 30)
    time.sleep(0.3)
    idle_cam = host.cam
    check("idle: a camera exists before any stream",
          idle_cam is not None and idle_cam.spec == (64, 36, 30))
    check("idle: black frames are flowing", idle_cam is not None and idle_cam.sent > 3,
          f"sent={idle_cam.sent if idle_cam else None}")
    users["n"] = 1
    check("consumers() = handles minus our own", host.consumers() == 1, f"{host.consumers()}")
    users["n"] = 0

    with host.session(64, 36, 30) as cam:
        check("a session borrows the idle camera, no re-create", cam is idle_cam,
              f"cameras made={len(FakeCam.made)}")
        before = cam.sent
        time.sleep(0.2)
        check("idle frames stop while a session sends", cam.sent == before, f"{before}->{cam.sent}")
    time.sleep(0.2)
    check("idle frames resume after the session", idle_cam.sent > before and not idle_cam.closed)

    with host.session(64, 36, 60) as cam2:
        check("a new frame rate re-creates the camera", cam2 is not idle_cam and idle_cam.closed)
    time.sleep(0.2)
    check("...and idling continues at the new rate", host.cam is cam2 and host.spec == (64, 36, 60))

    host.no_idle()
    check("no_idle(): the camera goes away between streams", host.cam is None and cam2.closed)
    with host.session(64, 36, 60) as cam3:
        pass
    check("no_idle(): a session's camera lives only as long as the session",
          host.cam is None and cam3.closed)
    host.close()
finally:
    (m.pyvirtualcam.Camera, m._section_open, m._section_close, m._own_handles,
     m._section_handles, m.VCAM_IDLE_FPS) = real_helpers

# ---- 4. the real handle counter (read-only; only if something is producing right now) ---------------
probe = m._section_open()
if not probe:
    print("SKIP  nothing is producing the OBS virtual camera right now — handle count not checked")
else:
    try:
        total, own = m._section_handles(probe), m._own_handles(probe)
        check("NtQueryObject sees the producer's handle and ours", total >= 2, f"total={total}")
        check("this process holds exactly the probe", own == 1, f"own={own}")
    finally:
        m._section_close(probe)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
