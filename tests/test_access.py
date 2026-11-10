"""End-to-end test of restricted incidents / program & agency membership (throwaway DB)."""
import json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, http.cookiejar

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "taciss.py")
PORT = 8097
data = tempfile.mkdtemp(prefix="taciss-acl-")
env = dict(os.environ, TACISS_DATA=data, TACISS_APRS="0")
PW = "correct-horse-1"


def cli(*a):
    subprocess.run([sys.executable, APP, *a], env=env, check=True, capture_output=True)


cli("create-user", "admin", "--role", "admin", "--password", PW)
LOG = open(os.path.join(data, 'server.log'), 'w')
srv = subprocess.Popen([sys.executable, "-u", APP, "serve", "--port", str(PORT), "--host", "127.0.0.1"], env=env,
                       stdout=LOG, stderr=subprocess.STDOUT)
time.sleep(1.5)
BASE = f"http://127.0.0.1:{PORT}/api/"
fails = 0


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


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


try:
    A = Client("admin")
    _, p1 = A.call("POST", "programs", {"name": "Davidson ARES", "kind": "ARES"})
    _, p2 = A.call("POST", "programs", {"name": "Knox Skywarn", "kind": "Skywarn"})
    _, a1 = A.call("POST", "agencies", {"name": "Davidson OEM"})
    _, a2 = A.call("POST", "agencies", {"name": "Knox EMA"})
    s, _ = A.call("PUT", f"programs/{p1['id']}", {"agencies": [a1["id"]]})
    check("program can list served agencies", s == 200)

    # users: op1 in p1, op2 in p2, agency user in a1, plain viewer
    s, u = A.call("POST", "users", {"username": "op1", "role": "operator", "password": PW, "programs": [p1["id"]]})
    check("create user with program", s == 201 and u["programs"] == [p1["id"]], u)
    A.call("POST", "users", {"username": "op2", "role": "operator", "password": PW, "programs": [p2["id"]]})
    s, ag = A.call("POST", "users", {"username": "oem", "role": "viewer", "password": PW, "agencies": [a1["id"]]})
    check("create user with agency", s == 201 and ag["agencies"] == [a1["id"]], ag)
    A.call("POST", "users", {"username": "viewer", "role": "viewer", "password": PW})
    s, _ = A.call("POST", "users", {"username": "bad", "role": "viewer", "password": PW, "programs": [999]})
    check("unknown program id rejected", s == 400)

    op1, op2, oem, vw = Client("op1"), Client("op2"), Client("oem"), Client("viewer")
    s, me = oem.call("GET", "me")
    check("/me includes agencies", me["user"]["agencies"] == [a1["id"]])

    # op2 opens a restricted incident assigned to p1 serving a1
    s, inc = op2.call("POST", "incidents", {"title": "Locked op", "visibility": "restricted",
                                             "programs": [p1["id"]], "agencies": [a1["id"]]})
    check("create restricted incident", s == 201 and inc["programs"] == [p1["id"]], inc)
    _, open_inc = op2.call("POST", "incidents", {"title": "Open op"})
    check("default visibility is open", open_inc["visibility"] == "open")
    iid = inc["id"]
    A.call("POST", "activity", {"incident_id": iid, "body": "secret log"})
    A.call("POST", "checkins", {"incident_id": iid, "callsign": "N0CALL"})
    A.call("POST", "reports", {"title": "secret report", "incident_id": iid})
    A.call("POST", "resources", {"name": "secret kit", "incident_id": iid})
    A.call("POST", "messages", {"channel": f"incident-{iid}", "body": "secret chat"})

    def sees(c):
        return any(i["id"] == iid for i in c.call("GET", "incidents")[1])

    check("admin sees restricted", sees(A))
    check("creator (op2, other program) sees restricted", sees(op2))
    check("program member op1 sees restricted", sees(op1))
    check("served-agency user sees restricted", sees(oem))
    check("plain viewer does NOT see restricted", not sees(vw))
    check("plain viewer still sees open incident", any(i["id"] == open_inc["id"] for i in vw.call("GET", "incidents")[1]))
    check("viewer GET restricted -> 404", vw.call("GET", f"incidents/{iid}")[0] == 404)
    for ent in ("activity", "checkins", "reports", "resources"):
        rows = vw.call("GET", ent)[1]
        check(f"viewer cannot see {ent} of restricted", not any(r.get("incident_id") == iid for r in rows))
        rows = op1.call("GET", ent)[1]
        check(f"member sees {ent} of restricted", any(r.get("incident_id") == iid for r in rows))
    check("viewer cannot see restricted chat", not any(m["channel"] == f"incident-{iid}" for m in vw.call("GET", "messages")[1]))
    check("viewer cannot post to restricted chat", vw.call("POST", "messages", {"channel": f"incident-{iid}", "body": "x"})[0] == 404)
    check("viewer cannot attach report to restricted", vw.call("POST", "reports", {"title": "x", "incident_id": iid})[0] == 400)
    st = vw.call("GET", "stats")[1]
    check("stats exclude restricted for viewer", sum(st["incidents"].values()) == 1, st["incidents"])

    # operator outside the program: can't see / edit / delete
    A.call("POST", "users", {"username": "op3", "role": "operator", "password": PW})
    op3 = Client("op3")
    check("non-member operator PUT -> 404", op3.call("PUT", f"incidents/{iid}", {"title": "hijack"})[0] == 404)
    check("non-member operator DELETE -> 404", op3.call("DELETE", f"incidents/{iid}")[0] == 404)
    check("non-member operator cannot log to it", op3.call("POST", "activity", {"incident_id": iid, "body": "x"})[0] == 400)

    # membership edited from the program side gives access
    s, mem = A.call("PUT", f"members/programs/{p1['id']}", {"user_ids": [op1_id := u["id"], ]})
    s, users = A.call("GET", "users")
    op3_id = next(x["id"] for x in users if x["username"] == "op3")
    s, mem = A.call("PUT", f"members/programs/{p1['id']}", {"user_ids": [op1_id, op3_id]})
    check("members PUT", s == 200 and {m["username"] for m in mem} == {"op1", "op3"}, mem)
    check("op3 sees after being added to program", sees(Client("op3")))
    check("viewer cannot edit members", vw.call("PUT", f"members/programs/{p1['id']}", {"user_ids": []})[0] == 403)
    check("operator cannot edit members", op1.call("PUT", f"members/programs/{p1['id']}", {"user_ids": []})[0] == 403)
    check("members GET for signed-in user", vw.call("GET", f"members/programs/{p1['id']}")[0] == 200)

    # opening the incident makes it visible to everyone
    check("a program member who didn't open it can't unlock it",
          op1.call("PUT", f"incidents/{iid}", {"visibility": "open"})[0] == 403)
    op2.call("PUT", f"incidents/{iid}", {"visibility": "open"})  # the operator who opened it
    check("after unlocking, viewer sees it", sees(vw))
    op2.call("PUT", f"incidents/{iid}", {"visibility": "restricted"})
    check("re-locked, viewer loses it", not sees(vw))

    # live stream: viewer must not be told about restricted changes; member must
    got = {"vw": [], "op1": []}

    def listen(name, c):
        req = urllib.request.Request(BASE + "stream")
        with c.op.open(req, timeout=6) as r:
            t0 = time.time()
            while time.time() - t0 < 4:
                line = r.readline().decode()
                if line.startswith("data:"):
                    got[name].append(json.loads(line[5:]))

    ths = [threading.Thread(target=listen, args=(n, c), daemon=True) for n, c in (("vw", vw), ("op1", op1))]
    [t.start() for t in ths]
    time.sleep(0.8)
    op1.call("POST", "activity", {"incident_id": iid, "body": "live"})
    op1.call("POST", "activity", {"incident_id": open_inc["id"], "body": "live open"})
    [t.join(7) for t in ths]
    check("SSE: member gets restricted event", any(e["entity"] == "activity" for e in got["op1"]) and len(got["op1"]) == 2, got["op1"])
    check("SSE: viewer gets only the open event", len(got["vw"]) == 1, got["vw"])
    check("SSE: no acl leaked", all("acl" not in e for e in got["op1"] + got["vw"]))

    # deleting a program strips it from incidents/user memberships
    A.call("DELETE", f"programs/{p1['id']}")
    s, inc2 = A.call("GET", f"incidents/{iid}")
    check("program delete removes it from incident.programs", inc2["programs"] == [], inc2["programs"])
    s, me1 = op1.call("GET", "me")
    check("program delete removes memberships", me1["user"]["programs"] == [], me1["user"])
    check("agency user still sees via agency", sees(oem))
finally:
    srv.terminate()
    srv.wait(5); LOG.close()
    out = open(os.path.join(data, 'server.log'), errors='replace').read()
    if "Traceback" in out:
        print(out[-3000:])
    shutil.rmtree(data, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
