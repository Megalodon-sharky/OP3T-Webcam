#!/usr/bin/env python3
"""
Build a portable single-file "OP3T Webcam.exe" that bundles ffmpeg + adb so the only
remaining prerequisite is OBS Studio (its virtual-camera driver is a system component
and cannot be packed into an exe).

Run:  python build_exe.py        (or double-click build_exe.bat)
Out:  windows/dist/OP3T Webcam.exe
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def resolve_ffmpeg():
    """shutil.which returns the winget shim under WinGet\\Links; that stub can't be bundled,
    so fall back to the real ffmpeg.exe inside WinGet\\Packages."""
    p = shutil.which("ffmpeg")
    if p:
        real = os.path.realpath(p)
        if os.path.exists(real) and "WinGet\\Links" not in real:
            return real
    for base in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages"),
                 r"C:\ffmpeg", os.environ.get("ProgramFiles", "")):
        if base:
            hits = glob.glob(os.path.join(base, "**", "ffmpeg.exe"), recursive=True)
            if hits:
                return hits[0]
    return p  # last resort: bundle the shim and hope


def adb_files():
    p = shutil.which("adb")
    if not p:
        return []
    d = os.path.dirname(p)
    files = [p]
    for dll in ("AdbWinApi.dll", "AdbWinUsbApi.dll"):
        f = os.path.join(d, dll)
        if os.path.exists(f):
            files.append(f)          # adb needs these DLLs next to it on Windows
    return files


def main():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "pyinstaller", "pyvirtualcam", "numpy",
                    # headless build: no Qt/GTK. Only used for cv2.resize on the crop path, which is
                    # MEASURED 5-8x faster than the numpy fallback and now runs on every frame while
                    # auto-framing is engaged.
                    "opencv-python-headless"], check=True)

    adds = []
    ff = resolve_ffmpeg()
    if ff and os.path.exists(ff):
        adds += ["--add-binary", f"{ff}{os.pathsep}tools"]
        print("bundling ffmpeg:", ff)
    else:
        print("WARNING: ffmpeg not found — exe will need ffmpeg on PATH")
    for f in adb_files():
        adds += ["--add-binary", f"{f}{os.pathsep}tools"]
        print("bundling:", f)
    if not adb_files():
        print("WARNING: adb not found — exe will need adb on PATH")

    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole",
           "--name", "OP3T Webcam", "--collect-all", "pyvirtualcam",
           # liquid-glass web UI (pywebview + WebView2). clr_loader/pythonnet = its Windows backend.
           "--collect-all", "webview", "--collect-all", "clr_loader",
           "--copy-metadata", "pywebview", "--hidden-import", "webui",
           "--hidden-import", "bottle", "--hidden-import", "proxy_tools",
           # cv2.pyd is ~80 MB and UPX-compressing it costs minutes of build time and seconds of
           # startup (onefile re-extracts the whole archive on EVERY launch) for no real saving.
           "--upx-exclude", "cv2.pyd",
           # NOTE: OpenCV's own ffmpeg DLL (~12 MiB) is dead weight here — this project shells out to
           # a bundled ffmpeg.exe and never touches cv2.VideoCapture. It is NOT excluded, because
           # there is no clean way to: cv2 ships as ONE binary extension, so --exclude-module does
           # nothing (verified: the built exe still contains opencv_videoio), and dropping the DLL
           # would mean post-filtering a.binaries in a spec file that this script regenerates every
           # run. Not worth 12 MiB on a 155 MiB archive.
           "--distpath", os.path.join(HERE, "dist"),
           "--workpath", os.path.join(HERE, "build"),
           "--specpath", os.path.join(HERE, "build"),
           *adds, os.path.join(HERE, "op3t_webcam.py")]
    subprocess.run(cmd, check=True)
    print("\nDone -> dist\\OP3T Webcam.exe")


if __name__ == "__main__":
    main()
