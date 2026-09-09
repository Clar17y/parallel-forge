[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "dev.py"
$python = "python"
if (Test-Path (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")) {
    $python = (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe")
}
& $python $scriptPath @Arguments
exit $LASTEXITCODE
