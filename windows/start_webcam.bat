@echo off
REM CLI/console launcher (for debugging). For a no-console launch, double-click "OP3T Webcam.vbs".
REM The GUI sets up the adb tunnel itself, so no adb command is needed here.
python "%~dp0op3t_webcam.py" %*
