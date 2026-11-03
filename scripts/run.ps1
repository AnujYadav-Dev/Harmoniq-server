<#
.SYNOPSIS
  Start harmoniq-server from the local .venv with tools\ on PATH.
.EXAMPLE
  scripts\run.ps1          # production-style, single process
  scripts\run.ps1 -Dev     # auto-reload + Swagger UI at /docs
#>
param([switch]$Dev)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { throw "No .venv found. Run scripts\setup.ps1 first." }

$env:PATH = (Join-Path $Root "tools") + ";" + $env:PATH
$argsList = @("-m", "app")
if ($Dev) { $argsList += "--dev" }
& $VenvPy @argsList
exit $LASTEXITCODE
