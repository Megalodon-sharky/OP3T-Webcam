"""
The producer side of the OBS Virtual Camera, written for this app so the exe ships no GPL-2.0-only
code next to OpenCV (Apache-2.0). It replaces pyvirtualcam, which did the same job here.

OBS Studio installs a DirectShow filter, "OBS Virtual Camera", that every capturing app loads into its
own process. The filter shows whatever a producer writes into one named, page-file-backed section, and
OBS's placeholder when there is none. The layout below was read out of a working producer
(pyvirtualcam 0.15.0) on 2026-10-05 and is checked end to end through OBS 32.1.2's filter by
test_obs_vcam.py. All fields little-endian:

    byte  field
    0     u32 write_idx   bumped by the producer BEFORE it fills slot write_idx % 3
    4     u32 read_idx    set to write_idx once that slot is complete; readers show slot read_idx % 3
    8     u32 state       1 starting, 2 ready (a frame has been written), 3 stopping (producer gone)
    12    u32 offset[3]   where each of the three frame slots starts
    24    u32 type        0 = video
    28    u32 width
    32    u32 height
    40    u64 interval    frame interval in 100 ns units: 10_000_000 / fps, truncated
    48    u32 reserved[8]
    96    first slot: u64 timestamp (ns), 24 zero bytes, then one NV12 frame with stride == width.
          Every slot starts 32-byte aligned.

A reader that sees state 3 lets go within a frame, but until every reader has, the old section still
exists — and a producer must never write into someone else's (OBS Studio's own camera, another copy
of this app). So creating one that already exists is refused, and VcamHost retries.
"""
import ctypes
import threading
import time
from ctypes import wintypes as w

import numpy as np

SECTION = "OBSVirtualCamVideo"
FILTER_CLSID = "{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"    # the "OBS Virtual Camera" filter
STARTING, READY, STOPPING = 1, 2, 3
HEADER_BYTES = 96                                          # 80-byte header, padded to 32
SLOT_HEADER_BYTES = 32

_k32 = []


def _kernel32():
    if not _k32:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileMappingW.restype = w.HANDLE
        k.CreateFileMappingW.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.DWORD, w.DWORD,
                                         w.LPCWSTR]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [w.HANDLE]
        _k32.append(k)
    return _k32[0]


def installed():
    """Is OBS Studio's virtual-camera filter registered? Without it no app lists the camera at all,
    and frames written here would go nowhere."""
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                       rf"CLSID\{FILTER_CLSID}\InprocServer32"))
        return True
    except OSError:
        return False


def layout(width, height):
    """([slot offsets], section size) for a width x height NV12 queue."""
    frame = width * height * 3 // 2
    offsets, size = [], HEADER_BYTES
    for _ in range(3):
        offsets.append(size)
        size = (size + SLOT_HEADER_BYTES + frame + 31) & ~31
    return offsets, size


class ObsVirtualCamera:
    """Writes NV12 frames to the OBS Virtual Camera: send(frame), close(), `with`, and .device —
    the calls this app made on pyvirtualcam.Camera."""

    device = "OBS Virtual Camera"

    def __init__(self, width, height, fps):
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError(f"NV12 needs an even width and height, not {width}x{height}")
        if not installed():
            raise RuntimeError("OBS Virtual Camera is not installed: install OBS Studio")
        self.width, self.height, self.fps = width, height, fps
        self.handle = self._view = None
        self._lock = threading.Lock()    # send() and close() never overlap: a send into memory
        offsets, size = layout(width, height)    # close() just unmapped would crash the process
        k = _kernel32()
        h = k.CreateFileMappingW(w.HANDLE(-1), None, 0x04, 0, size, SECTION)   # PAGE_READWRITE
        err = ctypes.get_last_error()
        if not h:
            raise RuntimeError(f"could not create the camera's shared memory (error {err})")
        if err == 183:                   # ERROR_ALREADY_EXISTS: somebody else's (see the top)
            k.CloseHandle(h)
            raise RuntimeError("already in use by another program (OBS Studio's own virtual camera?)")
        view = k.MapViewOfFile(h, 0x0002, 0, 0, 0)                            # FILE_MAP_WRITE
        if not view:
            err = ctypes.get_last_error()
            k.CloseHandle(h)
            raise RuntimeError(f"could not map the camera's shared memory (error {err})")
        self.handle, self._view = h, view
        mem = np.frombuffer((ctypes.c_uint8 * size).from_address(view), np.uint8)
        shape = (height * 3 // 2, width)
        self._hdr = mem[:HEADER_BYTES].view("<u4")
        self._ts = [mem[o:o + 8].view("<u8") for o in offsets]
        self._slots = [mem[o + SLOT_HEADER_BYTES:o + SLOT_HEADER_BYTES + shape[0] * width]
                       .reshape(shape) for o in offsets]
        self._shape = shape
        hdr = self._hdr                  # a new section is zero-filled: only the non-zero fields
        hdr[3:6] = offsets
        hdr[7], hdr[8] = width, height
        mem[40:48].view("<u8")[0] = int(10_000_000 / fps)
        hdr[2] = STARTING                # last: a reader that opens it early waits for READY

    def send(self, frame):
        """One NV12 frame: shape (height * 3 / 2, width), uint8 — the Y plane, then interleaved UV."""
        if frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"expected a {self._shape} uint8 NV12 frame, "
                             f"got {frame.shape} {frame.dtype}")
        with self._lock:
            hdr = self._hdr
            if hdr is None:
                raise RuntimeError("the virtual camera is closed")
            n = (int(hdr[0]) + 1) & 0xFFFFFFFF
            hdr[0] = n
            np.copyto(self._slots[n % 3], frame)
            self._ts[n % 3][0] = time.perf_counter_ns()
            hdr[1] = n                   # only now may readers pick this slot
            hdr[2] = READY

    def close(self):
        with self._lock:
            if self._view is None:
                return
            if self._hdr is not None:
                self._hdr[2] = STOPPING  # readers let go of the section when they see this
            self._hdr = self._ts = self._slots = None    # no view may outlive the mapping
            k = _kernel32()
            k.UnmapViewOfFile(self._view)
            k.CloseHandle(self.handle)
            self._view = self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
