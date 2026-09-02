[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TgxmArguments
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Virtual environment is missing. Run scripts/setup.ps1 first.'
}

Push-Location $projectRoot
try {
    & $venvPython -m tgxm @TgxmArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
