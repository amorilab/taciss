"""Mesh discovery test: AREDN services list and Tailscale peers, using a fake AREDN node and a fake
`tailscale` command. (LAN broadcast isn't tested here: CI runners often block multicast.)"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app", "taciss.py")
PORT = 8104
fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


data = tempfile.mkdtemp(prefix="taciss-disc-")
fakebin = tempfile.mkdtemp(prefix="taciss-fakebin-")
env = dict(os.environ, TACISS_DATA=data, TACISS_APRS="0")
srv = subprocess.Popen([sys.executable, "-u", APP, "serve", "--port", str(PORT), "--host", "127.0.0.1"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/mesh/whoami", timeout=1)
            break
        except OSError:
            time.sleep(0.25)

    # fake `tailscale status --json`: one online TACISS peer, one offline, one online without TACISS
    status = {"Peer": {
        "k1": {"HostName": "kit3", "DNSName": "kit3.tail1234.ts.net.", "TailscaleIPs": ["127.0.0.1", "fd7a::1"], "Online": True},
        "k2": {"HostName": "off", "TailscaleIPs": ["127.0.0.9"], "Online": False},
        "k3": {"HostName": "other", "TailscaleIPs": ["127.0.0.2"], "Online": True}}}
    with open(os.path.join(fakebin, "status.json"), "w") as f:
        json.dump(status, f)
    if os.name == "nt":
        with open(os.path.join(fakebin, "tailscale.cmd"), "w") as f:
            f.write(f'@type "{os.path.join(fakebin, "status.json")}"\n')
    else:
        p = os.path.join(fakebin, "tailscale")
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\ncat "{os.path.join(fakebin, "status.json")}"\n')
        os.chmod(p, 0o755)
    os.environ["PATH"] = fakebin + os.pathsep + os.environ["PATH"]

    sys.path.insert(0, os.path.join(HERE, "..", "app"))
    import taciss as T
    T.NODE["id"], T.NODE["name"] = "0000000000000000", "tester"

    class Fake(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps({"node": "KJ4ABC-pi", "services": [
                {"name": "TACISS Knox", "protocol": "tcp", "link": f"http://127.0.0.1:{PORT}/"},
                {"name": "Webcam", "protocol": "tcp", "link": "http://127.0.0.1:9/"},
                {"name": "TACISS dead", "protocol": "tcp", "link": "http://127.0.0.1:1/"}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    fake = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    T.AREDN_SYSINFO = f"http://127.0.0.1:{fake.server_port}/cgi-bin/sysinfo.json?services=1"

    card = T.mesh_probe(f"http://127.0.0.1:{PORT}")
    check("whoami answers with a node card", bool(card) and card["port"] == PORT)

    d = T.MeshDiscovery()
    d.scan_aredn()
    found = d.list()
    check("AREDN: finds the advertised TACISS node", any(x["via"] == "aredn" and x["node_id"] == card["id"] for x in found))
    check("AREDN: ignores non-TACISS and dead services", len(found) == 1)
    check("AREDN: status names the AREDN node", "KJ4ABC" in d.status["aredn"])

    T.CONFIG["port"] = PORT
    d2 = T.MeshDiscovery()
    d2.scan_tailscale()
    found = d2.list()
    check("Tailscale: finds the online TACISS peer only", len(found) == 1 and found[0]["via"] == "tailscale")
    check("Tailscale: uses the MagicDNS name", bool(found) and found[0]["url"] == f"http://kit3.tail1234.ts.net:{PORT}")

    T.AREDN_SYSINFO = "http://127.0.0.1:1/x"
    d3 = T.MeshDiscovery()
    d3.scan_aredn()
    check("no AREDN node -> 'not found'", d3.status["aredn"] == "not found")

    d4 = T.MeshDiscovery()
    d4.add({"id": "nothex!", "name": "x"}, "lan", "1.2.3.4")
    d4.add({"id": T.NODE["id"], "name": "me"}, "lan", "1.2.3.4")
    d4.add({"id": "abcdefabcdefabcd", "name": "y" * 500, "url": "javascript:alert(1)", "port": "zz"}, "lan", "10.0.0.5")
    found = d4.list()
    check("rejects bad ids and our own id", len(found) == 1)
    check("bad url falls back to sender address; long names cut", found[0]["url"] == "http://10.0.0.5:8080" and len(found[0]["name"]) == 60)
    for i in range(300):
        d4.add({"id": "%016x" % (i + 1), "name": "n"}, "lan", "10.0.0.1")
    check("list is capped", len(d4.list()) == T.DISC_MAX)
    d4.found["abcdefabcdefabcd"]["seen"] = time.time() - 9999
    check("forgets nodes not heard from", "abcdefabcdefabcd" not in [x["node_id"] for x in d4.list()])
finally:
    srv.terminate()
    srv.wait(10)
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(fakebin, ignore_errors=True)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
