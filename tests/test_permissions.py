"""Who can change what: submitters, program admins, server admins (throwaway DB)."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error, http.cookiejar

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "taciss.py")
PORT = 8094
data = tempfile.mkdtemp(prefix="taciss-perm-")
env = dict(os.environ, TACISS_DATA=data, TACISS_APRS="0")
PW = "correct-horse-1"
subprocess.run([sys.executable, APP, "create-user", "admin", "--role", "admin", "--password", PW], env=env, check=True,
               capture_output=True)
LOG = open(os.path.join(data, "server.log"), "w")
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

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(BASE + path, method=method, headers={"X-Requested-With": "TACISS", **(headers or {})})
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


def status(client, method, path, body=None):
    return client.call(method, path, body if body is not None else ({} if method == "PUT" else None))[0]



def upload(client, pid, data, ctype="image/png"):
    req = urllib.request.Request(BASE + f"programs/{pid}/logo", method="PUT", data=data,
                                 headers={"X-Requested-With": "TACISS", "Content-Type": ctype})
    try:
        with client.op.open(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def fetch_raw(client, path):
    try:
        with client.op.open(urllib.request.Request(BASE + path)) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None, b""


try:
    A = Client("admin")
    _, p1 = A.call("POST", "programs", {"name": "Davidson ARES"})
    _, p2 = A.call("POST", "programs", {"name": "Knox Skywarn"})
    _, ag = A.call("POST", "agencies", {"name": "Davidson OEM"})
    P1, P2 = p1["id"], p2["id"]
    mk = lambda name, role, progs, admin=None: A.call("POST", "users", {"username": name, "role": role, "password": PW,
                                                                      "programs": progs, "program_admin": admin or []})
    mk("op1", "operator", [P1])
    mk("op2", "operator", [P1])
    s, pa = mk("padmin", "operator", [P1], [P1])
    check("create a program admin", s == 201 and pa["program_admin"] == [P1], pa)
    mk("op3", "operator", [P2])
    mk("vw", "viewer", [P1])
    op1, op2, padm, op3, vw = (Client(n) for n in ("op1", "op2", "padmin", "op3", "vw"))

    # programs / agencies: server admins create; operators can't touch them
    check("operator can't create a program", status(op1, "POST", "programs", {"name": "x"}) == 403)
    check("operator can't create an agency", status(op1, "POST", "agencies", {"name": "x"}) == 403)
    check("operator can't edit a program", status(op1, "PUT", f"programs/{P1}", {"notes": "x"}) == 403)
    check("operator can't edit an agency", status(op1, "PUT", f"agencies/{ag['id']}", {"notes": "x"}) == 403)
    check("operator can't delete a program", status(op1, "DELETE", f"programs/{P1}") == 403)
    check("program admin edits their program", status(padm, "PUT", f"programs/{P1}", {"notes": "ok"}) == 200)
    check("program admin can't delete their program (server admins only)", status(padm, "DELETE", f"programs/{P1}") == 403)
    _, one = padm.call("GET", f"programs/{P1}")
    check("program row says can_edit but not can_delete", one.get("can_edit") is True and one.get("can_delete") is False, one)
    check("program admin can't edit another program", status(padm, "PUT", f"programs/{P2}", {"notes": "x"}) == 403)
    check("program admin can't edit agencies", status(padm, "PUT", f"agencies/{ag['id']}", {"notes": "x"}) == 403)
    check("server admin edits agencies", status(A, "PUT", f"agencies/{ag['id']}", {"notes": "ok"}) == 200)

    # incidents: submitter, program admin of an assigned program, server admin
    _, inc = op1.call("POST", "incidents", {"title": "Storm", "programs": [P1]})
    I = inc["id"]
    check("submitter sees can_edit", inc.get("can_edit") is True, inc)
    check("submitter edits own incident", status(op1, "PUT", f"incidents/{I}", {"location": "Davidson"}) == 200)
    check("other operator can't edit it", status(op2, "PUT", f"incidents/{I}", {"status": "closed"}) == 403)
    check("other operator can't delete it", status(op2, "DELETE", f"incidents/{I}") == 403)
    check("operator from another program can't edit it", status(op3, "PUT", f"incidents/{I}", {"status": "closed"}) == 403)
    check("program admin edits incident in their program", status(padm, "PUT", f"incidents/{I}", {"priority": "high"}) == 200)
    check("server admin edits it", status(A, "PUT", f"incidents/{I}", {"priority": "normal"}) == 200)
    _, lst = op2.call("GET", "incidents")
    check("can_edit is false for others in lists", [x["can_edit"] for x in lst if x["id"] == I] == [False], lst)
    _, one = padm.call("GET", f"incidents/{I}")
    check("can_edit is true for the program admin", one.get("can_edit") is True)

    # reports / sitreps / storm reports
    _, rep = op2.call("POST", "reports", {"title": "Trees down", "kind": "storm_report", "incident_id": I})
    _, sit = op2.call("POST", "reports", {"title": "County sitrep", "kind": "sitrep"})
    check("submitter edits own report", status(op2, "PUT", f"reports/{rep['id']}", {"severity": "minor"}) == 200)
    check("other operator can't edit a storm report", status(op1, "PUT", f"reports/{rep['id']}", {"severity": "major"}) == 403)
    check("other operator can't delete a sitrep", status(op1, "DELETE", f"reports/{sit['id']}") == 403)
    check("program admin edits a report on their program's incident",
          status(padm, "PUT", f"reports/{rep['id']}", {"severity": "moderate"}) == 200)
    check("program admin can't edit a sitrep outside their program", status(padm, "PUT", f"reports/{sit['id']}", {"severity": "major"}) == 403)
    check("server admin edits any sitrep", status(A, "PUT", f"reports/{sit['id']}", {"severity": "major"}) == 200)
    _, vrep = vw.call("POST", "reports", {"title": "Hail", "kind": "storm_report"})
    check("viewer edits their own report", status(vw, "PUT", f"reports/{vrep['id']}", {"magnitude": "1.0 in"}) == 200)
    check("viewer can't edit someone else's", status(vw, "PUT", f"reports/{sit['id']}", {"severity": "info"}) == 403)
    check("other operator can't mark a report sent to NWS",
          status(op1, "POST", f"reports/{rep['id']}/nws", {"via": "phone"}) == 403)
    check("reporter marks it sent to NWS", status(op2, "POST", f"reports/{rep['id']}/nws", {"via": "phone"}) == 200)

    # check-ins: submitter, incident owner (net control), program admin
    _, ci = op2.call("POST", "checkins", {"incident_id": I, "callsign": "KD4XYZ"})
    check("net control (incident owner) updates others' check-ins",
          status(op1, "PUT", f"checkins/{ci['id']}", {"status": "checked_out"}) == 200)
    check("unrelated operator can't", status(op3, "PUT", f"checkins/{ci['id']}", {"status": "standby"}) == 403)

    # activity log is append-only
    _, act = op1.call("POST", "activity", {"incident_id": I, "body": "Net opened"})
    check("log entries can't be edited by their author", status(op1, "PUT", f"activity/{act['id']}", {"body": "x"}) == 403)
    check("program admin can't rewrite the log either", status(padm, "PUT", f"activity/{act['id']}", {"body": "x"}) == 403)

    # resources and cameras follow the same owner rule
    _, res = op1.call("POST", "resources", {"name": "Go-kit 1"})
    check("other operator can't edit a resource", status(op2, "PUT", f"resources/{res['id']}", {"status": "assigned"}) == 403)
    check("owner edits their resource", status(op1, "PUT", f"resources/{res['id']}", {"status": "assigned"}) == 200)

    # records synced from another node: a same-named local user doesn't own them
    con = sqlite3.connect(os.path.join(data, "taciss.db"))
    con.execute("UPDATE incidents SET mesh_origin = 'othernode0000000' WHERE id = ?", (I,))
    con.commit()
    con.close()
    check("same username on another node doesn't own synced records",
          status(op1, "PUT", f"incidents/{I}", {"location": "x"}) == 403)
    check("program admin still manages it", status(padm, "PUT", f"incidents/{I}", {"location": "y"}) == 200)

    # program admins create incidents (assigned to their program), resources and cameras, even as viewers
    mk("vadmin", "viewer", [P1], [P1])
    vad = Client("vadmin")
    s, vi = vad.call("POST", "incidents", {"title": "Program admin's incident"})
    check("viewer-role program admin creates an incident", s == 201, (s, vi))
    check("it is assigned to their program automatically", s == 201 and vi["programs"] == [P1], vi)
    s, _ = vad.call("POST", "incidents", {"title": "x", "programs": [P2]})
    check("...and it must include their program", s == 201 and _["programs"] == [P1, P2], _)
    check("program admin adds a resource", status(vad, "POST", "resources", {"name": "Generator 2"}) == 201)
    check("program admin adds a camera", status(vad, "POST", "cameras", {"name": "Tower cam"}) == 201)
    check("program admin logs on their program's incident", status(vad, "POST", "activity", {"incident_id": vi["id"], "body": "ok"}) == 201)
    _, other = A.call("POST", "incidents", {"title": "Other program's incident", "programs": [P2]})
    check("...but not on another program's incident",
          status(vad, "POST", "checkins", {"incident_id": other["id"], "callsign": "W1AW"}) == 403)
    check("plain viewer still can't create incidents", status(vw, "POST", "incidents", {"title": "x"}) == 403)
    check("plain viewer still can't add resources", status(vw, "POST", "resources", {"name": "x"}) == 403)
    # assigning an existing incident (opened by someone else) to their program
    _, county = A.call("POST", "incidents", {"title": "County-wide flood"})
    s, r = vad.call("PUT", f"incidents/{county['id']}", {"programs": [P1]})
    check("program admin assigns another's incident to their program", s == 200 and r["programs"] == [P1], (s, r))
    s, r = vad.call("PUT", f"incidents/{county['id']}", {"programs": [P1, P2]})
    check("...but can't add someone else's program", s == 403, s)
    check("tagging an outsider's incident doesn't grant edit rights over it",
          status(vad, "PUT", f"incidents/{county['id']}", {"title": "hijacked"}) == 403)
    s, r = vad.call("PUT", f"incidents/{other['id']}", {"programs": [P2, P1]})
    check("program admin assigns another program's incident to theirs too", s == 200 and r["programs"] == [P1, P2], (s, r))
    s, r = vad.call("PUT", f"incidents/{other['id']}", {"programs": []})
    check("...can't remove the other program from it", s == 403, (s, r))
    s, r = vad.call("PUT", f"incidents/{other['id']}", {"programs": [P2]})
    check("...can take their own program back off", s == 200 and r["programs"] == [P2], (s, r))
    s, r = op3.call("PUT", f"incidents/{county['id']}", {"programs": [P1, P2]})
    check("operator who isn't a program admin can't assign programs on others' incidents", s == 403, s)

    # program logos: program admin or server admin uploads; anyone signed in sees it
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    check("operator can't upload a program logo", upload(op1, P1, PNG)[0] == 403)
    check("program admin can't upload another program's logo", upload(padm, P2, PNG)[0] == 403)
    s, r = upload(padm, P1, PNG)
    check("program admin uploads their program's logo", s == 200 and (r.get("logo") or "").startswith("png:"), (s, r))
    st, ct, body = fetch_raw(op1, f"programs/{P1}/logo")
    check("members see the logo with the right type", st == 200 and ct == "image/png" and body == PNG, (st, ct))
    check("signed-out users can't fetch it", fetch_raw(Client(), f"programs/{P1}/logo")[0] == 401)
    check("non-images are refused (even if labeled as images)", upload(padm, P1, b"<svg onload=alert(1)>", "image/png")[0] == 415)
    check("HTML is refused", upload(padm, P1, b"<html><script>x</script>", "text/html")[0] == 415)
    check("oversized logos are refused", upload(padm, P1, b"\x89PNG\r\n\x1a\n" + b"0" * 1600000)[0] == 413)
    s, r = upload(A, P1, b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg")
    check("server admin replaces it with a JPEG", s == 200 and r["logo"].startswith("jpg:"), r)
    st, ct, _ = fetch_raw(op1, f"programs/{P1}/logo")
    check("replacement is served as JPEG", st == 200 and ct == "image/jpeg", (st, ct))
    _, me = padm.call("GET", "me")
    check("/me program info carries the logo", (me["user"]["program_info"][0].get("logo") or "").startswith("jpg:"), me["user"]["program_info"])
    check("operator can't remove it", op1.call("DELETE", f"programs/{P1}/logo")[0] == 403)
    s, r = padm.call("DELETE", f"programs/{P1}/logo")
    check("program admin removes it", s == 200 and not r.get("logo"), r)
    check("no logo: 404", fetch_raw(op1, f"programs/{P1}/logo")[0] == 404)
    check("logo can't be set through a normal edit", status(padm, "PUT", f"programs/{P1}", {"logo": "png:evil"}) == 200
          and not padm.call("GET", f"programs/{P1}")[1].get("logo"))

    # weather station preference
    s, pr = vw.call("PUT", "me/prefs", {"wx_station": "kbna"})
    check("user picks their weather station", s == 200 and pr.get("wx_station") == "KBNA", pr)
    check("bad station IDs are refused", vw.call("PUT", "me/prefs", {"wx_station": "../etc"})[0] == 400)
    check("bad station lookup refused", vw.call("GET", "weather/station?id=../x")[0] == 400)

    # program rosters
    _, me = padm.call("GET", "me")
    check("/me lists program_admin", me["user"]["program_admin"] == [P1], me["user"])
    _, det = padm.call("GET", f"members/programs/{P1}", headers={"X-Members-Detail": "1"})
    check("program admin can manage their roster", det["can_manage"] is True and
          {m["username"]: m["member_role"] for m in det["members"]}.get("padmin") == "admin", det)
    _, det = op1.call("GET", f"members/programs/{P1}", headers={"X-Members-Detail": "1"})
    check("operator can't", det["can_manage"] is False)
    check("people list for program admins", status(padm, "GET", "members/people") == 200)
    check("people list refused for operators", status(op1, "GET", "members/people") == 403)
    _, ppl = padm.call("GET", "members/people")
    ids = {p["username"]: p["id"] for p in ppl}
    s, _ = padm.call("PUT", f"members/programs/{P1}", {"user_ids": [ids["padmin"], ids["op1"], ids["op2"], ids["op3"]],
                                                         "admin_ids": [ids["padmin"], ids["op2"]]})
    check("program admin adds a member and a second program admin", s == 200)
    _, me = op2.call("GET", "me")
    check("op2 is now a program admin", me["user"]["program_admin"] == [P1], me["user"])
    check("new program admin can edit the program's incident", status(op2, "PUT", f"incidents/{I}", {"location": "z"}) == 200)
    s, e = padm.call("PUT", f"members/programs/{P1}", {"user_ids": [ids["padmin"]], "admin_ids": []})
    check("program admin can't drop their own admin role", s == 400, (s, e))
    check("program admin can't change another program's roster",
          status(padm, "PUT", f"members/programs/{P2}", {"user_ids": []}) == 403)
    check("program admin can't change agency rosters",
          status(padm, "PUT", f"members/agencies/{ag['id']}", {"user_ids": []}) == 403)
    check("operator can't change rosters", status(op1, "PUT", f"members/programs/{P1}", {"user_ids": []}) == 403)

    # server admin sets program admins from the user form
    _, users = A.call("GET", "users")
    uid = {u["username"]: u["id"] for u in users}
    s, u = A.call("PUT", f"users/{uid['op1']}", {"program_admin": [P1]})
    check("server admin promotes via user form", s == 200 and u["program_admin"] == [P1] and u["programs"] == [P1], u)
    s, u = A.call("PUT", f"users/{uid['op1']}", {"programs": [P1, P2]})
    check("changing programs keeps program-admin roles", u["program_admin"] == [P1] and u["programs"] == [P1, P2], u)
    s, u = A.call("PUT", f"users/{uid['op1']}", {"programs": [P2]})
    check("leaving a program drops its admin role", u["program_admin"] == [] and u["programs"] == [P2], u)
finally:
    srv.terminate()
    srv.wait(5)
    LOG.close()
    shutil.rmtree(data, ignore_errors=True)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
