[CmdletBinding()]
param(
    [string]$DataDir,
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

$arguments = @("-m", "music_library", "--port", $Port)
if ($DataDir) {
    $arguments += @("--data-dir", $DataDir)
}
if ($NoBrowser) {
    $arguments += "--no-browser"
}

& $python @arguments
exit $LASTEXITCODE
