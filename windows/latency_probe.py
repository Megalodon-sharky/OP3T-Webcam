#!/usr/bin/env python3
"""
Latency probe — attributes the OP3T Webcam end-to-end lag to a STAGE instead of guessing.

It talks to the phone exactly like op3t_webcam.py does (same socket, same config line), but it
timestamps every step:

  t_au   : the moment the LAST byte of an H.264 access unit (one encoded frame) arrives on the socket
  t_dec  : the moment ffmpeg emits the matching decoded NV12 frame on its stdout

From those it reports:
  * arrival cadence + jitter at the socket   -> how evenly the phone is emitting frames
  * t_dec - t_au                             -> decode + pipe latency, exactly
  * first-byte -> first-frame                -> decoder startup / probe cost

Anything left over between "photons" and t_au is phone-side (capture + GL + encoder + USB); the
glass-to-glass number from the on-screen-millisecond-timer test minus (t_dec - t_au) minus the vcam
cost gives that residual.

IMPORTANT: close the OP3T Webcam app on the PC first — the phone accepts ONE client at a time.

Usage:
  python windows/latency_probe.py                          # 1080p60, default decoder, 10 s
  python windows/latency_probe.py --seconds 20 --fps 30
  python windows/latency_probe.py --decoder CPU
  python windows/latency_probe.py --flush-packets          # A/B the ffmpeg output-buffer fix
  python windows/latency_probe.py --socket-only            # phone cadence only, no ffmpeg
"""
import argparse
import bisect
import statistics
import subprocess
import socket
import sys
import threading
import time

from op3t_webcam import ADB, FFMPEG, DECODERS, DEFAULT_DECODER, NO_WINDOW, HIGH_PRIORITY, adb_forward

# NAL unit types (H.264, nal_unit_type = byte & 0x1F)
NAL_SLICE, NAL_IDR, NAL_SEI, NAL_SPS, NAL_PPS, NAL_AUD = 1, 5, 6, 7, 8, 9
VCL = (NAL_SLICE, NAL_IDR)
NAL_NAMES = {1: "P", 5: "IDR", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD"}

RECV = 4096  # small reads so an arrival timestamp is accurate to ~3 ms at 12 Mbps, not ~45 ms


class ByteClock:
    """Maps a byte offset in the stream to the wall-clock time that byte arrived."""

    def __init__(self):
        self.ends = []   # cumulative byte offset of the END of each recv chunk
        self.times = []  # arrival time of that chunk
        self.total = 0

    def add(self, n, t):
        self.total += n
        self.ends.append(self.total)
        self.times.append(t)

    def time_of(self, offset):
        i = bisect.bisect_left(self.ends, offset + 1)
        return self.times[min(i, len(self.times) - 1)] if self.times else None


def find_start_codes(buf, from_idx):
    """Yield (nal_payload_start, nal_type) for every Annex-B start code at/after from_idx."""
    i = from_idx
    n = len(buf)
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            return
        yield j + 3, buf[j + 3] & 0x1F
        i = j + 3


def torch_probe(args):
    """Measure the PHONE-SIDE residual (capture -> GL -> encode -> USB) with no human in the loop.

    Trick: we already control the torch over the same socket ("FLASH 1"). Fire it, timestamp, then
    watch the mean luma of each decoded frame for the step. The delta covers:
        LED actuation + HAL applying the repeating request + camera pipeline depth + encode + USB
        + decode + pipe
    Subtract the decode+pipe median (the normal probe mode measures it, ~22 ms at 1080p60 with
    -flush_packets) and the remainder is the phone-side stage that the socket-vs-decode probe cannot
    see. It is an UPPER BOUND on phone latency, not a true glass-to-glass number, because the LED and
    the capture-request round trip are inside it — but it is fully automatic and exact for A/B work.

    Point the phone at a close, matte surface (desk/wall) so the torch actually changes the exposure.
    """
    import numpy as np

    w, h, fps = args.width, args.height, args.fps
    frame_bytes = w * h * 3 // 2
    y_bytes = w * h

    adb_forward(args.port)
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as e:
        print(f"cannot reach the phone on {args.host}:{args.port} ({e}).")
        print("Is the phone app open, USB plugged in, and the PC app CLOSED?")
        return 1
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(f"{w}x{h}@{fps}\n".encode())
    sock.sendall(b"NR 0\n")
    sock.sendall(f"BITRATE {args.bitrate}\n".encode())
    sock.sendall(b"XFORM 0 0 0\n")
    sock.sendall(b"FLASH 0\n")

    lat = ["-flags", "low_delay", "-analyzeduration", "0"]
    lat += ["-probesize", "262144"] if args.decoder == "GPU (Intel QSV)" else ["-probesize", "32"]
    cmd = [FFMPEG, "-loglevel", "error", *lat, *DECODERS[args.decoder], "-i", "pipe:0",
           "-fps_mode", "passthrough", "-flush_packets", "1",
           "-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1"]
    print("ffmpeg:", " ".join(cmd[1:]))
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          creationflags=NO_WINDOW | HIGH_PRIORITY)

    samples = []      # (t, mean_luma) per decoded frame; list.append is atomic under the GIL
    stop = False

    def reader():
        out = ff.stdout
        while not stop:
            buf = out.read(frame_bytes)
            if len(buf) < frame_bytes:
                return
            t = time.perf_counter()
            y = np.frombuffer(buf, np.uint8, count=y_bytes).reshape(h, w)
            samples.append((t, float(y[::16, ::16].mean())))   # subsampled -> sub-ms

    def pump():
        while not stop:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                ff.stdin.write(data)
                ff.stdin.flush()
            except (OSError, ValueError):
                return

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()

    def wait_for(pred, mark, timeout=3.0):
        """First sample after index `mark` satisfying pred(luma). Returns its time, or None."""
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            for i in range(mark, len(samples)):
                if pred(samples[i][1]):
                    return samples[i][0]
            time.sleep(0.001)
        return None

    print(f"warming up (camera/AE settle) ...")
    time.sleep(3.0)
    if len(samples) < 10:
        print("no frames decoded — is the phone streaming? (close the PC app first)")
        return 1

    rises, falls = [], []
    for trial in range(args.torch):
        base = [l for _, l in samples[-30:]]
        b_med = statistics.median(base)
        b_sd = statistics.pstdev(base) or 0.5
        thr_up = b_med + max(5.0, 4 * b_sd)

        mark = len(samples)
        t0 = time.perf_counter()
        sock.sendall(b"FLASH 1\n")
        t1 = wait_for(lambda l: l > thr_up, mark)
        if t1 is None:
            print(f"  trial {trial+1}: no luma rise seen — point the phone at a nearby surface")
        else:
            rises.append((t1 - t0) * 1000)
            print(f"  trial {trial+1}: torch ON  -> {(t1-t0)*1000:6.1f} ms "
                  f"(baseline {b_med:.1f} -> thr {thr_up:.1f})")
        time.sleep(1.2)                      # let AE settle at the lit level

        lit = [l for _, l in samples[-30:]]
        l_med = statistics.median(lit)
        thr_dn = (l_med + b_med) / 2         # midpoint between lit and dark
        mark = len(samples)
        t0 = time.perf_counter()
        sock.sendall(b"FLASH 0\n")
        t1 = wait_for(lambda l: l < thr_dn, mark)
        if t1 is not None:
            falls.append((t1 - t0) * 1000)
            print(f"  trial {trial+1}: torch OFF -> {(t1-t0)*1000:6.1f} ms")
        time.sleep(1.2)

    stop = True
    try:
        sock.sendall(b"FLASH 0\n")
    except OSError:
        pass
    try: sock.close()
    except OSError: pass
    try: ff.terminate()
    except Exception: pass

    print()
    print("=" * 62)
    n = len(samples)
    if n > 2:
        span = samples[-1][0] - samples[0][0]
        print(f"decoded {n} frames over {span:.1f}s = {(n-1)/span:.1f} fps")
    for name, vals in (("torch ON  (rise)", rises), ("torch OFF (fall)", falls)):
        if vals:
            print(f"{name}: median {statistics.median(vals):6.1f} ms   "
                  f"min {min(vals):6.1f}   max {max(vals):6.1f}   n={len(vals)}")
        else:
            print(f"{name}: no detections")
    if rises:
        print()
        print("This is control->photons->socket->decode, an UPPER BOUND on phone-side latency")
        print("(it includes LED actuation + the capture-request round trip).")
        print("Subtract the decode+pipe median from the normal probe mode to isolate the phone.")
    print("=" * 62)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", type=int, default=12)
    ap.add_argument("--decoder", default=DEFAULT_DECODER, choices=list(DECODERS.keys()))
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--flush-packets", action="store_true",
                    help="add -flush_packets 1 to ffmpeg (A/B the held-frame theory)")
    ap.add_argument("--force-h264", action="store_true",
                    help="add -f h264 so ffmpeg skips format probing on the pipe")
    ap.add_argument("--socket-only", action="store_true", help="phone cadence only, skip ffmpeg")
    ap.add_argument("--extra", default="", help="extra ffmpeg INPUT args, e.g. \"-surfaces 4\"")
    ap.add_argument("--torch", type=int, default=0, metavar="N",
                    help="torch round-trip mode: N trials measuring the PHONE-SIDE residual "
                         "(fires FLASH and times the luma step in the decoded frames)")
    args = ap.parse_args()

    if args.torch:
        return torch_probe(args)

    w, h, fps = args.width, args.height, args.fps
    frame_bytes = w * h * 3 // 2

    adb_forward(args.port)
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as e:
        print(f"cannot reach the phone on {args.host}:{args.port} ({e}).")
        print("Is the phone app open, USB plugged in, and the PC app CLOSED?")
        return 1
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(f"{w}x{h}@{fps}\n".encode())
    sock.sendall(b"NR 0\n")
    sock.sendall(f"BITRATE {args.bitrate}\n".encode())
    sock.sendall(b"XFORM 0 0 0\n")

    ff = None
    dec_times = []
    if not args.socket_only:
        lat = ["-flags", "low_delay", "-analyzeduration", "0"]
        lat += ["-probesize", "262144"] if args.decoder == "GPU (Intel QSV)" else ["-probesize", "32"]
        in_args = list(DECODERS[args.decoder])
        if args.force_h264 and "-f" not in in_args:
            in_args = ["-f", "h264"] + in_args
        if args.extra:
            in_args = in_args + args.extra.split()
        cmd = [FFMPEG, "-loglevel", "error", *lat, *in_args, "-i", "pipe:0",
               "-fps_mode", "passthrough"]
        if args.flush_packets:
            cmd += ["-flush_packets", "1"]
        cmd += ["-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1"]
        print("ffmpeg:", " ".join(cmd[1:]))
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              creationflags=NO_WINDOW | HIGH_PRIORITY)

        def reader():
            out = ff.stdout
            while True:
                buf = out.read(frame_bytes)
                if len(buf) < frame_bytes:
                    return
                dec_times.append(time.perf_counter())

        threading.Thread(target=reader, daemon=True).start()

    clock = ByteClock()
    stream = bytearray()
    scanned = 0            # how far into `stream` we've looked for start codes
    au_end_times = []      # arrival time of the last byte of each access unit
    au_kinds = []          # "IDR" / "P" for each access unit
    au_sizes = []
    pending = None         # (payload_start_offset, kind) of the VCL NAL we haven't closed yet
    nal_counts = {}
    t_first = None

    deadline = time.perf_counter() + args.seconds
    try:
        while time.perf_counter() < deadline:
            sock.settimeout(max(0.05, deadline - time.perf_counter()))
            try:
                data = sock.recv(RECV)
            except socket.timeout:
                break
            if not data:
                break
            t = time.perf_counter()
            if t_first is None:
                t_first = t
            base = clock.total
            clock.add(len(data), t)
            stream += data
            if ff:
                ff.stdin.write(data)
                ff.stdin.flush()

            # Scan the freshly-appended region for start codes. Back up 3 bytes so a start code
            # straddling two recv chunks is still seen exactly once.
            start = max(scanned, 0)
            for pos, ntype in find_start_codes(stream, start):
                nal_counts[ntype] = nal_counts.get(ntype, 0) + 1
                if ntype in VCL:
                    if pending is not None:
                        prev_start, prev_kind = pending
                        end_off = pos - 4          # last byte before this NAL's start code
                        au_end_times.append(clock.time_of(end_off))
                        au_kinds.append(prev_kind)
                        au_sizes.append(end_off - prev_start)
                    pending = (pos, NAL_NAMES.get(ntype, str(ntype)))
            scanned = max(0, len(stream) - 3)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if ff:
            # ffmpeg's own CPU cost over the run: kernel+user time from the OS, before we kill it.
            try:
                import ctypes
                from ctypes import wintypes
                k = ctypes.windll.kernel32
                hp = k.OpenProcess(0x0400, False, ff.pid)   # PROCESS_QUERY_INFORMATION
                c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
                if hp and k.GetProcessTimes(hp, ctypes.byref(c), ctypes.byref(e),
                                            ctypes.byref(kt), ctypes.byref(ut)):
                    def secs(ftime):
                        return ((ftime.dwHighDateTime << 32) | ftime.dwLowDateTime) / 1e7
                    ff_cpu = secs(kt) + secs(ut)
                    print(f"\n  ffmpeg CPU time: {ff_cpu:.2f} s over {args.seconds:.0f} s wall "
                          f"= {100*ff_cpu/args.seconds:.0f}% of one core")
                if hp:
                    k.CloseHandle(hp)
            except Exception:
                pass
            time.sleep(0.3)          # let the last frames drain
            try:
                ff.stdin.close()
            except Exception:
                pass
            time.sleep(0.3)
            try:
                ff.terminate()
            except Exception:
                pass

    # ---------------- report ----------------
    dur = (au_end_times[-1] - au_end_times[0]) if len(au_end_times) > 1 else 0.0
    print()
    print(f"=== socket: {clock.total/1e6:.2f} MB in {args.seconds:.1f}s "
          f"({clock.total*8/1e6/max(args.seconds,1e-9):.1f} Mbps) ===")
    print("NAL types seen:", {NAL_NAMES.get(k, k): v for k, v in sorted(nal_counts.items())})
    if NAL_AUD not in nal_counts:
        print("  !! no AUD (access unit delimiter) NALs — the H.264 parser cannot know a frame ended")
        print("     until the NEXT frame's first bytes arrive. That is a full frame of added latency.")
    print(f"access units: {len(au_end_times)}  "
          f"({len(au_end_times)/dur:.1f} fps over {dur:.1f}s)" if dur else
          f"access units: {len(au_end_times)}")
    if len(au_end_times) > 2:
        gaps = [1000 * (b - a) for a, b in zip(au_end_times, au_end_times[1:])]
        gaps_s = sorted(gaps)
        print(f"  inter-frame arrival ms: mean {statistics.mean(gaps):6.2f}  "
              f"median {statistics.median(gaps):6.2f}  "
              f"p95 {gaps_s[int(len(gaps_s)*0.95)]:6.2f}  max {max(gaps):6.2f}  "
              f"(target {1000/fps:.2f})")
        print(f"  jitter (stdev) ms: {statistics.pstdev(gaps):.2f}")
        big = [(i, g) for i, g in enumerate(gaps) if g > 3 * 1000 / fps]
        if big:
            print(f"  !! {len(big)} stalls > 3 frame times (worst {max(g for _, g in big):.0f} ms):")
            t0 = au_end_times[0]
            for i, g in big[:12]:
                # gaps[i] is the gap BEFORE access unit i+1
                print(f"       t={au_end_times[i]-t0:6.2f}s  gap {g:7.1f} ms  "
                      f"au#{i+1} is {au_kinds[i+1]} ({au_sizes[i+1]/1024:.1f} KiB), "
                      f"prev was {au_kinds[i]}")
        idr_idx = [i for i, k in enumerate(au_kinds) if k == "IDR"]
        if idr_idx:
            print(f"  IDR access units at index {idr_idx} "
                  f"(t={[round(au_end_times[i]-au_end_times[0], 2) for i in idr_idx]}s)")
            pre = [gaps[i - 1] for i in idr_idx if i >= 1]
            if pre:
                print(f"  gap immediately BEFORE each IDR: {[round(g, 1) for g in pre]} ms")
        print(f"  mean AU size: {statistics.mean(au_sizes)/1024:.1f} KiB  "
              f"max {max(au_sizes)/1024:.1f} KiB (IDR)")

    if ff is not None:
        print()
        print(f"=== ffmpeg ({args.decoder}"
              f"{', flush_packets' if args.flush_packets else ''}"
              f"{', -f h264' if args.force_h264 else ''}) ===")
        print(f"decoded frames: {len(dec_times)}")
        if t_first and dec_times:
            print(f"  first byte -> first decoded frame: {1000*(dec_times[0]-t_first):.0f} ms "
                  f"(startup: probe + IDR wait)")
        # Align: the decoder emits its first frame for the first IDR access unit.
        try:
            first_idr = au_kinds.index("IDR")
        except ValueError:
            first_idr = 0
        pairs = []
        for j, td in enumerate(dec_times):
            i = first_idr + j
            if i < len(au_end_times) and au_end_times[i] is not None:
                pairs.append(1000 * (td - au_end_times[i]))
        if len(pairs) > 5:
            skip = min(len(pairs) // 3, 2 * fps)   # drop the camera/AE warm-up transient
            body = pairs[skip:]
            bs = sorted(body)
            print(f"  DECODE+PIPE latency ms (steady state, first {skip} frames dropped): "
                  f"mean {statistics.mean(body):6.2f}  median {statistics.median(body):6.2f}  "
                  f"p95 {bs[int(len(bs)*0.95)]:6.2f}  max {max(body):6.2f}")
            # Per-second timeline. A FLAT profile = a fixed offset (parser/buffer). A RISING profile
            # = a queue filling somewhere, which is the signature of the 300-500 ms complaint.
            print("  per-second median (ms):", end=" ")
            for sec in range(int(len(pairs) / max(fps, 1)) + 1):
                chunk = pairs[sec * fps:(sec + 1) * fps]
                if len(chunk) > 4:
                    print(f"{statistics.median(chunk):.0f}", end=" ")
            print()
            print(f"  (one frame time at {fps} fps = {1000/fps:.2f} ms — a mean near or above that "
                  f"means ffmpeg is holding a whole frame)")
        else:
            print("  not enough paired frames to measure (did the decoder produce output?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
