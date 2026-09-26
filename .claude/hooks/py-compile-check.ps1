# PostToolUse(Write|Edit) — byte-compile any Python file Claude just touched.
# Catches the failure that actually happened on 2026-07-28: an edit closed a docstring early and
# left windows/op3t_webcam.py a SyntaxError. Cheap, and a broken receiver is invisible until you run it.
$ErrorActionPreference = 'SilentlyContinue'

$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }
try { $j = $raw | ConvertFrom-Json } catch { exit 0 }

$f = $j.tool_input.file_path
if (-not $f) { $f = $j.tool_response.filePath }
if (-not $f) { exit 0 }
if ($f -notmatch '\.py$') { exit 0 }
if (-not (Test-Path -LiteralPath $f)) { exit 0 }

$out = & python -m py_compile -- "$f" 2>&1
if ($LASTEXITCODE -ne 0) {
    $msg = ($out | Out-String).Trim()
    @{
        decision = 'block'
        reason   = "py_compile FAILED for $f - fix before continuing:`n$msg"
    } | ConvertTo-Json -Compress
}
exit 0
