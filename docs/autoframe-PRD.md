# Auto-Framing (face-tracking zoom + pan) — PRD

**Status:** design, not yet implemented.
**Date:** 2026-08-16.
**Every number below is labelled MEASURED or ESTIMATED. Nothing is presented as measured that was not.**

---

## 1. Problem

The crop box is fixed once you set it (current saved config: `zoom 1.4`, `pan [0.49, 0.509]`). Lean back
and you shrink in frame. Lean in and you overflow it. Move sideways and you drift toward the edge.

Requested behaviour, verbatim: *"follow's my face zoomed in, if I move back it zooms in, if I move in
front it zooms out, if I move left or right or up or down it follows me"* — i.e. keep the face at a
roughly constant apparent size and a roughly constant position, hands-free.

## 2. Verdict

**Feasible, and cheaper than expected — in exactly one shape.**

The phone's ISP detects faces for free and ships **raw active-array rectangles** over a second socket
(`tcp:8081`). The PC does 100% of the geometry, the control law, and the crop.

Three facts force that shape:

1. **`scaler.croppingType = CENTER_ONLY`** on both cameras (MEASURED, `adb shell dumpsys media.camera`,
   OnePlus 3T). The phone physically **cannot pan its sensor crop** — the HAL relocates any requested
   crop rect to the centre of the active array. The `cx/cy` arithmetic at `CameraStreamer.java:401-404`
   has never done anything on this hardware. "Let the phone frame itself" fails the core of the request.
2. **The rear camera advertises `availableFaceDetectModes = [OFF, SIMPLE]`, `maxFaceCount = 10`,
   `supportedHardwareLevel = 3`** (MEASURED), with a Qualcomm shim reporting
   `max-num-detected-faces-hw: 10`. Detection is very likely ISP-side and free, and it sees the **full
   sensor FOV** — the only way to re-acquire a subject who has walked out of the PC crop.
3. **The frame budget is not the blocker.** The "10.6 ms" figure that drove the pessimism is the stale
   docstring at `op3t_webcam.py:254-255` being recycled as a measurement. Clean re-measure of the real
   function (MEASURED, 1080p NV12, 120 reps, medians):

   | z | 1.002 | 1.05 | 1.20 | 1.40 | 1.50 | 2.00 | 3.00 | 4.00 |
   |---|---|---|---|---|---|---|---|---|
   | ms | 11.22 | 10.80 | 9.94 | 8.70 | 7.86 | 6.13 | 4.62 | 3.70 |

   Cost is **monotone decreasing** in zoom — stage 1 (`:262`) materialises an `(h/z × w)` intermediate
   while the output is always a fixed 1920×1080. There is no cost cliff. Whole-`_send_loop` simulation
   paced at 60 fps with preview ON, worst realistic case (z=1.2): **median 10.54 ms, p95 16.80 ms, 5.0%
   of frames over the 16.67 ms budget** (MEASURED).

   60 fps survives, because the reader keeps only the newest frame (`:529`) and `:551` counts the rest —
   **an overrun costs delivered fps and bumps `dropped`; it never accumulates latency.** The acceptance
   metric is already built into the app.

Tier 1 adds **zero PC dependencies** and **zero bytes** to the 113.49 MiB exe (MEASURED). The Java change
is ~60 lines that never need to change again. Everything tunable — mapping, smoothing, dead zones,
subject lock — lives in Python, iterable in seconds without an APK rebuild. That matters: this project's
own history records *"the exe was 2 weeks stale, so no prior fix was ever tested."*

## 3. Architecture

| Layer | Decision |
|---|---|
| **Detector** | Camera2 HAL, `STATISTICS_FACE_DETECT_MODE_SIMPLE`. Bounds + score only — `faceLandmarks` and `faceIds` are **absent** from `availableResultKeys` (MEASURED), so no landmarks and **no HAL track identity**; subject association is the tracker's job. |
| **Transport** | Second TCP socket, `tcp:8081`, phone-as-server, newline-delimited ASCII, phone→PC only. |
| **Geometry** | 100% PC-side, from raw active-array rects. Every mapping bug is a one-line Python edit. |
| **Control law** | Critically-damped second-order springs on `log(zoom)` and on `(cx, cy)`, integrated **inline in `_send_loop`** with a MEASURED `dt`, behind an aspect-weighted radial dead zone with Schmitt-trigger engagement. |
| **Rendering** | Unchanged — the existing `_crop_scale` at `:244-267`, driven from one immutable `(z, cx, cy)` tuple. Phone still streams the FULL frame, so the preview keeps showing what lies outside the crop box. |

**Why 8081 and not the existing socket:** the phone→PC direction of 8080 is a raw Annex-B byte stream
that `_pump` relays verbatim into ffmpeg's stdin (`:591-595`). Injecting text corrupts the elementary
stream. A second port leaves the video path bit-identical and fails independently.

### Rejected alternatives

| Option | Why rejected |
|---|---|
| Phone detects **and** drives its own `SCALER_CROP_REGION` | **Dead on arrival.** `croppingType = CENTER_ONLY` (MEASURED) — can zoom, can never follow laterally. Also `pipelineMaxDepth = 8` (MEASURED) bounds a crop change at 8 frames = 133 ms @60 / 267 ms @30. |
| Phone crops in the GL texture matrix (`GlFlipRenderer.java:176-181`) | Best image quality (bilinear from a 4K source, `GL_LINEAR` at `:138-139`), zero extra draw calls — but forfeits the preview-outside-the-box UI, moves tuning into the APK, and stacks a second unverified geometry change onto a shader whose aspect behaviour is already in question. **Keep as tier 3.** |
| PC-side **opencv + YuNet** | Kept as the **named fallback**, not the default. Wheel is `opencv_python_headless-5.0.0.93-cp37-abi3-win_amd64.whl`, 41.8 MiB, installs on 3.14.5 (MEASURED). cv2 genuinely releases the GIL — ×1.02 slowdown against an ×10.11 pure-Python control (MEASURED). But: grows the exe to ~144 MiB, needs a 233 kB external model + `--add-data` + a `_MEIPASS` resolver, a module-level `import cv2` breaks `latency_probe.py:38`, inference latency is **entirely unmeasured**, and it only ever sees the transmitted crop. |
| PC-side **Haar cascade** | **It does not exist.** opencv-python-headless 5.0.0.93 — the version pip resolves — has removed `CascadeClassifier` and the `cv2.objdetect` submodule and ships **zero** haarcascade XMLs; `cv2.data.haarcascades` is an empty directory (MEASURED, by importing the actual downloaded wheel on 3.14.5). Reaching it means pinning back to 4.14.0.94 for a detector whose p95 hit 28.8 ms @320×180 and 41.2 ms @480×270 on noisy input (MEASURED). |
| **mediapipe** | Disqualified on weight. `--no-deps` hid the real tree: with deps it resolves to **19 wheels / 102.3 MiB**, largest member `opencv_contrib_python-5.0.0.93` at 51.3 MiB — *larger than the headless opencv it was meant to avoid* — plus matplotlib and pillow (MEASURED). 1.0.1 is also a breaking redesign: no `mediapipe/solutions/`, zero bundled models, a 52.68 MiB ctypes-loaded DLL with unproven PyInstaller behaviour. |
| Pure-numpy skin-tone / frame-difference | Credible tracker, not a detector — and distance-driven zoom **requires** a reliable scale estimate. Skin-blob area is confounded by hands, forearms, warm walls, wooden furniture, and degrades under this sensor's low-light noise. Cheap (0.32 ms subsampled UV mask, MEASURED) but it would decide zoom from a wrong signal. |
| SEI NAL units in the H.264 stream | Best frame association in principle, but needs hand-written emulation-prevention byte insertion (get it wrong → decoder dies) and a stateful NAL scanner in `_pump` (`:586-600`), which today relays 64 KiB chunks straddling NALs. All to solve a sync problem `SENSOR_TIMESTAMP` already solves free — same `CLOCK_MONOTONIC` value as the `presentationTimeUs` at `CameraStreamer.java:227`. |
| `adb logcat` scraping | Right for **probe 1**, wrong for production: lossy under load, unbounded latency, extra long-lived adb subprocess, and `shutdown_phone` kills the adb server at `:205`. |

## 4. Goals / non-goals

**Goals**
- Stable apparent face size and broadcast-correct position in the 1920×1080 vcam output, hands-free.
- Follow laterally and vertically without looking seasick, hunting, or breathing.
- **Cost nothing when OFF** — the `zoom <= 1.001` passthrough at `:567` stays byte-for-byte the current
  fast path (MEASURED 0.43 ms/frame whole-loop with preview on, vs 10.54 ms at z=1.2).
- Tier 1: zero new PC deps, zero exe growth.
- User stays in charge: any manual drag/scroll/reset instantly takes over, with no jump.
- Every tuning surface in Python, so it can be iterated without an APK rebuild.

**Non-goals**
- Multi-person conference framing. One subject, one lock, with an anti-flap rule.
- Body/pose tracking, gestures, background blur, any per-pixel effect.
- Face recognition/identity (the HAL offers no `faceIds` anyway — MEASURED).
- Panning via the phone's `SCALER_CROP_REGION` — impossible on this HAL.
- Minimising framing latency. 150–250 ms (ESTIMATED) reads as a human operator; the failure mode here is
  **too little** smoothing, not too much.
- Fixing the tkinter window's pre-existing **−83 px overflow** (MEASURED: requests 444×895 inside a fixed
  440×812, clipping the Start/Preview row and footer off-screen). Must not get worse; fixing it is
  separate work.

## 5. User stories

- Tick one checkbox → the camera follows my face. Untick → framing freezes exactly where it is, no jump.
- Lean back from the desk → the view zooms in over about a second, head stays the same size on the call.
- Shift to one side → the view pans to follow, then **stops and holds still** rather than creeping.
- Lean forward past normal → zooms back out, and if close enough returns to the full uncropped frame.
- Turn away, look down, or briefly leave → framing **holds**; gone more than a second or so → eases home
  over a couple of seconds.
- Grab the preview crop box and drag → auto-framing switches itself off and the checkbox unticks, so I
  can see I took over.
- The crop box drawn on the preview always shows what the vcam is actually sending — it must never sit
  still while the output pans.

## 6. Requirements

### P0 — the feature does not work without these

| ID | Requirement | Acceptance |
|---|---|---|
| **P0-1** | Phone enables `STATISTICS_FACE_DETECT_MODE_SIMPLE` in `applyImageTuning` (`CameraStreamer.java:311`) so it survives every request rebuild (`:286`, `:345`, `:362`, `:423`). | logcat prints a non-zero face count with a plausible active-array rect, and **keeps** printing after toggling Denoise, Flash, Exposure and Refocus in the PC UI. |
| **P0-2** | **All five** `setRepeatingRequest` sites (`:287, :346, :363, :406, :424`) pass one shared `CaptureCallback`. Today every one passes `null`. | `grep setRepeatingRequest(` returns 5 hits, **zero** containing `, null,`. |
| **P0-3** | Face data on `tcp:8081`, own accept thread, 1-deep drop-oldest slot, own writer thread. Never blocks `camHandler`, never touches 8080. | Kill the PC face reader mid-session → video fps unchanged, zero new `dropped`. Never open 8081 at all → phone still streams normally. |
| **P0-4** | Wire format carries **raw active-array rects** + the **read-back** crop region + capture-stream size, per update. All transforms on the PC. | A recorded 30 s capture of 8081 replays offline through the PC mapping and reproduces identical `(cx, cy, fw, fh)` — the mapping is a pure function of wire data + rot/flip, no phone-side state. |
| **P0-5** | PC maps active-array rects through **all four stages**: read-back crop region → centre 16:9 FOV crop of the 4:3 active array → rotation **then** flip → uniform scale. Rotation matrix is `[[cos, sin], [-sin, cos]]` in top-left-origin image coords. | Debug overlay: at **rotation 0** the drawn box lands on the face. This is the discriminating test — see §8 risk 1. |
| **P0-6** | The 16:9 FOV crop stage is **mandatory**. MEASURED: active array 4656×3496 (4:3), capture stream 16:9 → effective FOV 4656×2619 = **74.91%** of sensor height. **25.09% of reported face positions never reach the encoder.** | A face held outside the transmitted frame produces a mapped `cy` outside `[0,1]` and is **rejected**, not panned toward. |
| **P0-7** | Control law runs inline in `_send_loop` with `dt` measured by `time.monotonic()` each iteration and clamped, using exact exponential springs. Never a fixed alpha, never `alpha = dt/tau`. | 30 vs 60 fps settle times indistinguishable. A fixed `alpha=0.03` spans 0.386 / 0.634 / 0.866 of the way to target after the same 0.55 s at 30/60/120 fps (MEASURED simulation); the exponential form gives 0.62 / 0.63 / 0.63. |
| **P0-8** | Detector thread publishes ONE immutable tuple by atomic reference swap; `_send_loop` reads the applied view as ONE tuple. Current `:567-568` does **four** attribute loads of three attributes (`self.zoom` twice — once in the ternary test, once as the argument). | Code review: the crop call site contains exactly one attribute read for the view. No lock used, none needed. |
| **P0-9** | Disengagement **snaps** zoom to exactly 1.0 once within `AF_SNAP_Z`, restoring the free passthrough. | Turn auto off with the subject close → `dropped` stops growing, per-frame cost returns to 0.43 ms. Without the snap the spring parks at z≈1.002 = **11.22 ms/frame forever** (MEASURED). |
| **P0-10** | Auto-framing must **not** route through `send_zoom()` (`:693-698`) or the zoom `Scale`. That path calls `save_now()` → synchronous JSON write to `%APPDATA%`, contains a hidden `if z <= 1.05: pan = 0.5, 0.5` recentring rule at `:695-696`, and the Scale's `command=on_zoom` (`:705`) re-enters it and rounds z to `resolution=0.1`. | Run 60 s with auto on → `config.json` mtime unchanged. |
| **P0-11** | `webui.py` `Api.save` must not inject the live pan while auto is on. Today `webui.py:324` does `cfg["pan"] = list(self.pipe.pan)` unconditionally, and `Api.save` is reached from every slider handler. | Move a slider with auto on, close, reopen → saved pan is the last **manual** framing, not wherever the face was. |
| **P0-12** | New `autoframe` key added in **three** places: `DEFAULTS` (`:104-109`), tkinter `snapshot()` (`:836-842`), webui `snap()` (`webui.py:242-244`). | Tick the box, move a slider, restart → still ticked. **This trap is already live**: `focus` is in `DEFAULTS` at `:108` but missing from `snapshot()`, and the real `config.json` on disk has no `focus` key. |
| **P0-13** | Manual input (drag, scroll, zoom slider, double-click reset) disengages immediately, **keeps** current framing with no jump, unticks the toggle. | Drag mid-follow → image does not jump, box follows cursor, toggle clears within one UI refresh (100 ms tk / 250 ms web). |
| **P0-14** | Both previews draw the crop box from the Pipeline's **applied view**, not UI-local state. tkinter `refresh()` reads `float(zoom.get())` at `:935` and the local `pan` list from `:774`; webui reads its own JS `view` at `:250-254`. | With auto on and the subject moving, the box tracks the subject. Today it would sit motionless while the output pans — the preview would actively lie. |
| **P0-15** | webui `applyView` must not echo the controller back down: `if (api && !afOn) api.set_view(...)` at `webui.py:257`. | No visible 250–500 ms oscillation of the crop box while engaged. |

### P1 — the feature is unpleasant without these

| ID | Requirement | Acceptance |
|---|---|---|
| **P1-1** | Subject lock with size gate + anti-flap steal timer + deterministic tie-break. No `faceIds` (MEASURED) with `maxFaceCount 10` → association is entirely ours. | Two similar-size people cross over: framing stays locked for ≥ `AF_LOCK_STEAL_S`, never oscillates. |
| **P1-2** | Face loss **holds** for `AF_LOST_HOLD_S`, then eases home over `AF_RETURN_SETTLE_S`. Never a snap. | Turn side-on 0.8 s → framing does not move at all. Leave 5 s → eases to full-frame over ~2.5 s, no visible step. |
| **P1-3** | Dead zone radial in **output pixels**, not raw normalised units. `cx` spans 1920 px, `cy` spans 1080 — an unweighted hypot makes it a 1.78:1 ellipse and vertical hunting starts at half the horizontal displacement. | Move 100 output px laterally and 100 px vertically from rest: both trigger a follow, or neither does. |
| **P1-4** | Headroom target computed from the **applied** zoom (`self.z`), not the target (`z_t`). They are deliberately desynchronised (pan 0.55 s, zoom 1.10 s). | Still subject, engagement from z=1.0 to z≈1.55: the eye line does not crawl vertically. Using `z_t` puts it 32 px low at 1080p at engagement, decaying to zero — a visible crawl through every zoom move (MEASURED simulation). |
| **P1-5** | Headless parity: `--autoframe` on `run_headless` (`:981-994`, argparse `:1010-1020`), effective `(z, cx, cy)` in the status line at `:989-990`. `run_headless` never calls `load_config`, so a config key alone does nothing there. | `--headless --autoframe` prints a changing z/pan as the subject moves. |

### P2 — worth doing, not required

| ID | Requirement | Acceptance |
|---|---|---|
| **P2-1** | Optional `cv2.resize` fast path for `_crop_scale` behind a guarded import, numpy retained as fallback. | Bit-identity across a **sweep** of non-integer z (1.05–4.0 step 0.01, both planes) — not only z=2.0, where agreement is structurally guaranteed (cw=960, w=1920 → the OpenCV floor and the numpy integer floor cannot disagree). And `latency_probe.py:38` still imports with cv2 absent. |
| **P2-2** | One-shot autofocus re-trigger on a large hysteretic distance change, reusing `CameraStreamer.refocus()` (`:419-429`). | Lean back 40 cm → re-sharpens within ~1 s, and does **not** hunt while sitting still. Partially reverses a documented decision (`README.md:130`), so gate it hard and default it off. |
| **P2-3** | Centred sensor base-zoom as a slow outer loop, keeping the PC crop factor around 1.3–1.5. | At total magnification 2.0 the image is measurably sharper. Sensor zoom is information-lossless to **Z = 4656/1920 = 2.425×** (MEASURED, identical at 30 and 60 fps because the encoder always emits 1920×1080), whereas a PC crop at z=2.0 leaves only 960×540 = **25.00%** of 1080p. |

## 7. Settings and constants

One persisted config key. Everything else is a module constant beside `PREVIEW_INTERVAL_S` (`:96`),
matching this file's convention that every tunable carries its measurement and its reason.

| Key | Default | Meaning |
|---|---|---|
| `autoframe` (config) | `False` | Master on/off. **Must** be added to `DEFAULTS`, tkinter `snapshot()` **and** webui `snap()` or it is silently erased on every save — the exact live bug that destroys `focus` today. |
| `AF_TIGHTNESS` | `0.31` | Detector box height as a fraction of **output** height. With hair at ~1.35× that is a 0.42 head height = broadcast medium close-up. **Re-calibrate** once against the real HAL box convention (a full-head box needs ~0.42). |
| `AF_EYE_IN_FACE` / `AF_EYE_LINE_OUT` | `0.35` / `0.36` | Eye line inside the box, and where it sits in the output. Together they put the hair top at `0.36 − 0.70×AF_TIGHTNESS = 0.143` of output height **independent of subject distance** (MEASURED across fh = 0.24/0.20/0.16) — 14.3% headroom, matching broadcast practice. |
| `AF_ZOOM_MAX` | `2.00` | Far below the manual 4.0. z=2.0 crops 960×540 = 25.00% of 1080p; z=4.0 crops 480×270 = **6.25%** (MEASURED), nearest-neighbour upscaled from a frame the phone encoded whole at fixed bitrate. A distant user is framed a little loose rather than upscaled into mush. Manual zoom keeps its 4.0 range. |
| `AF_ENGAGE_Z` / `AF_RELEASE_Z` / `AF_ZOOM_MIN` | `1.25` / `1.12` / `1.25` | Schmitt trigger for entering/leaving the cropped path. **Not** a cost cliff — an earlier "forbidden band" claim was refuted; the cost curve is smooth and monotone. Rationale is value-for-money: a sub-1.25× crop is a barely visible framing change that costs ~10 ms/frame, where staying at exactly 1.0 costs 0. |
| `AF_SNAP_Z` | `1.010` | **Mandatory, not an optimisation.** A spring approaches 1.0 asymptotically and would park the pipeline at z≈1.002 = 11.22 ms/frame (MEASURED) instead of 0. Cost: a 1% scale step, ~5 px at the frame edge. |
| `AF_PAN_SETTLE_S` / `AF_ZOOM_SETTLE_S` | `0.55` / `1.10` | 2:1, **never shared**. `z* = TIGHTNESS/fh` passes box-**size** noise straight through (size is far noisier than centre); looming is a much stronger nausea trigger than translation; and a continuously changing z changes which source columns the nearest-neighbour gather duplicates → edge boiling that pan does not produce. |
| `AF_PAN_ENTER` / `AF_PAN_EXIT` | `0.055` / `0.018` | Radial dead zone in output units, **aspect-weighted** (multiply the cy error by 1080/1920 = 0.5625 before the hypot). 0.055 ≈ 106 output px — above seated sway, typing and breathing; below a deliberate lean. The exit threshold is 8.6 crop-origin quanta at z=2, above the MEASURED 2-source-pixel quantisation floor, so it cannot limit-cycle. |
| `AF_ZOOM_ENTER` / `AF_ZOOM_EXIT` | `0.14` / `0.045` | Log-space, so they read directly as a 14% / 4.5% apparent-size change (~9 cm at a 65 cm working distance). Sized ~3σ above an **ESTIMATED** 3–6% box-height jitter — if the real jitter is 10%+ this must rise to ~0.25 or the zoom will pump. |
| `AF_LOST_HOLD_S` / `AF_RETURN_SETTLE_S` / `AF_REACQUIRE_N` | `1.20` / `2.50` / `3` | Hold through a head turn or a blink; ease home only after a real absence; require three stable detections before acting on a re-acquisition. |
| `AF_LOCK_STEAL_S` / `AF_LOCK_SIZE_GATE` | `1.50` / `2.00` | A rival face must be continuously larger for 1.5 s before stealing the lock; candidates outside a 2× size band are ignored. Pure argmax over box height is discontinuous at the crossover and every switch commands a full-frame whip-pan. |

## 8. Risks

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| 1 | **The rotation sign.** The mapping is invisibly correct at rot=90 and rot=270 (`eff`=180 and 0 are self-inverse under negation) and a **full 180° error at rot=0 and rot=180**. rot=0 is the shipped default (`:105`) **and** the user's saved config. The most likely test plan — test at 90, it looks fine — cannot detect it. | **high** | The step-5 debug overlay is a mandatory gate and its acceptance case is **rot=0 specifically**, with all 16 rotation × flip combinations checked. |
| 2 | ~~**The GL stage may already be anamorphic at rot=0.**~~ **REFUTED 2026-08-16 by measurement.** Captured the same scene at rot=270 (eff=0, no GL rotation) and rot=0 (eff=90, the predicted-stretch case) straight off the phone socket. The rot=0 frame is upright with **correct proportions** — head, glasses and circular headphone cups all undistorted. The predicted `(16/9)² = 3.160` stretch is absent. The in-code assertion at `GlFlipRenderer.java:167-169` stands. | ~~high~~ **none** | No action. D1 withdrawn, tier-3 step 17 deleted, and the PC-side detector fallback (D2) is **not** void after all. |
| 3 | **Face results die silently after the first slider touch** — all five `setRepeatingRequest` sites pass `null` today. Fix only the session-start one and the feature works in testing, then stops forever with no log line the moment the user changes Denoise/Exposure/Flash/Focus. | **high** | One shared `resultCb` at all five sites, verified by grep **plus** a behavioural test toggling all four controls with logcat open. Highest-probability latent defect in the feature. |
| 4 | **The ISP may not actually populate `STATISTICS_FACES`.** All that is established is that SIMPLE mode is *advertised*. Whether the Qualcomm legacy HAL fills it usefully, at what rate, and how well in the denoise mode the user actually runs, is unmeasured — and each iteration costs an APK rebuild. | **high** | Probe 1(a) falsifies it in one build and five minutes, before any plumbing. If it fails → decision D2: swap a PC-side YuNet detector behind the same boundary. Mapping, control law, UI, config and crop are all unchanged. |
| 5 | **Auto-framing permanently retires the zero-cost passthrough.** 0.43 ms/frame today → median 10.54 / p95 16.80 ms at z=1.2, 5.0% of frames over budget (MEASURED). A real, permanent regression on the thread that owes the vcam a frame. | medium | Drop-newest (`:529`, `:551`) converts overrun into fps and a rising `dropped`, never latency. Ships behind a toggle that is OFF by default and fully restores z=1.0. Tier-2 step 10's `cv2.resize` refunds ~7 ms/frame. |
| 6 | **The previews will silently lie** — both draw the box from their own stale copy, so with auto on the box sits motionless while the output pans. Appears for free unless ownership is inverted. | medium | P0-14/P0-15. Do **not** raise the webui *status* poll rate — it was deliberately slowed to 500 ms for GIL reasons at `webui.py:234-236`; carry the view on the 250 ms *preview* poll instead. |
| 7 | **Config corruption in both directions.** A `DEFAULTS` key missing from either writer is erased on the next save (proven live with `focus`). And `webui.py:324` injects the live pan on **every** save → the app restarts framed wherever the face happened to be. | medium | Add `autoframe` to all three places in one commit, fix the `focus` omission in the same edit, skip the pan injection while auto is on. |
| 8 | **A worker anchored in the wrong place leaks on every CPU-watchdog fallback.** `_run` **returns** its recursive fallback at `:513`, so a thread started at `:487-488` is duplicated — and unlike `_pump`/`_reader` it has no fd for `:508`/`:510` to close. Surfaces only as mysterious CPU and two controllers fighting over the framing. | medium | Anchor at the top of `_send_loop` (`:533-536`), join in the existing `finally` (`:577-584`). `_send_loop` runs exactly once per session on every path; `stop()`'s `join(timeout=2)` at `:379` covers it transitively. |
| 9 | **Multi-face flapping.** No `faceIds`, `maxFaceCount 10` → naive "largest face" whip-pans between people at the size crossover. Invisible in single-person testing. | medium | Lock-with-gate + 1.5 s steal timer + fully deterministic tie-break. Step 9 makes the two-person crossover a named, gated test. |
| 10 | **Crop origin is quantised to 2 source pixels at every zoom** (`:239-240`, required for NV12 chroma pairing) → the output translates in `2z`-pixel jumps (MEASURED 4.0 px at z=2.0, 6.0 at z=3.0). Slow creeping pan **always** stair-steps; no amount of smoothing removes it. | medium | Generous dead zone and long settle times — hold still, move decisively, hold still. Document the quantum next to the constants, because a future tuner trying to make it "smoother" by shrinking the dead zone would make it strictly worse. |
| 11 | **Focus is deliberately parked, which fights the headline behaviour.** `CameraStreamer.java:282` + `:296-298` fire one autofocus then idle (`README.md:130`: *"Continuous AF hunts and looks jittery"*). So the exact motion that triggers auto-zoom-in — leaning back — produces a **soft** zoomed-in face, and the softness then degrades detection. There is no third option. | medium | Tier-3 step 15: one-shot refocus on a large hysteretic distance change, 4 s minimum interval, off by default. Decision D4 puts the trade in front of the user rather than picking silently. |
| 12 | **Current denoise setting conflicts with tracking.** Saved config has `denoise: true`, and `CameraStreamer.java:312-314` lets AE drop to ~7 fps for a long exposure — motion-blurring a moving face exactly when the tracker needs it, in the low light where denoise is most wanted. | low | Surface it in the status line; do not silently override a deliberate user choice. The dead zone plus `AF_LOST_HOLD_S` already tolerate intermittent dropouts. |
| 13 | **The tkinter GUI is already 83 px over its fixed window** (MEASURED). Any new row makes a live bug worse. | low | The new control costs **zero** vertical pixels — a fifth Checkbutton on the existing `frow`, fitting with 13 px to spare after shortening "Denoise (phone)" → "Denoise" and tightening padx 16 → 10 (MEASURED). |

## 9. Success criteria

- Subject moving normally at a desk: face stays within **±10%** of its target output height and within the
  centre third of the frame, hands-free.
- 60 fps, preview ON, controller settled at z≈1.4: `dropped` grows at no more than **~3/s** (the MEASURED
  whole-loop p95 at the worse z=1.2 case is 16.80 ms against a 16.67 ms budget, 5.0% over).
- Auto OFF: per-frame cost byte-identical to today — the `zoom <= 1.001` branch is taken and `dropped`
  does not grow.
- A still subject produces **zero** crop-box movement for at least 10 s. No dithering, no breathing.
- Toggling on/off, and grabbing the box mid-follow, never produces a visible jump.
- The debug overlay's face box sits on the face at rotation 0 **and** 90 **and** 180 **and** 270, with
  each flip combination.
- No new file in `windows/`, no new entry in `requirements.txt`, exe within 1 MiB of 113.49 MiB — tier 1.

## 10. Decisions that need your call

**D1 — ~~If the aspect probe shows the output IS stretched at rotation 0…~~ WITHDRAWN.**
Settled 2026-08-16 by measurement: the output is **not** stretched. Captured the same scene at rot=270
(eff=0) and rot=0 (eff=90); the rot=0 frame is upright and correctly proportioned. Nothing to decide,
nothing to fix, and `AF_TIGHTNESS` needs no distortion allowance.

**D2 — If the phone's ISP face detection doesn't work, add OpenCV to the PC (≈ +30 MiB on a 113.49 MiB
exe, MEASURED) or drop the feature?**
Options: (a) opencv-python-headless + YuNet (exe ~144 MiB); (b) onnxruntime only (~+14 MiB marginal) and
hand-write the anchor decode and NMS that `cv2.FaceDetectorYN` gives free; (c) no dependency, abandon.
**Recommend (a).** It is the only option whose latency cost is **negative**: `cv2.resize` replaces the
numpy take-chain at 1.12 ms vs 8.75 ms at z=1.2 (MEASURED), refunding more per frame than the detector
consumes and taking the whole-loop p95 off the budget line. That refund is a real justification for a
dependency in a codebase that demands one. Void if the anamorphism probe comes back positive.

**D3 — Should the auto-frame toggle persist across restarts?**
Options: (a) persist, matching every other live control; (b) never persist, like Flash, deliberately not
persisted at `:669` ("torch — never persisted (no surprise relight)").
**Recommend (a).** No safety or surprise dimension: auto-framing always starts at z=1.0 and only engages
once a face is detected, so the worst case is the camera framing you on your first frame — which is what
you asked for. The stronger reason is diagnostic: a toggle that silently resets itself is
indistinguishable from the config-erasure bug that already destroys `focus`, and would send the next
debugger down the wrong path.

**D4 — When you move far enough to change focus: re-trigger autofocus (bringing back the hunting this
codebase deliberately removed) or leave the lens parked (so leaning back gives a soft, zoomed-in face)?**
Options: (a) leave parked, accept softness; (b) one-shot re-trigger on a large hysteretic change (~42%
apparent-size, 4 s minimum interval); (c) re-trigger plus its own checkbox.
**Recommend (a) for tier 1, then (b) in tier 3.** The tension is genuine and unavoidable, but shipping
the framing first means you find out whether the softness is actually noticeable at `AF_ZOOM_MAX = 2.0`
before paying for it. Not (c) — the tkinter row has no space and the decision is not one you can evaluate
without an A/B.
