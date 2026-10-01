[CmdletBinding()]
param(
    [switch] $OpenFolder
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
$extension = Join-Path $root "extension"
$manifest = Join-Path $extension "manifest.json"
$output = Join-Path $root "dist"
$firefoxStaging = Join-Path $output ".firefox-staging"

if (-not (Test-Path -LiteralPath $manifest)) {
    throw "Extension manifest not found: $manifest"
}

try {
    $metadata = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
} catch {
    throw "extension\manifest.json is not valid JSON: $($_.Exception.Message)"
}

if ($metadata.manifest_version -ne 3 -or -not $metadata.version) {
    throw "The manifest must be Manifest V3 and include a version."
}

foreach ($size in @("16", "32", "48", "128")) {
    $iconPath = Join-Path $extension ("icons\icon-{0}.png" -f $size)
    if (-not (Test-Path -LiteralPath $iconPath)) {
        throw "Missing extension icon: $iconPath"
    }
}

New-Item -ItemType Directory -Force -Path $output | Out-Null
# Keep release artifacts unambiguous. Only the current build belongs in dist.
Get-ChildItem -LiteralPath $output -Force | Where-Object { $_.Name -ne ".gitkeep" } | Remove-Item -Recurse -Force
$safeVersion = ([string] $metadata.version) -replace "[^0-9A-Za-z._-]", "_"
$baseName = "local-music-library-player-$safeVersion"
$chromeZip = Join-Path $output "$baseName-chrome.zip"
$firefoxXpi = Join-Path $output "$baseName-firefox-zen.xpi"
$firefoxZip = Join-Path $output "$baseName-firefox-zen.zip"

Remove-Item -LiteralPath $chromeZip -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $firefoxXpi -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $firefoxZip -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $firefoxStaging -Recurse -Force -ErrorAction SilentlyContinue

# Chrome uses Manifest V3's service worker. Firefox uses its scripts fallback
# until MV3 background service workers are supported there, so stage a Firefox
# manifest rather than making either browser ignore a background declaration.
& $python (Join-Path $root "tools\package_zip.py") $extension $chromeZip
Copy-Item -LiteralPath $extension -Destination $firefoxStaging -Recurse
$firefoxManifest = Join-Path $firefoxStaging "manifest.json"
$firefoxMetadata = Get-Content -LiteralPath $firefoxManifest -Raw | ConvertFrom-Json
$firefoxMetadata.background = [PSCustomObject]@{
    scripts = @("background.js")
    type = "module"
}
$firefoxMetadata.browser_specific_settings.gecko.strict_min_version = "142.0"
$firefoxMetadata.browser_specific_settings.gecko | Add-Member -NotePropertyName data_collection_permissions -NotePropertyValue ([PSCustomObject]@{
    required = @("none")
}) -Force
$firefoxMetadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $firefoxManifest -Encoding UTF8
& $python (Join-Path $root "tools\package_zip.py") $firefoxStaging $firefoxZip
Move-Item -LiteralPath $firefoxZip -Destination $firefoxXpi -Force
Remove-Item -LiteralPath $firefoxStaging -Recurse -Force

Write-Host ""
Write-Host "Extension packages created:" -ForegroundColor Green
Write-Host "  Chrome / Chromium: $chromeZip"
Write-Host "  Zen / Firefox:     $firefoxXpi"
Write-Host ""
Write-Host "Chrome / Chromium: chrome://extensions → Developer mode → Load unpacked → select the extension folder." -ForegroundColor Cyan
Write-Host "Zen / Firefox: about:debugging#/runtime/this-firefox → Load Temporary Add-on → select the .xpi (or manifest.json)." -ForegroundColor Cyan
Write-Host "Important: do not use Zen/Firefox's normal Install Add-on From File route; it rejects unsigned local XPIs as corrupt." -ForegroundColor Yellow
Write-Host "A self-distributed Firefox .xpi still needs Mozilla signing for permanent installation; temporary loading works immediately." -ForegroundColor Yellow

if ($OpenFolder) {
    Start-Process explorer.exe -ArgumentList $output
}
