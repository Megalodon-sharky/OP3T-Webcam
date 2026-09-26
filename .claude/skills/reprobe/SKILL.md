---
name: reprobe
description: Rebuild the OP3T Webcam exe and measure end-to-end latency with latency_probe.py, comparing against the stored baseline. Use after any change to windows/*.py or the ffmpeg flags.
disable-model-invocation: true
---

# reprobe

Answers "did that change actually help?" with numbers instead of opinion. Every previous latency
session failed by skipping this.

Arguments (optional): a decoder name to test, e.g. `/reprobe CPU`. Default: the app's current
`DEFAULT_DECODER`.

## Preconditions — check these first, in order

1. **Phone attached and authorised.** `adb devices` must list a device with `device` status (not
   `offline`, not `unauthorized`). If offline, run `adb reconnect offline`.
2. **Nothing else holds the socket.** The phone accepts exactly ONE client. Check for a running
   receiver and stop it, because the probe will otherwise fail with "cannot reach the phone":

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name LIKE 'OP3T%' OR Name='ffmpeg.exe'" | Select-Object ProcessId,Name,CommandLine
   ```

   **Ask the user before killing anything** — they may be mid-call. Only kill `OP3T Webcam.exe` and
   the `ffmpeg.exe` whose command line contains `pipe:0`. Never kill unrelated `python.exe`
   processes; on this machine most of them are Claude's own MCP servers.
3. **Idle box.** Do not run this while a build, a subagent fleet, or a screenshare is running.
   Measured proof this matters: the CPU decoder read 25.9 ms on an idle box and 41.5 ms under load.

## Steps

1. **Rebuild** so the binary matches the source:

   ```bash
   python windows/build_exe.py
   ```

   Confirm `windows/dist/OP3T Webcam.exe` is now newer than every `windows/*.py`.

2. **Measure.** Run at least 16 seconds so the camera/AE warm-up transient is excluded:

   ```bash
   python windows/latency_probe.py --seconds 20 --flush-packets
   ```

   Add `--decoder "<name>"` if the user named one, and `--extra "<ffmpeg args>"` to A/B a flag
   without editing the app.

3. **Read the output the right way.** The number that matters is
   `DECODE+PIPE latency ms (steady state, ...)` **median**, plus the `per-second median` timeline:
   - flat timeline = a fixed offset (parser / buffer / decoder depth)
   - rising timeline = a queue filling — a real bug, chase it
   - high `mean` with a flat median = warm-up or an outlier, not a regression
   Also check `inter-frame arrival` median stays near `1000/fps`; if the phone itself went bursty,
   the PC numbers are meaningless.

4. **Compare to baseline.** Read `windows/latency_baseline.txt`. If it does not exist, create it from
   this run and say so. Otherwise report a table: metric, baseline, now, delta. Treat anything under
   ~5 ms as noise — run-to-run spread on this box is real.

5. **Update the baseline ONLY if the user confirms** the change is an improvement worth locking in.
   Write the probe's summary lines plus the date and the exact probe command used.

## Reference numbers (1080p60, measured 2026-07-28, idle box)

| decoder | steady decode+pipe | ffmpeg CPU | first frame |
|---|---|---|---|
| CPU | 26-42 ms | 53% core | 52 ms |
| NVIDIA cuvid | 59-62 ms | 23% core | ~330 ms |
| Intel QSV + `-extra_hw_frames 2` | ~40 ms | 27% core | ~1.1 s |

Floor is roughly 16.7 ms (the H.264 parser cannot close a frame until the next one starts — the
phone emits no AUD NALs) plus real decode time.

## What this does NOT measure

Photons -> socket on the phone. For true glass-to-glass, point the phone at a millisecond timer on
screen and capture the phone and the vcam output in one shot, then subtract the decode+pipe median.
