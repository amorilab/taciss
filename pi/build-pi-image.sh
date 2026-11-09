#!/usr/bin/env bash
# Build a Raspberry Pi SD-card image with TACISS, starting from the official
# Raspberry Pi OS Lite (64-bit). Works on Pi 3, 4, 5, 400 and Zero 2 W.
#
# Run on Linux as root (GitHub Actions does this for every release):
#   sudo bash pi/build-pi-image.sh
#
# Options (environment variables):
#   PI_OS_URL=...     Image to start from (default: latest Raspberry Pi OS Lite arm64)
#   SOURCE_IMG=path   Use an already-downloaded .img or .img.xz instead (not checksum-checked)
#   TACISS_VERSION=x  Version for the file name (default: read from app/taciss.py)
#
# Output: dist/taciss-<version>-raspberrypi-arm64.img.xz (+ .sha256)
#
# TACISS itself is installed on the Pi's FIRST BOOT, not here, so every Pi gets
# its own mesh node ID and its own one-time setup code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
CACHE="$HERE/cache"
DIST="$ROOT/dist"
PI_OS_URL="${PI_OS_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
VERSION="${TACISS_VERSION:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/app/taciss.py")}"
WORK="$(mktemp -d)"
MNT="$WORK/root"
LOOP=""

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
cleanup() {
  set +e
  mountpoint -q "$MNT/boot/firmware" && umount "$MNT/boot/firmware"
  mountpoint -q "$MNT/boot" && umount "$MNT/boot"
  mountpoint -q "$MNT" && umount "$MNT"
  [[ -n "$LOOP" ]] && losetup -d "$LOOP"
  rm -rf "$WORK"
}
trap cleanup EXIT

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ -n "$VERSION" ]] || die "could not read VERSION from app/taciss.py"

say "Installing build tools"
need=()
for pair in xz:xz-utils curl:curl losetup:mount partx:util-linux sha256sum:coreutils; do
  command -v "${pair%%:*}" >/dev/null 2>&1 || need+=("${pair#*:}")
done
if ((${#need[@]})); then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${need[@]}" ca-certificates >/dev/null
fi

# ---------------------------------------------------------------- source image
mkdir -p "$CACHE" "$DIST"
if [[ -n "${SOURCE_IMG:-}" ]]; then
  [[ -f "$SOURCE_IMG" ]] || die "SOURCE_IMG not found: $SOURCE_IMG"
  SRC="$SOURCE_IMG"
  say "Using provided image $SRC (not checksum-checked)"
else
  say "Finding the latest Raspberry Pi OS Lite (64-bit)"
  FINAL=$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$PI_OS_URL")
  NAME=$(basename "$FINAL")
  [[ "$NAME" == *.img.xz ]] || die "unexpected download name: $NAME"
  SRC="$CACHE/$NAME"
  say "Downloading $NAME"
  curl -fL --retry 3 -C - -o "$SRC" "$FINAL" || curl -fL --retry 3 -o "$SRC" "$FINAL"
  say "Verifying checksum"
  curl -fsSL -o "$SRC.sha256" "$FINAL.sha256"
  (cd "$CACHE" && sha256sum -c "$NAME.sha256") || die "checksum mismatch; delete $SRC and retry"
fi

say "Unpacking"
IMG="$WORK/taciss.img"
case "$SRC" in
  *.xz) xz -dc -T0 "$SRC" > "$IMG" ;;
  *)    cp "$SRC" "$IMG" ;;
esac

# ---------------------------------------------------------------- mount
LOOP=$(losetup -f --show -P "$IMG")
partx -u "$LOOP" 2>/dev/null || true
command -v udevadm >/dev/null && udevadm settle || true
for _ in 1 2 3 4 5; do [[ -b "${LOOP}p2" ]] && break; sleep 1; done
[[ -b "${LOOP}p2" ]] || die "partitions of $LOOP did not appear"

mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
if [[ -d "$MNT/boot/firmware" ]]; then BOOT="$MNT/boot/firmware"; else BOOT="$MNT/boot"; fi
mount "${LOOP}p1" "$BOOT"
[[ -f "$MNT/etc/os-release" ]] || die "image has no /etc/os-release - is it Raspberry Pi OS?"
grep -q '^ID=debian\|^ID_LIKE=debian\|^ID=raspbian' "$MNT/etc/os-release" || die "image isn't Debian-based"
. "$MNT/etc/os-release"
say "Base: $PRETTY_NAME"

# ---------------------------------------------------------------- add TACISS
say "Adding TACISS $VERSION"
rm -rf "$MNT/opt/taciss-src"
mkdir -p "$MNT/opt/taciss-src"
cp -r "$ROOT/app" "$ROOT/deploy" "$ROOT/README.md" "$MNT/opt/taciss-src/"
find "$MNT/opt/taciss-src" -name '__pycache__' -prune -exec rm -rf {} +
touch "$MNT/opt/taciss-src/.firstboot"

install -m 0755 "$HERE/taciss-firstboot.sh" "$MNT/usr/local/sbin/taciss-firstboot"
install -m 0644 "$HERE/taciss-firstboot.service" "$MNT/etc/systemd/system/taciss-firstboot.service"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/taciss-firstboot.service "$MNT/etc/systemd/system/multi-user.target.wants/taciss-firstboot.service"

# Default hostname "taciss" -> http://taciss.local:8080. A name picked in Raspberry Pi Imager still wins.
echo taciss > "$MNT/etc/hostname"
sed -i 's/\braspberrypi\b/taciss/g' "$MNT/etc/hosts"

# Instructions you can read by putting the card in any computer
sed "s/@VERSION@/$VERSION/" "$HERE/TACISS-README.txt" | sed 's/$/\r/' > "$BOOT/TACISS-README.txt"

echo "TACISS $VERSION built $(date -u +%Y-%m-%dT%H:%MZ) from $PRETTY_NAME" > "$MNT/opt/taciss-src/BUILD-INFO"
df -h "$MNT" | tail -1 | awk '{print "    root filesystem: " $4 " free"}'

sync
umount "$BOOT"
umount "$MNT"
losetup -d "$LOOP"
LOOP=""

# ---------------------------------------------------------------- compress
OUT="$DIST/taciss-$VERSION-raspberrypi-arm64.img.xz"
say "Compressing to $OUT"
rm -f "$OUT" "$OUT.sha256"
xz -T0 -6 -c "$IMG" > "$OUT"
(cd "$DIST" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
ls -lh "$OUT"
cat "$OUT.sha256"
cat <<EOF

Write it to a microSD card (16 GB or larger) with Raspberry Pi Imager:
  "Choose OS" -> "Use custom" -> $(basename "$OUT")
Then put the card in the Pi and power it on.
EOF
