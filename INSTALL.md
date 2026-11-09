# Installing TACISS: a first-time guide

This guide takes you from nothing to a working TACISS server, step by step. You don't need to know Linux.

**Pick the one that matches your hardware:**

| You have… | Use | Time |
|---|---|---|
| A **Raspberry Pi** (3, 4, 5, 400 or Zero 2 W) | [A. Raspberry Pi image](#a-raspberry-pi) | ~15 min |
| A **spare PC, mini PC or virtual machine** you can erase | [B. Installer ISO](#b-pc-or-virtual-machine-installer-iso) | ~30 min |
| A machine **already running Debian 13** | [C. Existing Debian 13](#c-existing-debian-13-machine) | ~5 min |

Everything you download is on the **[Releases page](https://github.com/amorilab/taciss/releases/latest)**. TACISS runs on **Debian 13 ("trixie")**. The Pi image is Raspberry Pi OS, which is Debian 13 for the Pi. Each release is automatically installed and tested on a clean Debian 13 system before it's published.

---

## A. Raspberry Pi

**You need:** a Raspberry Pi 3, 4, 5, 400 or Zero 2 W, its power supply, a **microSD card (16 GB or larger)**, and a computer with an SD card reader.

1. **Download** `taciss-<version>-raspberrypi-arm64.img.xz` from the [Releases page](https://github.com/amorilab/taciss/releases/latest). Don't unzip it.
2. **Install Raspberry Pi Imager** on your computer from <https://www.raspberrypi.com/software/>.
3. **Write the card.** In Imager:
   - **Device:** your Pi model.
   - **OS:** scroll to the bottom, choose **Use custom**, and pick the `.img.xz` file you downloaded.
   - **Storage:** your microSD card. Double-check it's the card, because it will be erased.
   - Click **Next**. When it asks about OS customisation, click **Edit settings**. These are optional, but recommended:
     - **Username and password:** lets you log in to the Pi later (for updates). TACISS itself doesn't use this.
     - **Wireless LAN:** your Wi-Fi name and password, if you won't plug in an Ethernet cable.
     - **Hostname:** leave it blank to use `taciss`, or give each kit its own name, like `blount-kit`.
     - **Services tab:** turn on **SSH** if you want to manage the Pi from another computer.
   - Click **Yes** to write. It takes a few minutes.
4. **Boot it.** Put the card in the Pi, plug in Ethernet (or rely on the Wi-Fi you set), then plug in power.
5. **Wait about 3 minutes.** The first boot sets up TACISS. The Pi may restart once on its own.
6. **Open TACISS** from a phone or laptop on the **same network**:
   - **http://taciss.local:8080**, or your own hostname, like `http://blount-kit.local:8080`.
   - If that doesn't load, see [Can't find it?](#cant-find-it) below.
7. **Create your admin account.** The page asks for a **setup code**. You'll find it in either of these places:
   - **on a screen plugged into the Pi**, shown above the login prompt, or
   - **on the SD card:** shut the Pi down (unplug it), put the card in your computer, and open **`TACISS-SETUP.txt`**. It also lists the Pi's IP address.

   Enter the code, choose a username and password, and you're in. The code stops working once the first account exists.

---

## B. PC or virtual machine (installer ISO)

**You need:** a 64-bit PC, mini PC or virtual machine with at least 1 GB of RAM and 8 GB of disk, an Ethernet connection **with internet** during the install, and a USB stick (4 GB or larger).

> ⚠ **This erases the computer's disk.** Use a machine you don't need anything from.

1. **Download** `taciss-<version>-installer-amd64.iso` from the [Releases page](https://github.com/amorilab/taciss/releases/latest).
2. **Make a bootable USB stick:**
   - **Windows:** use [Rufus](https://rufus.ie). Select the ISO, click Start, and choose **"Write in DD Image mode"** when asked.
   - **Mac or Linux:** use [balenaEtcher](https://etcher.balena.io).
   - **Virtual machine** (VirtualBox, Proxmox, Hyper-V): attach the ISO as a CD drive instead.
3. **Boot from the USB stick.** Usually you press F12, F11, F10 or Esc right after power-on to open the boot menu.
4. The menu starts **"TACISS server – automated install"** by itself after 15 seconds. The installer asks you only two things:
   - **which disk to erase** (only if there's more than one), and
   - a **password for the `tacadmin` account**. Write it down: it's your login for the machine itself.
5. Everything else is automatic. When it finishes, **remove the USB stick**. The machine restarts.
6. The machine's screen now shows **the web address and a setup code**.
7. From a phone or laptop on the same network, open **http://taciss.local:8080**, or the address shown on the screen. Enter the setup code and create your admin account.

Need to install where there's **no internet at all**? Build the 4 GB offline DVD version yourself. See "Option 1" in the [README](README.md#option-1-taciss-installer-iso-easiest).

---

## C. Existing Debian 13 machine

**You need:** a machine running **Debian 13 (trixie)** that's connected to the internet for the download. (Raspberry Pi OS and Ubuntu are Debian-based and work too.)

1. **Check the Debian version:**

   ```bash
   cat /etc/debian_version
   ```

   It should say `13.something`.

2. **Make sure you can use `sudo` and `wget`.** Run:

   ```bash
   sudo true && command -v wget
   ```

   If it prints `/usr/bin/wget`, skip to step 3. Otherwise (for example `sudo: command not found`, `is not in the sudoers file`, or nothing printed), fix it once:

   ```bash
   su -
   ```

   Type the **root** password you chose when installing Debian. Then run these two lines, using your own login name in place of `yourname`:

   ```bash
   apt update && apt install -y sudo wget
   usermod -aG sudo yourname
   ```

   Type `exit`, then **log out and back in** (or reboot) so the change takes effect.

   Don't want to set up `sudo`? You can stay in `su -` and run the commands below without the word `sudo`.

3. **Download and unpack TACISS.** Copy and paste these lines. Replace `1.0.1` with the newest version from the [Releases page](https://github.com/amorilab/taciss/releases/latest):

   ```bash
   cd ~
   wget https://github.com/amorilab/taciss/releases/download/v1.0.1/taciss-1.0.1.tar.gz
   tar xzf taciss-1.0.1.tar.gz
   cd taciss-1.0.1
   ```

   No internet on that machine? Download the `.tar.gz` on another computer and copy it over with a USB stick.

4. **Install:**

   ```bash
   sudo bash deploy/install.sh
   ```

   At the end it offers to create the admin account right there. You can also leave it blank and use the setup code it prints.

5. **Open** the address it prints, for example `http://192.168.1.50:8080`, from any device on the network.

If the machine has a firewall (`ufw`) turned on, open the ports:

```bash
sudo ufw allow 8080/tcp
sudo ufw allow 47474/udp
```

---

## After you're in

1. **Make accounts for your people:** **Admin → Users**. Give each person a role:
   - **viewer** can read, chat and submit reports.
   - **operator** can also run incidents, logs and resources.
   - **admin** can do everything.
2. **Add your programs and agencies** (ARES, CERT, the EOC, and so on) so incidents can be assigned to them.
3. **Linking several TACISS boxes** (field kits plus the EOC)? Open the **🕸 Mesh** tab. Nearby kits show up under **📡 Nearby nodes**. Click **Pair…** and enter the pairing code from the other box. See [Mesh](README.md#-mesh-linking-taciss-nodes-raspberry-pi-kits-eocs).
4. **Want it reachable from the internet?** See [Making it reachable online](README.md#making-it-reachable-online).

## Updating

When a new version is out, admins see a **"TACISS x.y.z is available"** note at the top of the app (the server has to have internet for this). To install it, log in to the machine itself, through SSH or a keyboard and screen. On a Pi, that's the username you set in Raspberry Pi Imager. Then run:

```bash
sudo taciss update
```

Your data and settings are kept, and the database is backed up first.

**No internet on that box?** Download `taciss-<version>.tar.gz` and `SHA256SUMS` from the Releases page on another computer. Copy both to the box (for example with a USB stick), then run:

```bash
sudo taciss update taciss-<version>.tar.gz
```

## Can't find it?

- **`taciss.local` doesn't load:**
  - **Windows 10 and 11** support `.local` names. Some older Android phones don't, so use the IP address instead.
  - **Find the IP address** in your router's list of connected devices (look for "taciss"), on a screen plugged into the box, or in `TACISS-SETUP.txt` on the Pi's SD card.
- **Several Pis on one network** each try to use the name `taciss`, so they show up as `taciss.local`, `taciss-2.local`, and so on. Give each one its own hostname in Raspberry Pi Imager to avoid the guessing.
- **The page doesn't load at all:**
  - Make sure your phone or laptop is on the **same network** (not mobile data).
  - Make sure you typed **`:8080`** at the end of the address.
  - A Pi's first boot takes about 3 minutes, so give it time.
- **Lost the setup code:** on the machine, run `sudo taciss setup-code`. If an admin account already exists, reset its password instead: `sudo taciss reset-password <username>`.
- **Check whether TACISS is running:** `sudo systemctl status taciss`. Its log: `journalctl -u taciss -f`.
