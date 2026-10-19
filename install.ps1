# Delegate a built module folder or ZIP to the installed Core lifecycle.
[CmdletBinding()]
param(
    [string]$Root,
    [string]$Source = $PSScriptRoot,
    [switch]$Plan
)
$ErrorActionPreference = "Stop"
$core = Get-Command contextopus -CommandType Application -ErrorAction SilentlyContinue
if (-not $core) {
    [Console]::Error.WriteLine("Core 1.5+ is required. Install its starter first:")
    [Console]::Error.WriteLine("https://github.com/pikiby/contextopus-core/releases")
    exit 127
}
if (-not $PSBoundParameters.ContainsKey("Source") -and -not (Test-Path -LiteralPath (Join-Path $Source "module.json") -PathType Leaf)) {
    [Console]::Error.WriteLine("Run this installer from an extracted module release, or pass -Source to a built folder/ZIP.")
    [Console]::Error.WriteLine("From source, first run: python scripts/build-module.py /path/to/new/session_search")
    exit 2
}
$coreArguments = @()
if ($Root) { $coreArguments += @("--root", $Root) }
$coreArguments += @("modules", "install", "session_search", "--source", $Source)
if ($Plan) { $coreArguments += "--plan" }
& $core.Source @coreArguments
exit $LASTEXITCODE
