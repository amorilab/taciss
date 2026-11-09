"""End-to-end test of the incident debrief kit PDF (throwaway DB)."""
import json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.error, http.cookiejar, zlib

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "taciss.py")
PORT = 8096
data = tempfile.mkdtemp(prefix="taciss-debrief-")
env = dict(os.environ, TACISS_DATA=data, TACISS_APRS="0")
PW = "correct-horse-1"


def cli(*a):
    subprocess.run([sys.executable, APP, *a], env=env, check=True, capture_output=True)


cli("create-user", "admin", "--role", "admin", "--password", PW)
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

    def call(self, method, path, body=None, raw=False):
        req = urllib.request.Request(BASE + path, method=method, headers={"X-Requested-With": "TACISS"})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.op.open(req) as r:
                b = r.read()
                return r.status, (b, dict(r.headers)) if raw else json.loads(b or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def pdf_text(pdf):
    """Decompress every page stream and pull out the shown strings."""
    out = []
    for m in re.finditer(rb"stream\n(.*?)\nendstream", pdf, re.S):
        try:
            s = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        out += [t.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\").decode("cp1252")
                for t in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", s)]
    return "\n".join(out)


try:
    A = Client("admin")
    _, prog = A.call("POST", "programs", {"name": "Davidson ARES", "kind": "ARES", "coordinator": "Pat Doe"})
    _, ag = A.call("POST", "agencies", {"name": "Davidson OEM"})
    _, inc = A.call("POST", "incidents", {"title": "Tornado warning (exercise)", "kind": "tornado",
                                          "programs": [prog["id"]], "agencies": [ag["id"]],
                                          "commander": "W4ABC", "radio_net": "146.940 (-) 107.2",
                                          "description": "Line of storms crossing the county. ° é – “quotes”"})
    iid = inc["id"]
    A.call("POST", "activity", {"incident_id": iid, "body": "Net opened at 1900. 6 stations."})
    A.call("POST", "activity", {"incident_id": iid, "body": "Very long entry " + "lorem ipsum " * 900})
    A.call("POST", "reports", {"title": "Trees down on Main St", "kind": "storm_report", "incident_id": iid,
                               "hazard": "wind_damage", "magnitude": "60 mph", "body": "Two trees (1 large) across road."})
    A.call("POST", "checkins", {"incident_id": iid, "callsign": "KD4XYZ", "name": "Sam", "assignment": "Shelter 2"})
    A.call("POST", "resources", {"name": "Go-kit 3", "incident_id": iid, "status": "assigned"})
    A.call("POST", "messages", {"channel": f"incident-{iid}", "body": "Copy, moving to shelter"})
    A.call("POST", "messages", {"channel": "general", "body": "unrelated general chat"})
    _, other = A.call("POST", "incidents", {"title": "Other incident"})
    A.call("POST", "activity", {"incident_id": other["id"], "body": "belongs to the other incident"})

    s, err = A.call("GET", f"incidents/{iid}/debrief.pdf")
    check("open incident: debrief refused with 409", s == 409, (s, err))

    s, _ = A.call("PUT", f"incidents/{iid}", {"status": "closed"})
    check("close incident", s == 200)
    s, (pdf, hdr) = A.call("GET", f"incidents/{iid}/debrief.pdf", raw=True)
    check("closed incident: PDF downloads", s == 200 and hdr.get("Content-Type") == "application/pdf", hdr)
    check("file name in Content-Disposition", f"debrief-incident-{iid}-tornado-warning-exercise.pdf"
          in hdr.get("Content-Disposition", ""), hdr.get("Content-Disposition"))
    check("valid PDF header and trailer", pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"))
    # xref offsets point at the objects they claim to
    xref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    offs = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", pdf[xref:])]
    check("xref offsets are correct", all(re.match(rb"\d+ 0 obj", pdf[o:o + 12]) for o in offs) and len(offs) > 8)
    pages = int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", pdf).group(1))
    check("multi-page packet", pages >= 5, pages)
    text = pdf_text(pdf)
    for needle in ("INCIDENT DEBRIEF KIT", "Tornado warning (exercise)", "Davidson ARES", "Davidson OEM",
                   "Net opened at 1900", "Trees down on Main St", "Wind damage", "60 mph", "KD4XYZ", "Shelter 2",
                   "Go-kit 3", "Copy, moving to shelter", "W4ABC", "146.940 (-) 107.2", "Debrief worksheet",
                   "Change history", "Line of storms crossing the county. ° é – “quotes”", f"Page {pages} of {pages}"):
        check(f"packet contains {needle!r}", needle in text)
    check("long entry is not lost (wraps across pages)", text.count("lorem ipsum") > 800, text.count("lorem ipsum"))
    check("other incident's data is not included", "belongs to the other incident" not in text)
    check("general chat is not included", "unrelated general chat" not in text)

    # access: a viewer can download an open-access closed incident; signed-out users cannot
    A.call("POST", "users", {"username": "viewer", "role": "viewer", "password": PW})
    s, _ = Client("viewer").call("GET", f"incidents/{iid}/debrief.pdf", raw=True)
    check("viewer can download", s == 200, s)
    s, _ = Client().call("GET", f"incidents/{iid}/debrief.pdf")
    check("signed-out user gets 401", s == 401, s)
    # restricted incident: hidden from a viewer who isn't in its programs/agencies
    A.call("PUT", f"incidents/{iid}", {"visibility": "restricted"})
    s, _ = Client("viewer").call("GET", f"incidents/{iid}/debrief.pdf")
    check("restricted incident hidden from outsider (404)", s == 404, s)
    s, (pdf2, _) = A.call("GET", f"incidents/{iid}/debrief.pdf", raw=True)
    check("restricted packet is marked RESTRICTED", s == 200 and "RESTRICTED INCIDENT" in pdf_text(pdf2))
    s, _ = A.call("GET", "incidents/99999/debrief.pdf")
    check("unknown incident 404", s == 404, s)
finally:
    srv.terminate()
    srv.wait(5)
    LOG.close()
    shutil.rmtree(data, ignore_errors=True)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
