#!/bin/sh
# Runs inside the Debian installer before partitioning.
# If exactly one disk exists besides the install media, select it automatically.
# Otherwise leave the question for the person at the keyboard, so we never
# guess and erase the wrong drive.
media=$(awk '$2 == "/cdrom" { print $1 }' /proc/mounts | head -n 1)
cands=""
for d in $(list-devices disk); do
  case "$media" in
    "$d"*) continue ;;   # the USB stick / disc we booted from
  esac
  cands="$cands $d"
done
# shellcheck disable=SC2086
set -- $cands
if [ $# -eq 1 ]; then
  logger -t taciss "auto-selecting install disk $1"
  debconf-set partman-auto/disk "$1"
else
  logger -t taciss "found $# candidate disks ($cands); asking the user"
fi
exit 0
