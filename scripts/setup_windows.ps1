# Creates the two virtual environments outside OneDrive and installs the exact versions.
# Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File ".\scripts\setup_windows.ps1"
$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$venvs   = Join-Path $env:USERPROFILE ".venvs"
$main    = Join-Path $venvs "doc-update"
$sandbox = Join-Path $venvs "doc-update-sandbox"

# Python 3.11 or later through the Windows launcher
$py = "py"
& $py -3 -c "import sys; assert sys.version_info >= (3, 11), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or later is required (install it from python.org)" }

New-Item -ItemType Directory -Force -Path $venvs | Out-Null
if (-not (Test-Path (Join-Path $main "Scripts\python.exe")))    { & $py -3 -m venv "$main" }
if (-not (Test-Path (Join-Path $sandbox "Scripts\python.exe"))) { & $py -3 -m venv "$sandbox" }

& "$main\Scripts\python.exe" -m pip install --upgrade pip
& "$main\Scripts\python.exe" -m pip install -r "$project\requirements.txt"
& "$sandbox\Scripts\python.exe" -m pip install --upgrade pip
& "$sandbox\Scripts\python.exe" -m pip install -r "$project\requirements-sandbox.txt"

Push-Location "$project"
try {
    & "$main\Scripts\python.exe" -m doc_update check-setup
} finally {
    Pop-Location
}
Write-Host "Done. Activate with:  & `"$main\Scripts\Activate.ps1`""
