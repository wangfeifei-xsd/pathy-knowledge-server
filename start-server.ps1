# PowerShell one-click launcher for pathy-knowledge-server
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Get-PythonLauncher {
    $candidates = @(
        @{ Label = "py -3"; Cmd = "py"; Args = @("-3") },
        @{ Label = "py"; Cmd = "py"; Args = @() },
        @{ Label = "python3"; Cmd = "python3"; Args = @() },
        @{ Label = "python"; Cmd = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        try {
            $out = & $c.Cmd @($c.Args + @("--version")) 2>&1
            if ($LASTEXITCODE -ne 0) { continue }
            $ver = ($out | Out-String).Trim()
            if ($ver -match "Microsoft Store" -or $ver -match "App Installer") { continue }
            return @{ Label = $c.Label; Cmd = $c.Cmd; Args = $c.Args; Version = $ver }
        } catch {
            continue
        }
    }
    return $null
}

function Configure-EmbeddedPythonPth {
    param(
        [string]$EmbedRoot
    )

    $pthFile = Get-ChildItem -Path $EmbedRoot -Filter "python*._pth" | Select-Object -First 1
    if (-not $pthFile) {
        throw "Embedded Python bootstrap failed: python*._pth not found."
    }

    $zipEntry = Get-ChildItem -Path $EmbedRoot -Filter "python*.zip" | Select-Object -First 1
    if ($zipEntry) {
        $zipLine = $zipEntry.Name
    } else {
        $zipLine = "python312.zip"
    }

    $pthLines = @(
        $zipLine,
        ".",
        "Lib",
        "Lib\site-packages",
        "import site"
    )
    Set-Content -Path $pthFile.FullName -Value ($pthLines -join "`r`n") -Encoding ASCII
}

function Test-EmbeddedPythonHasPip {
    param(
        [string]$PythonExe
    )
    & $PythonExe -c "import pip" *> $null
    return $LASTEXITCODE -eq 0
}

function Install-EmbeddedPythonPip {
    param(
        [string]$EmbedRoot,
        [string]$PythonExe
    )

    $getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
    $getPipPath = Join-Path $EmbedRoot "get-pip.py"

    Write-Host "==> Installing pip into embedded Python" -ForegroundColor Yellow
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPipPath
    & $PythonExe $getPipPath --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        throw "Embedded Python bootstrap failed while installing pip."
    }
    Remove-Item $getPipPath -Force -ErrorAction SilentlyContinue
}

function Initialize-EmbeddedPythonWindows {
    $isWindows = $env:OS -eq "Windows_NT"
    if (-not $isWindows) { return $null }

    $embedRoot = Join-Path $scriptDir ".python-embed"
    $embedPython = Join-Path $embedRoot "python.exe"
    if (Test-Path $embedPython) {
        Configure-EmbeddedPythonPth -EmbedRoot $embedRoot
        if (-not (Test-EmbeddedPythonHasPip -PythonExe $embedPython)) {
            Install-EmbeddedPythonPip -EmbedRoot $embedRoot -PythonExe $embedPython
        }
        $ver = (& $embedPython --version 2>&1 | Out-String).Trim()
        return @{ Label = "embedded-python"; Cmd = $embedPython; Args = @(); Kind = "embedded"; Version = $ver }
    }

    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
    if ($arch -ne "x64") {
        throw "Automatic embedded Python bootstrap currently supports Windows x64 only. Current architecture: $arch"
    }

    $pyVersion = "3.12.3"
    $zipUrl = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
    $zipPath = Join-Path $embedRoot "python-embed.zip"

    Write-Host "==> No system Python found, bootstrapping portable Python ($pyVersion)..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $embedRoot -Force | Out-Null

    Write-Host "==> Downloading embedded Python" -ForegroundColor Yellow
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

    Write-Host "==> Extracting embedded Python" -ForegroundColor Yellow
    Expand-Archive -Path $zipPath -DestinationPath $embedRoot -Force
    Remove-Item $zipPath -Force

    if (-not (Test-Path $embedPython)) {
        throw "Embedded Python bootstrap failed: python.exe not found after extract."
    }

    New-Item -ItemType Directory -Path (Join-Path $embedRoot "Lib") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $embedRoot "Lib\site-packages") -Force | Out-Null

    Configure-EmbeddedPythonPth -EmbedRoot $embedRoot
    if (-not (Test-EmbeddedPythonHasPip -PythonExe $embedPython)) {
        Install-EmbeddedPythonPip -EmbedRoot $embedRoot -PythonExe $embedPython
    }

    $ver = (& $embedPython --version 2>&1 | Out-String).Trim()
    return @{ Label = "embedded-python"; Cmd = $embedPython; Args = @(); Kind = "embedded"; Version = $ver }
}

function Invoke-Python {
    param(
        [hashtable]$Launcher,
        [string[]]$PythonArgs
    )
    & $Launcher.Cmd @($Launcher.Args + $PythonArgs)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $($Launcher.Label) $($PythonArgs -join ' ')"
    }
}

Write-Host "==> Starting pathy-knowledge-server on port $Port" -ForegroundColor Cyan

$py = Get-PythonLauncher
if (-not $py) {
    $py = Initialize-EmbeddedPythonWindows
}
if (-not $py) {
    Write-Host ""
    Write-Host "No usable Python found and automatic bootstrap is unavailable on this platform." -ForegroundColor Red
    Write-Host "Install Python 3.10+ manually, then rerun this script." -ForegroundColor Yellow
    throw "No usable Python."
}

Write-Host "==> Using $($py.Label): $($py.Version)" -ForegroundColor Green

if (-not $py.Kind -or $py.Kind -ne "embedded") {
    if (-not (Test-Path ".\.venv")) {
        Write-Host "==> Creating virtual environment (.venv)" -ForegroundColor Yellow
        try {
            Invoke-Python -Launcher $py -PythonArgs @("-m", "venv", ".venv")
        } catch {
            Write-Host ""
            Write-Host "venv failed. Try manually:" -ForegroundColor Yellow
            Write-Host "  $($py.Label) -m venv .venv"
            Write-Host "If that fails, reinstall Python and enable 'venv' / pip in the installer." -ForegroundColor Yellow
            throw
        }
    }

    $venvPython = $null
    if (Test-Path ".\.venv\Scripts\python.exe") {
        $venvPython = ".\.venv\Scripts\python.exe"
    } elseif (Test-Path ".\.venv\bin\python") {
        $venvPython = ".\.venv\bin\python"
    }
    if (-not $venvPython) {
        throw "Venv Python not found under .venv. Delete the .venv folder and run this script again."
    }
} else {
    Write-Host "==> Using project-local embedded Python runtime (no global install needed)" -ForegroundColor Green
    $venvPython = $py.Cmd
}

Write-Host "==> Installing dependencies" -ForegroundColor Yellow
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed. Check the error output above."
}

Write-Host "==> Launching uvicorn" -ForegroundColor Green
Write-Host "    Swagger: http://127.0.0.1:$Port/docs"
Write-Host "    Health : http://127.0.0.1:$Port/health"

& $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port $Port
