param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m clipforge @ScriptArgs

