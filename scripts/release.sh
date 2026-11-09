#!/usr/bin/env bash
# Publish a new TACISS version from Linux/macOS:   bash scripts/release.sh 1.1.0
# Commits anything you've changed, sets the version, tags it and pushes to GitHub.
# GitHub Actions then tests it on Debian 13, builds the Raspberry Pi image and the
# installer ISO, and publishes them as a Release (about 30 minutes).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

V="${1:-}"; V="${V#v}"
[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 1.2.3" >&2; exit 1; }
[[ -z "$(git tag --list "v$V")" ]] || { echo "v$V already exists. Pick a higher number." >&2; exit 1; }
CUR=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app/taciss.py)
[[ "$(printf '%s\n%s\n' "$CUR" "$V" | sort -V | tail -n1)" == "$V" && "$V" != "$CUR" ]] \
  || { echo "v$V isn't higher than the current $CUR" >&2; exit 1; }

sed -i.bak "s/^VERSION = \".*\"/VERSION = \"$V\"/" app/taciss.py && rm -f app/taciss.py.bak
echo "==> TACISS $CUR -> $V"
git add -A
git status --short
git commit -m "Release v$V"
git tag -a "v$V" -m "TACISS $V"
git push origin HEAD "v$V"

REPO=$(git remote get-url origin | sed -E 's#.*github\.com[:/](.*)$#\1#; s#\.git$##')
echo
echo "==> Pushed. Watch the build:  https://github.com/$REPO/actions"
echo "    The release appears at:   https://github.com/$REPO/releases/tag/v$V"
