#!/usr/bin/env bash
# TACISS installer for Debian 13 (trixie). Works with no internet access:
# the only dependency is python3, which ships on the Debian ISO.
#
#   sudo ./deploy/install.sh              # install or upgrade on a running system
#   ./deploy/install.sh --no-start        # used by the TACISS ISO inside the installer chroot
#
set -euo pipefail
# "su" (without "-") leaves out /usr/sbin, where useradd and runuser live
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

NO_START=0
[[ "${1:-}" == "--no-start" ]] && NO_START=1

[[ $EUID -eq 0 ]] || { echo "Please run as root: sudo bash $0   (no sudo? run: su -   then: bash $0)" >&2; exit 1; }
if [[ $NO_START == 0 && ! -d /run/systemd/system ]]; then
  echo "==> systemd isn't running here (chroot or container): installing without starting the service"
  NO_START=1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/taciss
DATA=/var/lib/taciss
ETC=/etc/taciss
BACKUPS=/var/backups/taciss

echo "==> Checking python3"
if ! command -v python3 >/dev/null 2>&1; then
  echo "    python3 missing; installing from apt (uses the Debian ISO/DVD if no internet)"
  apt-get install -y python3
fi
python3 - <<'PY'
import sys, sqlite3
assert sys.version_info >= (3, 9), "Python 3.9+ required"
print(f"    Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
PY

echo "==> Creating service user"
if ! id taciss >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin taciss
fi

echo "==> Installing files to $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/app.new"
cp -r "$SRC/app" "$PREFIX/app.new"
rm -rf "$PREFIX/app.old"
[[ -d "$PREFIX/app" ]] && mv "$PREFIX/app" "$PREFIX/app.old"
mv "$PREFIX/app.new" "$PREFIX/app"
chown -R root:root "$PREFIX"
find "$PREFIX/app" -type d -exec chmod 0755 {} +
find "$PREFIX/app" -type f -exec chmod 0644 {} +

install -d -o taciss -g taciss -m 0750 "$DATA"
install -d -o taciss -g taciss -m 0750 "$BACKUPS"
install -d -m 0755 "$ETC"
if [[ ! -f "$ETC/taciss.env" ]]; then
  install -m 0640 -g taciss "$SRC/deploy/taciss.env" "$ETC/taciss.env"
  echo "    wrote $ETC/taciss.env (edit to change port, public access, proxy settings)"
fi

echo "==> Installing systemd units and tools"
for unit in taciss.service taciss-backup.service taciss-backup.timer taciss-issue.service; do
  install -m 0644 "$SRC/deploy/$unit" "/etc/systemd/system/$unit"
done
install -m 0755 "$SRC/deploy/taciss-cli"   /usr/local/bin/taciss
install -m 0755 "$SRC/deploy/taciss-issue" /usr/local/sbin/taciss-issue
install -m 0755 "$SRC/deploy/taciss-update" /usr/local/sbin/taciss-update

# Database + one-time web setup code (only exists until the first account is made)
taciss setup-code --quiet >/dev/null

if [[ $NO_START == 1 ]]; then
  # Inside the installer chroot: services start on first boot.
  systemctl enable taciss.service taciss-backup.timer taciss-issue.service
  echo "==> Installed. TACISS starts on first boot."
  exit 0
fi

systemctl daemon-reload
systemctl enable --now taciss.service taciss-backup.timer
systemctl enable taciss-issue.service
systemctl restart taciss.service
/usr/local/sbin/taciss-issue || true

CODE=$(taciss setup-code --quiet)
PORT=$(grep -E '^TACISS_PORT=' "$ETC/taciss.env" | cut -d= -f2 || true)
PORT=${PORT:-8080}
if [[ -n "$CODE" && -t 0 ]]; then
  echo
  echo "==> No accounts yet. Create the first admin now, or skip and use the web setup page"
  echo "    with setup code: $CODE"
  read -rp "    Admin username (blank = use web setup): " ADMIN
  if [[ -n "$ADMIN" ]]; then
    read -rp "    Callsign (optional): " CALL
    taciss create-user "$ADMIN" --role admin ${CALL:+--callsign "$CALL"}
    CODE=""
  fi
fi

echo
echo "==> TACISS is running."
for ip in $(hostname -I); do echo "    http://$ip:$PORT"; done
echo "    http://$(hostname).local:$PORT   (if avahi-daemon is installed)"
[[ -n "$CODE" ]] && echo "    First-time setup code: $CODE"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
  echo "    ufw is active: run 'sudo ufw allow $PORT/tcp' to allow LAN access,"
  echo "    and 'sudo ufw allow 47474/udp' so nearby TACISS nodes can find this one."
fi
echo "    Logs:    journalctl -u taciss -f"
echo "    Manage:  sudo taciss create-user NAME --role operator"
echo "    Online access: see README.md (Caddy / Cloudflare Tunnel / Tailscale)."
