# AI Key Scout v5 - Windows installer
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "== AI Key Scout v5 - Windows installer ==" -ForegroundColor Cyan

function Test-Command($Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Test-Command "python")) {
    throw "Python 3 is required. Install Python 3.11+ and make sure 'python' is on PATH."
}

if (-not (Test-Command "git")) {
    Write-Warning "Git is not installed. It is not required to run the checked-out project, but is recommended for updates."
}

if (-not (Test-Command "cmake")) {
    Write-Host "[+] CMake was not found." -ForegroundColor Yellow
    $InstallCMake = Read-Host "Install CMake with winget now? [Y/n]"
    $InstallCMake = if ([string]::IsNullOrWhiteSpace($InstallCMake)) { "Y" } else { $InstallCMake }

    if ($InstallCMake -match "^[Yy]$") {
        if (Test-Command "winget") {
            winget install --id Kitware.CMake -e --accept-source-agreements --accept-package-agreements
        } else {
            Write-Warning "winget is not available. Install CMake manually from cmake.org/download/"
        }
    }
} else {
    Write-Host "[OK] CMake detected."
}

Write-Host ""
$UseVenv = Read-Host "Create and use a Python virtual environment (.venv)? [Y/n]"
$UseVenv = if ([string]::IsNullOrWhiteSpace($UseVenv)) { "Y" } else { $UseVenv }

if ($UseVenv -match "^[Yy]$") {
    Write-Host "[+] Creating .venv..."
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv." }
    $PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
} else {
    Write-Warning "Installing into the current Python environment."
    $PythonExe = (Get-Command python).Source
}

Write-Host "[+] Upgrading pip/setuptools/wheel..."
& $PythonExe -m pip install --upgrade pip setuptools wheel

Write-Host "[+] Installing runtime dependencies..."
& $PythonExe -m pip install PyQt6 requests PyYAML aiohttp

Write-Host "[+] Checking Python compilation..."
& $PythonExe -m compileall -q .
if ($LASTEXITCODE -ne 0) { throw "Python compilation check failed." }

Write-Host ""
Write-Host "[OK] AI Key Scout v5 is installed." -ForegroundColor Green
Write-Host "[+] Start with:"
if ($UseVenv -match "^[Yy]$") {
    Write-Host "    .\.venv\Scripts\Activate.ps1"
}
Write-Host ('    "' + $PythonExe + '" main.py')
