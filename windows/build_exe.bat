@echo off
REM Build a portable single-file "OP3T Webcam.exe" with ffmpeg + adb bundled in.
REM Output: windows\dist\OP3T Webcam.exe   (only OBS Studio remains a prerequisite)
cd /d "%~dp0"
python build_exe.py
pause
