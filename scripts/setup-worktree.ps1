[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
& python -m forge.cli.main worktree setup @Arguments
exit $LASTEXITCODE
