# TACISS — self-hosted

**Tactical Communications & Incident Situational Sharing.** This is an incident and information-sharing board for emergency management and auxiliary communications groups (ARES, RACES, Skywarn, CERT). It runs on your own Debian 13 machine and keeps working without internet. When you want, you can also put it online at your own domain.

| | |
|---|---|
| **Runtime** | Python 3 standard library only, so there's no `pip` and no internet needed to install |
| **Database** | SQLite in a single file (`/var/lib/taciss/taciss.db`), backed up automatically every 6 hours |
| **Frontend** | Plain HTML/JS with Leaflet bundled in the repo, so no CDN calls |
| **Live updates** | Server-Sent Events: every open screen updates within about a second when anyone changes something |

## Get it

**New here? Start with [INSTALL.md](INSTALL.md)**, a step-by-step guide for first-time users.

Download from the **[Releases page](https://github.com/amorilab/taciss/releases/latest)**:

| Download | For |
|---|---|
| `taciss-<version>-raspberrypi-arm64.img.xz` | **Raspberry Pi** 3/4/5/400/Zero 2 W. Write it to a microSD card with Raspberry Pi Imager, power on, and open `http://taciss.local:8080`. |
| `taciss-<version>-installer-amd64.iso` | **PC, mini PC or VM.** Automated Debian 13 + TACISS installer. |
| `taciss-<version>.tar.gz` | An **existing Debian 13** machine (`sudo bash deploy/install.sh`), or offline updates. |

Updating a running server: `sudo taciss update`. See [Updates and releases](#updates-and-releases).

## Features

- **Dashboard**: SEOC activation level bar (Normal / Monitoring / Partial / Full), local and Zulu clocks, open incidents, recent reports, program map and status counts
- **Incidents**: type, priority, status, IC/net control, frequency and location. Each incident has its own **ICS-214-style activity log** (printable), linked reports, assigned resources and a chat channel. Incidents can be **open** to everyone or **🔒 restricted** to their assigned programs and served agencies.
- **🛰 SITREP War Room**: your SITREPs, storm, damage, net and shelter reports (viewers can submit them too, which suits spotters in the field), alongside live NWS/SPC/Spotter Network storm reports, NWS warnings, radar, a camera wall, and power and internet outages. Storm and damage reports can be handed off to the NWS. See [SITREP War Room](#sitrep-war-room).
- **Programs**: ARES/RACES/Skywarn/CERT groups with coordinator, callsign, frequencies/tones, counties and status
- **Agencies**: served agencies with contact info
- **Resources**: people, teams, go-kits, repeaters, shelters and generators, with available/assigned/out-of-service status and incident assignment
- **Chat**: `#general`, `#ops`, `#net-control`, plus an automatic channel for each open incident
- **Map**: incidents, reports from the last 24h, programs and agencies. A NEXRAD radar overlay is available when online.
- **🕸 Mesh**: pair TACISS nodes (Raspberry Pi kits, EOC servers) over LAN, AREDN, Tailscale/WireGuard or the internet; they share incidents, logs, reports and chat store-and-forward style and keep working offline. See [Mesh](#-mesh-linking-taciss-nodes-raspberry-pi-kits-eocs).
- **Accounts and roles**: `viewer` (read, chat, submit reports), `operator` (manage incidents, logs, resources, SEOC level), `admin` (users, settings, audit log, JSON export)
- **Security**: PBKDF2 password hashing, login throttling, HttpOnly cookies, CSRF guard, strict CSP and a full audit log

## Option 1: TACISS installer ISO (easiest)

Build a bootable Debian 13 image with TACISS baked in. Boot it on a machine and a few minutes later that machine is a TACISS server.

The ready-made netinst ISO is on the [Releases page](https://github.com/amorilab/taciss/releases/latest). Build it yourself if you want the offline DVD version, or a version with your own changes.

### Build the ISO

You need a Debian or Ubuntu machine, or Docker on any OS. The build downloads the official Debian 13 image, checks its signature and checksum, and adds TACISS to it.

```bash
sudo bash iso/build-iso.sh                         # on Debian/Ubuntu
docker run --rm -v "$PWD":/src debian:trixie bash /src/iso/build-iso.sh   # anywhere with Docker
```

On Windows with Docker Desktop:

```powershell
powershell -ExecutionPolicy Bypass -File iso\build-iso.ps1
```

| Flavor | Size | Internet during install? |
|---|---|---|
| `FLAVOR=dvd` (default) | ~4 GB | **No.** Every package is on the disc. |
| `FLAVOR=netinst` | ~800 MB | Yes, it downloads packages from deb.debian.org |

The result is written to `dist/taciss-13.x-<flavor>-amd64.iso`. Write it to a USB stick with Rufus (DD mode), balenaEtcher or `dd`, or attach it to a VM.

### Install from the ISO

1. Boot the stick. **"TACISS server – automated install"** is the default entry, for both BIOS and UEFI, and starts after 15 seconds.
2. The installer only asks for:
   - the **disk to erase**, and only if there's more than one besides the USB stick
   - a **password for the `tacadmin` Linux account**, which has sudo and is your SSH login
3. Everything else is automatic:
   - hostname `taciss`, DHCP networking, and US/Central time
   - SSH server, plus mDNS so the box answers at `taciss.local`
   - TACISS installed as a service
4. After the reboot, the console login screen shows the web address and a **first-time setup code**.
5. From any device on the network, open `http://taciss.local:8080` or the IP address shown. Enter the setup code and create your TACISS admin account. The code stops working once that account exists.

If there's no DHCP on the network, the installer carries on without networking. Set a static IP afterwards by editing `/etc/network/interfaces`.

## Option 2: Install on an existing Debian 13 machine (no internet needed)

1. Install Debian 13 from the ISO. A minimal "standard system utilities" install is enough; `python3` is on the ISO.
2. Copy this folder to the machine, for example with a USB stick or `scp -r taciss user@box:`.
3. Run the installer:

   ```bash
   cd taciss
   sudo bash deploy/install.sh
   ```

   It creates a `taciss` service user, installs to `/opt/taciss`, starts a hardened systemd service plus a backup timer, and prompts you for the first admin account. At the end it prints the LAN URLs, such as `http://192.168.1.50:8080`.
4. Open that URL from any phone or laptop on the same network. That's the offline setup done.

To upgrade, run `sudo taciss update`, or copy the new folder over and run `sudo bash deploy/install.sh` again. Your data and config are kept.

## Option 3: Raspberry Pi image

Download `taciss-<version>-raspberrypi-arm64.img.xz` from the Releases page, or build it yourself on Linux with `sudo bash pi/build-pi-image.sh`.

Write it with Raspberry Pi Imager: **Use custom**, then **Edit settings** to set a login, Wi-Fi and SSH. The image is the official Raspberry Pi OS Lite (64-bit, Debian 13) plus TACISS:

- TACISS installs itself on the **first boot**, so every Pi gets its own mesh node ID and setup code.
- The hostname is `taciss` unless you set one in Imager.
- After the first boot, the SD card's boot partition has a `TACISS-SETUP.txt` with the address and setup code. Any computer can read it.

## Updates and releases

**Updating a server or Pi:**

- `sudo taciss update` downloads the newest GitHub release, checks its SHA-256, backs up the database and installs it.
- `sudo taciss update --check` only reports whether a newer one exists.
- `sudo taciss update taciss-1.2.0.tar.gz` installs from a file you copied over, for offline kits.

Admins also see a "new version available" note in the app. The server checks GitHub once a day. Set `TACISS_UPDATE_CHECK=0` in `/etc/taciss/taciss.env` to turn that off. Nothing is ever installed automatically.

**Publishing a new version (maintainers):** commit your changes, then run one of these:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 1.1.0
```

```bash
bash scripts/release.sh 1.1.0
```

The script sets the version in `app/taciss.py`, commits, tags `v1.1.0` and pushes. GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)) then:

1. checks the code and **installs and tests TACISS on a clean Debian 13 system**, including an update that keeps data
2. builds the **Raspberry Pi image** and the **installer ISO**
3. publishes a **GitHub Release** with both, the source `.tar.gz` and `SHA256SUMS`

It takes about 30 minutes. Every push to `main` runs step 1 only.

### Day-to-day commands

```bash
sudo taciss create-user kd4abc --role operator --callsign KD4ABC
sudo taciss reset-password kd4abc
sudo taciss backup /root/taciss-now.db
sudo systemctl restart taciss        # after editing /etc/taciss/taciss.env
journalctl -u taciss -f              # logs
```

Backups are written to `/var/backups/taciss/` every 6 hours and kept for 14 days. To restore, stop the service, copy a backup over `/var/lib/taciss/taciss.db`, `chown taciss:taciss` it, and start the service again.

## Making it reachable online

The same server keeps serving the LAN on port 8080 while you add one of the following. Pick one:

### A. Your own domain with HTTPS (Caddy): best with a public IP

```bash
sudo apt install caddy
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # change taciss.example.com → e.g. taciss.w3fde.com
sudo systemctl reload caddy
```

1. Add a DNS `A` record for `taciss.w3fde.com` pointing at your public IP.
2. Port-forward TCP 80 and 443 to the box.
3. Set `TACISS_TRUST_PROXY=1` in `/etc/taciss/taciss.env` and restart the service.

Caddy gets and renews the Let's Encrypt certificate on its own. If you'd rather use nginx, see `deploy/nginx.conf.example`.

### B. Cloudflare Tunnel: works behind CGNAT, Starlink or LTE with no port forwarding

Install `cloudflared` from Cloudflare's apt repo, then run `cloudflared tunnel login` and create a tunnel that routes `taciss.w3fde.com` to `http://localhost:8080`. Set `TACISS_TRUST_PROXY=1`. Your domain needs to be on Cloudflare DNS.

### C. Tailscale: members-only, not public

Install Tailscale on the box and on each member's device. They reach `http://<box-name>:8080` from anywhere and nothing is exposed to the internet. This is the safest option if only your own team needs remote access.

> When it's online, keep `TACISS_PUBLIC_READ=0` unless you deliberately want the public to see programs, agencies, incidents and reports. Use 10+ character passwords, and only give `operator` and `admin` roles to people who need them.

## Programs, served agencies and restricted incidents

- **Members:** each user can belong to one or more **programs** and/or **served agencies**. An admin sets this in two places: Admin → Users, or the **✎ Edit members** button inside a program or agency record.
- **Programs serve agencies:** a program's *Served Agencies* field lists the agencies it supports.
- **Incidents:** each incident has an **Access** setting, **Assigned Programs** and **Served Agencies**.

| Access | Who can see the incident |
|---|---|
| `open` (default) | Everyone signed in (and the public, if `TACISS_PUBLIC_READ=1`) |
| `restricted` 🔒 | Admins, the person who opened it, members of an assigned program, and users of a served agency |

A restricted incident hides everything attached to it: its activity log, net check-ins, linked reports, assigned resources and incident chat. It also drops out of the counts, the map and live notifications. To anyone without access it looks like it doesn't exist (404). An operator outside the incident's programs can't view, edit or log to it.

**Served-agency logins:** give agency staff an account (usually the `viewer` role) and assign them to their agency. Their dashboard then opens with a **🏛 Serving *agency*** panel listing every incident (restricted ones included) and program that is serving that agency. Membership changes take effect live, with no need to sign out.

## Weather (NWS)

The **⛈ Weather** tab and the dashboard alert ticker use data from the National Weather Service (`api.weather.gov`). The TACISS server polls active alerts every 60 seconds and serves everything to browsers itself, so LAN clients don't need their own internet connection. Radar images and the SPC outlook are the exception: those load straight from NWS/SPC and do need internet on the viewing device. If the server goes offline, the last alerts it received stay visible, marked as stale.

The tab includes:
- **Active alerts** for your state, which you can filter to your counties
- **Radar loops**, picked by NWS office (MRX, OHX, MEG… set in Admin), with the radar site selectable
- **A map** of storm-based warning polygons over a live radar mosaic
- **A 7-day forecast** for the site's location
- **The latest HWO or AFD** for the selected office
- **The SPC Day 1 convective outlook**

**My counties:** each user picks their counties, either on the Weather tab or with the 📍 button on the ticker. When the NWS issues an alert covering one of them, the dashboard ticker turns red, orange or gray and scrolls the alert, and the "My Counties" card pulses. Open screens also flash and show a popup, and play a tone if that user turned it on. County choices are saved to the user's account, or to the browser for signed-out public viewers.

**Exercise alerts:** operators can use **🧪 Exercise alert** to inject a simulated, clearly labelled alert inside TACISS for drills. Nothing is sent to the NWS. These alerts expire on their own and are recorded in the audit log.

**Admin settings:**
- `wx_states`: the states to pull alerts and counties for, such as `TN` or `TN,KY`
- `wx_offices`: the offices to offer for radar and products
- `wx_contact`: a contact email. NWS asks API users to identify themselves, so please set this.

## SITREP War Room

The **🛰 SITREP War Room** tab puts the whole picture on one screen. **⛶ War-room mode** turns it into a full-screen wall display. Like the weather data, every feed is fetched by the TACISS server and passed on to browsers, so LAN users don't need their own internet. When a source can't be reached, the last saved copy stays on screen with a warning. Live feeds are shown to signed-in users only.

| Panel | Source | Key needed? |
|---|---|---|
| Storm / spotter reports (map + live feed, pop-up on new ones) | NWS Local Storm Reports via Iowa Environmental Mesonet, SPC preliminary reports, Spotter Network public reports | No |
| NWS warnings + radar | api.weather.gov, NEXRAD via IEM | No |
| Power outages by county | ORNL ODIN (participating utilities only), plus links to utility outage maps | No |
| Internet connectivity | IODA (Georgia Tech): BGP, active probing, telescope signals | No |
| Weather cameras | Any camera you add in **Manage cameras** (snapshot image URL), plus Windy.com webcams | Windy: free key from api.windy.com |
| TDOT SmartWay traffic cameras | TDOT open-data API | Yes: request one from TDOT Traffic Operations |

Put API keys in **Admin → Site settings**. They are never sent back to browsers or written to the audit log.

**Privacy:** the Spotter Network *positions* feed includes spotters' phone numbers and e-mail addresses, so TACISS never uses it. Contact details found in report text are removed.

**Reporting to the NWS:** storm and damage reports have Hazard, Magnitude and "When it happened" fields. **📤 Report to NWS** formats a standard spotter report that you can copy, e-mail to your office (`nws_report_email`), or send through mPING or your office's reporting page (`nws_report_links`). Then **Mark as reported** records how and by whom.

There's no open API for third parties to post reports to the NWS or Spotter Network. mPING accepts reports only with a token that OU grants, and Spotter Network uses each spotter's private application ID, so TACISS hands the report off instead of posting it silently. Reports the NWS accepts come back into the war room as Local Storm Reports.

Other settings: `lsr_hours` (how far back storm reports go), `feeds_bbox` (the area for Spotter Network reports) and `outage_links`. The storm-report and outage feeds cover the states in `wx_states`.

## 🕸 Mesh: linking TACISS nodes (Raspberry Pi kits, EOCs)

Each TACISS install (a Raspberry Pi in a go-kit, a laptop at a shelter, the county EOC server) is a **node**. It keeps working on its own with no connection, and swaps changes with the nodes it's paired with whenever it can reach them. Everyone sees what the others are doing, and nothing is lost while a link is down.

**How it behaves**

- **Store-and-forward.** Each node passes along what it received. If the Pi in Blount reaches the Knox EOC and the Knox EOC reaches the state server, the state server gets the Pi's reports even though the two never talk directly. A kit only needs to reach *one* other node.
- **Offline-tolerant.** Changes queue up while a link is down and flow as soon as it's back. Syncs run every 30 seconds in the background (`mesh_interval`), and right away after any change.
- **Conflicts.** Logs, check-ins, chat and reports only ever get added to, so they can't conflict. If two nodes edit the same incident field, the newest edit wins on every node. Each record shows a **⇄ Node name** tag when it came from another node, and usernames travel as `user@Node`.
- **What's shared:** incidents, activity logs, net check-ins, SITREPs/reports, resources, programs, agencies, cameras and chat.
- **Never shared:** user accounts, passwords, memberships, settings and API keys. Each node has its own logins.
- **🔒 Restricted incidents** (with their log, check-ins, reports and chat) go only to peers you mark **"share restricted incidents"**. If you lock an incident later, untrusted peers drop their copy. If you unlock it, they get it back.

**Pairing two nodes** (admin, **🕸 Mesh** tab)

1. On node A, set **This node's address** (for example `http://10.54.3.21:8080`) and a **Node name**.
2. On node A, click **Create pairing code**. You get a 20-character code that works once and expires after 24 h. It's easy to read over the radio.
3. On node B, go to **Join a node** and enter A's address, the code and the same link type.

That's it. Both nodes sync in both directions. Only one side needs to be able to reach the other, so a Pi behind NAT or on LTE can join an EOC that it can reach, even though the EOC can't reach it.

**Link types**

| Link | Address looks like | Security |
|---|---|---|
| **LAN / Wi-Fi** | `http://192.168.1.50:8080` | Signed |
| **AREDN** (ham microwave mesh) | `http://kx4abc-pi.local.mesh:8080` | **Signed, not encrypted.** Part 97 forbids encryption on amateur frequencies, so TACISS refuses `https://` on AREDN links. Anyone can read the traffic, but it can't be forged or altered. Keep chat on-topic for amateur rules. |
| **Tailscale / WireGuard** | `http://100.101.102.103:8080` or `http://kit3.tailnet.ts.net:8080` | Signed and encrypted by the VPN |
| **Internet** | `https://eoc.example.org` | Signed and encrypted by HTTPS (Caddy/nginx in front, see above). `http://` is refused. |

Every sync request and reply is signed with HMAC-SHA256 using the pairing secret. A node that isn't paired, or a message that was changed in transit, is rejected.

**Finding nearby nodes** (📡 Nearby nodes on the Mesh tab)

You don't have to type addresses. TACISS looks for other nodes three ways, and lists what it finds with a **Pair…** button. The button fills in the address and link type under **Join a node**. You still need that node's pairing code, so being nearby isn't enough to join.

| Where | How it's found |
|---|---|
| **LAN / Wi-Fi**, or the same AREDN node's LAN ports | Each node announces itself every 30 s by UDP broadcast and multicast (`239.255.84.67`) on port **47474**. Open that port if a firewall is on (`sudo ufw allow 47474/udp`). |
| **Across the AREDN mesh** | TACISS reads the services list from the AREDN node it's plugged into (`localnode.local.mesh`). On each Pi's AREDN node, add an **Advertised Service** whose name contains **TACISS** (for example "TACISS Blount Kit"), with the Pi's hostname and port. |
| **Tailscale** | If `tailscale` is installed, TACISS checks each online device on your tailnet for TACISS on the same port as this node. |

A full scan runs every 2 minutes, or click **⌕ Look now**. To keep a node from announcing itself, set **Let nearby nodes find this one** to **No**. It then also stops answering `GET /api/mesh/whoami`, which only ever returns the node's name, address and version.

**Raspberry Pi notes**

- Easiest is the ready-made Pi image (Option 3). On a Pi that already runs Raspberry Pi OS, use Option 2 (`sudo bash deploy/install.sh`). TACISS is pure Python, so the same files run on the Pi's ARM processor.
- A Pi has no battery-backed clock. TACISS orders edits with a hybrid clock, so a wrong clock won't scramble the order of edits. The Mesh page still warns you when a peer's clock is more than 2 minutes off. Install `fake-hwclock` and `chrony`, or add a GPS or RTC hat for field kits.
- On AREDN, plug the Pi into the node's LAN port. Set `mesh_url` to its `*.local.mesh` hostname and port, and advertise it as a service with "TACISS" in the name so other nodes find it.

## Offline maps

Out of the box the base map uses OpenStreetMap tiles, and radar comes from the Iowa Environmental Mesonet, so both need internet. When offline, every marker still shows on a blank background. For real offline maps, run a local tile server on the box (for example `tileserver-gl-light` with a Tennessee `.mbtiles` extract). Then set **Admin → Map tile URL** to `http://<box>:8081/styles/basic/{z}/{x}/{y}.png`.

## Development

```bash
python3 app/taciss.py create-user admin --role admin
python3 app/taciss.py seed-demo      # optional example data
python3 app/taciss.py serve --port 8080
```

Data goes to `./data/` unless `TACISS_DATA` is set. To add a field, add an `F(...)` line to `ENTITIES` in `app/taciss.py`. The column is created on the next start, and the forms and tables pick it up automatically.
