# Blink launcher (Windows PowerShell).
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RootDir = Split-Path -Parent $ScriptDir
Set-Location $RootDir

if (-not (Test-Path ".venv")) {
    Write-Host "[Blink] Creating virtual environment..."
    python -m venv .venv
}
& ".venv\Scripts\Activate.ps1"

Write-Host "[Blink] Installing dependencies..."
python -m pip install --upgrade pip | Out-Null
pip install -r requirements.txt

if ((Get-Command npm -ErrorAction SilentlyContinue) -and (-not (Test-Path "frontend\dist"))) {
    Write-Host "[Blink] Building frontend..."
    Push-Location frontend
    npm install
    npm run build
    Pop-Location
}

Write-Host "[Blink] Starting Blink..."
python main.py $args
