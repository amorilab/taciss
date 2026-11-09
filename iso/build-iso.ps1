# Build the TACISS installer ISO on Windows using Docker Desktop.
#   powershell -ExecutionPolicy Bypass -File iso\build-iso.ps1            # offline DVD flavor
#   powershell -ExecutionPolicy Bypass -File iso\build-iso.ps1 -Flavor netinst
param(
  [ValidateSet('dvd', 'netinst')][string]$Flavor = 'dvd'
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw 'Docker Desktop is required (https://www.docker.com/products/docker-desktop/). Or run iso/build-iso.sh on any Debian machine.'
}
$root = Split-Path -Parent $PSScriptRoot
docker run --rm -e "FLAVOR=$Flavor" -v "${root}:/src" debian:trixie bash /src/iso/build-iso.sh
if ($LASTEXITCODE -ne 0) { throw "ISO build failed (exit $LASTEXITCODE)" }
Write-Host "ISO written to $root\dist"
