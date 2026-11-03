<#
.SYNOPSIS
  One-time (and re-runnable) setup for local mode on Windows.

.DESCRIPTION
  Creates .venv from your installed Python, installs all libraries into it, downloads FFmpeg,
  ffprobe and Deno into tools\, and creates data\, secrets\ and .env (with a fresh app token).
  Nothing is installed system-wide.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Python "C:\Python313\python.exe"
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -UpdateTools
#>
param(
    [string]$Python = "",
    [switch]$UpdateTools
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Tools = Join-Path $Root "tools"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# 1. Find a Python >= 3.11 -------------------------------------------------------------------------
function Test-Python($cmd) {
    try {
        $out = & $cmd[0] @($cmd[1..($cmd.Length)] | Where-Object { $_ }) -c "import sys; print(int(sys.version_info >= (3, 11)))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "1")
    } catch { return $false }
}

$candidates = @()
if ($Python) { $candidates += ,@($Python) }
$candidates += ,@("py", "-3.13")
$candidates += ,@("py", "-3")
$candidates += ,@("python")
$PyCmd = $null
foreach ($c in $candidates) { if (Test-Python $c) { $PyCmd = $c; break } }
if (-not $PyCmd) {
    throw "No Python 3.11+ found. Install Python 3.13 from python.org (tick 'py launcher'), or pass -Python <path>."
}
Step "Using Python: $($PyCmd -join ' ')"

# 2. Virtual environment + libraries ---------------------------------------------------------------
if (-not (Test-Path $VenvPy)) {
    Step "Creating .venv"
    & $PyCmd[0] @($PyCmd[1..($PyCmd.Length)] | Where-Object { $_ }) -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Step "Installing Python libraries into .venv"
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $VenvPy -m pip install --quiet --upgrade "yt-dlp[default]"
if ($LASTEXITCODE -ne 0) { throw "yt-dlp install failed" }

# 3. Tools -----------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Tools | Out-Null
$tmp = Join-Path $env:TEMP ("harmoniq-setup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    if ($UpdateTools -or -not (Test-Path (Join-Path $Tools "ffmpeg.exe")) -or -not (Test-Path (Join-Path $Tools "ffprobe.exe"))) {
        Step "Downloading FFmpeg (yt-dlp/FFmpeg-Builds, win64 GPL)"
        $zip = Join-Path $tmp "ffmpeg.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "ffmpeg") -Force
        Get-ChildItem -Path (Join-Path $tmp "ffmpeg") -Recurse -Include "ffmpeg.exe", "ffprobe.exe" |
            ForEach-Object { Copy-Item $_.FullName -Destination $Tools -Force }
    }
    if ($UpdateTools -or -not (Test-Path (Join-Path $Tools "deno.exe"))) {
        Step "Downloading Deno"
        $zip = Join-Path $tmp "deno.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $Tools -Force
    }
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# 4. Folders and .env ------------------------------------------------------------------------------
foreach ($d in "data", "secrets") { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Step "Creating .env with a new app token"
    $token = & $VenvPy -c "import secrets; print('hq_' + secrets.token_hex(32))"
    (Get-Content (Join-Path $Root ".env.example")) -replace '^APP_TOKENS=$', "APP_TOKENS=app-v1:$token" |
        Set-Content -Encoding utf8 $envFile
    Write-Host "    App token (put this in the Android build later): $token" -ForegroundColor Yellow
}

# 5. Self-check ------------------------------------------------------------------------------------
Step "Installed versions"
& $VenvPy -c "import sys, yt_dlp; print('python ', sys.version.split()[0]); print('yt-dlp ', yt_dlp.version.__version__)"
& (Join-Path $Tools "ffmpeg.exe") -hide_banner -version | Select-Object -First 1
& (Join-Path $Tools "deno.exe") --version | Select-Object -First 1
Write-Host ""
Write-Host "Setup complete. Start the server with:  scripts\run.ps1" -ForegroundColor Green
