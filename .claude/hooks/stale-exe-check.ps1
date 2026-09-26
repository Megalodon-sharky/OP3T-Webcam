# PostToolUse(Write|Edit) — warn when windows/*.py is newer than windows/dist/OP3T Webcam.exe.
#
# This is THE bug of this project. On 2026-07-28 the shipped exe was 2 weeks older than the source:
# built 2026-06-26 14:30, source fixed through 2026-07-10 00:57. Two sessions of latency fixes were
# judged against a binary that did not contain any of them (proof: the running app's ffmpeg still
# said "-pix_fmt bgr24", i.e. pre-NV12). Never let that happen silently again.
$ErrorActionPreference = 'SilentlyContinue'

$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }
try { $j = $raw | ConvertFrom-Json } catch { exit 0 }

$f = $j.tool_input.file_path
if (-not $f) { $f = $j.tool_response.filePath }
if (-not $f) { exit 0 }
# Anchor on start-of-string too: hook input is normally absolute, but a relative path
# ("windows\op3t_webcam.py") must match as well or the check silently never fires.
if ($f -notmatch '(^|[\\/])windows[\\/].*\.py$') { exit 0 }
if (-not (Test-Path -LiteralPath $f)) { exit 0 }

# .claude/hooks -> .claude -> repo root
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$exe = Join-Path $repo 'windows\dist\OP3T Webcam.exe'
if (-not (Test-Path -LiteralPath $exe)) { exit 0 }

$src = (Get-Item -LiteralPath $f).LastWriteTime
$bin = (Get-Item -LiteralPath $exe).LastWriteTime
if ($src -le $bin) { exit 0 }

$age = [int]($src - $bin).TotalMinutes
$name = Split-Path -Leaf $f
@{
    systemMessage      = "STALE BINARY: $name is $age min newer than dist\OP3T Webcam.exe. Testing the exe will NOT test this change. Rebuild: python windows\build_exe.py"
    hookSpecificOutput = @{
        hookEventName     = 'PostToolUse'
        additionalContext = "windows\dist\OP3T Webcam.exe was built $bin but $name was modified $src. The packaged app does not contain this edit. Before drawing ANY conclusion from running 'OP3T Webcam.exe', run: python windows\build_exe.py. Running from source (python windows\op3t_webcam.py) is unaffected."
    }
} | ConvertTo-Json -Compress -Depth 5
exit 0
