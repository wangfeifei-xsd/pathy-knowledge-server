# PowerShell one-click launcher for pathy-knowledge-server
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "==> Starting pathy-knowledge-server on port $Port" -ForegroundColor Cyan

if (-not (Test-Path ".\.venv")) {
    Write-Host "==> Creating virtual environment (.venv)" -ForegroundColor Yellow
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "创建 .venv 失败。请确认 Python 可用（建议 `python --version` / `py --version`）。"
    }
}

$venvPython = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } elseif (Test-Path ".\.venv\bin\python") { ".\.venv\bin\python" } else { $null }
if (-not $venvPython) {
    throw "未找到虚拟环境 Python：.venv\Scripts\python.exe（或 .venv/bin/python）。请删除 .venv 后重试。"
}

Write-Host "==> Installing dependencies" -ForegroundColor Yellow
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "依赖安装失败，请检查上方错误输出。"
}

Write-Host "==> Launching uvicorn" -ForegroundColor Green
Write-Host "    Swagger: http://127.0.0.1:$Port/docs"
Write-Host "    Health : http://127.0.0.1:$Port/health"

& $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port $Port
