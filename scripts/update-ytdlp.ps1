<#
.SYNOPSIS
  Update yt-dlp (and its matching yt-dlp-ejs) inside .venv. Restart the server afterwards.
.EXAMPLE
  scripts\update-ytdlp.ps1                       # latest stable
  scripts\update-ytdlp.ps1 -Version 2026.10.26   # pin a version
  scripts\update-ytdlp.ps1 -Nightly              # latest nightly (pre-release)
#>
param([string]$Version = "", [switch]$Nightly)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { throw "No .venv found. Run scripts\setup.ps1 first." }

if ($Version) {
    & $VenvPy -m pip install --upgrade "yt-dlp[default]==$Version"
} elseif ($Nightly) {
    & $VenvPy -m pip install --upgrade --pre "yt-dlp[default]"
} else {
    & $VenvPy -m pip install --upgrade "yt-dlp[default]"
}
if ($LASTEXITCODE -ne 0) { throw "pip failed" }
& $VenvPy -c "import yt_dlp; print('yt-dlp now', yt_dlp.version.__version__)"
Write-Host "Restart the server to use the new version." -ForegroundColor Yellow
