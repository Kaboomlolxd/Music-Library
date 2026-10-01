param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]] $Source,
    [string] $Playlist,
    [string] $MinViews,
    [switch] $DryRun,
    [string] $TempDir,
    [string] $ManualUploadDir,
    [int] $MaxHeight = 1080
)

$arguments = @($Source)
if ($Playlist) { $arguments += @('--playlist', $Playlist) }
if ($MinViews) { $arguments += @('--min-views', $MinViews) }
if ($DryRun) { $arguments += '--dry-run' }
if ($TempDir) { $arguments += @('--temp-dir', $TempDir) }
if ($ManualUploadDir) { $arguments += @('--manual-upload-dir', $ManualUploadDir) }
if ($MaxHeight) { $arguments += @('--max-height', $MaxHeight) }

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
& $python (Join-Path $PSScriptRoot 'bili2yt.py') @arguments
exit $LASTEXITCODE
