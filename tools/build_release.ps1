[CmdletBinding()]
param([string]$Output = "dist")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content (Join-Path $root "extension\manifest.json") -Raw | ConvertFrom-Json
$version = [string]$manifest.version
$outputDir = Join-Path $root $Output
$stage = Join-Path $env:TEMP "local-music-library-release-$version"
$archive = Join-Path $outputDir "local-music-library-$version-windows.zip"

Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage, $outputDir | Out-Null

$files = @(
  "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md", "SUPPORT.md",
  "pyproject.toml", "requirements.txt", "setup.ps1", "MusicLibrary.ps1",
  "MusicLibrary.bat", "Bili2YouTube.ps1", "Bili2YouTube.bat", "bili2yt.py",
  "run.ps1", "PackMusicLibraryExtension.ps1", "PackMusicLibraryExtension.bat",
  "tools\build_release.ps1", "tools\release_check.py", "tools\package_zip.py"
)
foreach ($relative in $files) {
  $source = Join-Path $root $relative
  if (-not (Test-Path -LiteralPath $source)) { throw "Missing release file: $relative" }
  $destination = Join-Path $stage $relative
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
  Copy-Item -LiteralPath $source -Destination $destination -Force
}
Copy-Item -LiteralPath (Join-Path $root "music_library") -Destination (Join-Path $stage "music_library") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root "extension") -Destination (Join-Path $stage "extension") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $stage "tools") | Out-Null
Copy-Item -LiteralPath (Join-Path $root "tools\release_check.py") -Destination (Join-Path $stage "tools\release_check.py") -Force
Copy-Item -LiteralPath (Join-Path $root "tools\package_zip.py") -Destination (Join-Path $stage "tools\package_zip.py") -Force
Get-ChildItem -LiteralPath $stage -Recurse -Directory -Force -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stage -Recurse -File -Force | Where-Object { $_.Extension -in @(".pyc", ".pyo") } | Remove-Item -Force

Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$archive.sha256" -Value "$hash  $(Split-Path -Leaf $archive)" -Encoding ascii
Remove-Item -LiteralPath $stage -Recurse -Force
Write-Host "Desktop release created: $archive"
Write-Host "SHA-256: $hash"
