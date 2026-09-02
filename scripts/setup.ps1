[CmdletBinding()]
param(
    [ValidateSet('3.11', '3.12')]
    [string]$PythonVersion = '3.12'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    & py "-$PythonVersion" -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Python $PythonVersion virtual environment."
    }
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }

    & $venvPython -m pip install -e '.[telegram,mt5,webtrader,dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed.' }

    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium installation failed.' }

    & $venvPython -m tgxm --config config/settings.example.json validate-config
    if ($LASTEXITCODE -ne 0) { throw 'Installed project validation failed.' }
}
finally {
    Pop-Location
}

Write-Host "Ready: $venvPython"
