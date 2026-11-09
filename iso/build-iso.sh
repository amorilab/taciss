#!/usr/bin/env bash
# Build a TACISS installer ISO from the official Debian 13 (trixie) amd64 image.
#
# Run on Debian/Ubuntu as root, or anywhere with Docker:
#   sudo bash iso/build-iso.sh
#   docker run --rm -v "$PWD":/src debian:trixie bash /src/iso/build-iso.sh
#
# Options (environment variables):
#   FLAVOR=dvd       (default) Debian DVD-1, ~4 GB. Installs with NO internet.
#   FLAVOR=netinst   ~800 MB. Needs internet during install to fetch packages.
#   SOURCE_ISO=path  Use an already-downloaded Debian ISO instead of downloading.
#   DEBIAN_URL=...   Directory to download from (default: cdimage.debian.org "current").
#   BOOT_TIMEOUT=15  Seconds before the TACISS boot entry starts automatically.
#
# Output: dist/taciss-<debian version>-<flavor>-amd64.iso (+ .sha256)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
FLAVOR="${FLAVOR:-dvd}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-15}"
CACHE="$HERE/cache"
DIST="$ROOT/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

case "$FLAVOR" in
  dvd)     SUBDIR=iso-dvd; PATTERN='-amd64-DVD-1\.iso$' ;;
  netinst) SUBDIR=iso-cd;  PATTERN='-amd64-netinst\.iso$' ;;
  *) die "FLAVOR must be dvd or netinst" ;;
esac
DEBIAN_URL="${DEBIAN_URL:-https://cdimage.debian.org/debian-cd/current/amd64/$SUBDIR}"

[[ $EUID -eq 0 ]] || die "run as root (or inside the debian:trixie Docker container)"

say "Installing build tools"
need=()
for pair in xorriso:xorriso wget:wget gpgv:gpgv sha256sum:coreutils md5sum:coreutils; do
  command -v "${pair%%:*}" >/dev/null 2>&1 || need+=("${pair#*:}")
done
[[ -f /usr/share/keyrings/debian-role-keys.gpg ]] || need+=(debian-keyring)
if ((${#need[@]})); then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${need[@]}" ca-certificates >/dev/null
fi

# ---------------------------------------------------------------- source ISO
mkdir -p "$CACHE" "$DIST"
if [[ -n "${SOURCE_ISO:-}" ]]; then
  [[ -f "$SOURCE_ISO" ]] || die "SOURCE_ISO not found: $SOURCE_ISO"
  SRC="$SOURCE_ISO"
  say "Using provided ISO $SRC (not signature-checked)"
else
  say "Fetching checksums from $DEBIAN_URL"
  wget -q -O "$CACHE/SHA256SUMS" "$DEBIAN_URL/SHA256SUMS"
  wget -q -O "$CACHE/SHA256SUMS.sign" "$DEBIAN_URL/SHA256SUMS.sign"
  gpgv --keyring /usr/share/keyrings/debian-role-keys.gpg "$CACHE/SHA256SUMS.sign" "$CACHE/SHA256SUMS" \
    || die "SHA256SUMS signature check failed"
  NAME=$(awk '{print $2}' "$CACHE/SHA256SUMS" | grep -E -e "$PATTERN" | grep -v -e edu -e mac | head -n1)
  [[ -n "$NAME" ]] || die "no $FLAVOR image listed at $DEBIAN_URL"
  [[ "$NAME" == debian-13.* ]] || die "$NAME is not Debian 13. Set DEBIAN_URL to a 13.x archive, e.g. https://cdimage.debian.org/cdimage/archive/13.2.0/amd64/$SUBDIR"
  SRC="$CACHE/$NAME"
  say "Downloading $NAME (resumes if partially downloaded)"
  wget -c -q --show-progress -O "$SRC" "$DEBIAN_URL/$NAME"
  say "Verifying checksum"
  (cd "$CACHE" && grep " $NAME\$" SHA256SUMS | sha256sum -c -) || die "checksum mismatch; delete $SRC and retry"
fi
BASENAME=$(basename "$SRC")
DEBVER=$(echo "$BASENAME" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)
DEBVER=${DEBVER:-13}

iso_has() { xorriso -indev "$SRC" -find "$1" 2>/dev/null | grep -q .; }
iso_get() { xorriso -osirrox on -indev "$SRC" -extract "$1" "$2" >/dev/null 2>&1; chmod -R u+w "$2"; }

say "Inspecting $BASENAME"
iso_has /install.amd/vmlinuz   || die "this ISO has no /install.amd/vmlinuz (is it an amd64 Debian installer image?)"
iso_has /install.amd/initrd.gz || die "this ISO has no /install.amd/initrd.gz"
iso_has /boot/grub/grub.cfg    || die "this ISO has no /boot/grub/grub.cfg"

KARGS="auto=true priority=high preseed/file=/cdrom/taciss/iso/preseed.cfg"
LABEL="TACISS server - automated install (ERASES a disk)"

# ------------------------------------------------------------ stage payload
say "Staging TACISS files"
STAGE="$WORK/taciss"
mkdir -p "$STAGE"
cp -r "$ROOT/app" "$ROOT/deploy" "$STAGE/"
mkdir -p "$STAGE/iso"
cp "$HERE/preseed.cfg" "$HERE/pick-disk.sh" "$HERE/late-command.sh" "$STAGE/iso/"
cp "$ROOT/README.md" "$STAGE/"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +
# Normalize line endings in case the repo was checked out on Windows
find "$STAGE" -type f \( -name '*.sh' -o -name '*.cfg' -o -name '*.py' -o -name '*.service' \
  -o -name '*.timer' -o -name '*.env' -o -name 'taciss-cli' -o -name 'taciss-issue' \) -exec sed -i 's/\r$//' {} +

if [[ "$FLAVOR" == netinst ]]; then
  cat >> "$STAGE/iso/preseed.cfg" <<'EOF'

### netinst flavor: packages come from the internet
d-i apt-setup/use_mirror boolean true
# apt-setup reads this answer even when DHCP worked; "Do not configure" makes it
# skip the mirror, leaving a netinst with nothing to install software from.
d-i netcfg/dhcp_options select Retry network autoconfiguration
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i mirror/protocol string http
d-i mirror/http/mirror select deb.debian.org
d-i mirror/suite string trixie
d-i apt-setup/services-select multiselect security, updates
d-i apt-setup/security_host string security.debian.org
EOF
fi

MAPS=(-map "$STAGE" /taciss)
CHANGED=()

# ---------------------------------------------------------- UEFI: GRUB menu
say "Adding TACISS entry to the GRUB (UEFI) menu"
iso_get /boot/grub/grub.cfg "$WORK/grub.cfg"
awk -v label="$LABEL" -v kargs="$KARGS" -v t="$BOOT_TIMEOUT" -v q="'" '
  !done && /^menuentry/ {
    print "set default=0"
    print "set timeout=" t
    print "menuentry --hotkey=t " q label q " {"
    print "    set background_color=black"
    print "    linux    /install.amd/vmlinuz " kargs " vga=788 --- quiet"
    print "    initrd   /install.amd/initrd.gz"
    print "}"
    done = 1
  }
  { print }
' "$WORK/grub.cfg" > "$WORK/grub.cfg.new"
grep -q "TACISS server" "$WORK/grub.cfg.new" || die "could not find a menuentry in grub.cfg to insert before"
mv "$WORK/grub.cfg.new" "$WORK/grub.cfg"
MAPS+=(-map "$WORK/grub.cfg" /boot/grub/grub.cfg)
CHANGED+=(./boot/grub/grub.cfg:"$WORK/grub.cfg")

# -------------------------------------------------- BIOS: ISOLINUX menu
if iso_has /isolinux/menu.cfg; then
  say "Adding TACISS entry to the ISOLINUX (BIOS) menu"
  iso_get /isolinux "$WORK/isolinux"
  cat > "$WORK/isolinux/taciss.cfg" <<EOF
label taciss
	menu label ^$LABEL
	menu default
	kernel /install.amd/vmlinuz
	append $KARGS vga=788 initrd=/install.amd/initrd.gz --- quiet
EOF
  # our entry first; drop "menu default" from the stock entries so ours is the default
  sed -i '0,/^include /s//include taciss.cfg\ninclude /' "$WORK/isolinux/menu.cfg"
  for f in gtk.cfg txt.cfg; do
    [[ -f "$WORK/isolinux/$f" ]] && sed -i '/^\s*menu default\s*$/d' "$WORK/isolinux/$f"
  done
  sed -i "s/^timeout .*/timeout $((BOOT_TIMEOUT * 10))/" "$WORK/isolinux/isolinux.cfg"
  grep -q '^timeout' "$WORK/isolinux/isolinux.cfg" || echo "timeout $((BOOT_TIMEOUT * 10))" >> "$WORK/isolinux/isolinux.cfg"
  for f in taciss.cfg menu.cfg gtk.cfg txt.cfg isolinux.cfg; do
    [[ -f "$WORK/isolinux/$f" ]] || continue
    MAPS+=(-map "$WORK/isolinux/$f" "/isolinux/$f")
    CHANGED+=(./isolinux/$f:"$WORK/isolinux/$f")
  done
else
  say "No ISOLINUX menu on this image (UEFI-only boot); skipping BIOS menu"
fi

# --------------------------------------------------------- md5sum.txt
if iso_has /md5sum.txt; then
  iso_get /md5sum.txt "$WORK/md5sum.txt"
  for entry in "${CHANGED[@]}"; do
    path="${entry%%:*}"; file="${entry#*:}"
    sum=$(md5sum "$file" | cut -d' ' -f1)
    grep -vF "  $path" "$WORK/md5sum.txt" > "$WORK/md5.tmp" || true
    echo "$sum  $path" >> "$WORK/md5.tmp"
    mv "$WORK/md5.tmp" "$WORK/md5sum.txt"
  done
  (cd "$STAGE" && find . -type f -print0 | sort -z | xargs -0 md5sum | sed 's#  \./#  ./taciss/#') >> "$WORK/md5sum.txt"
  MAPS+=(-map "$WORK/md5sum.txt" /md5sum.txt)
fi

# ------------------------------------------------------------ write ISO
OUT="$DIST/taciss-${DEBVER}-${FLAVOR}-amd64.iso"
rm -f "$OUT" "$OUT.sha256"
say "Writing $OUT"
# "-boot_image any replay" copies the original BIOS + UEFI boot setup (hybrid
# USB/DVD layout, EFI partition, Secure Boot shim) unchanged.
xorriso -indev "$SRC" -outdev "$OUT" "${MAPS[@]}" -boot_image any replay
(cd "$DIST" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

say "Done"
ls -lh "$OUT"
cat "$OUT.sha256"
cat <<EOF

Write it to a USB stick (erases the stick):
  Linux:   sudo dd if=$OUT of=/dev/sdX bs=4M status=progress oflag=sync
  Windows: Rufus (choose "DD image" mode) or balenaEtcher
Or attach it to a VM (Proxmox / VirtualBox / Hyper-V Gen2) to test first.
EOF
