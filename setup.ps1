$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')

Write-Host ''
Write-Host 'Setup finished. Start the local metadata-only library with:'
Write-Host '  .\MusicLibrary.ps1'
Write-Host ''
Write-Host 'The older Bili2YouTube relay remains available but needs ffmpeg, agent-browser, and (for RAM mode) ImDisk.'
