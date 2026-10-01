[CmdletBinding()]
param(
    # These switches keep the launcher useful from PowerShell too, while the
    # no-argument form is intended for double-clicking Bili2YouTube.bat.
    [string] $Source,
    [string] $Playlist,
    [string] $MinViews,
    [switch] $All,
    [switch] $DryRun,
    [int] $MaxItems,
    [switch] $YouTubePopular,
    [switch] $Headless,
    [switch] $Force,
    [switch] $NoState,
    [string] $CookiesFromBrowser = 'zen',
    [string] $ManualUploadDir
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Read-RequiredValue([string] $Prompt) {
    while ($true) {
        $value = (Read-Host $Prompt).Trim()
        if ($value) {
            return $value
        }
        Write-Host 'Please enter a value.' -ForegroundColor Yellow
    }
}

if (-not $Source) {
    Write-Host ''
    Write-Host 'Bili2YouTube - bulk relay to an unlisted YouTube playlist' -ForegroundColor Cyan
    Write-Host 'Paste a Bilibili or YouTube video/list/channel/playlist URL.'
    $Source = Read-RequiredValue 'Source URL'
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Write-Error "Python was not found. Run setup.ps1 in this folder first."
        exit 1
    }
    $python = $pythonCommand.Source
    Write-Warning 'The project .venv was not found; using Python from PATH.'
}

# Keep the normal interactive questions enabled by leaving --playlist,
# --all, and --min-views out unless the caller supplied their equivalents.
$arguments = @(
    $Source,
    '--max-height', '1080',
    '--cookies-from-browser', $CookiesFromBrowser,
    '--request-delay', '2'
)

if ($Playlist) { $arguments += @('--playlist', $Playlist) }
if ($MinViews) { $arguments += @('--min-views', $MinViews) }
if ($All) { $arguments += '--all' }
if ($DryRun) { $arguments += '--dry-run' }
if ($MaxItems -gt 0) { $arguments += @('--max-items', $MaxItems) }
if ($YouTubePopular) { $arguments += '--youtube-popular' }
if ($Headless) { $arguments += '--headless' }
if ($Force) { $arguments += '--force' }
if ($NoState) { $arguments += '--no-state' }
if ($ManualUploadDir) { $arguments += @('--manual-upload-dir', $ManualUploadDir) }

Write-Host ''
Write-Host 'Starting with 1080p video, best available audio, and RAM-backed temporary media.' -ForegroundColor DarkCyan
Write-Host 'The Python program will now ask for the playlist and view-count cutoff.' -ForegroundColor DarkCyan
Write-Host ''

& $python (Join-Path $projectRoot 'bili2yt.py') @arguments
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host ''
    Write-Host "The import ended with exit code $exitCode." -ForegroundColor Red
}
exit $exitCode
