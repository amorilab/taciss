#!/bin/sh
# Runs at the end of the Debian install, with the new system mounted at /target.
set -e
log() { logger -t taciss "$*"; echo "taciss: $*"; }

log "copying TACISS sources"
rm -rf /target/opt/taciss-src
mkdir -p /target/opt/taciss-src
cp -r /cdrom/taciss/. /target/opt/taciss-src/

log "running TACISS installer in target"
in-target bash /opt/taciss-src/deploy/install.sh --no-start

# The DVD flavor installs with no network mirror. Add the standard Debian
# sources so `apt update` works later when the box does get internet.
# (Harmless while offline; skipped if the installer already set a mirror.)
if ! grep -rqsE '^[^#]*https?://' /target/etc/apt/sources.list /target/etc/apt/sources.list.d/; then
  log "adding Debian online package sources for later updates"
  sed -i 's/^deb cdrom:/# deb cdrom:/' /target/etc/apt/sources.list 2>/dev/null || true
  cat > /target/etc/apt/sources.list.d/debian.sources <<'EOF'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
fi

log "done"
exit 0
