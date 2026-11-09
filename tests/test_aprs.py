"""APRS: packet parser, passcodes, and the /api/aprs endpoints against a fake APRS-IS server.
Nothing here touches the real APRS network."""
import json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error, http.cookiejar

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
APP = os.path.join(APP_DIR, "taciss.py")
sys.path.insert(0, APP_DIR)
import taciss  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


near = lambda a, b, tol=0.001: a is not None and abs(a - b) < tol

# ---------------------------------------------------------------- parser
check("passcode for N0CALL is 13023", taciss.aprs_passcode("N0CALL") == 13023)
check("passcode ignores SSID and case", taciss.aprs_passcode("n0call-9") == 13023)

p = taciss.parse_aprs("KD4XYZ-9>APRS,WIDE2-1:!3610.00N/08645.00W>088/036/A=000600 hello")
check("uncompressed position", p and p["type"] == "position" and near(p["lat"], 36.166667) and near(p["lon"], -86.75)
      and p["sym"] == "/>", p)
check("course/speed/altitude", p and p.get("course") == 88 and near(p.get("speed_kmh"), 66.7, 0.1)
      and p.get("alt_m") == 183 and p.get("comment") == "hello", p)

p = taciss.parse_aprs("W4WX>APRS:@092345z3610.00N/08645.00W_220/004g005t077r001p000P000h50b10132 Davis")
check("weather station with timestamp", p and p["wx"].get("temp_f") == 77 and p["wx"].get("wind_dir") == 220
      and p["wx"].get("humidity") == 50 and near(p["wx"].get("pressure_mb"), 1013.2) and
      near(p["wx"].get("rain_1h_in"), 0.01), p and p.get("wx"))

p = taciss.parse_aprs("TEST>APRS:=/5L!!<*e7>7P[")
check("compressed position (spec example)", p and near(p["lat"], 49.5, 0.0005) and near(p["lon"], -72.75, 0.0005)
      and p.get("course") == 88 and near(p.get("speed_kmh"), 67.0, 0.6), p)

p = taciss.parse_aprs("KD4XYZ-9>361P0P,WIDE1-1:`rINo v>/Mobile test")
check("Mic-E position", p and near(p["lat"], 36.166667) and near(p["lon"], -86.758333) and p["sym"] == "/>"
      and near(p.get("speed_kmh"), 55.6, 0.1) and p.get("course") == 90 and p.get("comment") == "Mobile test", p)

p = taciss.parse_aprs("KD4XYZ>APRS::W3FDE    :Hello there{123")
check("message with number", p and p["type"] == "message" and p["to"] == "W3FDE" and p["text"] == "Hello there"
      and p["msgno"] == "123", p)
p = taciss.parse_aprs("W3FDE>APRS::KD4XYZ   :ack123")
check("ack", p and p.get("ack") == "ack" and p.get("msgno") == "123", p)

p = taciss.parse_aprs("W3FDE>APZTAC,TCPIP*:;SHELTER2 *092345z3610.00N\\08645.00Wz40 people")
check("object", p and p["type"] == "object" and p["call"] == "SHELTER2" and p["by"] == "W3FDE" and p["sym"] == "\\z"
      and p["comment"] == "40 people" and not p["killed"], p)
p = taciss.parse_aprs("W3FDE>APRS:)AID #2!4903.50N/07201.75WA")
check("item", p and p["type"] == "item" and p["call"] == "AID #2" and near(p["lat"], 49.058333), p)
p = taciss.parse_aprs("KD4WX>APRS:_10090556c220s004g005t-05r000p000P000h50b09900wRSW")
check("positionless weather, negative temp", p and p["type"] == "weather" and p["wx"]["temp_f"] == -5
      and p["wx"]["wind_dir"] == 220, p)
p = taciss.parse_aprs("W3FDE>APRS:}KD4XYZ>APRS,TCPIP,W3FDE*:!3610.00N/08645.00W-home")
check("third-party packet", p and p["call"] == "KD4XYZ" and p["sym"] == "/-", p)
check("garbage is ignored", taciss.parse_aprs("not a packet") is None and taciss.parse_aprs("# comment") is None)
check("broken position doesn't crash", taciss.parse_aprs("X>APRS:!36xx.00N/08645.00W>") is None)
check("aprs_coord", taciss.aprs_coord(36.166667, -86.75) == ("3610.00N", "08645.00W"))
check("aprs_coord south/east", taciss.aprs_coord(-33.8688, 151.2093) == ("3352.13S", "15112.56E"))
check("aprs_clean strips non-ASCII", taciss.aprs_clean("Shelter ° open|ok", 43) == "Shelter  open/ok")

svc = taciss.AprsService()
svc.ingest("KD4XYZ-9>APRS:!3610.00N/08645.00W>moving")
svc.ingest("KD4XYZ-9>APRS:>On my way")
s = {x["call"]: x for x in svc.snapshot(1)}
check("status keeps last position", s["KD4XYZ-9"]["lat"] and s["KD4XYZ-9"]["status"] == "On my way")
svc.ingest("W3FDE>APZTAC,TCPIP*:;SHELTER2 *092345z3610.00N\\08645.00Wz40 people")
svc.ingest("W3FDE>APZTAC,TCPIP*:;SHELTER2 _092345z3610.00N\\08645.00Wzclosed")
check("killed object is removed", "SHELTER2" not in {x["call"] for x in svc.snapshot(1)})

# ---------------------------------------------------------------- fake APRS-IS server
received, logins = [], []


def fake_aprs_is(srv):
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(c,), daemon=True).start()


def handle(c):
    f = c.makefile("rb")
    c.sendall(b"# fake aprsc 2.1\r\n")
    login = f.readline().decode().strip()
    logins.append(login)
    parts = login.split()
    call, pw = parts[1], parts[3]
    if pw == "-1":
        c.sendall(f"# logresp {call} unverified, server FAKE1\r\n".encode())
        c.sendall(b"KD4XYZ-9>APRS,WIDE2-1:!3610.00N/08645.00W>088/036 mobile\r\n")
        c.sendall(b"W4WX>APRS:@092345z3611.00N/08646.00W_220/004g005t077h50b10132\r\n")
        c.sendall(b"KD4XYZ>APRS::W3FDE    :Hello W3FDE{77\r\n")
        try:
            while f.readline():
                pass
        except OSError:
            pass
    else:
        ok = pw.isdigit() and int(pw) == taciss.aprs_passcode(call)
        c.sendall(f"# logresp {call} {'verified' if ok else 'unverified'}, server FAKE1\r\n".encode())
        if ok:
            line = f.readline().decode().strip()
            if line:
                received.append(line)
    c.close()


srv_sock = socket.socket()
srv_sock.bind(("127.0.0.1", 0))
srv_sock.listen()
FAKE_PORT = srv_sock.getsockname()[1]
threading.Thread(target=fake_aprs_is, args=(srv_sock,), daemon=True).start()

PORT = 8095
data = tempfile.mkdtemp(prefix="taciss-aprs-")
env = dict(os.environ, TACISS_DATA=data)
PW = "correct-horse-1"
subprocess.run([sys.executable, APP, "create-user", "admin", "--role", "admin", "--password", PW], env=env, check=True,
               capture_output=True)
# point APRS at the fake server before the service starts
con = sqlite3.connect(os.path.join(data, "taciss.db"))
con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('aprs_server', ?)", (f"127.0.0.1:{FAKE_PORT}",))
con.commit()
con.close()
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

    def call(self, method, path, body=None):
        req = urllib.request.Request(BASE + path, method=method, headers={"X-Requested-With": "TACISS"})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.op.open(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


try:
    A = Client("admin")
    for _ in range(40):
        _, st = A.call("GET", "aprs/status")
        if st.get("connected") and st.get("stations", 0) >= 2:
            break
        time.sleep(0.25)
    check("connects to APRS-IS read-only", st.get("connected") and logins and " pass -1 " in logins[0], (st, logins))
    check("area filter sent at login", logins and "filter r/35.860/-86.350/150" in logins[0], logins[:1])
    _, stations = A.call("GET", "aprs/stations")
    calls = {x["call"]: x for x in stations}
    check("stations heard", "KD4XYZ-9" in calls and calls["W4WX"].get("wx", {}).get("temp_f") == 77, stations)
    _, pk = A.call("GET", "aprs/packets")
    check("raw packets kept", len(pk) >= 3)
    s, _ = Client().call("GET", "aprs/stations")
    check("signed-out users can't read APRS", s == 401, s)

    # sending needs a callsign + verified passcode
    _, st = A.call("GET", "aprs/status")
    check("no callsign: can't send, with reason", not st["me"]["can_send"] and "callsign" in st["me"]["reason"], st["me"])
    s, _ = A.call("POST", "aprs/send", {"type": "position", "lat": 36.1, "lon": -86.7})
    check("send refused without callsign", s == 400, s)
    s, users = A.call("GET", "users")
    A.call("PUT", f"users/{users[0]['id']}", {"callsign": "W3FDE"})
    s, e = A.call("PUT", "me/prefs", {"aprs_passcode": "12345"})
    check("wrong passcode rejected", s == 400 and "passcode" in e.get("error", ""), (s, e))
    good = str(taciss.aprs_passcode("W3FDE"))
    s, prefs = A.call("PUT", "me/prefs", {"aprs_passcode": good, "aprs_ssid": "9", "aprs_symbol": "/o",
                                          "aprs_watch": ["kd4xyz-9"]})
    check("passcode saved, never echoed back", s == 200 and prefs.get("aprs_passcode_set") is True
          and "aprs_passcode" not in prefs, prefs)
    _, me = A.call("GET", "me")
    check("/me hides the passcode", "aprs_passcode" not in json.dumps(me["user"]["prefs"]).replace("aprs_passcode_set", ""))
    s, e = A.call("PUT", "me/prefs", {"aprs_ssid": "22"})
    check("bad SSID rejected", s == 400, s)
    _, st = A.call("GET", "aprs/status")
    check("can send once set up", st["me"]["can_send"] and st["me"]["from"] == "W3FDE-9", st["me"])

    s, r = A.call("POST", "aprs/send", {"type": "position", "lat": 36.166667, "lon": -86.75, "comment": "EOC ° online"})
    time.sleep(0.5)
    check("position sent", s == 200 and received and received[-1] == "W3FDE-9>APZTAC,TCPIP*:=3610.00N/08645.00WoEOC  online",
          (s, r, received))
    check("aprs.fi link returned", s == 200 and r["aprsfi"].endswith("/W3FDE-9"), r)
    s, e = A.call("POST", "aprs/send", {"type": "message", "to": "KD4XYZ", "text": "hi"})
    check("rate limit between packets", s == 429, s)
    time.sleep(10.5)
    s, r = A.call("POST", "aprs/send", {"type": "object", "name": "SHELTER 2", "lat": 36.1, "lon": -86.8,
                                        "symbol": "\\z", "comment": "40 people"})
    time.sleep(0.5)
    check("object sent", s == 200 and received[-1].startswith("W3FDE-9>APZTAC,TCPIP*:;SHELTER 2*")
          and received[-1].endswith("3606.00N\\08648.00Wz40 people"), (s, r, received[-1:]))
    time.sleep(10.5)
    s, r = A.call("POST", "aprs/send", {"type": "message", "to": "kd4xyz", "text": "Copy, thanks"})
    time.sleep(0.5)
    check("message sent", s == 200 and received[-1].startswith("W3FDE-9>APZTAC,TCPIP*::KD4XYZ   :Copy, thanks{"),
          (s, r, received[-1:]))
    _, msgs = A.call("GET", "aprs/messages")
    texts = [m["text"] for m in msgs["mine"]]
    check("my messages: received and sent", "Hello W3FDE" in texts and "Copy, thanks" in texts, texts)
    s, e = A.call("GET", "aprs/lookup?name=KD4XYZ")
    check("aprs.fi lookup needs an API key", s == 400 and "aprs.fi" in e.get("error", ""), (s, e))
    _, st = A.call("GET", "settings")
    check("aprs.fi key is a secret setting", st.get("aprsfi_api_key") == "")
    s, _ = A.call("PUT", "settings", {"aprs_tx_enabled": "0"})
    _, st = A.call("GET", "aprs/status")
    check("admin can turn sending off", s == 200 and not st["me"]["can_send"], st["me"])
    s, _ = A.call("PUT", "settings", {"aprs_server": "not a server"})
    check("bad server setting rejected", s == 400, s)
finally:
    srv.terminate()
    srv.wait(5)
    LOG.close()
    srv_sock.close()
    shutil.rmtree(data, ignore_errors=True)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
