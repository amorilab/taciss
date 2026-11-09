"""3-node mesh test: A <-> B <-> C (A and C never talk directly). Throwaway DBs."""
import http.cookiejar, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.error

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "taciss.py")
PW = "correct-horse-1"
NODES = {"A": 8101, "B": 8102, "C": 8103}
fails = 0
procs, dirs, logs = {}, {}, {}


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""), flush=True)
    fails += 0 if cond else 1


def start(n):
    env = dict(os.environ, TACISS_DATA=dirs[n])
    logs[n] = open(os.path.join(dirs[n], "server.log"), "a")
    procs[n] = subprocess.Popen([sys.executable, "-u", APP, "serve", "--port", str(NODES[n]), "--host", "127.0.0.1"],
                                env=env, stdout=logs[n], stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{NODES[n]}/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)


def stop(n):
    procs[n].terminate()
    procs[n].wait(10)
    logs[n].close()


class Client:
    def __init__(self, n):
        self.base = f"http://127.0.0.1:{NODES[n]}/api/"
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "login", {"username": "admin", "password": PW})

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, headers={"X-Requested-With": "TACISS"})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.op.open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def wait(fn, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.3)
    return None


def find(c, ent, **kw):
    rows = c.call("GET", ent)[1]
    return next((r for r in rows if all(r.get(k) == v for k, v in kw.items())), None)


try:
    for n in NODES:
        dirs[n] = tempfile.mkdtemp(prefix=f"taciss-mesh-{n}-")
        subprocess.run([sys.executable, APP, "create-user", "admin", "--role", "admin", "--password", PW],
                       env=dict(os.environ, TACISS_DATA=dirs[n]), check=True, capture_output=True)
        start(n)
    cl = {n: Client(n) for n in NODES}
    for n, c in cl.items():
        s, _ = c.call("PUT", "settings", {"wx_enabled": "0", "feeds_enabled": "0", "mesh_node_name": f"Node {n}",
                                          "mesh_url": f"http://127.0.0.1:{NODES[n]}", "mesh_interval": "5"})
        check(f"settings on {n}", s == 200)
    A, B, C = cl["A"], cl["B"], cl["C"]

    # --- link policy
    s, e = A.call("POST", "mesh/peers", {"url": "https://10.1.1.1", "link": "aredn", "code": "X" * 20})
    check("AREDN refuses https (Part 97)", s == 400 and "Part 97" in e["error"], e)
    s, e = A.call("POST", "mesh/peers", {"url": "http://example.org", "link": "internet", "code": "X" * 20})
    check("internet link requires https", s == 400, e)
    s, e = B.call("POST", "mesh/peers", {"url": "http://127.0.0.1:8101", "link": "lan", "code": "WRONG-CODE-ABCD-EFGH-JKLM"})
    check("wrong pairing code rejected", s == 502 and "not recognized" in e["error"], e)

    # --- pair A<->B (B joins with A's code), B<->C (C joins with B's code)
    s, inv = A.call("POST", "mesh/invites", {"link": "lan", "note": "for B"})
    check("A creates invite", s == 201 and len(inv["code"]) == 24, inv)
    s, j = B.call("POST", "mesh/peers", {"url": "http://127.0.0.1:8101", "link": "lan", "code": inv["code"].lower()})
    check("B joins A", s == 201 and j["peer"]["name"] == "Node A", j)
    s, st = A.call("GET", "mesh")
    check("A lists B as peer, invite used up", any(p["name"] == "Node B" for p in st["peers"]) and not st["invites"], st)
    s, j2 = B.call("POST", "mesh/peers", {"url": "http://127.0.0.1:8101", "link": "lan", "code": inv["code"]})
    check("invite can't be reused by a different... (same node re-pair ok)", s in (201, 502))
    _, inv2 = B.call("POST", "mesh/invites", {"link": "aredn"})
    s, j = C.call("POST", "mesh/peers", {"url": "http://127.0.0.1:8102", "link": "aredn", "code": inv2["code"]})
    check("C joins B over 'AREDN' (http)", s == 201, j)

    # --- create on A -> shows up on B and C (relay)
    _, prog = A.call("POST", "programs", {"name": "Knox ARES", "kind": "ARES"})
    _, inc = A.call("POST", "incidents", {"title": "Tornado warning Knox", "kind": "tornado", "programs": [prog["id"]]})
    ib = wait(lambda: find(B, "incidents", title="Tornado warning Knox"))
    check("incident A -> B", ib is not None)
    ic = wait(lambda: find(C, "incidents", title="Tornado warning Knox"))
    check("incident A -> C via B (store-and-forward)", ic is not None)
    if ic:
        check("program refs translated on C", [p["name"] for p in C.call("GET", "programs")[1] if p["id"] in ic["programs"]] == ["Knox ARES"], ic)
        check("creator shown as admin@Node A on C", ic["created_by"] == "admin@Node A", ic["created_by"])
        check("origin tagged", ic["mesh_origin"] == A.call("GET", "mesh")[1]["node"]["id"])
    check("creator stays plain 'admin' on A", find(A, "incidents", title="Tornado warning Knox")["created_by"] == "admin")

    # --- child records from C reach A attached to the right incident; chat channel too
    C.call("POST", "activity", {"incident_id": ic["id"], "body": "Spotter on I-40 reports rotation"})
    C.call("POST", "messages", {"channel": f"incident-{ic['id']}", "body": "net control copies"})
    C.call("POST", "checkins", {"incident_id": ic["id"], "callsign": "KC4XYZ", "name": "Field Pi 3"})
    act = wait(lambda: find(A, "activity", body="Spotter on I-40 reports rotation"))
    check("activity C -> A (2 hops)", act is not None and act["incident_id"] == inc["id"], act)
    msg = wait(lambda: find(A, "messages", body="net control copies"))
    check("incident chat channel mapped", msg is not None and msg["channel"] == f"incident-{inc['id']}", msg)
    ck = wait(lambda: find(A, "checkins", callsign="KC4XYZ"))
    check("check-in C -> A", ck is not None and ck["incident_id"] == inc["id"])

    # --- edits converge; last writer wins
    A.call("PUT", f"incidents/{inc['id']}", {"status": "monitoring"})
    time.sleep(0.05)
    C.call("PUT", f"incidents/{ic['id']}", {"status": "closed", "priority": "high"})
    def converged():
        vals = [(find(x, "incidents", title="Tornado warning Knox") or {}) for x in (A, B, C)]
        return all(v.get("status") == "closed" and v.get("priority") == "high" for v in vals)
    check("concurrent edits converge (newest wins everywhere)", wait(converged, 25))

    # --- deletes propagate
    _, rep = B.call("POST", "reports", {"title": "Hail 1.75in", "kind": "storm_report", "incident_id": ib["id"]})
    check("report B -> C", wait(lambda: find(C, "reports", title="Hail 1.75in")) is not None)
    rc = find(C, "reports", title="Hail 1.75in")
    C.call("DELETE", f"reports/{rc['id']}")
    check("delete C -> A", wait(lambda: find(A, "reports", title="Hail 1.75in") is None and find(B, "reports", title="Hail 1.75in") is None))

    # --- restricted incidents are not shared unless the peer is trusted
    _, sec = A.call("POST", "incidents", {"title": "Hospital comms (locked)", "visibility": "restricted"})
    A.call("POST", "activity", {"incident_id": sec["id"], "body": "secret entry"})
    _, marker = A.call("POST", "incidents", {"title": "after-locked marker"})
    wait(lambda: find(B, "incidents", title="after-locked marker"))
    check("restricted incident NOT sent to untrusted B", find(B, "incidents", title="Hospital comms (locked)") is None)
    check("its log NOT sent either", find(B, "activity", body="secret entry") is None)
    peers = A.call("GET", "mesh")[1]["peers"]
    pb = next(p for p in peers if p["name"] == "Node B")
    A.call("PUT", f"mesh/peers/{pb['id']}", {"share_restricted": True})
    A.call("POST", f"mesh/peers/{pb['id']}/sync")
    check("trusted B now gets restricted incident", wait(lambda: find(B, "incidents", title="Hospital comms (locked)")) is not None)
    check("... and its log", wait(lambda: find(B, "activity", body="secret entry")) is not None)
    time.sleep(3)
    check("C (B doesn't trust C) still doesn't have it", find(C, "incidents", title="Hospital comms (locked)") is None)
    A.call("PUT", f"incidents/{sec['id']}", {"visibility": "open"})
    check("unlocking shares it with C", wait(lambda: find(C, "incidents", title="Hospital comms (locked)")) is not None)
    check("... including its log", wait(lambda: find(C, "activity", body="secret entry")) is not None)
    A.call("PUT", f"incidents/{sec['id']}", {"visibility": "restricted"})
    check("re-locking removes it from C", wait(lambda: find(C, "incidents", title="Hospital comms (locked)") is None))
    check("... and its log", wait(lambda: find(C, "activity", body="secret entry") is None))
    check("B (trusted) keeps it", find(B, "incidents", title="Hospital comms (locked)") is not None)

    # --- node offline then back: store and forward
    stop("B")
    A.call("POST", "incidents", {"title": "Created while B was down"})
    C.call("POST", "reports", {"title": "C report while B down"})
    time.sleep(2)
    _, st = A.call("GET", "mesh")
    pb = next(p for p in st["peers"] if p["name"] == "Node B")
    A.call("POST", f"mesh/peers/{pb['id']}/sync")
    err = wait(lambda: next(p for p in A.call("GET", "mesh")[1]["peers"] if p["name"] == "Node B")["last_error"])
    check("A shows error for offline B", err and "reach" in err, err)
    start("B")
    cl["B"] = B = Client("B")
    check("after B returns: A's record reaches C", wait(lambda: find(C, "incidents", title="Created while B was down"), 30) is not None)
    check("after B returns: C's record reaches A", wait(lambda: find(A, "reports", title="C report while B down"), 30) is not None)

    # --- security: forged peer calls
    req = urllib.request.Request("http://127.0.0.1:8101/api/mesh/changes?since=0",
                                 headers={"X-Mesh-Node": "0123456789abcdef", "X-Mesh-Time": "1", "X-Mesh-Sig": "00"})
    try:
        urllib.request.urlopen(req)
        check("unknown node rejected", False)
    except urllib.error.HTTPError as e:
        check("unknown node rejected", e.code == 403)
    nidB = B.call("GET", "mesh")[1]["node"]["id"]
    req = urllib.request.Request("http://127.0.0.1:8101/api/mesh/changes", method="POST", data=b'{"items":[]}',
                                 headers={"X-Mesh-Node": nidB, "X-Mesh-Time": "1", "X-Mesh-Sig": "bad",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
        check("forged signature rejected", False)
    except urllib.error.HTTPError as e:
        check("forged signature rejected", e.code == 403)
    A.call("POST", "users", {"username": "viewer1", "role": "viewer", "password": PW})
    v = Client.__new__(Client)
    v.base, v.op = A.base, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    v.call("POST", "login", {"username": "viewer1", "password": PW})
    check("viewer can't see mesh status/peers", v.call("GET", "mesh")[0] == 403)
    check("viewer can read node names", v.call("GET", "mesh/nodes")[0] == 200)

    # --- counts match everywhere
    def same():
        out = []
        for x in (A, B, C):
            out.append(sorted(r["title"] for r in x.call("GET", "incidents")[1] if "locked" not in r["title"]))
        return out[0] == out[1] == out[2] and out[0]
    check("A, B, C agree on open incidents", wait(same, 20), [sorted(r["title"] for r in x.call("GET", "incidents")[1]) for x in (A, B, C)])
    ok = wait(lambda: not next(p for p in A.call("GET", "mesh")[1]["peers"] if p["name"] == "Node B")["last_error"], 30)
    check("A's own sync to B recovers (error clears)", ok)
    _, st = A.call("GET", "mesh")
    print("A status:", json.dumps({k: st[k] for k in ("log", "pending")}), [(p["name"], p["sent"], p["received"], p["last_error"]) for p in st["peers"]])
finally:
    for n in list(procs):
        try:
            stop(n)
        except Exception:
            pass
    for n in dirs:
        out = open(os.path.join(dirs[n], "server.log"), errors="replace").read()
        if "Traceback" in out or "mesh: skipped" in out:
            print(f"---- {n} log ----\n" + out[-4000:])
        shutil.rmtree(dirs[n], ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
