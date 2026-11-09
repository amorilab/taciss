# Publish a new TACISS version from Windows:
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 1.1.0
#
# Commits anything you've changed, sets the version, tags it and pushes to GitHub.
# GitHub Actions then tests it on Debian 13, builds the Raspberry Pi image and the
# installer ISO, and publishes them as a Release (about 30 minutes).
param([Parameter(Mandatory = $true)][string]$Version)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like 1.2.3" }
if (git tag --list "v$Version") { throw "v$Version already exists. Pick a higher number." }

$app = 'app/taciss.py'
$text = [IO.File]::ReadAllText($app)
$current = [regex]::Match($text, '(?m)^VERSION = "([^"]+)"').Groups[1].Value
if ([version]$Version -le [version]$current) { throw "v$Version isn't higher than the current $current" }
$text = [regex]::Replace($text, '(?m)^VERSION = "[^"]+"', "VERSION = `"$Version`"")
[IO.File]::WriteAllText((Resolve-Path $app), $text, (New-Object Text.UTF8Encoding $false))

Write-Host "==> TACISS $current -> $Version"
git add -A
git status --short
git commit -m "Release v$Version"
if ($LASTEXITCODE) { throw "git commit failed" }
git tag -a "v$Version" -m "TACISS $Version"
git push origin HEAD "v$Version"
if ($LASTEXITCODE) { throw "git push failed" }

$repo = (git remote get-url origin) -replace '.*github\.com[:/](.*?)(\.git)?$', '$1'
Write-Host ""
Write-Host "==> Pushed. Watch the build:  https://github.com/$repo/actions"
Write-Host "    The release appears at:   https://github.com/$repo/releases/tag/v$Version"
