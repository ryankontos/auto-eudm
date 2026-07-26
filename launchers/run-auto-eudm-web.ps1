$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 "$Root\start_auto_eudm.py" @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$Root\start_auto_eudm.py" @args
} else {
    Write-Error "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/, then run this file again."
    exit 1
}
exit $LASTEXITCODE
