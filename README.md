# OP3T Webcam

Use a **OnePlus 3T (A3003, Android 9)** rear camera as a high-quality USB webcam on Windows.

- **Transport:** USB (ADB TCP tunnel) — low latency, no WiFi.
- **Android app:** Camera2 → hardware H.264 encode (MediaCodec), low-latency, screen blacks out to save the OLED.
- **Windows app:** small **control GUI** → ffmpeg **GPU** decode → OBS Virtual Camera.
- **Result:** phone shows up as **"OBS Virtual Camera"** in Discord / Zoom / Teams / OBS / anything.

Measured: **1920×1080 @ 59.9 fps** end-to-end with GPU (d3d11va) decode.

The OnePlus 3T uses the **Sony IMX298** (16 MP stills, 4608×3456). Its Snapdragon 821
AVC encoder does live H.264 up to **4K30**; the app streams **1080p60 by default** and you
pick resolution/fps from the GUI.

---

## What you need (install once)

**On the PC:**

The prebuilt **`OP3T Webcam.exe`** bundles ffmpeg + adb, so the only thing you must install is OBS:

| Tool | Why | Get it |
|---|---|---|
| OBS Studio | provides the virtual camera (a system driver — can't be bundled) | https://obsproject.com — **never need to open it** |

Running from source instead of the exe? Then you also need:
| Tool | Why | Get it |
|---|---|---|
| Android platform-tools (adb) | USB tunnel | https://developer.android.com/tools/releases/platform-tools |
| ffmpeg | GPU decode | `winget install Gyan.FFmpeg` — then open a new terminal |
| Python 3 deps | the receiver/GUI | `pip install -r windows/requirements.txt` (tkinter ships with Python) |

Build the portable exe yourself: double-click **`windows/build_exe.bat`** → `windows/dist/OP3T Webcam.exe`.

**On the phone:** Android 9, **USB debugging enabled** (Settings → Developer options).

---

## Build & install the Android app

The project builds with **Gradle 9.x + Android Gradle Plugin 8.11.1** (JDK 17).

**Android Studio:** open `android/` → Run on the connected phone. Easiest.

**CLI** (Gradle on PATH, phone plugged in):
```
cd android
gradle installDebug        # builds + installs straight to the phone
```
The app listens on **TCP 8080** and waits for the PC. No wrapper jar is committed; use your
system Gradle or Android Studio's.

---

## Run it

1. Plug the phone in via USB. Confirm: `adb devices` lists it.
2. Open the **OP3T Webcam** app on the phone ("Waiting for PC on tcp:8080").
3. On the PC, run **`OP3T Webcam.exe`** (portable, nothing else to install but OBS). It opens the GUI
   and sets up the USB tunnel itself. (From source: double-click `OP3T Webcam.vbs` for a no-console
   launch, or `start_webcam.bat` for the console/CLI version.)
4. In the GUI: pick **Resolution / Frame rate / Orientation**, click **Start**.
5. In Discord/Zoom/etc, pick camera **"OBS Virtual Camera"**.
   (Quit the meeting app fully and reopen it if it was running *before* you clicked Start —
   apps enumerate cameras at launch.)

### The GUI

Controls are grouped by **how often you touch them**, not by what kind of widget they are. *Framing*
and *Image* stay visible for mid-call use; *Setup* is collapsed because you set it once. The widget
type carries meaning: a **switch** is persistent state, **segments** are pick-one, a **checkbox** is
an independent toggle, a **button** does something once.

| Control | Where | What it does |
|---|---|---|
| **Auto-frame** | Framing | Face-tracking zoom + pan. The phone's ISP reports face rectangles over a second port (tcp:8081) and the PC drives the crop box to hold your face at a steady size and position. Move back and it zooms in; lean in and it zooms out; move sideways and it follows. Any manual drag, scroll or slider hands control straight back — and the panel now says so instead of silently unchecking itself. |
| **Zoom** ⇄ **Framing tightness** | Framing | One slider whose job depends on Auto-frame, relabelled in front of you rather than silently redefined. Off → **Zoom**, a PC-side digital crop 1.0–4.0× of the full frame (so the preview can still show what falls outside the box). On → **Framing tightness**, how much of the frame your face should fill; the controller owns the actual crop. (The sensor's own max digital zoom is 4.0× — measured; earlier docs said 1.9×, which was wrong.) |
| **Auto-frame** | Face-tracking zoom + pan. The phone's ISP reports face rectangles over a second port (tcp:8081) and the PC drives the crop box to hold your face at a steady size and position. Move back and it zooms in; lean in and it zooms out; move sideways and it follows. Any manual drag, scroll or slider hands control straight back. |
| **Sensor zoom** | Framing | `Off` / `Auto` / `On`. Who does the magnification. Its own slider appears only in **On** — in **Auto** the controller owns the ratio, so you get a live readout of what the phone is *actually applying* instead of a control that would fight back. **Off** — the PC crop does it all (upscaling an already-downscaled 1080p frame). **Auto** — the phone's own sensor crop takes over *only* once the PC crop is pinned at its 2.0× limit and you are still too small, and hands it back when you come closer. **On** — a fixed ratio you dial, 1.0–2.4×. The 2.40× ceiling is measured: that is the last ratio whose sensor crop (1939 px) is still at least as wide as the 1920 px output. The trade is real — `croppingType` is `CENTER_ONLY`, so a sensor crop **cannot pan**, and every bit of zoom moved onto the sensor is pan range auto-frame gives up. Hence the default is Off. |
| **Exposure / Focus** | Image | Exposure compensation ±6 steps. Focus is a slider with an **Auto** detent at 0; anything above it locks the lens. |
| **Refocus** | Image | Re-runs a one-shot autofocus. Focus is **locked** otherwise (no continuous-AF jitter). |
| **Flash** | Image | Rear torch. A switch, because it is state — it used to be drawn as a button. |
| **Frame rate** | Setup | 60 or 30 fps, sent to the phone. **60 looks far smoother.** At 30 the phone captures 4K and downscales on its own GPU. Badged `restarts` — it tears the stream down and back up. |
| **Decoder** | Setup | `NVIDIA (cuvid)` default. `Intel QSV` / `CPU` selectable. Also badged `restarts`. |
| **Orientation** | Setup | 0 / 90 / 180 / 270°. Rotates on the phone's GPU; applies live. |
| **Quality** | Setup | Encoder bitrate, 8–20 Mbps. A named dropdown now, not an unlabelled 4–20 slider. |
| **Mirror / Flip / Noise reduction** | Setup | Independent toggles, so they are checkboxes. |
| **Preview** | — | Live in-GUI preview so you can frame the shot without opening another app. **640×360 at 30 fps**, served as MJPEG over loopback so the browser pulls it directly — no pywebview bridge call per frame. (Was 384×216 at 4 fps as a base64 BMP over the bridge: 8.6 ms and 324 KB per frame against 4.3 ms and ~15 KB now — measured. It was slower *and* worse.) |
| **Start / Stop** | — | Only Frame rate and Decoder restart the stream; both are badged. Everything else applies live. |

Status line shows live **fps** and frame count when streaming.

### OLED power save

After **30 s** of streaming the phone screen blacks out and drops brightness to ~2/255 — on the
OLED that's near-zero power, while the camera keeps streaming. **Tap the screen** to wake it.

### Command line (no GUI)

```
python windows\op3t_webcam.py --test                      # vcam test pattern, no phone needed
python windows\op3t_webcam.py --headless --width 1280 --height 720 --fps 60 --rotation 90
```

---

## How it fits together

```
[OnePlus 3T]                          [Windows PC]
Camera2 (IMX298)                      GUI: res / fps / orientation
   |                                     |  "1280x720@60\n"  (config line)
MediaCodec H.264 (HW, low-latency)       v
   |  raw Annex-B                      op3t_webcam.py
TCP server :8080  <==== USB / ADB ====>  TCP client
   ^  screen blacks out after 30s        |  pipe raw H.264
                                       ffmpeg  (GPU decode + rotate)
                                          |  BGR24 frames
                                       pyvirtualcam -> "OBS Virtual Camera"
                                          |
                                       Discord / Zoom / Teams / OBS
```

On connect the PC sends one line `WIDTHxHEIGHT@FPS`; the phone configures its encoder to match.
Orientation is applied PC-side, so changing it never has to touch the phone.

---

## Design choices (and what was deliberately skipped)

- **No custom DirectShow filter.** Reused OBS's signed virtual camera via `pyvirtualcam` (~stdlib + 1 dep).
- **GPU decode, `-flags low_delay`.** `d3d11va` decodes 1080p60 with frames to spare. NVIDIA `cuvid`
  was *faster but dropped frames*, so it isn't the default. **Do not** add `-fflags nobuffer` — with
  this MediaCodec stream it makes the H.264 parser flush ~2 frames then stall (black after 1 frame).
- **Latency / choppiness fixes.** The big one: the PC must `flush()` ffmpeg's stdin after every socket
  read — otherwise frames buffer and arrive in bursts (worse at lower bitrates, which is why 720p felt
  *more* choppy). Plus `-fps_mode passthrough` (no CFR re-pacing), encoder realtime priority, and EIS
  **off** on the phone (stabilisation adds latency + wobble).
- **Focus locked.** Continuous AF hunts and looks jittery; the app does one autofocus then parks the lens.
  Use **Refocus** if the subject distance changes. Zoom is sensor crop (`SCALER_CROP_REGION`), live.
- **tkinter GUI.** Stdlib — no Qt/Electron/web stack for four dropdowns and a button.
- **USB only.** Lower latency, stable bandwidth. WiFi would need the phone's IP instead of the adb tunnel.

## Troubleshooting

- **GUI status stuck on "connecting"** → app not open on phone, or tunnel missing. `start_webcam.bat`
  sets the tunnel; if you ran Python directly, run `adb forward tcp:8080 tcp:8080` first.
- **No "OBS Virtual Camera" in your app** → OBS Studio not installed, or the app was open before you
  clicked Start; fully quit and reopen it.
- **`ffmpeg` not found** → `winget install Gyan.FFmpeg`, then open a **new** terminal (PATH refresh).
- **Want lower CPU / rock-solid frames** → drop to 720p or 30 fps in the GUI.
- **Decoder errors on a specific GPU** → switch the Decoder dropdown to `CPU` (always works) or another GPU.
- **`EADDRINUSE` on the phone** → a stale instance holds the port: `adb shell am force-stop com.op3t.webcam`, reopen.
