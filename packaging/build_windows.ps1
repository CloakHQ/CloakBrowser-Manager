# Build the native Windows CloakBrowser Manager: freeze -> Inno Setup installer.
# Unsigned (SmartScreen click-through) for the test. CI-ready.
#
# Requires: Python 3.10+, Node 18+, and Inno Setup 6 (iscc.exe on PATH or at the
# default install location). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$AppName = "CloakBrowser Manager"
# Version: explicit $env:CB_VERSION wins (CI / transferred source with no usable
# .git); otherwise git describe. Never let a git hiccup abort the build
# (ErrorAction=Stop would otherwise treat git's stderr as fatal).
$Version = "dev"
if ($env:CB_VERSION) {
  # cmd's `set VAR=x & cmd2` leaves a trailing space in VAR — trim it so the
  # installer filename isn't "…-g9dd49cf -setup.exe".
  $Version = $env:CB_VERSION.Trim()
} else {
  try {
    $v = (& git describe --tags --always 2>$null)
    if ($v) { $Version = "$v".Trim() }
  } catch { }
}
$Dist = Join-Path $Root "dist_native"
$Build = Join-Path $Root "build_native"
$BuildVenv = if ($env:PACKAGING_VENV) { $env:PACKAGING_VENV } else { Join-Path $Root ".venv-build" }

Write-Host "[build] CloakBrowser Manager $Version (Windows)"

# Bake the version into the bundle so the frozen app can log its own build
# (no .git at runtime). manager.spec ships backend/version.txt as data.
[IO.File]::WriteAllText((Join-Path $Root "backend/version.txt"), $Version)

# 1. Frontend.
Write-Host "[build] frontend"
Push-Location (Join-Path $Root "frontend")
npm ci
npm run build
Pop-Location

# 2. Build venv with runtime + build deps.
$VenvPy = Join-Path $BuildVenv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
  Write-Host "[build] creating build venv at $BuildVenv"
  python -m venv $BuildVenv
}
& $VenvPy -m pip install --disable-pip-version-check -q `
  -r (Join-Path $Root "backend\requirements.txt") `
  -r (Join-Path $Root "packaging\requirements-build.txt")

# 3. Freeze.
Write-Host "[build] pyinstaller"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
& $VenvPy -m PyInstaller --noconfirm --clean `
  --distpath $Dist --workpath $Build `
  (Join-Path $Root "packaging\manager.spec")

$AppDir = Join-Path $Dist $AppName
if (-not (Test-Path $AppDir)) { throw "[error] $AppDir not produced" }

# 4. Compile the Inno Setup installer.
$Iscc = "iscc.exe"
if (-not (Get-Command $Iscc -ErrorAction SilentlyContinue)) {
  $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
  if (-not (Test-Path $Iscc)) { throw "[error] Inno Setup (ISCC.exe) not found on PATH or default location" }
}
Write-Host "[build] Inno Setup"
& $Iscc "/DAppVersion=$Version" "/DSourceDir=$AppDir" (Join-Path $Root "packaging\installer.iss")

Write-Host "[done] $Dist\CloakBrowser-Manager-$Version-setup.exe"
