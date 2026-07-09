#!/usr/bin/env pwsh
# dashboard.ps1 — thin PowerShell shim (path resolution only). Prefers the COLLAB_KIT_ROOT
# install-time override, else walks up for the collab-kit root signature ([C17]), then execs
# the Python core. No logic lives here ([C14]/[C37]).
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = if ($env:COLLAB_KIT_ROOT) { $env:COLLAB_KIT_ROOT } else {
  $d = $dir
  while ($d -and -not (Test-Path (Join-Path $d '.collab-kit')) -and
         -not ((Test-Path (Join-Path $d 'tools')) -and (Test-Path (Join-Path $d 'install.sh')))) {
    $d = Split-Path -Parent $d
  }
  $d
}
$py = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { 'python3' }
& $py (Join-Path $root 'tools/lib/dashboard.py') @args
exit $LASTEXITCODE
