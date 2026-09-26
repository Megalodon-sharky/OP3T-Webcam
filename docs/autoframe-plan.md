# Auto-Framing — implementation plan

Companion to [`autoframe-PRD.md`](autoframe-PRD.md). Ordered. Each step names real files and real
function anchors, and each verification is a measurement or an observable behaviour — never "check it
works".

**Tiers.** Stop after **tier 1** and you have a working feature. Tier 2 is the good version. Tier 3 is
gold-plating.

---

## STATUS — implemented 2026-08-16. 16 of 17 steps done; step 17 deleted by measurement.

Working end to end. `python windows/op3t_webcam.py --headless --autoframe`:

```
streaming: 60.0 fps  1146 frames  10 dropped  z=1.30 pan=(0.474,0.594)
streaming: 60.6 fps  1328 frames  10 dropped  z=1.30 pan=(0.474,0.594)
```

**Measured results**
- **Both gating probes passed.** No anamorphism (step 17 deleted, D1 withdrawn). The ISP does populate
  `STATISTICS_FACES` — `n=1`, score 55–91, `id=-1` — so no PC-side detector is needed and D2 never fired.
- **60 fps survives auto-framing.** `dropped` stopped growing after startup. The `_crop_scale` cost that
  drove the whole risk analysis is gone: cv2 does it in **1.18 ms vs 9.47 ms** at z=1.2 (5–8× at every z).
- **A still subject produces zero crop-box movement** for 20+ s — the dead zone works.
- **`AF_TIGHTNESS` recalibrated 0.31 → 0.22** from 299 on-device samples. 0.31 implied z=2.13, above
  `AF_ZOOM_MAX`, which would have pinned the user at the clamp permanently. (Then **0.22 → 0.294** on
  2026-08-22 when the FOV-crop axis was fixed — see correction 3. Same delivered zoom, new units.)
- **Detector box height has σ = 0.0000** sitting still — the HAL quantises box size, so there is no zoom
  jitter to suppress.
- **The dead zone survives only because of the aspect weighting**: measured cy σ = 0.0207 → 3σ = 0.062,
  which exceeds `AF_PAN_ENTER` = 0.055. The ×0.5625 weighting brings it to 0.035. Remove it and the
  frame twitches vertically on every breath.

**Three corrections the work forced**
1. **Step 5's formula was wrong.** The overlay caught it on the first try. Real mapping: transpose +
   x-mirror, FOV crop, then a rotation in NORMALISED coords. See the correction box in step 5.
2. **The "isotropy fix" was also wrong — but not for the reason recorded at the time.** Rotating in
   square units with a cover-crop put the box on the subject's chest at 2.3× size, and that was read as
   "the frame is not isotropic, stop trying". It is isotropic. The real defect was correction 3.
3. **The FOV crop was on the wrong array axis** (found and fixed 2026-08-22, from "my face is not
   perfectly centered, it's usually to the left"). A 16:9 stream out of a 4:3 active array crops
   array-**Y**; the X extent is transmitted in FULL. Cropping X divided the face's *X* offset by 0.749
   instead of its Y offset, so the tracker aimed the crop **1.335× further from centre than the face
   actually was** — the face landed on the opposite side of centre, worse the further off-centre you sat
   and worse again at high zoom. MEASURED on a decoded frame: box 317×238 out px, aspect 1.33, sitting
   ~60 px left of the face; after the fix 237×317, aspect **0.75 = the sensor rect's 0.748**, on the
   face. End to end the delivered framing error went from ~127 px to the dead zone alone.
   `AF_TIGHTNESS` 0.22 → 0.294 and `AF_FRAC_PER_X` 0.155 → 0.207, purely so the zoom the user gets is
   unchanged. The aspect identity is now asserted in `windows/test_autoframe.py` — it is the check that
   catches this whole class of bug with **nobody in frame**.

**Not verified**: the mapping is visually confirmed at rotation 0 only (the shipped default and your
saved setting). Rotations 90 and 270 (`eff` 0 and 180) transpose in normalised coords, which blows the
mapped box aspect up by 5.6× — almost certainly wrong too, deliberately left alone because nobody runs
them and the right answer needs `GlFlipRenderer`'s cover-crop measured on the device. Tier-3 steps 15
and 16 are implemented but default **off** and have not been exercised on the device.

---

## Step 1 — Settle the two gating unknowns BEFORE writing feature code · tier 1

**Files:** `android/.../CameraStreamer.java`, `windows/op3t_webcam.py`

Two throwaway probes. Both cheap. Both able to kill or redirect the design.

**(a) Face probe.** In `applyImageTuning` (`CameraStreamer.java:311`) add:

```java
b.set(CaptureRequest.STATISTICS_FACE_DETECT_MODE, CaptureRequest.STATISTICS_FACE_DETECT_MODE_SIMPLE);
```

and give the session-start `setRepeatingRequest` at `:287` a temporary `CaptureCallback` whose
`onCaptureCompleted` logs `result.get(CaptureResult.STATISTICS_FACES)` length, the first `Face`'s
`getBounds()`/`getScore()`, and `result.getFrameNumber()`. One build.

**(b) Aspect probe — DONE 2026-08-16, result NEGATIVE.** Captured the same scene straight off the phone
socket at rot=270 (eff=0, no GL rotation) and rot=0 (eff=90, the predicted-stretch case). The rot=0 frame
is upright with **correct proportions** — the `(16/9)² = 3.160` stretch is absent, the assertion at
`GlFlipRenderer.java:167-169` stands, D1 is withdrawn and step 17 below is deleted. Original reasoning
kept for the record:

> With rotation=0, look at the PC preview holding a round object.
`GlFlipRenderer.java:162` sets `glViewport(0,0,1920,1080)` and `:178` applies a **pure normalised**
rotation, so at `eff = (rot + 90) % 360 ∈ {90, 270}` a source-pixel square renders 177.8 × 56.2 output px
— an anisotropy of exactly `(16/9)² = 3.160` (MEASURED in simulation). The in-code assertion at
`:167-169` that this is a distortion-free similarity transform verified singular values *in a space that
is itself 16:9-anisotropic*, so it does not follow. rot=0 is the shipped default (`op3t_webcam.py:105`)
**and** your saved config.

**Verification**
- (a) logcat prints a non-zero face count with a rect inside the active array while a face is in view.
  Count **distinct** rect values over 10 s to get the true update rate in Hz.
- (b) A circle held in front of the camera renders as a circle in the preview at rotation 0. If it renders
  as a ~3.16:1 ellipse, that is a serious pre-existing bug and **decision D1** applies.

```bash
adb logcat -c && adb logcat -s CameraStreamer:I
```

---

## Step 2 — Phone: enable SIMPLE face detection durably, fix all five null callbacks · tier 1

**Files:** `android/.../CameraStreamer.java`

Set `STATISTICS_FACE_DETECT_MODE_SIMPLE` inside `applyImageTuning` (`:311`) — the durable place, because
it is re-invoked from all four request-rebuild sites (`:286`, `:345`, `:362`, `:423`).

Extract `private final CameraCaptureSession.CaptureCallback resultCb` that reads `STATISTICS_FACES`,
`SCALER_CROP_REGION` (**from the RESULT, never the request** — `croppingType` is `CENTER_ONLY` so the HAL
rewrites what was asked for, and `:403-404` already clamps before that) and `SENSOR_TIMESTAMP`. Pass it at
**all five** `setRepeatingRequest` sites: `:287, :346, :363, :406, :424`. Every one currently passes
`null`.

Rate-limit the publish to ~12 Hz with a timestamp compare inside the callback. Do **no socket I/O on
`camHandler`** — that thread services the camera. Write into a single-slot volatile field that a separate
writer thread drains.

**Verification** — `grep setRepeatingRequest(` returns exactly 5 hits and none contains `, null,`. Then,
with logcat open, toggle Denoise, Flash, Exposure and press Refocus in the PC UI: face lines keep flowing
after every one. *Missing any single site produces a feature that works until the user touches a slider
and then dies silently with no error.*

---

## Step 3 — Phone: second socket on tcp:8081, independent of the video path · tier 1

**Files:** `android/.../MainActivity.java`

A second `ServerSocket` on 8081 with its **own** accept thread, mirroring `startServer()` (`:86-104`) but
never touching `streamer` lifetime — the existing accept loop at `:93-97` is single-client sequential and
must not gain a second responsibility. Writer thread drains the 1-deep drop-oldest slot from step 2.

Wire format — ASCII, newline-delimited, phone→PC only:

```
A <arrLeft> <arrTop> <arrW> <arrH> <sensorOrientation>            # once, on connect
F <ptsUs> <cropL> <cropT> <cropW> <cropH> <capW> <capH> <n>       # per update
<l> <t> <w> <h> <score>                                           # × n
```

Send the crop region and capture size on **every** line so the PC parser is stateless. Send **raw
active-array rectangles** — no phone-side geometry at all.

**Verification**

```bash
adb forward tcp:8081 tcp:8081
```

then `python -c "import socket;s=socket.create_connection(('127.0.0.1',8081));print(s.recv(4096))"` prints
the `A` header and `F` lines at roughly 12 Hz. Then: close that socket mid-session and confirm the video
stream's fps and `dropped` are unaffected; and start a session with **nothing** connected to 8081 and
confirm video streams normally.

---

## Step 4 — PC: adb forward 8081, and a FaceLink reader thread anchored in `_send_loop` · tier 1

**Files:** `windows/op3t_webcam.py`

One line in `adb_forward` (`:167-178`, after the existing forward at `:171`):
`_adb("forward", "tcp:8081", "tcp:8081")`, and the matching `_adb("forward", "--remove", "tcp:8081")`
beside `:201` in `shutdown_phone` — or stale forwards accumulate across runs.

Add `Pipeline._face_loop` as a daemon thread **started at the top of `_send_loop`** (`:533-536`, just
before the `with pyvirtualcam.Camera(...)` at `:538`) and **joined in the existing `finally`**
(`:577-584`).

> **This anchor is load-bearing.** `_run` **returns** its recursive CPU-watchdog fallback at `:513`, so
> `:515 self._send_loop(...)` is unreachable on that path and `_send_loop` therefore executes exactly
> **once** per session on every path. A worker started at `:487-488` alongside `_pump`/`_reader` *would*
> be duplicated by that fallback, and unlike those two it has no fd for the fallback to close. `stop()`
> covers it transitively: `:365` sets `_stop` before closing anything, `:379` joins `self.thread` with
> `timeout=2`, and `_send_loop` runs on `self.thread` — so keep the poll interval well under 2 s and add
> **no** second join.

**Verification** — force the CPU watchdog (select Intel QSV so the `DECODE_WATCHDOG_S` path at `:494-513`
fires) and confirm exactly one face-reader thread exists afterwards (`threading.enumerate()`). Then press
Stop: thread count returns to baseline within 2 s.

---

## Step 5 — PC: the four-stage coordinate mapping, with a visual acceptance test · tier 1

**Files:** `windows/op3t_webcam.py`

Module-level pure function `face_to_frame(...)` placed beside `_crop_rect` / `_crop_scale` /
`_nv12_preview_rgb` (`:233-289`), so `latency_probe.py` can import and A/B it the way the decoder flags
were (it already does `from op3t_webcam import ...` at `latency_probe.py:38`).

> **CORRECTION 2026-08-16 — the formula below is WRONG, measured on-device.** The overlay test caught
> it on the first try, which is what it was for. Real face rect `Rect(1825, 1768 - 2429, 2574)` in a
> `4656x3496` array, drawn on the matching frame at rot=270 (eff=0):
>
> - The transform at eff=0 is **not identity**. It is a **transpose + x-mirror**:
>   `out_x = 1 - arr_y/3496`, `out_y = arr_x/4656`. The camera buffer axes are transposed relative to
>   active-array coords (consistent with `sensorOrientation = 90`).
> - ~~The 16:9 FOV crop applies to **arr-X**, the axis that becomes output-vertical — not arr-Y.~~
>   **STRUCK 2026-08-22 — this half of the correction was wrong and the formula below (stage 2, arr-Y)
>   was right all along.** Both put the box on the face when the subject is near frame centre, which is
>   the only case the overlay was checked in, so the overlay could not tell them apart. Off centre it
>   can: cropping arr-X makes the mapping anamorphic by 1.335 in each axis, aims the crop 1.335× too far
>   from centre, and is what put the user's face off to one side. MEASURED 2026-08-22 on a decoded frame:
>   arr-X crop → box 317×238 out px (aspect 1.33, ~60 px left of the face); arr-Y crop → 237×317
>   (aspect **0.75 = the sensor rect's 0.748**), on the face. Aspect is the discriminator, not position.
>
> The transpose + x-mirror half of this correction stands: with it the box lands on the face, without it
> on empty wall. Still to confirm: how `eff` composes on top of that base transform at rotations 90/270.

Four stages, **all mandatory**:

1. Normalise inside the **read-back** crop region.
2. Centre-crop that region to the capture stream's 16:9 aspect. MEASURED: the 4:3 active array 4656×3496
   yields a 4656×2619 effective FOV, so **25.09% of sensor height never reaches the encoder** and a face
   there is real but invisible.
3. Rotation **then** flip, using `[[cos, sin], [-sin, cos]]` in top-left-origin coordinates.
4. Uniform scale to 1920×1080 — a no-op on normalised coords.

Transform two opposite corners and take min/max — `eff` is always a multiple of 90 so the rect stays
axis-aligned, and this yields `fh` in **output-vertical units** directly, which is what the zoom law needs
whether or not the GL stage is anamorphic.

Ship a temporary debug overlay that draws the mapped box on the tkinter preview in `refresh()`
(`:936-946`).

**Verification** — the drawn box sits on the face **at rotation 0** with both flips off. Then repeat at
90, 180, 270 and with each flip combination — **16 cases**.

> rot=0 is the discriminating case. The classic error (copying the shader's `-eff` literal, or writing a
> textbook `[[c,-s],[s,c]]` in image coordinates) is **exactly zero error at rot=90 and rot=270**, because
> `eff` = 180 and `eff` = 0 are self-inverse under negation, and a **full 180° error at rot=0 and
> rot=180**. Testing only at 90 proves nothing.

---

## Step 6 — PC: the AutoFramer control law, inline in `_send_loop` · tier 1

**Files:** `windows/op3t_webcam.py`

One module-level `_spring()` helper plus one `AutoFramer` class beside the frame-math helpers, and three
new `Pipeline` attributes (`autoframe`, `auto_on`, `auto_view`). Full implementation in §Control law
below.

Detector thread calls `af.submit(boxes, now)` (selection, median-of-3, lock); send thread calls
`af.step(now)` every frame (springs, dead zone, clamps, snap).

Rewrite the crop call site at `:567-568` to read **one immutable tuple** — today it does four attribute
loads of three attributes (`self.zoom` twice: once in the ternary test at `:567` and again as the argument
at `:568`), so the fast-path *decision* and the zoom actually *applied* can come from different values.

Cost of the control math itself is **0.7 µs/frame** (MEASURED) — 0.004% of the budget. The real cost is
the crop it commands.

**Verification** — instrument `_send_loop` for 60 s at 60 fps with preview ON; log the per-frame wall-time
distribution and the `dropped` delta. Expect roughly the MEASURED whole-loop figures: median ~10.5 ms, p95
~16.8 ms, ~5% of frames over 16.67 ms at the worst zoom. Then a still-subject test: `dropped` growth and
crop-box position both flat for 10 s. Then a 30-vs-60 fps A/B: settle time from a step displacement must be
indistinguishable, proving `dt` is measured and not assumed.

---

## Step 7 — PC: one toggle per UI, manual override, preview ownership inversion · tier 1

**Files:** `windows/op3t_webcam.py`, `windows/webui.py`

**tkinter has NEGATIVE vertical slack** — it requests 444×895 inside a fixed 440×812 (`:628-629`),
clipping the Start/Preview row and the footer entirely off-screen (MEASURED). So the new control must cost
**zero vertical pixels**: put an "Auto-frame" `Checkbutton` on the existing `frow` (`:714-736`), copying
the Flash checkbox at `:733-736` (its own `command`, not `on_toggle`, since it neither restarts the stream
nor sends a phone verb). It fits after two edits: shorten `:726` from "Denoise (phone)" to "Denoise", and
change padx from `(0,16)` to `(0,10)` at `:722` and `:736` — MEASURED, five checkboxes then total 367 px of
the 380 px available.

webui gets a fourth `.chip` after `webui.py:165` in the existing flex-wrap toggles row, wired near `:225`
with the `tog()` helper at `:246-248`. Unlimited space there, which is why tkinter drives the design.

**Manual override:** `af.disengage(keep_current=True)` as the **first statement** of `on_drag` (`:792`),
`on_wheel` (`:810`), `on_zoom` (`:700`) and `reset_view` (`:805`), plus the webui equivalents at `:228`,
`:230`, `:260-265`.

**Preview inversion:** tkinter `refresh()` (`:935-946`) reads the pipe's applied view instead of
`float(zoom.get())` and the local `pan` list (`:774`) when auto is on — and the `z > 1.05` gate at `:936`
must also accept "auto is on" or the box vanishes whenever the controller settles near 1.0. webui carries
`(z,cx,cy)` on the **250 ms PREVIEW poll** (`webui.py:279`, `:377-379`), **not** the 500 ms status poll —
that was deliberately slowed to 500 ms for GIL reasons documented at `webui.py:234-236` — and `applyView`
(`:257`) becomes `if (api && !afOn) api.set_view(...)`.

**Config:** add `"autoframe": False` to `DEFAULTS` (`:106`), tkinter `snapshot()` (`:836-842`) **and**
webui `snap()` (`webui.py:242-244`) — and fix the pre-existing `focus` omission from `snapshot()` in the
same edit.

**Verification**
1. With auto on and the subject moving, the crop box on **both** previews tracks the subject rather than
   sitting still.
2. Drag the box mid-follow: no jump in the output, and the checkbox/chip clears within one refresh.
3. Tick the toggle, move the Quality slider (forces a save), restart: still ticked.
4. Run 60 s with auto on and confirm the mtime of `%APPDATA%\OP3T Webcam\config.json` is unchanged.
5. With auto on, move a webui slider, close, reopen: the restored pan is your last **manual** framing, not
   your face position.

---

## Step 8 — Calibrate `AF_TIGHTNESS` and `AF_EYE_IN_FACE` against the real detector box · tier 1

**Files:** `windows/op3t_webcam.py`

`AF_TIGHTNESS = 0.31` and `AF_EYE_IN_FACE = 0.35` are calibrated for a box spanning roughly eyebrow to
chin. The Camera2 SIMPLE box convention on this HAL is **unknown** until step 1 runs; a full-head box
(including hair) would need ≈ `0.42` / `0.45`.

Capture 20 mapped boxes with the step-5 debug overlay at a normal seating distance, measure where the eyes
actually fall inside the box, and set both constants with a comment recording the measurement — matching
this file's convention (`DECODE_WATCHDOG_S` at `:91`, `PREVIEW_INTERVAL_S` at `:92-96`) that every tunable
carries its reason and its number. If step 1(b) found the output anamorphic, recalibrate again for the
affected rotations, since `fh` is an output-**vertical** measure.

**Verification** — at a normal seating distance the settled framing puts the top of the head at **12–16%**
of frame height and the eye line at **34–38%**. Measure on a captured output frame with a pixel ruler, not
by eye.

---

## Step 9 — Two-person flap test and dead-zone tuning against real jitter · tier 1

**Files:** `windows/op3t_webcam.py`

`AF_ZOOM_ENTER = 0.14` is sized at ~3σ above an **ESTIMATED** 3–6% detector box-height jitter. Log the
box-height RMS with the subject deliberately still for 60 s and raise `AF_ZOOM_ENTER` to ~3× the measured
σ if it exceeds 5%.

Separately, exercise the lock: two people crossing over in apparent size, one person walking behind the
subject, and a face on a second monitor. Tune `AF_LOCK_STEAL_S` and `AF_LOCK_SIZE_GATE` against those, and
confirm the tie-break is deterministic — *a flip-flopping choice IS the failure mode being prevented*.

**Verification** — still-subject box-height σ logged as a number, and `AF_ZOOM_ENTER` set to at least 3× it.
Two-person crossover: frame stays on the original subject for ≥ `AF_LOCK_STEAL_S` and never oscillates.
This failure mode is invisible in single-person testing, which is exactly why it is a named step.

---

## Step 10 — Optional `cv2.resize` fast path for `_crop_scale` · tier 2

**Files:** `windows/op3t_webcam.py`, `windows/requirements.txt`, `windows/build_exe.py`

Auto-framing makes the crop path run on **every** frame forever, so this is the highest-value latency work
available. MEASURED on the real even-aligned crop rect:

| | z=1.2 | z=1.4 | z=2.0 |
|---|---|---|---|
| numpy take-chain | 8.75 ms | — | 6.05 ms |
| `cv2.resize` INTER_NEAREST | 1.12 ms | — | 1.05 ms |
| `cv2.resize` INTER_LINEAR | — | 1.07 ms | — |

INTER_LINEAR is also **better quality** than today's nearest-neighbour. cv2 genuinely releases the GIL
(×1.02 slowdown on a concurrent main-thread workload, against ×10.11 for a pure-Python control —
MEASURED), so it does not steal from `_send_loop`.

Import must be guarded (`try: import cv2 / except ImportError: cv2 = None`) with the numpy path retained
as fallback, or `from op3t_webcam import ADB, FFMPEG, ...` at `latency_probe.py:38` breaks. Call
`cv2.setNumThreads(2-4)` explicitly — it defaults to 8 on this box (MEASURED) and would contend with
ffmpeg's decode threads, which run at `HIGH_PRIORITY` per the existing lag fix at `:88-90`.

Build: add `opencv-python-headless` to `requirements.txt` **and** the pip line at `build_exe.py:50-51`;
add a `--upx-exclude` for the ~82 MiB `cv2.pyd` (the generated spec sets `upx=True` at line 42, and
`build_exe.py` regenerates the spec each run so the exclusion belongs in the argument list). Also exclude
`opencv_videoio_ffmpeg500_64.dll` — 12.73 MiB compressed of pure waste, since this project shells out to a
bundled `ffmpeg.exe` and never uses `cv2.VideoCapture`.

**Verification** — bit-identity across a **sweep** of non-integer z (1.05–4.0 in 0.01 steps, both Y and UV
planes), not just z=2.0 where agreement is structurally guaranteed (cw=960, w=1920 make the OpenCV
double-precision floor exactly equal the numpy integer floor). Then re-run the step-6 whole-loop timing:
expect p95 to drop off the 16.67 ms budget line. Then confirm `python windows/latency_probe.py` still runs
with cv2 uninstalled, and time a cold start of the rebuilt exe (onefile re-extracts the whole archive to
`%TEMP%` on every launch).

---

## Step 11 — Repurpose the Zoom slider as framing tightness while auto is on · tier 2

**Files:** `windows/op3t_webcam.py`, `windows/webui.py`

Zero new widgets, zero new config keys, zero widget reconfiguration — the slider keeps its `from_=1.0` /
`to=4.0` / `resolution=0.1` (`:705`, `webui.py:144`) so no value clamping is needed on toggle. Map its
value onto `AF_TIGHTNESS` with a module constant (e.g. `AF_FRAC_PER_X = 0.155`, so 2.0× ≈ 0.31), and flip
the label at `:690`/`:700-702` from "Zoom 2.0x" to "Frame 2.0x". **The label change is the only signal the
user gets that the control means something different** — skipping it makes the slider silently mean two
things.

**Verification** — toggle auto on: the label text changes within one refresh. Drag the slider: the settled
face height in the output changes proportionally, measured on a captured frame at two slider positions.

---

## Step 12 — Feedback states on the existing crop box, plus a face box · tier 2

**Files:** `windows/op3t_webcam.py`, `windows/webui.py`

Recolour the **existing** crop box rather than adding a second one — the controller drives the same
zoom/pan the box already visualises.

tkinter: `:943`'s `outline=ACC` becomes a variable — OK green `#a6e3a1` while tracking, WARN yellow
`#f9e2af` when the face is lost past the grace period, ACC blue `#89b4fa` for manual (all three already
exist at `:606-609`). Add a thin dashed face box (`dash=(3,3)`, `width=1`, `outline=SUB`) drawn only while
auto is on — it is the only way to diagnose "why isn't it following me" (bad light, side profile, too
far). Replace the hint string at `:944-946` with state text, and append the auto state to the status line
at `:924`.

webui: `#cbox.auto{border-color:var(--ok)}`, `#cbox.lost{...}`, `#cbox.auto #chandle{display:none}` after
`webui.py:118`, plus a `#fbox` div at `:184`.

All of this is **free** with respect to the frame budget: tkinter drawing runs on the `root.after(100)`
loop (`:949`) and webui's is CSS — a full overlay redraw measures 0.547 ms at 10 Hz (MEASURED), entirely
off the send thread.

**Verification** — cover the lens: box turns yellow after `AF_LOST_HOLD_S` and the status line says so.
Uncover: returns to green within `AF_REACQUIRE_N` detections. Drag the box: turns blue. Confirm with a
profiler that `_send_loop`'s per-frame time is unchanged.

---

## Step 13 — Headless parity · tier 2

**Files:** `windows/op3t_webcam.py`

`run_headless` (`:981-994`) never calls `load_config` and, unlike `run_gui` (`:616-623`) and webui's
`Api.__init__` (`webui.py:306-310`), never seeds `pipe.zoom`/`pipe.pan` — so a config key alone has **no
effect** there. Add `--autoframe` to argparse (`:1010-1020`), set the Pipeline attribute before `start()`,
and extend the status line at `:989-990` to print the effective z/pan so the feature is observable without
a GUI.

**Verification** — `python windows/op3t_webcam.py --headless --autoframe` prints a changing z and pan as
the subject moves; without the flag they stay at 1.0 / [0.5, 0.5].

---

## Step 14 — Pay off the defects this work walks past · tier 2

**Files:** `windows/op3t_webcam.py`, `windows/webui.py`, `README.md`

Five small, independently verified corrections, all in code this feature touches.

- **(a)** `focus` is in `DEFAULTS` (`:108`) but missing from tkinter `snapshot()` (`:836-842`), so every
  tkinter save **deletes it from disk** — confirmed: the real `config.json` has no `focus` key.
- **(b)** `needs_filter` (`:210`) and `build_filter` (`:216`) are **dead** — zero call sites, `_run`
  hardcodes `vf = None` at `:453` and the `if vf:` at `:472` never fires. Worse, `needs_filter`'s
  docstring at `:212` claims *"Flips are PC-side again"*, which is the **opposite** of live behaviour
  (flips are sent to the phone as `XFORM` at `:417`/`:446` and applied in `GlFlipRenderer.java:179`).
  Anyone reasoning about where the flip happens from that docstring gets the face-coordinate mapping
  backwards.
- **(c)** The CPU-watchdog fallback leaks a pipe handle: `:508` terminates `_ff` but never closes `_ffout`
  before the recursive `return self._run(...)` at `:513`, and `:485` then rebinds it.
- **(d)** `README.md:77` says *"Real-time digital zoom (1.0-1.9x, the IMX298's max)"* and the comment at
  `:704` repeats the 1.9 figure — the real value is **4.0** (MEASURED). The code was always right
  (`CameraStreamer.java:266-267` reads it at runtime, `:396` clamps to it); only the prose is wrong.
- **(e)** The comment at `:92` still says "webui every 150 ms"; the actual preview poll is 250 ms
  (`webui.py:279`).

**Verification** — (a) set a manual focus, move a slider in the tkinter GUI, restart: focus survives.
(b) grep confirms zero references before deleting. (c) force the QSV fallback three times and confirm the
handle count does not grow. (d,e) read the corrected text.

---

## Step 15 — One-shot autofocus re-trigger on a large distance change · tier 3

**Files:** `windows/op3t_webcam.py`, `android/.../CameraStreamer.java`

Closes the feature's most likely "it looks wrong" moment. Focus is deliberately parked
(`CameraStreamer.java:282` sets `CONTROL_AF_MODE_AUTO`, `:296-298` fire one trigger then idle;
`README.md:130`: *"Continuous AF hunts and looks jittery; the app does one autofocus then parks the
lens"*). But the headline requested behaviour — lean back, zoom in — produces a **soft** zoomed-in face,
and the softness then degrades detection.

Send the existing `FOCUS` verb (already wired: `MainActivity.handleControl` → `CameraStreamer.refocus()`
at `:419-429`) when `|ln(z_applied) − ln(z_at_last_refocus)| > ~0.35` ln-units (a 42% apparent-size
change) with a minimum 4 s between triggers. Off by default; folded behind the autoframe toggle only once
measured.

**Verification** — lean back 40 cm: image re-sharpens within ~1 s. Then sit perfectly still for 60 s and
confirm **zero** refocus events in logcat — reintroducing AF hunting is the failure this must not cause.

---

## Step 16 — Centred sensor base-zoom as a slow outer loop · tier 3

**Files:** `windows/op3t_webcam.py`, `android/.../CameraStreamer.java`

Recovers essentially all the resolution the PC crop discards. Sensor digital zoom is information-lossless
up to **Z = 4656/1920 = 2.425×** (MEASURED, and identical at 30 and 60 fps, because the encoder always
emits 1920×1080 at `CameraStreamer.java:160` and the sensor region is the same — the 3840-wide capture
stream only marks where the ISP switches from down- to up-sampling at Z = 1.2125, which is a resampling
round trip, not an information loss). At total z=2.0 the PC crop alone delivers **25.00%** of 1080p; a
sensor base-zoom delivers **100%**.

`CENTER_ONLY` does not restrict a **centred** zoom, so send `ZOOM <Zs>` (`MainActivity.java:158-162` →
`CameraStreamer.setZoom`, `:395-408`) as a slow outer loop, re-issued seconds apart and eased, chosen so
the residual PC crop factor stays around 1.3–1.5. **Never per frame:** `pipelineMaxDepth = 8` (MEASURED)
bounds a crop-region change at up to 8 frames = 133 ms @60 / 267 ms @30.

Note the trade-off explicitly in a comment: pan headroom shrinks to `±(1 − 1/Zp)/(2·Zs)`, so at total
z=2.0 with Zs=1.4 the available pan is ±0.107 of frame width instead of ±0.25, and the preview can no
longer show what the sensor cropped away.

**Verification** — capture one output frame at total magnification 2.0 with and without the base-zoom and
compare a resolution target (or measure high-frequency energy). Then confirm no visible hitch when the
base zoom steps — watch for a frame-rate dip or an AE/AF re-converge in the same second.

---

## ~~Step 17 — Fix the GL stage's anamorphism~~ · DELETED

**Probe 1(b) came back negative on 2026-08-16 — there is no anamorphism.** This step does not apply.
Everything below is kept only so the reasoning is on record.

### ~~Step 17 — Fix the GL stage's anamorphism, if step 1(b) confirmed it · tier 3~~

**Files:** `android/.../GlFlipRenderer.java`

Conditional on probe 1(b). You cannot rotate a 16:9 image by 90° into a fixed 16:9 frame without either
distortion or pillarboxing, and `drawFrame()` (`:161-197`) silently chose distortion: `:178` applies a
pure normalised rotation and `:162` draws into a 1920×1080 viewport.

The fix is a **cover-crop** — scale the rotated texcoords by 9/16 on the long axis so the rotated content
fills the frame at correct aspect and the overflow is cropped. That is exactly the treatment
`build_filter` already applies for 90/270 on the (now dead) ffmpeg path at `op3t_webcam.py:221-226`
(*"Pillarboxing it leaves a tiny strip, so instead fill the 16:9 canvas and centre-crop"*).

Correct the false assertion at `:167-169`, and the false claim that `stMatrix` *"already carries the
texture pixel-aspect"* — `SurfaceTexture.getTransformMatrix` returns a crop/flip transform in normalised
texture coordinates, not an aspect correction.

This is a pre-existing bug at the shipped default rotation, worth its own change. Face-coordinate mapping
must be re-verified afterwards because the effective FOV crop changes.

**Verification** — a circle renders as a circle at rotation 0, 90, 180 and 270. Then re-run the step-5
16-case overlay test and re-run step 8's calibration.

---

## Device probes needed

Things that genuinely cannot be settled without running on the phone/PC.

1. **Face detection actually works** (gates the whole architecture). Step 1(a). PASS = a non-zero count
   with a rect inside the active array while a face is in view. Also count **distinct** rect values over
   10 s to get the real update rate in Hz — the HAL may decimate internally, and that number sets whether
   12 Hz on the wire is even achievable.
2. **Cost of SIMPLE mode in capture→socket latency** (ESTIMATED near-zero, never measured). A/B the
   **already-existing** once-per-second log at `CameraStreamer.java:226-229` between an OFF build and a
   SIMPLE build, 60 s each, compare medians. Cross-check the DequeueBuffer latency histogram in
   `adb shell dumpsys media.camera` (currently 97.45% of frames under 5 ms).

   ```bash
   adb logcat -c && adb logcat -s CameraStreamer:I | findstr "capture->socket"
   ```
3. **Is the output anamorphic at rotation 0?** (gates `AF_TIGHTNESS` calibration and voids every PC-side
   detector fallback). Zero-code: set Orientation to 0, turn the preview on, hold a round object in front
   of the lens. A circle must render as a circle. Exact variant if the eye is not trusted: forward 8080,
   open the socket, send `1920x1080@30\n` + `XFORM 0 0 0\n`, pipe the byte stream to
   `windows\tools\ffmpeg.exe -i pipe:0 -frames:v 1 -y probe.png`, measure the object in the PNG.
4. **Face detection in the denoise mode you actually run.** Saved config has `denoise: true`, and
   `CameraStreamer.java:312-314` lets AE fall to ~7 fps for a long exposure. With the SIMPLE build
   installed, repeat the logcat probe with Denoise ON in low light while moving normally, and count the
   fraction of one-second windows with zero faces. Above ~20% → `AF_LOST_HOLD_S` must rise or the UI must
   warn.
5. **Does the detector box include hair?** (sets `AF_TIGHTNESS` and `AF_EYE_IN_FACE`). Step 8.
   Eyebrow-to-chin convention → 0.31 / 0.35; full-head → ~0.42 / ~0.45. Everything else in the control law
   is detector-independent.
6. **Real box-height jitter with the subject still** (sets `AF_ZOOM_ENTER`). Log `fh` for 60 s motionless
   and compute RMS as a percentage. 0.14 assumes 3–6% (ESTIMATED); real σ of 10%+ → raise to ~0.25 or the
   zoom will pump visibly.
7. **The frame budget on this box, end to end.** With auto engaged and settled, run 60 s at 60 fps with
   preview ON, then OFF, then 30 fps, recording `dropped` per second from the status line (`:924` /
   `webui.py:270`). `cam.send` is the one term nobody could measure without taking over the OBS Virtual
   Camera device, so this end-to-end number is the only fully trustworthy one.
8. **Only if probe 1 fails and the PC fallback is taken:** YuNet's real inference latency, which nobody
   has measured. Download `face_detection_yunet_2023mar.onnx` (233 kB, **FP32** — the INT8 variant is
   reported to detect nothing under OpenCV 5.x, `github.com/opencv/opencv/issues/28798`), then time
   `cv2.FaceDetectorYN.detect` on 320×180 and 480×270 grey frames, 200 reps, `cv2.setNumThreads(2)`. Two
   minutes, and it is the only remaining term with an unbounded error bar.
9. **Only if tier-3 step 16 is taken:** does a `SCALER_CROP_REGION` change cause a visible hitch? Send
   `ZOOM 1.4` mid-session and watch for a frame-rate dip or an AE/AF re-converge in the same second.

---

## Control law

Drop-in reference implementation. Real constants, real names, correct fps handling.

```python
import math, time

# =====================================================================================
# AUTO-FRAMING.  Module-level pure helpers + one class, placed beside _crop_rect /
# _crop_scale / _nv12_preview_rgb (op3t_webcam.py:233-289) so latency_probe.py can import
# and A/B them the way the decoder flags were.  NO new dependency.
#
# WHERE THE NUMBERS COME FROM
# ---------------------------
# _crop_scale cost is a MONOTONE DECREASING function of zoom.  MEASURED on a quiet box
# (real shipping function, 1080p NV12, 120 reps, medians): z=1.002 11.22 ms | 1.05 10.80 |
# 1.20 9.94 | 1.40 8.70 | 1.50 7.86 | 2.00 6.13 | 3.00 4.62 | 4.00 3.70.  Structural: stage 1
# (op3t_webcam.py:262) materialises an (h/z x w) intermediate while the output is always a
# fixed 1920x1080.  There is NO cost cliff -- an earlier "forbidden band" claim was measured
# under concurrent load and does not reproduce.
#
# The zoom<=1.001 passthrough at :567 is worth ~10 ms/frame (MEASURED whole-loop: 0.43 ms
# passthrough+preview vs 10.54 ms median at z=1.2+preview).  Whole-loop at 60 fps, worst
# realistic case (z=1.2, preview ON, paced): median 10.54 / p95 16.80 ms, 5.0% of frames over
# the 16.67 ms budget.  60 fps SURVIVES: the reader keeps only the newest frame (:529) and
# :551 counts the rest, so an overrun costs delivered fps and bumps `dropped`, never latency.
#
# Crop origin is quantised to 2 SOURCE pixels at EVERY zoom (:239-240, required for NV12
# chroma pairs), so the output translates in 2*z-pixel jumps -- MEASURED 2.5 px at z=1.25,
# 3.0 at 1.5, 4.0 at 2.0, 6.0 at 3.0.  Slow creeping pan therefore ALWAYS stair-steps.  The
# cure is behavioural -- hold still, move decisively, hold still -- which is why the dead zone
# is generous and the settle times are long.  Anyone who "improves smoothness" by shrinking
# the dead zone or lengthening the settle will make it visibly WORSE.
# =====================================================================================

# ---- target ----------------------------------------------------------------------
AF_TIGHTNESS    = 0.31   # detector box height as a fraction of OUTPUT height.  With hair at
                         # ~1.35x this is a 0.42 head height = broadcast medium close-up.
                         # RE-CALIBRATE once against the real HAL box convention (a full-head
                         # box needs ~0.42) and again if the GL stage turns out anamorphic.
AF_EYE_IN_FACE  = 0.35   # eye line inside the detector box (0 = top)
AF_EYE_LINE_OUT = 0.36   # where that eye line sits in the output.  Together these put the
                         # hair top at 0.36 - 0.70*AF_TIGHTNESS = 0.143 INDEPENDENT of subject
                         # distance (MEASURED across fh = 0.24/0.20/0.16) -- 14.3% headroom.

# ---- limits ----------------------------------------------------------------------
AF_ZOOM_MAX = 2.00   # far below the manual 4.0.  z=2.0 crops 960x540 = 25.00% of 1080p;
                     # z=4.0 crops 480x270 = 6.25% (MEASURED), upscaled by NEAREST NEIGHBOUR
                     # from a frame the phone encoded whole at a fixed bitrate.  A distant
                     # user is framed a little loose rather than upscaled into mush.
AF_ZOOM_MIN = 1.25   # NOT a cost cliff (refuted).  Value-for-money: a sub-1.25x crop is a
AF_ENGAGE_Z = 1.25   # barely visible framing change that costs ~10 ms/frame, where staying at
AF_RELEASE_Z= 1.12   # exactly 1.0 costs 0.  Hysteresis so box-size noise cannot toggle it.
AF_SNAP_Z   = 1.010  # MANDATORY, not an optimisation.  A spring approaches 1.0 asymptotically
                     # and would park the pipeline at z~1.002 = 11.22 ms/frame forever instead
                     # of 0.  Cost of the snap: a 1% scale step, ~5 px at the frame edge.

# ---- smoothing -------------------------------------------------------------------
AF_PAN_SETTLE_S    = 0.55   # f_n = 1.37 Hz: a 5 Hz detection staircase is attenuated 14x,
AF_ZOOM_SETTLE_S   = 1.10   # 10 Hz 54x.  Zoom f_n = 0.685 Hz: 8 Hz box-height noise 137x, so
AF_ENGAGE_SETTLE_S = 0.45   # 4% fh jitter -> 0.03% zoom jitter (invisible).  A first-order
AF_RETURN_SETTLE_S = 2.50   # EMA at alpha=0.3 has HF gain alpha/(2-alpha) = 0.176 -> ~0.7%
                            # breathing, clearly visible.  NEVER share pan and zoom time
                            # constants: z* = T/fh passes box-SIZE noise straight through,
                            # looming is a far stronger nausea trigger than translation, and a
                            # continuously changing z changes which source columns the
                            # nearest-neighbour gather duplicates -> edge boiling that pan
                            # does not produce.
AF_PAN_VMAX     = 0.45   # output-widths/s
AF_ZOOM_VMAX_LN = 0.45   # ln-units/s (= 1.57x/s)

# ---- dead zone -------------------------------------------------------------------
AF_PAN_ENTER  = 0.055  # ~106 output px: above seated sway/typing/breathing, below a lean.
AF_PAN_EXIT   = 0.018  # ~35 px = 8.6 crop-origin quanta at z=2 -- above the MEASURED 2-source-
                       # pixel quantisation floor, so the controller cannot limit-cycle on it.
AF_ZOOM_ENTER = 0.14   # log-space, so it reads directly as a 14% apparent-size change (~9 cm
AF_ZOOM_EXIT  = 0.045  # at a 65 cm working distance).  Sized ~3 sigma above an ESTIMATED 3-6%
                       # box-height jitter -- MEASURE the real sigma and raise if it is >5%.
AF_PAN_REARM_S  = 0.35
AF_ZOOM_REARM_S = 1.50

# ---- loss / subject lock ---------------------------------------------------------
AF_LOST_HOLD_S    = 1.20  # hold framing through a head turn or a blink
AF_REACQUIRE_N    = 3     # stable detections before acting on a re-acquisition
AF_LOCK_STEAL_S   = 1.50  # a rival must win continuously this long to take the lock
AF_LOCK_SIZE_GATE = 2.00  # ignore candidates outside a 2x size band of the locked subject
AF_MIN_SCORE      = 0.60
AF_MAX_DT, AF_DISCONT_DT = 0.25, 0.50

_SETTLE_K = 4.74   # (1 + u)e^-u = 0.05  ->  u = 4.74.   omega = _SETTLE_K / settle_seconds
_ASPECT_Y = 1080.0 / 1920.0   # cy spans 1080 px, cx spans 1920 -- see e_pan below


def _spring(x, v, target, omega, dt, vmax):
    """Exact critically damped step.  x(t) = target + (A + B t) e^-wt, A = x-target,
    B = v + w*A.  Stable for ANY dt -- the send loop DROPS frames (op3t_webcam.py:551), so dt
    is NOT 1/fps even at a fixed fps.  C1-continuous across a target change (velocity carries),
    which is why the lost-face return, the manual handoff and mid-move re-acquisition all need
    no special-case code.

    NOTE: critical damping bars OSCILLATION, not overshoot.  y crosses zero when A and B have
    opposite signs, i.e. when the current velocity is aimed hard at the target -- and the slew
    clamp below force-sets exactly that velocity.  MEASURED simulation: from x=1.0, v=-20,
    omega=8.62 the minimum is -0.228, a 22.8% overshoot.  Small and self-correcting; do NOT
    write "no overshoot by construction" in the docstring, it is not true."""
    a = x - target
    b = v + omega * a
    e = math.exp(-omega * dt)
    xn = target + (a + b * dt) * e
    vn = (b - omega * (a + b * dt)) * e
    step, lim = xn - x, vmax * dt          # slew clamp: bounds a whip-pan on a big target jump
    if   step >  lim: xn, vn = x + lim,  vmax
    elif step < -lim: xn, vn = x - lim, -vmax
    return xn, vn


# --- exact trig for multiples of 90; float sin/cos would leak 1e-17 into the corners ---
_COS = {0: 1.0, 90: 0.0, 180: -1.0, 270: 0.0}
_SIN = {0: 0.0, 90: 1.0, 180:  0.0, 270: -1.0}


def _rot_flip(x, y, eff, flip_h, flip_v):
    """Normalised source point -> normalised output point, TOP-LEFT ORIGIN, y DOWN.

    THE SIGN TRAP -- read this before changing a character.  GlFlipRenderer.java:176-180 builds
    M = T(.5).R(-eff).S(f).T(-.5) and applies it to TEXCOORDS, so a SOURCE point maps FORWARD
    through M^-1 = T(.5).S(f).R(+eff).T(-.5): rotate first, THEN flip.  The "+eff" is only true
    in the y-UP texcoord frame.  Here y points DOWN (numpy row order), and flipping the y axis
    conjugates a rotation into its inverse, so the correct matrix is [[c, s], [-s, c]] --
    numerically the SAME sign as the -eff literal at :178.  Writing a textbook [[c,-s],[s,c]]
    here is a full 180-degree error at rot=0 and rot=180 and EXACTLY ZERO error at rot=90 and
    rot=270 (eff=180 and eff=0 are self-inverse under negation).  rot=0 is the shipped default
    (op3t_webcam.py:105) AND the user's saved config, so rot=0 is the acceptance test and
    rot=90 proves nothing.

    Self-check, sensorOrientation = 90 (MEASURED) so eff = (rot + 90) % 360.  Active-array
    TOP-LEFT lands at:  rot=0 -> BOTTOM-LEFT (0,1) | rot=90 -> BOTTOM-RIGHT (1,1) |
    rot=180 -> TOP-RIGHT (1,0) | rot=270 -> TOP-LEFT (0,0)."""
    dx, dy = x - 0.5, y - 0.5
    c, s = _COS[eff], _SIN[eff]
    rx =  dx * c + dy * s
    ry = -dx * s + dy * c
    if flip_h: rx = -rx
    if flip_v: ry = -ry
    return 0.5 + rx, 0.5 + ry


def face_to_frame(fl, ft, fw_a, fh_a, crop, cap_w, cap_h, eff, flip_h, flip_v):
    """One HAL face rect in ACTIVE-ARRAY pixels -> (cx, cy, fw, fh) in normalised OUTPUT
    coords, top-left origin -- exactly the space Pipeline/_crop_rect consume.

    `crop` is (cl, ct, cw, ch): the crop region READ BACK from the CaptureResult, never the
    requested one.  croppingType is CENTER_ONLY on this HAL (MEASURED, both cameras), so the
    HAL rewrites what was asked for; and CameraStreamer.java:403-404 clamps before that anyway.

    FOUR stages.  Skipping any one is a silent framing bug:
      1. crop region -> normalised
      2. FOV CROP.  The 4:3 active array is CENTRE-CROPPED to the 16:9 capture stream.
         MEASURED: active array 4656x3496 -> effective FOV 4656x2619 = 74.91% of sensor
         height.  25.09% of sensor height NEVER REACHES THE ENCODER.  A face reported in that
         dead band is real but INVISIBLE -- skip this stage and the tracker drifts toward the
         edges chasing faces the user cannot see.  A partially visible face can legitimately
         produce coords outside [0,1]; the CALLER must reject or clamp, not assume validity.
      3. rotation + flip (see _rot_flip)
      4. uniform scale to 1920x1080 -- no effect on normalised coords."""
    cl, ct, cw, ch = crop
    # stage 1
    x0, y0 = (fl - cl) / cw, (ft - ct) / ch
    x1, y1 = (fl + fw_a - cl) / cw, (ft + fh_a - ct) / ch
    # stage 2: centred sub-rect of the crop region matching the capture-stream aspect
    ar_c, ar_s = cw / ch, cap_w / cap_h
    if ar_s > ar_c:                       # stream is wider -> lose HEIGHT (this device)
        keep = ar_c / ar_s                # fraction of crop-region height that survives
        y0, y1 = (y0 - (1 - keep) / 2) / keep, (y1 - (1 - keep) / 2) / keep
    else:                                 # stream is taller -> lose WIDTH
        keep = ar_s / ar_c
        x0, x1 = (x0 - (1 - keep) / 2) / keep, (x1 - (1 - keep) / 2) / keep
    # stage 3: eff is always a multiple of 90, so the rect stays axis-aligned -- transform two
    # opposite corners and take min/max.  This also yields fh directly in OUTPUT-VERTICAL
    # units, which is what the zoom law needs whether or not the GL stage is anamorphic.
    ax, ay = _rot_flip(x0, y0, eff, flip_h, flip_v)
    bx, by = _rot_flip(x1, y1, eff, flip_h, flip_v)
    lo_x, hi_x = min(ax, bx), max(ax, bx)
    lo_y, hi_y = min(ay, by), max(ay, by)
    return (lo_x + hi_x) * 0.5, lo_y, (hi_x - lo_x), (hi_y - lo_y)   # cx, TOP y, fw, fh


class AutoFramer:
    """submit() runs on the FACE-LINK thread at ~12 Hz.  step() runs INLINE in _send_loop every
    frame (MEASURED 0.7 us -- 0.004% of the 16.67 ms budget).  They share exactly one immutable
    tuple, swapped atomically, so _crop_scale can never see a torn (cx, cy) pair.  Do NOT run
    the controller on the detector thread: Pipeline.pan is a mutable list read non-atomically
    at op3t_webcam.py:568, and running step() inline also makes dt naturally equal the real
    send-loop interval."""

    def __init__(self):
        self.enabled = False
        self.z, self.cx, self.cy = 1.0, 0.5, 0.5   # APPLIED state, handed to _crop_scale
        self.lz = 0.0                              # ln(z).  Zoom is smoothed in LOG space so a
        self.vlz = self.vcx = self.vcy = 0.0       # 1.0->1.2 and a 2.0->2.4 move feel equal.
        self.engaged = False
        self.pan_moving = self.zoom_moving = False
        self.t_pan_stop = self.t_zoom_stop = 0.0
        self.t_prev = None
        self.det = None                            # (cx, top_y, fh), atomically swapped
        self.t_last_det = -1e9
        self.hist = []                             # last 3 raw boxes -> median-of-3
        self.lock = None                           # (cx, cy, fh) of the tracked subject
        self.t_rival = 0.0
        self.reacq = 0

    # ---------------- FACE-LINK THREAD, ~12 Hz ----------------
    def submit(self, boxes, now):
        """boxes: [(cx, top_y, fw, fh, score)] already mapped to normalised OUTPUT coords by
        face_to_frame.  Anything whose box is entirely outside [0,1] is in the FOV dead band --
        reported by the HAL, absent from the video -- and must be dropped here."""
        boxes = [b for b in boxes
                 if b[4] >= AF_MIN_SCORE and -0.15 < b[0] < 1.15 and b[1] + b[3] > 0.0
                 and b[1] < 1.0]
        b = self._select(boxes, now)
        if b is None:
            self.reacq = 0
            return                                 # miss: t_last_det goes stale -> lost path
        if now - self.t_last_det > AF_LOST_HOLD_S: # returning from a full loss
            self.reacq += 1
            if self.reacq < AF_REACQUIRE_N:
                return
        self.hist.append((b[0], b[1], b[3]))
        del self.hist[:-3]
        cols = list(zip(*self.hist))               # median-of-3 per component: kills a single
        med = tuple(sorted(c)[len(c) // 2] for c in cols)   # outlier, ~1 sample of lag
        self.det = med                             # single atomic reference swap
        self.t_last_det = now

    def _select(self, boxes, now):
        """The HAL supplies NO face IDs (MEASURED: faceIds absent from availableResultKeys) and
        maxFaceCount is 10, so association is entirely our job.  Pure argmax over box height is
        discontinuous at a size crossover and every switch commands a full-frame whip-pan --
        the artifact that makes auto-framing worse than none."""
        if not boxes:
            return None
        if self.lock is None:                      # fresh start: largest ~= nearest ~= the user
            best = max(boxes, key=lambda b: (b[3], -abs(b[0] - 0.5)))
        else:
            lcx, lcy, lh = self.lock
            d = lambda b: math.hypot(b[0] - lcx, (b[1] + b[3] * .5 - lcy) * _ASPECT_Y)
            gated = [b for b in boxes
                     if 1.0 / AF_LOCK_SIZE_GATE <= b[3] / lh <= AF_LOCK_SIZE_GATE]
            if not gated:
                return None                        # nobody plausible -> treat as a miss
            # nearest to the previous subject; ties (<5% apart) break on frame-centre
            # proximity, then detector index -- DETERMINISTIC, never a coin flip, because a
            # flip-flopping choice IS the failure mode being prevented.
            best = min(gated, key=lambda b: (round(d(b) / 0.05),
                                             abs(b[0] - 0.5), boxes.index(b)))
            big = max(boxes, key=lambda b: b[3])   # anti-flap lock steal
            if big is not best and big[3] > best[3] * 1.25:
                if self.t_rival == 0.0:
                    self.t_rival = now
                elif now - self.t_rival >= AF_LOCK_STEAL_S:
                    best, self.t_rival = big, 0.0
            else:
                self.t_rival = 0.0
        self.lock = (best[0], best[1] + best[3] * 0.5, best[3])
        return best

    # ---------------- SEND THREAD, every frame ----------------
    def step(self, now):
        """Returns (z, cx, cy) to hand straight to _crop_scale."""
        if self.t_prev is None:
            self.t_prev = now
            return self.z, self.cx, self.cy
        raw_dt, self.t_prev = now - self.t_prev, now
        if raw_dt > AF_DISCONT_DT:                 # long stall: hold, do not leap
            self.vlz = self.vcx = self.vcy = 0.0
            return self.z, self.cx, self.cy
        dt = min(max(raw_dt, 1.0 / 240.0), AF_MAX_DT)   # MEASURED, never 1.0/fps

        fresh = self.det is not None and (now - self.t_last_det) <= AF_LOST_HOLD_S
        if fresh:
            fcx, fy, fh = self.det
            z_want = AF_TIGHTNESS / max(fh, 1e-3)  # "face is a fixed fraction of OUTPUT height"
            if   not self.engaged and z_want >= AF_ENGAGE_Z:  self.engaged = True
            elif     self.engaged and z_want <  AF_RELEASE_Z: self.engaged = False
            if not self.engaged:
                z_t, cx_t, cy_t = 1.0, 0.5, 0.5
            else:
                z_t  = min(max(z_want, AF_ZOOM_MIN), AF_ZOOM_MAX)
                cx_t = fcx                          # centred laterally; no thirds offset
                # HEADROOM.  out_y = 0.5 + (full_y - cy) * z, and y grows DOWNWARD, so putting
                # the eyes HIGH in frame means aiming the crop LOWER: cy_t is numerically
                # GREATER than the face centre.  The '+' is load-bearing; '-' puts the eye line
                # at 0.640 instead of 0.360, burying the face in the bottom half (MEASURED).
                # Divide by self.z (APPLIED), NOT z_t (target).  They are deliberately
                # desynchronised (pan 0.55 s vs zoom 1.10 s), and using z_t makes the pan loop
                # chase a target inconsistent with the frame actually being sent -- MEASURED
                # 32 px of vertical crawl at 1080p at the moment of engagement, decaying to
                # zero as the zoom settles.  Two individually correct loops, one coupling bug.
                zc   = max(self.z, 1.0)
                cy_t = fy + AF_EYE_IN_FACE * fh + (0.5 - AF_EYE_LINE_OUT) / zc
                # Clamp the TARGET, not just the state.  If the subject is near a frame edge the
                # ideal centre is unreachable, and comparing the dead zone against an
                # unreachable target latches pan_moving FOREVER -- the controller springs
                # against a clamp and never re-arms.
                half = 0.5 / zc
                cx_t = min(max(cx_t, half), 1.0 - half)
                cy_t = min(max(cy_t, half), 1.0 - half)
            w_pan = _SETTLE_K / AF_PAN_SETTLE_S
        else:                                       # LOST: held exactly LOST_HOLD_S above
            z_t, cx_t, cy_t = 1.0, 0.5, 0.5         # (target recomputes identical, spring at
            self.engaged = False                    #  rest), then eases home over 2.5 s
            w_pan = _SETTLE_K / AF_RETURN_SETTLE_S
            self.pan_moving = self.zoom_moving = True   # the return is not dead-zoned

        crossing = (self.z <= 1.001) != (z_t <= 1.001)
        w_zoom = _SETTLE_K / (AF_ENGAGE_SETTLE_S if crossing else
                              (AF_ZOOM_SETTLE_S if fresh else AF_RETURN_SETTLE_S))

        # ---- dead zone / hysteresis, in OUTPUT units so thresholds scale with zoom.
        # ASPECT WEIGHT: cx spans 1920 px, cy spans 1080.  An unweighted hypot makes the
        # "radial" dead zone a 1.78:1 ellipse in real pixels, so AF_PAN_ENTER = 0.055 would be
        # 105.6 px horizontally but only 59.4 px vertically -- vertical hunting would start at
        # half the displacement of horizontal, twitching on every nod.
        e_pan = math.hypot(cx_t - self.cx, (cy_t - self.cy) * _ASPECT_Y) * self.z
        e_z   = abs(math.log(z_t) - self.lz)        # log => reads as a % of apparent size
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
            self.zoom_moving = True                 # engage/disengage is never dead-zoned

        # ---- integrate.  No velocity extrapolation anywhere: head motion reverses direction
        # constantly and a predictor overshoots on every reversal.  ~150 ms of measurement lag
        # against a 550 ms settle is not perceived as lag -- it reads as an operator reacting.
        if self.pan_moving:
            vmax = AF_PAN_VMAX / max(self.z, 1.0)   # VMAX is output-widths/s; cx is full-frame
            self.cx, self.vcx = _spring(self.cx, self.vcx, cx_t, w_pan, dt, vmax)
            self.cy, self.vcy = _spring(self.cy, self.vcy, cy_t, w_pan, dt, vmax)
        if self.zoom_moving:
            self.lz, self.vlz = _spring(self.lz, self.vlz, math.log(z_t), w_zoom, dt,
                                        AF_ZOOM_VMAX_LN)
            self.z = math.exp(self.lz)

        # ---- restore the free passthrough.  A spring never REACHES 1.0, and parking at
        # z=1.002 costs 11.22 ms/frame (MEASURED) instead of 0.  Snap the last 1%.
        if z_t <= 1.001 and self.z < AF_SNAP_Z:
            self.z, self.lz, self.vlz = 1.0, 0.0, 0.0
            self.cx = self.cy = 0.5
            self.vcx = self.vcy = 0.0
            self.zoom_moving = self.pan_moving = False
        self.z = min(max(self.z, 1.0), AF_ZOOM_MAX)
        half = 0.5 / self.z
        self.cx = min(max(self.cx, half), 1.0 - half)
        self.cy = min(max(self.cy, half), 1.0 - half)
        return self.z, self.cx, self.cy

    def disengage(self, keep_current=True):
        """Manual override.  keep_current leaves the image EXACTLY where it is -- no jump at
        the handoff.  Called as the FIRST statement of on_drag (:792), on_wheel (:810),
        on_zoom (:700) and reset_view (:805), and the webui equivalents.  Rejected
        alternatives: disabling the manual handles reads as a broken app, and mapping a drag to
        'framing tightness' means the drag is undone within 0.55 s and feels like it did not
        take.  Touching the manual control hands control back -- the universal convention for
        auto-exposure, autofocus and cruise control."""
        self.enabled = False
        if not keep_current:
            self.z, self.cx, self.cy, self.lz = 1.0, 0.5, 0.5, 0.0
        self.vlz = self.vcx = self.vcy = 0.0

    def arm(self, z, cx, cy):
        """Re-enable from wherever manual left it, at rest -> eases in, never snaps."""
        self.z, self.cx, self.cy = z, cx, cy
        self.lz, self.vlz = math.log(max(z, 1.0)), 0.0
        self.vcx = self.vcy = 0.0
        self.engaged = z > AF_RELEASE_Z
        self.pan_moving = self.zoom_moving = False
        self.t_prev = None
        self.det, self.lock, self.hist = None, None, []
        self.enabled = True
```

### Call site, replacing `op3t_webcam.py:567-568`

**One** immutable-tuple read. The current code does four attribute loads of three attributes
(`self.zoom` **twice** — once in the ternary test at `:567` and again as the argument at `:568`), so the
fast-path *decision* and the zoom actually *applied* can come from different updates. A single tuple load
closes all four and needs no lock.

```python
af = self.autoframe
if af.enabled:
    view = af.step(time.monotonic())
else:
    p = self.pan
    view = (self.zoom, p[0], p[1])
self.auto_view = view          # immutable; BOTH previews read this to draw the box
z, cx, cy = view
out = full if z <= 1.001 else _crop_scale(full, w, h, z, cx, cy)
cam.send(out)
```
