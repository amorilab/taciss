"""MeshCore tab against fake MeshCore-map and CoreScope servers (no internet needed)."""
import gzip, json, os, shutil, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error, http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "taciss.py")
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 40 * 86400))
nodes = [  # Tennessee nodes, one far away, one stale, one with a clock in the future, plus filler
    {"public_key": "aa" * 32, "type": 2, "adv_name": "Nashville Rptr 🗼", "last_advert": now, "adv_lat": 36.16, "adv_lon": -86.78,
     "params": {"freq": 910.525, "sf": 7, "bw": 62.5, "cr": 5}, "updated_date": now},
    {"public_key": "bb" * 32, "type": 1, "adv_name": "Old companion", "last_advert": old, "adv_lat": 35.0, "adv_lon": -85.3,
     "params": {}, "updated_date": old},
    {"public_key": "cc" * 32, "type": 3, "adv_name": "Bad clock room", "last_advert": "2105-03-13T14:48:08.000Z",
     "adv_lat": 35.9, "adv_lon": -83.9, "params": {}, "updated_date": now},
    {"public_key": "dd" * 32, "type": 2, "adv_name": "London", "last_advert": now, "adv_lat": 51.5, "adv_lon": -0.12,
     "params": {}, "updated_date": now},
] + [{"public_key": f"{i:064x}", "type": 2, "adv_name": "filler " + "x" * 300, "last_advert": now, "adv_lat": 10.0,
      "adv_lon": 10.0, "params": {"freq": 869.5}, "updated_date": now} for i in range(4000)]
MAP_GZ = gzip.compress(json.dumps(nodes).encode())
state = {"up": True, "hits": 0}

CS = {
    "/api/packets": {"packets": [
        {"timestamp": now, "payload_type": 4, "observer_name": "Nash Obs", "observer_iata": "BNA", "_parsedPath": ["A1", "B2"],
         "snr": 9.5, "rssi": -90, "observation_count": 3, "hash": "h1",
         "decoded_json": json.dumps({"type": "ADVERT", "name": "Nashville Rptr", "lat": 36.16, "lon": -86.78,
                                     "flags": {"repeater": True}})},
        {"timestamp": now, "payload_type": 5, "observer_name": "Nash Obs", "observer_iata": "BNA", "_parsedPath": [],
         "decoded_json": json.dumps({"type": "GRP_TXT", "channelHashHex": "E6", "decryptionStatus": "decryption_failed"})},
        {"timestamp": now, "payload_type": 5, "observer_name": "Nash Obs", "observer_iata": "BNA", "_parsedPath": [],
         "decoded_json": json.dumps({"type": "GRP_TXT", "channel": "#tn", "decryptionStatus": "decrypted"})}]},
    "/api/observers": {"observers": [
        {"id": "O1", "name": "Nash Obs", "iata": "BNA", "lat": 36.1, "lon": -86.7, "last_seen": now, "packetsLastHour": 40},
        {"id": "O2", "name": "Far Obs", "iata": "LHR", "lat": 51.5, "lon": -0.1, "last_seen": now, "packetsLastHour": 9}]},
    "/api/nodes": {"nodes": [{"public_key": "aa" * 32, "name": "Nashville Rptr", "role": "repeater", "lat": 36.16, "lon": -86.78,
                              "last_heard": now}]},
    "/api/channels": {"channels": [{"hash": "#tn", "name": "#tn", "lastActivity": now, "messageCount": 2},
                                   {"hash": "Public", "name": "Public", "lastActivity": old, "messageCount": 5}]},
    "/api/channels/%23tn/messages": {"messages": [{"timestamp": old, "sender": "A", "text": "older"},
                                                 {"timestamp": now, "sender": "B", "text": "newer"}]},
}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not state["up"]:
            self.send_response(503); self.end_headers(); return
        state["hits"] += 1
        path = self.path.split("?")[0]
        if path == "/map/nodes":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(MAP_GZ)))
            self.end_headers()
            for i in range(0, len(MAP_GZ), 7777):  # odd-sized pieces: objects split across reads
                self.wfile.write(MAP_GZ[i:i + 7777]); self.wfile.flush()
            return
        body = CS.get(path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        if path == "/api/packets":
            state["last_packets_query"] = self.path
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
FP = fake.server_address[1]
threading.Thread(target=fake.serve_forever, daemon=True).start()

PORT = 8093
data = tempfile.mkdtemp(prefix="taciss-mc-")
env = dict(os.environ, TACISS_DATA=data, TACISS_APRS="0")
PW = "correct-horse-1"
subprocess.run([sys.executable, APP, "create-user", "admin", "--role", "admin", "--password", PW], env=env, check=True,
               capture_output=True)
LOG = open(os.path.join(data, "server.log"), "w")
srv = subprocess.Popen([sys.executable, "-u", APP, "serve", "--port", str(PORT), "--host", "127.0.0.1"], env=env,
                       stdout=LOG, stderr=subprocess.STDOUT)
time.sleep(1.5)
BASE = f"http://127.0.0.1:{PORT}/api/"


class Client:
    def __init__(self, user=None):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if user:
            self.call("POST", "login", {"username": user, "password": PW})

    def call(self, method, path, body=None, raw=False):
        req = urllib.request.Request(BASE + path, method=method, headers={"X-Requested-With": "TACISS"})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.op.open(req) as r:
                return (r.status, dict(r.headers)) if raw else (r.status, json.loads(r.read() or b"null"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


try:
    A = Client("admin")
    check("signed-out users can't read MeshCore", Client().call("GET", "meshcore/status")[0] == 401)
    s, st = A.call("GET", "meshcore/status")
    check("status: default links and Tennessee area", s == 200 and len(st["links"]) == 3 and st["bbox"][0] == 34.9, st)
    s, e = A.call("GET", "meshcore/packets")
    check("no analyzer set: clear error", s == 400 and "analyzer" in e["error"], (s, e))

    s, _ = A.call("PUT", "settings", {"meshcore_map_url": f"http://127.0.0.1:{FP}/map/nodes",
                                      "meshcore_analyzer_url": f"http://127.0.0.1:{FP}/", "meshcore_regions": "bna, tys"})
    check("point MeshCore at the fake servers", s == 200)
    s, r = A.call("GET", "meshcore/nodes")
    names = {n["name"] for n in r["data"]}
    check("map nodes streamed and filtered to the area", s == 200 and r["total_in_area"] == 3 and "London" not in names, r.get("error"))
    check("nodes older than the day limit are hidden", "Old companion" not in names and "Nashville Rptr 🗼" in names, names)
    check("a node clock in the future falls back to when the map saw it", "Bad clock room" in names, names)
    n = next(x for x in r["data"] if x["name"].startswith("Nashville"))
    check("node fields", n["type"] == "repeater" and n["radio"].startswith("910.525 MHz SF7") and n["key"] == "aa" * 32, n)
    _, r = A.call("GET", "meshcore/nodes?days=60")
    check("longer window includes the older node", "Old companion" in {x["name"] for x in r["data"]})
    hits = state["hits"]
    A.call("GET", "meshcore/nodes")
    check("map list is cached (not re-downloaded)", state["hits"] == hits)

    _, p = A.call("GET", "meshcore/packets")
    check("analyzer packets summarized", p["data"][0]["type"] == "ADVERT" and p["data"][0]["summary"] == "Nashville Rptr (repeater)"
          and p["data"][0]["hops"] == 2 and p["data"][1]["summary"].endswith("(encrypted)") and p["data"][2]["summary"] == "#tn", p["data"])
    check("region codes passed to the analyzer", "region=BNA%2CTYS" in state.get("last_packets_query", ""), state.get("last_packets_query"))
    _, o = A.call("GET", "meshcore/observers")
    check("observers filtered to our regions", [x["name"] for x in o["data"]] == ["Nash Obs"], o["data"])
    _, c = A.call("GET", "meshcore/channels")
    check("channels newest first", [x["name"] for x in c["data"]] == ["#tn", "Public"], c["data"])
    _, m = A.call("GET", "meshcore/messages?channel=%23tn")
    check("channel messages newest first", [x["text"] for x in m["data"]] == ["newer", "older"], m["data"])
    _, ln = A.call("GET", "meshcore/live-nodes")
    check("analyzer nodes in the area", len(ln["data"]) == 1 and ln["data"][0]["type"] == "repeater", ln)

    # only certain public channels
    s, _ = A.call("PUT", "settings", {"meshcore_channels": " tn , Public "})
    check("channel list saved", s == 200 and A.call("GET", "settings")[1]["meshcore_channels"] == "tn,Public")
    A.call("PUT", "settings", {"meshcore_channels": "#tn"})
    _, c = A.call("GET", "meshcore/channels")
    check("only listed channels are shown", [x["name"] for x in c["data"]] == ["#tn"], c["data"])
    check("listed channel's messages load", A.call("GET", "meshcore/messages?channel=%23tn")[0] == 200)
    check("other channels' messages are refused", A.call("GET", "meshcore/messages?channel=Public")[0] == 403)
    time.sleep(16)  # packet cache lifetime
    _, p = A.call("GET", "meshcore/packets")
    kinds = [(x["type"], x["summary"]) for x in p["data"]]
    check("packets: other/encrypted channel traffic hidden, listed channel kept, adverts kept",
          kinds == [("ADVERT", "Nashville Rptr (repeater)"), ("GRP_TXT", "#tn")], kinds)
    check("bad channel list rejected", A.call("PUT", "settings", {"meshcore_channels": "a;b"})[0] == 400)
    A.call("PUT", "settings", {"meshcore_channels": ""})
    _, c = A.call("GET", "meshcore/channels")
    check("blank list shows all channels again", len(c["data"]) == 2)

    # offline: sources down -> last data still served, with a note
    state["up"] = False
    time.sleep(16)  # packet cache lifetime
    _, p2 = A.call("GET", "meshcore/packets")
    check("analyzer down: last packets still shown with a note", p2["data"] and "unreachable" in (p2["error"] or ""), p2.get("error"))
    state["up"] = True

    # embedded analyzer: allowed in the page's security policy only as https
    s, hdr = A.call("GET", "meshcore/status", raw=True)
    check("CSP allows the embedded analyzer", "frame-src 'self' https://analyzer.letsmesh.net" in hdr.get("Content-Security-Policy", ""),
          hdr.get("Content-Security-Policy"))
    s, e = A.call("PUT", "settings", {"meshcore_embed_url": "http://insecure.example"})
    check("embed must be https", s == 400, (s, e))
    A.call("PUT", "settings", {"meshcore_embed_url": ""})
    s, hdr = A.call("GET", "meshcore/status", raw=True)
    check("no embed: no frame-src", "frame-src" not in hdr.get("Content-Security-Policy", ""), hdr.get("Content-Security-Policy"))
    check("bad area rejected", A.call("PUT", "settings", {"meshcore_bbox": "1,2,3"})[0] == 400)
    check("bad region codes rejected", A.call("PUT", "settings", {"meshcore_regions": "B;NA"})[0] == 400)
    A.call("PUT", "settings", {"meshcore_enabled": "0"})
    s, e = A.call("GET", "meshcore/status")
    check("admin can turn the tab off", s == 400 and "off" in e["error"], (s, e))
finally:
    srv.terminate()
    srv.wait(5)
    LOG.close()
    fake.shutdown()
    shutil.rmtree(data, ignore_errors=True)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
