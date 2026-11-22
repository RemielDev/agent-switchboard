param(
  [Parameter(Mandatory = $true)][ValidateSet('mail', 'discord', 'queue', 'house')][string]$Mode,
  [Parameter(Mandatory = $true)][string]$Root,
  [string]$PermissionMode = 'acceptEdits'
)
# Windows: keeps one switchboard process alive and logs it to <Root>\Queue. setup/autostart.py adds one
# HKCU Run entry per mode that starts this script hidden at logon; no administrator rights are needed.
$switchboard = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'switchboard'
$env:SWITCHBOARD_ROOT = $Root
$log = Join-Path $Root "Queue\worker-$Mode-$env:COMPUTERNAME.log"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { "$(Get-Date -Format o) python not found on PATH" | Add-Content $log; exit 1 }
$run = @{
  mail    = "`"$py`" -u -X utf8 `"$(Join-Path $switchboard 'mail-runner.py')`" --poll 10 --permission-mode $PermissionMode --mcp"
  discord = "`"$py`" -u -X utf8 `"$(Join-Path $switchboard 'discord-bridge.py')`" run"
  queue   = "`"$py`" -u -X utf8 `"$(Join-Path $switchboard 'queue-runner.py')`" --poll 60"
  house   = "`"$py`" -u -X utf8 `"$(Join-Path $switchboard 'housekeeper.py')`""
}[$Mode]
while ($true) {
  "$(Get-Date -Format o) start $Mode" | Add-Content $log
  try { cmd /c "$run >> `"$log`" 2>&1" } catch { "$(Get-Date -Format o) error $_" | Add-Content $log }
  if (Test-Path (Join-Path $Root 'Queue\STOP')) { Start-Sleep 300 } else { Start-Sleep 20 }
}
