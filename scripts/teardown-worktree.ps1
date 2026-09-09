[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
& python -m forge.cli.main worktree teardown @Arguments
exit $LASTEXITCODE
