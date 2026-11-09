TACISS @VERSION@ for Raspberry Pi
================================

1. Put this card in a Raspberry Pi (3, 4, 5, 400 or Zero 2 W) and power it on.
   Plug in Ethernet, or set up Wi-Fi in Raspberry Pi Imager before writing the card.
2. Wait about 3 minutes. The first boot sets everything up.
3. From a phone or laptop on the same network, open:
       http://taciss.local:8080
4. Enter the first-time setup code and create your admin account.

Where's the setup code?
  - On a screen plugged into the Pi (shown above the login prompt), or
  - Power the Pi off, put this card in a computer, and open TACISS-SETUP.txt
    (it appears here after the first boot, with the code and the Pi's IP address).

taciss.local doesn't work? Use the IP address from TACISS-SETUP.txt or your router.
Several Pis on one network? They become taciss.local, taciss-2.local, and so on.
To pick your own name, set the hostname in Raspberry Pi Imager.

Linux login: set a username and password (and turn on SSH) in Raspberry Pi Imager's
"Edit settings" before writing the card. TACISS itself doesn't need one.

Updates: sudo taciss update   (or copy a release .tar.gz over and:  sudo taciss update FILE)
