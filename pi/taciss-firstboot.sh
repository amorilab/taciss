#!/bin/bash
# Runs once, on the Raspberry Pi's first boot: installs TACISS from /opt/taciss-src
# (so this Pi gets its own mesh node ID and setup code) and writes the web address
# and setup code to the SD card's boot partition.
set -euo pipefail
log() { echo "taciss-firstboot: $*"; logger -t taciss-firstboot "$*" 2>/dev/null || true; }

BOOT=/boot/firmware
[[ -d $BOOT ]] || BOOT=/boot

# Raspberry Pi OS (or cloud-init) may have set the stock name back; use ours unless
# the user chose a name in Raspberry Pi Imager.
if [[ "$(hostname)" == raspberrypi ]]; then
  log "setting hostname to taciss"
  hostnamectl set-hostname taciss 2>/dev/null || echo taciss > /etc/hostname
  sed -i 's/\braspberrypi\b/taciss/g' /etc/hosts
  systemctl try-restart avahi-daemon.service || true
fi

log "installing TACISS"
bash /opt/taciss-src/deploy/install.sh </dev/null

PORT=$(grep -E '^TACISS_PORT=' /etc/taciss/taciss.env | cut -d= -f2 || true)
PORT=${PORT:-8080}
CODE=$(cat /var/lib/taciss/setup-code 2>/dev/null || true)
HOST=$(hostname)
IPS=$(hostname -I 2>/dev/null || true)
{
  echo "TACISS is installed and running on this Raspberry Pi."
  echo
  echo "Open one of these from a phone or laptop on the same network:"
  echo "  http://$HOST.local:$PORT"
  for ip in $IPS; do [[ $ip == *:* ]] || echo "  http://$ip:$PORT"; done
  echo
  if [[ -n "$CODE" ]]; then
    echo "First-time setup code:  $CODE"
    echo "Enter it on the web page to create the first admin account."
    echo "The code stops working once that account exists."
  fi
  echo
  echo "Installed $(date -u '+%Y-%m-%d %H:%M UTC'). Updates: sudo taciss update"
} | sed 's/$/\r/' > "$BOOT/TACISS-SETUP.txt" || log "could not write $BOOT/TACISS-SETUP.txt"

rm -f /opt/taciss-src/.firstboot
systemctl disable taciss-firstboot.service || true
log "done - http://$HOST.local:$PORT"
