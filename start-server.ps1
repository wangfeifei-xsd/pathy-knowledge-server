# PowerShell one-click launcher for pathy-knowledge-server
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

Write-Host "==> Starting pathy-knowledge-server on port $Port" -ForegroundColor Cyan

if (-not (Test-Path ".\.venv")) {
    Write-Host "==> Creating virtual environment (.venv)" -ForegroundColor Yellow
    python -m venv .venv
}

Write-Host "==> Activating virtual environment" -ForegroundColor Yellow
& ".\.venv\Scripts\Activate.ps1"

Write-Host "==> Installing dependencies" -ForegroundColor Yellow
python -m pip install -r requirements.txt

Write-Host "==> Launching uvicorn" -ForegroundColor Green
Write-Host "    Swagger: http://127.0.0.1:$Port/docs"
Write-Host "    Health : http://127.0.0.1:$Port/health"

uvicorn app.main:app --host 0.0.0.0 --port $Port
