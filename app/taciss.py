#!/usr/bin/env python3
"""
TACISS - Tactical Amateur Communication Information Sharing System
Self-hosted edition. Pure Python 3 standard library (no pip installs),
so it runs on a stock Debian 13 box with zero internet access.

  python3 taciss.py serve                 # run the server
  python3 taciss.py create-user admin --role admin
  python3 taciss.py reset-password admin
  python3 taciss.py backup /path/to/file.db
  python3 taciss.py seed-demo             # load example data

Configuration is via environment variables (see deploy/taciss.env).
"""
import argparse
import collections
import csv
import getpass
import gzip
import hashlib
import hmac
import io
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import traceback
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urlparse

VERSION = "1.2.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CONFIG = {
    "host": os.environ.get("TACISS_HOST", "0.0.0.0"),
    "port": int(os.environ.get("TACISS_PORT", "8080")),
    "data_dir": os.environ.get("TACISS_DATA", os.path.join(BASE_DIR, "..", "data")),
    # Allow signed-out visitors to see the dashboard, programs, agencies, incidents and reports.
    "public_read": env_bool("TACISS_PUBLIC_READ", False),
    # Set when running behind Caddy/nginx/Cloudflare so client IPs and https are detected.
    "trust_proxy": env_bool("TACISS_TRUST_PROXY", False),
    # Force the Secure flag on cookies (always do this when reachable from the internet).
    "secure_cookies": env_bool("TACISS_SECURE_COOKIES", False),
    "session_hours": int(os.environ.get("TACISS_SESSION_HOURS", "72")),
    # GitHub repo for `taciss update` and the "new version available" notice; 0 turns the daily check off.
    "repo": os.environ.get("TACISS_REPO", "amorilab/taciss"),
    "update_check": env_bool("TACISS_UPDATE_CHECK", True),
    # Public address (e.g. https://taciss.com) used in link previews on Facebook and other sites.
    "public_url": os.environ.get("TACISS_PUBLIC_URL", "").strip().rstrip("/"),
    # APRS-IS live feed (also needs aprs_enabled=1 in settings); 0 keeps the server off APRS-IS entirely.
    "aprs": env_bool("TACISS_APRS", True),
}

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
MAX_BODY = 1024 * 1024
MAX_TEXT = 20000
PBKDF2_ITER = 600_000
SHUTDOWN = threading.Event()


def db_path():
    return os.path.join(CONFIG["data_dir"], "taciss.db")


def setup_code_path():
    return os.path.join(CONFIG["data_dir"], "setup-code")


def ensure_setup_code():
    """While no accounts exist, keep a one-time code that the web setup page asks for.
    It is shown on the server console, so only someone with access to the box can claim it."""
    init_db()
    con = connect()
    n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    path = setup_code_path()
    if n:
        if os.path.exists(path):
            os.remove(path)
        return None
    if not os.path.exists(path):
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(code + "\n")
    with open(path) as fh:
        return fh.read().strip()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Data model. The frontend builds its forms and tables from this, so adding a
# field here is all it takes (the column is added to the DB on next start).
# --------------------------------------------------------------------------
def F(name, label, type="text", required=False, choices=None, list=False, ref=None, default=None, hidden=False):
    # hidden = set by the server only (not shown in forms, not accepted from clients)
    return {"name": name, "label": label, "type": type, "required": required,
            "choices": choices, "list": list, "ref": ref, "default": default, "hidden": hidden}


STORM_HAZARDS = ["tornado", "funnel_cloud", "wall_cloud", "rotation", "hail", "wind_damage", "high_wind",
                 "flash_flood", "flood", "heavy_rain", "lightning_damage", "snow_ice", "other"]


INCIDENT_KINDS = ["severe_weather", "tornado", "flood", "winter_storm", "wildfire", "hazmat",
                  "search_rescue", "power_outage", "comms_outage", "public_event", "exercise", "other"]

ENTITIES = {
    "incidents": {
        "label": "Incidents", "singular": "Incident", "public": True,
        "create": "operator", "edit": "owner", "delete": "owner",  # owner = submitter, program admin or server admin (Handler.can_modify)
        "program_admin_create": True,  # program admins may add these even below the create role
        "fields": [
            F("title", "Title", required=True, list=True),
            F("kind", "Type", choices=INCIDENT_KINDS, list=True, default="severe_weather"),
            F("status", "Status", choices=["active", "monitoring", "closed"], list=True, default="active"),
            F("priority", "Priority", choices=["low", "normal", "high", "critical"], list=True, default="normal"),
            F("visibility", "Access", choices=["open", "restricted"], list=True, default="open"),
            F("location", "Location / Counties", list=True),
            F("programs", "Assigned Programs", "refs", ref="programs", list=True),
            F("agencies", "Served Agencies", "refs", ref="agencies"),
            F("lat", "Latitude", "real"), F("lon", "Longitude", "real"),
            F("commander", "Incident Commander / Net Control"),
            F("radio_net", "Radio Net / Frequency"),
            F("description", "Description", "textarea"),
        ],
    },
    "activity": {
        "label": "Activity Log", "singular": "Log Entry", "public": False, "append_only": True,
        "create": "operator", "edit": "admin", "delete": "admin",
        "program_admin_create": True,  # program admins may add these even below the create role
        "fields": [
            F("incident_id", "Incident", "ref", required=True, ref="incidents", list=True),
            F("body", "Entry", "textarea", required=True, list=True),
        ],
    },
    "checkins": {
        "label": "Net Check-ins", "singular": "Check-in", "public": False,
        "create": "operator", "edit": "owner", "delete": "owner",
        "program_admin_create": True,  # program admins may add these even below the create role
        "fields": [
            F("incident_id", "Incident", "ref", required=True, ref="incidents", list=True),
            F("callsign", "Callsign", required=True, list=True),
            F("name", "Name", list=True),
            F("assignment", "Location / Assignment", list=True),
            F("status", "Status", choices=["checked_in", "standby", "assigned", "checked_out"], list=True,
              default="checked_in"),
            F("traffic", "Traffic", choices=["none", "routine", "priority", "emergency"], list=True, default="none"),
            F("checked_out_at", "Checked out at"),
            F("notes", "Notes", "textarea"),
        ],
    },
    "reports": {
        "label": "Reports", "singular": "Report", "public": True,
        "create": "viewer", "edit": "owner", "delete": "owner",
        "fields": [
            F("title", "Title", required=True, list=True),
            F("kind", "Type", choices=["sitrep", "storm_report", "damage", "net_report", "shelter", "other"],
              list=True, default="sitrep"),
            F("severity", "Severity", choices=["info", "minor", "moderate", "major"], list=True, default="info"),
            F("incident_id", "Incident", "ref", ref="incidents", list=True),
            F("hazard", "Hazard (storm/damage reports)", choices=STORM_HAZARDS),
            F("magnitude", "Magnitude (hail size in, wind mph, water depth…)"),
            F("event_time", "When it happened (local time)"),
            F("location", "Location", list=True),
            F("lat", "Latitude", "real"), F("lon", "Longitude", "real"),
            F("body", "Details", "textarea"),
            F("nws_sent", "Reported to NWS", hidden=True),
        ],
    },
    "programs": {
        "label": "Programs", "singular": "Program", "public": True,
        "create": "admin", "edit": "owner", "delete": "owner",  # owner = see Handler.can_modify
        "fields": [
            F("name", "Name", required=True, list=True),
            F("kind", "Type", choices=["ARES", "RACES", "Skywarn", "CERT", "AUXCOMM", "Other"], list=True, default="ARES"),
            F("status", "Status", choices=["operational", "standby", "active_event", "inactive"], list=True,
              default="operational"),
            F("counties", "Counties", list=True),
            F("region", "Region"),
            F("coordinator", "Coordinator", list=True),
            F("coordinator_call", "Coordinator Callsign", list=True),
            F("assistant", "Assistant(s)"),
            F("frequencies", "Frequencies / Tones", list=True),
            F("agencies", "Served Agencies", "refs", ref="agencies"),
            F("lat", "Latitude", "real"), F("lon", "Longitude", "real"),
            F("notes", "Notes", "textarea"),
            F("logo", "Logo", hidden=True),  # "png:<hash>" when a logo is uploaded (file in data/program-logos)
        ],
    },
    "agencies": {
        "label": "Agencies", "singular": "Agency", "public": True,
        "create": "admin", "edit": "owner", "delete": "owner",  # owner = see Handler.can_modify
        "fields": [
            F("name", "Name", required=True, list=True),
            F("kind", "Type", choices=["Emergency Management", "Public Safety", "Fire/EMS", "Law Enforcement",
                                       "Hospital/Health", "NWS", "Red Cross/VOAD", "Utility", "Other"],
              list=True, default="Emergency Management"),
            F("status", "Status", choices=["active", "inactive"], list=True, default="active"),
            F("county", "County", list=True),
            F("phone", "Phone", list=True),
            F("email", "Email"),
            F("lat", "Latitude", "real"), F("lon", "Longitude", "real"),
            F("notes", "Notes", "textarea"),
        ],
    },
    "resources": {
        "label": "Resources", "singular": "Resource", "public": False,
        "create": "operator", "edit": "owner", "delete": "owner",
        "program_admin_create": True,  # program admins may add these even below the create role
        "fields": [
            F("name", "Name", required=True, list=True),
            F("kind", "Type", choices=["personnel", "team", "vehicle", "go_kit", "repeater", "shelter",
                                       "generator", "other"], list=True, default="personnel"),
            F("status", "Status", choices=["available", "assigned", "out_of_service"], list=True, default="available"),
            F("incident_id", "Assigned Incident", "ref", ref="incidents", list=True),
            F("location", "Location", list=True),
            F("contact", "Contact / Callsign", list=True),
            F("notes", "Notes", "textarea"),
        ],
    },
    "cameras": {
        "label": "Cameras", "singular": "Camera", "public": False,
        "create": "operator", "edit": "owner", "delete": "owner",
        "program_admin_create": True,  # program admins may add these even below the create role
        "fields": [
            F("name", "Name", required=True, list=True),
            F("kind", "Type", choices=["weather", "traffic", "river", "facility", "other"], list=True, default="weather"),
            F("status", "Status", choices=["active", "inactive"], list=True, default="active"),
            F("image_url", "Snapshot image URL (JPEG/PNG that refreshes)", list=False),
            F("page_url", "Live stream / web page URL"),
            F("county", "County", list=True),
            F("lat", "Latitude", "real"), F("lon", "Longitude", "real"),
            F("notes", "Notes", "textarea"),
        ],
    },
    "messages": {
        "label": "Chat", "singular": "Message", "public": False, "append_only": True,
        "create": "viewer", "edit": "admin", "delete": "admin",
        "fields": [
            F("channel", "Channel", default="general", list=True),
            F("body", "Message", "textarea", required=True, list=True),
        ],
    },
}

SETTINGS_DEFAULTS = {
    "site_name": "TACISS",
    "org_name": "Tactical Amateur Communication Information Sharing System",
    "seoc_level": "normal",  # normal | monitoring | partial | full
    "banner": "",
    "map_lat": "35.86", "map_lon": "-86.35", "map_zoom": "7",
    "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "tile_attribution": "&copy; OpenStreetMap contributors",
    # NWS integration (needs internet on the server; last data is kept for offline viewing)
    "wx_enabled": "1",
    "wx_states": "TN",              # comma list of state codes to pull alerts/counties for
    "wx_offices": "MRX,OHX,MEG",    # NWS forecast offices shown as radar/product choices
    "wx_contact": "",               # email/URL sent in the User-Agent, as NWS asks API users to do
    # SITREP war room live feeds (server needs internet; LAN clients get everything through TACISS)
    "feeds_enabled": "1",
    "feeds_bbox": "34.9,-90.4,36.7,-81.6",  # S,W,N,E box for Spotter Network reports (default: Tennessee)
    "lsr_hours": "24",
    "windy_api_key": "",            # free key from api.windy.com -> weather webcams
    "tdot_api_key": "",             # TDOT SmartWay open-data key (request from TDOT) -> traffic cameras
    "nws_report_email": "",         # where "Report to NWS" e-mails go (your NWS office's spotter report address)
    "nws_report_links": "NWS Nashville – report severe weather|https://www.weather.gov/ohx/ReportSevereWeather\n"
                        "mPING (NOAA/OU crowd reports)|https://mping.ou.edu/",
    "outage_links": "PowerOutage.us – Tennessee|https://poweroutage.us/area/state/tennessee\n"
                    "NES (Nashville)|https://www.nespower.com/outages/\n"
                    "KUB (Knoxville)|https://www.kub.org/outage-center\n"
                    "EPB (Chattanooga)|https://epb.com/outage-map/\n"
                    "MLGW (Memphis)|https://www.mlgw.com/outages\n"
                    "Middle TN Electric|https://www.mtemc.com/outages\n"
                    "Cumberland EMC|https://www.cemc.org/outage-map\n"
                    "IODA – Tennessee internet|https://ioda.inetintel.cc.gatech.edu/region/4446",
    # Mesh: sync with other TACISS nodes (Pis, EOCs) over LAN, AREDN, Tailscale/WireGuard or internet
    "mesh_enabled": "1",
    "mesh_node_name": "",           # how this node is shown to others (blank = computer name)
    "mesh_url": "",                 # address other nodes use to reach this one, e.g. http://10.54.3.21:8080
    "mesh_interval": "30",          # seconds between background syncs (changes also sync right away)
    "mesh_discoverable": "1",       # announce this node on the LAN / answer discovery probes
    # APRS: live APRS-IS feed around the map center, aprs.fi lookups, sending under users' own callsigns
    "aprs_enabled": "1",
    "aprs_server": "rotate.aprs2.net:14580",  # APRS-IS server (filtered port)
    "aprs_range_km": "150",         # radius around the map center to receive
    "aprs_filter_extra": "",        # advanced: extra APRS-IS filter terms, e.g. "p/W4 t/w"
    "aprsfi_api_key": "",           # free key from aprs.fi (Account -> API key) for callsign lookups
    "aprs_tx_enabled": "1",         # let users send positions/objects/messages under their own callsign
    # MeshCore: LoRa mesh traffic from internet-connected observers (needs internet on the server)
    "meshcore_enabled": "1",
    "meshcore_bbox": "34.9,-90.4,36.7,-81.6",  # S,W,N,E area to show (default: Tennessee)
    "meshcore_days": "14",          # hide map nodes not heard from in this many days
    "meshcore_map_url": "https://map.meshcore.dev/api/v1/nodes",  # public MeshCore node map
    "meshcore_analyzer_url": "",    # CoreScope analyzer for live packets/channels, e.g. https://analyzer.example.org
    "meshcore_regions": "",         # observer area codes (IATA) to show from the analyzer, e.g. BNA,TYS,CHA,MEM
    "meshcore_channels": "",        # only these public channels, e.g. #weather,#wx,#bna-wx,Public (blank = all)
    "meshcore_embed_url": "https://analyzer.letsmesh.net/",  # web analyzer shown inside the tab (blank = none)
    "meshcore_links": "MeshCore map|https://map.meshcore.dev/\n"
                      "LetsMesh analyzer|https://analyzer.letsmesh.net/\n"
                      "CoreScope (run your own analyzer)|https://github.com/Kpa-clawbot/CoreScope",
}
SECRET_SETTINGS = {"windy_api_key", "tdot_api_key", "aprsfi_api_key"}  # never sent back to browsers
SECRET_MASK = "••••••••"
SEOC_LEVELS = ["normal", "monitoring", "partial", "full"]
SETTINGS_OPERATOR_KEYS = {"seoc_level", "banner"}

SQL_TYPES = {"text": "TEXT", "textarea": "TEXT", "real": "REAL", "int": "INTEGER", "ref": "INTEGER",
             "refs": "TEXT"}  # refs = JSON list of ids, e.g. "[1,4]"
MEMBER_KINDS = ("programs", "agencies")  # what a user can be a member of


def parse_refs(v):
    try:
        out = json.loads(v) if isinstance(v, str) and v else (v or [])
        return [int(x) for x in out] if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def out_row(entity, row):
    """DB row -> API dict (refs columns become real lists)."""
    d = dict(row)
    for f in ENTITIES[entity]["fields"]:
        if f["type"] == "refs":
            d[f["name"]] = parse_refs(d.get(f["name"]))
    return d


def incident_channel(channel):
    m = re.fullmatch(r"incident-(\d+)", channel or "")
    return int(m.group(1)) if m else None


def incident_id_of(entity, row):
    """The incident a record belongs to (for access checks), or None."""
    if row is None:
        return None
    keys = row.keys()
    if entity == "incidents":
        return row["id"]
    if "incident_id" in keys and row["incident_id"] is not None:
        return row["incident_id"]
    if entity == "messages" and "channel" in keys:
        return incident_channel(row["channel"])
    return None


def access_for(con, user):
    acc = {"user": user, "admin": bool(user and user["role"] == "admin")}
    for k in MEMBER_KINDS:
        acc[k] = set()
    acc["program_admin"] = set()  # programs this user is a program admin of
    if user:
        for r in con.execute("SELECT kind, ref_id, role FROM memberships WHERE user_id = ?", (user["id"],)):
            if r["kind"] in acc:
                acc[r["kind"]].add(r["ref_id"])
                if r["kind"] == "programs" and r["role"] == "admin":
                    acc["program_admin"].add(r["ref_id"])
    return acc


def stream_allowed(user, acl):
    """Live-stream check for an event tied to a restricted incident (memberships read fresh)."""
    if not user:
        return False
    if user["role"] == "admin":
        return True
    con = connect()
    try:
        acc = access_for(con, user)
    finally:
        con.close()
    return any(incident_allowed(a, acc) for a in acl)


def incident_allowed(inc, acc):
    if (inc.get("visibility") or "open") != "restricted":
        return True
    u = acc["user"]
    if not u:
        return False
    if acc["admin"] or (inc.get("created_by") and inc.get("created_by") == u["username"]):
        return True
    return bool(set(parse_refs(inc.get("programs"))) & acc["programs"] or
                set(parse_refs(inc.get("agencies"))) & acc["agencies"])


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def connect():
    con = sqlite3.connect(db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def init_db():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    con = connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            display_name TEXT, callsign TEXT, role TEXT NOT NULL DEFAULT 'viewer',
            password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_login TEXT);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, expires_at REAL NOT NULL, ip TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, ip TEXT,
            action TEXT NOT NULL, entity TEXT, entity_id INTEGER, detail TEXT);
        """)
        for name, spec in ENTITIES.items():
            cols = ["id INTEGER PRIMARY KEY", "created_at TEXT NOT NULL", "updated_at TEXT NOT NULL",
                    "created_by TEXT", "updated_by TEXT"]
            con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({', '.join(cols)})")
            existing = {r["name"] for r in con.execute(f"PRAGMA table_info({name})")}
            for f in spec["fields"]:
                if f["name"] not in existing:
                    con.execute(f"ALTER TABLE {name} ADD COLUMN {f['name']} {SQL_TYPES[f['type']]}")
            for f in spec["fields"]:
                if f["type"] == "ref" or f["name"] in ("status", "channel", "callsign"):
                    con.execute(f"CREATE INDEX IF NOT EXISTS idx_{name}_{f['name']} ON {name}({f['name']})")
        if "prefs" not in {r["name"] for r in con.execute("PRAGMA table_info(users)")}:
            con.execute("ALTER TABLE users ADD COLUMN prefs TEXT")
        # which programs / served agencies each user belongs to (drives restricted-incident access)
        con.execute("""CREATE TABLE IF NOT EXISTS memberships (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, ref_id INTEGER NOT NULL, PRIMARY KEY (user_id, kind, ref_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_memberships_ref ON memberships(kind, ref_id)")
        # role within a program: 'member', or 'admin' (program admin: manages that program and its records)
        if "role" not in {r["name"] for r in con.execute("PRAGMA table_info(memberships)")}:
            con.execute("ALTER TABLE memberships ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        con.execute("UPDATE incidents SET visibility = 'open' WHERE visibility IS NULL")
        for k, v in SETTINGS_DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        # 1.1: the name changed; update servers still showing the old default (custom subtitles are kept)
        con.execute("UPDATE settings SET value = ? WHERE key = 'org_name' AND value = ?",
                    (SETTINGS_DEFAULTS["org_name"], "Tactical Communications & Incident Situational Sharing"))
        mesh_init(con)
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Passwords & sessions
# --------------------------------------------------------------------------
def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(pw, stored):
    try:
        algo, it, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_password_policy(pw):
    if not isinstance(pw, str) or len(pw) < 10:
        raise ApiError(400, "Password must be at least 10 characters.")
    if len(pw) > 256:
        raise ApiError(400, "Password too long.")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class LoginLimiter:
    """Simple per-IP throttle: 10 failed logins per 15 minutes."""

    def __init__(self, limit=10, window=900):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def blocked(self, ip):
        with self.lock:
            t = time.time()
            arr = [x for x in self.fails.get(ip, []) if t - x < self.window]
            self.fails[ip] = arr
            return len(arr) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def clear(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


LIMITER = LoginLimiter()


# --------------------------------------------------------------------------
# Live event bus (Server-Sent Events). Events carry no record data, only
# "entity X changed" so clients refetch with their own permissions.
# --------------------------------------------------------------------------
class EventBus:
    def __init__(self):
        self.subs = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def count(self):
        with self.lock:
            return len(self.subs)


BUS = EventBus()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# National Weather Service integration (api.weather.gov)
# --------------------------------------------------------------------------
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11",
    "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30",
    "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
    "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}
WX_PRODUCTS = {"HWO": "Hazardous Weather Outlook", "AFD": "Area Forecast Discussion"}
DAY = 86400


class WeatherService:
    """Polls NWS alerts in the background and proxies/caches the other NWS calls so that
    browsers only ever talk to TACISS (works for LAN clients when only the server has internet)."""
    API = "https://api.weather.gov"

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}  # url -> (fetched_at, data)
        self.alerts, self.updated, self.error, self.states = [], None, None, ""
        self.tests = []  # exercise alerts injected by operators
        self.sig = None

    # -- plumbing --
    @staticmethod
    def settings():
        con = connect()
        try:
            s = dict(SETTINGS_DEFAULTS)
            s.update({r[0]: r[1] for r in con.execute("SELECT key, value FROM settings WHERE key LIKE 'wx_%' OR key LIKE 'map_%'")})
            return s
        finally:
            con.close()

    def http(self, url):
        contact = self.settings().get("wx_contact", "").strip()
        req = urllib.request.Request(url, headers={
            "User-Agent": f"TACISS/{VERSION} ({contact or 'self-hosted incident information board'})",
            "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def disk_path(self, name):
        return os.path.join(CONFIG["data_dir"], name)

    def save_disk(self, name, data):
        tmp = self.disk_path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.disk_path(name))

    def load_disk(self, name):
        try:
            with open(self.disk_path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def fetch(self, path, params=None, ttl=300, disk=None):
        """GET from api.weather.gov with an in-memory cache; falls back to stale/disk copies when offline."""
        url = self.API + path + ("?" + urlencode(params) if params else "")
        with self.lock:
            hit = self.cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            data = self.http(url)
        except Exception as e:  # network down, NWS outage, bad request
            if hit:
                return hit[1]
            if disk:
                d = self.load_disk(disk)
                if d is not None:
                    return d
            reason = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", e))
            raise ApiError(502, f"Could not reach the National Weather Service ({reason}).")
        with self.lock:
            self.cache[url] = (time.time(), data)
            if len(self.cache) > 400:  # keep memory bounded
                for k, _ in sorted(self.cache.items(), key=lambda kv: kv[1][0])[:100]:
                    del self.cache[k]
        if disk:
            self.save_disk(disk, data)
        return data

    # -- alerts --
    @staticmethod
    def slim(feature):
        p = feature.get("properties", {})
        geo = p.get("geocode") or {}
        return {
            "id": p.get("id"), "event": p.get("event"), "severity": p.get("severity"),
            "urgency": p.get("urgency"), "certainty": p.get("certainty"), "headline": p.get("headline"),
            "description": p.get("description"), "instruction": p.get("instruction"),
            "area": p.get("areaDesc"), "sender": p.get("senderName"), "message_type": p.get("messageType"),
            "sent": p.get("sent"), "effective": p.get("effective"), "onset": p.get("onset"),
            "expires": p.get("expires"), "ends": p.get("ends"),
            "same": geo.get("SAME", []), "ugc": geo.get("UGC", []), "geometry": feature.get("geometry"),
            "test": False,
        }

    def poll(self):
        s = self.settings()
        if s.get("wx_enabled", "1") != "1":
            with self.lock:
                self.alerts, self.error = [], "NWS integration is turned off in Admin settings."
            return
        states = s.get("wx_states") or "TN"
        try:
            data = self.http(self.API + "/alerts/active?" + urlencode({"area": states, "status": "actual"}))
            alerts = [self.slim(f) for f in data.get("features", [])]
            with self.lock:
                self.alerts, self.updated, self.error, self.states = alerts, now_iso(), None, states
            self.save_disk("wx-alerts.json", {"updated": self.updated, "states": states, "alerts": alerts})
        except Exception as e:
            with self.lock:
                self.error = f"Last update failed: {getattr(e, 'reason', e)}"
        self.notify_if_changed()

    def notify_if_changed(self):
        with self.lock:
            sig = frozenset(a["id"] for a in self.alerts) | frozenset(t["id"] for t in self.tests)
            changed = sig != self.sig
            self.sig = sig
        if changed:
            BUS.publish({"entity": "weather", "action": "update", "public": True})

    def snapshot(self):
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.tests = [t for t in self.tests if t["ends"] > now]
            return {"updated": self.updated, "error": self.error, "states": self.states,
                    "alerts": self.tests + self.alerts}

    def add_test(self, event, same, area, minutes, by):
        start = datetime.now(timezone.utc)
        end = datetime.fromtimestamp(start.timestamp() + minutes * 60, timezone.utc)
        sev = "Extreme" if "Tornado Warning" in event else "Severe" if "Warning" in event else "Moderate"
        alert = {
            "id": "exercise-" + secrets.token_hex(6), "event": event, "severity": sev, "urgency": "Immediate",
            "certainty": "Observed", "headline": f"EXERCISE EXERCISE EXERCISE - {event} for {area}",
            "description": f"*** THIS IS AN EXERCISE ***\nSimulated {event} injected in TACISS by {by} for training. "
                           f"This is NOT a real National Weather Service alert.",
            "instruction": "Practice your normal procedures for this alert type.",
            "area": area, "sender": f"TACISS exercise ({by})", "message_type": "Alert",
            "sent": start.isoformat(), "effective": start.isoformat(), "onset": start.isoformat(),
            "expires": end.isoformat(), "ends": end.isoformat(), "same": same, "ugc": [], "geometry": None,
            "test": True,
        }
        with self.lock:
            self.tests.insert(0, alert)
        self.notify_if_changed()
        return alert

    def clear_tests(self):
        with self.lock:
            self.tests = []
        self.notify_if_changed()

    def run(self):
        cached = self.load_disk("wx-alerts.json")
        if cached:
            with self.lock:
                self.alerts, self.updated, self.states = cached.get("alerts", []), cached.get("updated"), cached.get("states", "")
                self.error = "Showing saved alerts until the first NWS update succeeds."
        while not SHUTDOWN.is_set():
            self.poll()
            SHUTDOWN.wait(60)

    def start(self):
        threading.Thread(target=self.run, name="nws-poller", daemon=True).start()

    # -- reference data --
    def counties(self):
        states = self.settings().get("wx_states") or "TN"
        key = re.sub(r"[^A-Z,]", "", states.upper())
        data = self.fetch("/zones", {"type": "county", "area": key}, ttl=30 * DAY,
                          disk=f"wx-counties-{key.replace(',', '-')}.json")
        out = []
        for f in data.get("features", []):
            p = f.get("properties", {})
            zid = p.get("id", "")
            if len(zid) == 6 and zid[2] == "C" and zid[:2] in STATE_FIPS:
                out.append({"same": "0" + STATE_FIPS[zid[:2]] + zid[3:], "ugc": zid, "name": p.get("name"),
                            "state": zid[:2], "cwa": p.get("cwa") or []})
        out.sort(key=lambda c: (c["state"], c["name"] or ""))
        return out

    def forecast(self, lat, lon):
        pts = self.fetch(f"/points/{lat:.4f},{lon:.4f}", ttl=30 * DAY)
        props = pts.get("properties", {})
        url = props.get("forecast") or ""
        if not url.startswith(self.API + "/"):
            raise ApiError(502, "NWS returned no forecast for that location.")
        fc = self.fetch(url[len(self.API):], ttl=900)
        loc = (props.get("relativeLocation") or {}).get("properties", {})
        return {
            "office": props.get("gridId"), "radar": props.get("radarStation"),
            "place": ", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
            "updated": fc.get("properties", {}).get("updated"),
            "periods": [{k: p.get(k) for k in ("name", "isDaytime", "temperature", "temperatureUnit", "windSpeed",
                                               "windDirection", "shortForecast", "detailedForecast")}
                        | {"pop": (p.get("probabilityOfPrecipitation") or {}).get("value")}
                        for p in fc.get("properties", {}).get("periods", [])[:14]],
        }

    # -- weather stations and current conditions --
    def stations(self):
        """NWS observation stations in the configured states (ASOS/AWOS and other networks NWS ingests)."""
        out = []
        for st in re.sub(r"[^A-Z,]", "", (self.settings().get("wx_states") or "TN").upper()).split(","):
            if not st:
                continue
            data = self.fetch("/stations", {"state": st, "limit": 500}, ttl=7 * DAY, disk=f"wx-stations-{st}.json")
            pages = [data]
            for n in range(1, 8):  # NWS pages its station list 500 at a time
                nxt = (data.get("pagination") or {}).get("next") or ""
                if not nxt.startswith(self.API + "/") or len(data.get("features") or []) < 500:
                    break
                data = self.fetch(nxt[len(self.API):], ttl=7 * DAY, disk=f"wx-stations-{st}-{n}.json")
                pages.append(data)
            seen = set()
            for page in pages:
                for f in page.get("features", []):
                    p, c = f.get("properties", {}), (f.get("geometry") or {}).get("coordinates") or [None, None]
                    sid = p.get("stationIdentifier")
                    if sid and sid not in seen and c[1] is not None:
                        seen.add(sid)
                        out.append({"id": sid, "name": p.get("name") or "", "lat": c[1], "lon": c[0], "source": "nws"})
        return out

    def nearest_station(self, lat, lon):
        props = self.fetch(f"/points/{lat:.4f},{lon:.4f}", ttl=30 * DAY).get("properties", {})
        url = props.get("observationStations") or ""
        if not url.startswith(self.API + "/"):
            raise ApiError(502, "NWS returned no stations for that location.")
        feats = self.fetch(url[len(self.API):], ttl=30 * DAY).get("features") or []
        if not feats:
            raise ApiError(404, "No weather stations near that location.")
        return feats[0]["properties"]["stationIdentifier"]

    def observation(self, sid):
        """Latest observation at an NWS station, in US units."""
        data = self.fetch(f"/stations/{sid}/observations/latest", ttl=300)
        p = data.get("properties", {})
        val = lambda k: (p.get(k) or {}).get("value")
        c_to_f = lambda c: None if c is None else round(c * 9 / 5 + 32)
        kmh_to_mph = lambda v: None if v is None else round(v / 1.609)
        name = self.fetch(f"/stations/{sid}", ttl=30 * DAY).get("properties", {}).get("name") or sid
        return {"id": sid, "name": name, "time": p.get("timestamp"), "text": p.get("textDescription") or "",
                "night": "/night/" in (p.get("icon") or ""), "temp_f": c_to_f(val("temperature")),
                "dewpoint_f": c_to_f(val("dewpoint")), "feels_f": c_to_f(val("heatIndex") if val("heatIndex") is not None
                                                                         else val("windChill")),
                "humidity": None if val("relativeHumidity") is None else round(val("relativeHumidity")),
                "wind_dir": val("windDirection"), "wind_mph": kmh_to_mph(val("windSpeed")),
                "gust_mph": kmh_to_mph(val("windGust")),
                "pressure_inhg": None if val("barometricPressure") is None else round(val("barometricPressure") / 3386.39, 2),
                "visibility_mi": None if val("visibility") is None else round(val("visibility") / 1609.34, 1)}

    def product(self, ptype, office):
        # look up by issuing office (KOHX, KMRX...): some offices don't file products under their own location code
        listing = self.fetch("/products", {"type": ptype, "office": "K" + office, "limit": 1}, ttl=600)
        items = listing.get("@graph") or []
        if not items:
            other = "AFD" if ptype == "HWO" else "HWO"
            raise ApiError(404, f"{office} hasn't issued a {WX_PRODUCTS[ptype]} recently. Try the {other}.")
        prod = self.fetch(f"/products/{items[0]['id']}", ttl=30 * DAY)
        return {"type": ptype, "name": WX_PRODUCTS[ptype], "office": office, "issued": prod.get("issuanceTime"),
                "text": prod.get("productText", "")}


WX = WeatherService()


# --------------------------------------------------------------------------
# War-room live feeds: storm/spotter reports, cameras, power & internet outages.
# The server fetches and caches; browsers only talk to TACISS.
# --------------------------------------------------------------------------
STATE_NAMES = dict(x.split(":") for x in (
    "AL:Alabama AK:Alaska AZ:Arizona AR:Arkansas CA:California CO:Colorado CT:Connecticut DE:Delaware "
    "DC:District_of_Columbia FL:Florida GA:Georgia HI:Hawaii ID:Idaho IL:Illinois IN:Indiana IA:Iowa KS:Kansas "
    "KY:Kentucky LA:Louisiana ME:Maine MD:Maryland MA:Massachusetts MI:Michigan MN:Minnesota MS:Mississippi "
    "MO:Missouri MT:Montana NE:Nebraska NV:Nevada NH:New_Hampshire NJ:New_Jersey NM:New_Mexico NY:New_York "
    "NC:North_Carolina ND:North_Dakota OH:Ohio OK:Oklahoma OR:Oregon PA:Pennsylvania RI:Rhode_Island "
    "SC:South_Carolina SD:South_Dakota TN:Tennessee TX:Texas UT:Utah VT:Vermont VA:Virginia WA:Washington "
    "WV:West_Virginia WI:Wisconsin WY:Wyoming PR:Puerto_Rico").replace("_", "~").split())
STATE_NAMES = {k: v.replace("~", " ") for k, v in STATE_NAMES.items()}

LSR_TYPES = {  # NWS LSR type code -> our hazard vocabulary
    "T": "tornado", "C": "funnel_cloud", "H": "hail", "G": "high_wind", "D": "wind_damage", "M": "high_wind",
    "F": "flash_flood", "E": "flood", "R": "heavy_rain", "L": "lightning_damage", "S": "snow_ice", "Z": "snow_ice",
    "5": "snow_ice", "W": "other", "O": "other",
}
PRIVATE_RE = re.compile(r"(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})|([\w.+-]+@[\w-]+\.[\w.]+)")


class FeedService:
    UA = f"TACISS/{VERSION} (self-hosted emergency management board)"

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}       # key -> (time, data, error)
        self.cam_src = {}     # camera id -> real snapshot URL (only these may be proxied)
        self.img_cache = {}   # camera id -> (time, content_type, bytes)
        self.seen_storm = None

    def flush(self):
        with self.lock:
            self.cache.clear()

    @staticmethod
    def settings():
        con = connect()
        try:
            s = dict(SETTINGS_DEFAULTS)
            s.update({r[0]: r[1] for r in con.execute("SELECT key, value FROM settings")})
            return s
        finally:
            con.close()

    def get(self, url, headers=None, raw=False, timeout=20, limit=8 * 1024 * 1024):
        h = {"User-Agent": self.UA, "Accept": "*/*"}
        h.update(headers or {})
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise ValueError("response too large")
            if raw:
                return r.headers.get("Content-Type", ""), body
            return json.loads(body.decode("utf-8", "replace"))

    def cached(self, key, ttl, fn):
        """Return {"updated", "error", "data"}; keeps serving the last good copy (memory, then disk) when offline."""
        with self.lock:
            hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return {"updated": hit[3], "error": hit[2], "data": hit[1]}
        disk = os.path.join(CONFIG["data_dir"], f"feed-{key}.json")
        try:
            data, err, updated = fn(), None, now_iso()
            try:
                with open(disk + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump({"updated": updated, "data": data}, fh)
                os.replace(disk + ".tmp", disk)
            except OSError:
                pass
        except ApiError as e:
            data, err, updated = (hit[1] if hit else None), e.message, (hit[3] if hit else None)
        except Exception as e:  # offline / source down: fall back to last copy
            reason = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", e))[:160]
            data, updated = (hit[1], hit[3]) if hit else (None, None)
            if data is None:
                try:
                    with open(disk, encoding="utf-8") as fh:
                        saved = json.load(fh)
                    data, updated = saved.get("data"), saved.get("updated")
                except (OSError, ValueError):
                    pass
            err = f"Source unreachable ({reason})" + ("; showing last saved data." if data is not None else ".")
        with self.lock:
            # failures are retried sooner than the normal TTL
            self.cache[key] = (time.time() - (ttl * 0.7 if err else 0), data, err, updated)
        return {"updated": updated, "error": err, "data": data}

    @staticmethod
    def states(s):
        return [x for x in (s.get("wx_states") or "TN").split(",") if x in STATE_FIPS]

    # -- storm reports: NWS Local Storm Reports (IEM), SPC preliminary reports, Spotter Network --
    def storm_reports(self):
        s = self.settings()
        hours = int(s.get("lsr_hours") or 24)
        out, errors, sources = [], [], {}
        lsr = self.cached("lsr", 120, lambda: self._lsr(s, hours))
        spc = self.cached("spc", 300, lambda: self._spc(s))
        sn = self.cached("sn", 180, lambda: self._spotter_network(s))
        for name, res in (("NWS LSR", lsr), ("SPC", spc), ("Spotter Network", sn)):
            sources[name] = {"updated": res["updated"], "error": res["error"], "count": len(res["data"] or [])}
            if res["error"]:
                errors.append(f"{name}: {res['error']}")
        out = list(lsr["data"] or [])
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600

        def near_dup(r):  # SPC/SN usually repeat what's already in an LSR
            for o in out:
                if abs(o["lat"] - r["lat"]) < 0.08 and abs(o["lon"] - r["lon"]) < 0.08 and \
                        abs(o["ts"] - r["ts"]) < 1800 and (o["hazard"] == r["hazard"] or "other" in (o["hazard"], r["hazard"])):
                    return True
            return False

        for r in (spc["data"] or []) + (sn["data"] or []):
            if r["ts"] >= cutoff and not near_dup(r):
                out.append(r)
        out = [r for r in out if r["ts"] >= cutoff]
        out.sort(key=lambda r: r["ts"], reverse=True)
        return {"reports": out, "sources": sources, "error": "; ".join(errors) or None, "hours": hours}

    def _lsr(self, s, hours):
        q = urlencode({"states": ",".join(self.states(s)), "hours": hours})
        data = self.get("https://mesonet.agron.iastate.edu/geojson/lsr.php?" + q)
        out = []
        for f in data.get("features", []):
            p = f.get("properties") or {}
            try:
                ts = datetime.fromisoformat(p["valid"].replace("Z", "+00:00")).timestamp()
                lat, lon = float(p["lat"]), float(p["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            mag = p.get("magnitude") if p.get("magnitude") not in (None, "", 0) else p.get("magf")
            out.append({
                "id": "lsr-" + hashlib.sha1(f"{p.get('valid')}{lat}{lon}{p.get('type')}".encode()).hexdigest()[:12],
                "src": "NWS LSR", "ts": ts, "time": p["valid"], "hazard": LSR_TYPES.get(p.get("type"), "other"),
                "event": (p.get("typetext") or "").title(), "mag": f"{mag} {p.get('unit') or ''}".strip() if mag else "",
                "city": p.get("city"), "county": p.get("county"), "state": p.get("state") or p.get("st"),
                "lat": lat, "lon": lon, "remark": p.get("remark") or "", "reporter": p.get("source") or "",
                "wfo": p.get("wfo"),
            })
        return out

    def _spc(self, s):
        states = set(self.states(s))
        now = datetime.now(timezone.utc)
        out = []
        for name in ("yesterday", "today"):
            day = (now - timedelta(hours=12)).date() - (timedelta(days=1) if name == "yesterday" else timedelta(0))
            _, body = self.get(f"https://www.spc.noaa.gov/climo/reports/{name}_filtered.csv", raw=True)
            kind = None
            for row in csv.reader(io.StringIO(body.decode("utf-8", "replace"))):
                if not row:
                    continue
                if row[0] == "Time":
                    kind = {"F_Scale": "tornado", "Speed": "wind_damage", "Size": "hail"}.get(row[1], "other")
                    continue
                if len(row) < 8 or row[4] not in states:
                    continue
                try:
                    hh, mm = int(row[0][:2]), int(row[0][2:4])
                    t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
                    if hh < 12:  # SPC "day" runs 12Z-12Z
                        t += timedelta(days=1)
                    lat, lon = float(row[5]), float(row[6])
                except ValueError:
                    continue
                mag = row[1] if row[1] not in ("UNK", "") else ""
                if kind == "hail" and mag.isdigit():
                    mag = f"{int(mag) / 100:.2f} in"
                elif kind == "wind_damage" and mag.isdigit():
                    mag = f"{mag} mph"
                out.append({
                    "id": "spc-" + hashlib.sha1(",".join(row).encode()).hexdigest()[:12], "src": "SPC",
                    "ts": t.timestamp(), "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "hazard": kind,
                    "event": {"tornado": "Tornado", "wind_damage": "Wind", "hail": "Hail"}.get(kind, "Report"),
                    "mag": mag, "city": row[2], "county": row[3], "state": row[4], "lat": lat, "lon": lon,
                    "remark": row[7], "reporter": "", "wfo": "",
                })
        return out

    def _spotter_network(self, s):
        """Spotter Network public report placefile. Only the reports feed: the positions feed carries
        spotters' personal contact details, and phone numbers / e-mails are stripped from reports too."""
        try:
            south, west, north, east = (float(x) for x in s.get("feeds_bbox", "").split(","))
        except ValueError:
            return []
        _, body = self.get("https://www.spotternetwork.org/feeds/reports.txt", raw=True)
        out, lat = [], None
        for line in body.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.lower().startswith("object:"):
                try:
                    lat, lon = (float(x) for x in line.split(":", 1)[1].split(",")[:2])
                except ValueError:
                    lat = None
            elif line.lower().startswith("icon:") and lat is not None and '"' in line:
                if not (south <= lat <= north and west <= lon <= east):
                    continue
                text = line.split('"', 1)[1].rsplit('"', 1)[0].replace("\\n", "\n")
                text = PRIVATE_RE.sub("[removed]", text)
                lines = [x.strip() for x in text.split("\n") if x.strip()]
                low = text.lower()
                hz = next((h for k, h in (("tornado", "tornado"), ("funnel", "funnel_cloud"), ("wall cloud", "wall_cloud"),
                                          ("rotation", "rotation"), ("hail", "hail"), ("flood", "flood"),
                                          ("wind", "wind_damage")) if k in low), "other")
                when = datetime.now(timezone.utc)
                m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*(UTC|Z)?", text)
                if m:
                    try:
                        when = datetime.fromisoformat(m.group(1).replace(" ", "T")).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                out.append({
                    "id": "sn-" + hashlib.sha1(f"{lat}{lon}{text}".encode()).hexdigest()[:12], "src": "Spotter Network",
                    "ts": when.timestamp(), "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "hazard": hz,
                    "event": hz.replace("_", " ").title(), "mag": "", "city": "", "county": "", "state": "",
                    "lat": lat, "lon": lon, "remark": " / ".join(lines[:6]), "reporter": "Spotter Network", "wfo": "",
                })
                lat = None
        return out

    # -- power outages: ORNL ODIN real-time, by county --
    def power(self):
        s = self.settings()
        names = [STATE_NAMES[x] for x in self.states(s) if x in STATE_NAMES]

        def fetch():
            rows = []
            for name in names:
                q = urlencode({"where": f'state="{name}"', "limit": 100,
                               "select": "name,utility_id,county,state,metersaffected,customersrestored,"
                                         "reportedstarttime,estimatedrestorationtime,cause"})
                rows += self.get("https://ornl.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
                                 "odin-real-time-outages-county/records?" + q).get("results", [])
            by = {}
            for r in rows:
                key = (r.get("state"), r.get("county"))
                c = by.setdefault(key, {"county": r.get("county"), "state": r.get("state"), "out": 0,
                                        "utilities": [], "etr": None, "cause": None})
                c["out"] += int(r.get("metersaffected") or 0)
                util = (r.get("name") or "").rsplit(",", 1)[0].strip().title()
                if util and util not in c["utilities"]:
                    c["utilities"].append(util)
                etr = r.get("estimatedrestorationtime")
                if etr and (not c["etr"] or etr > c["etr"]):
                    c["etr"] = etr
                c["cause"] = c["cause"] or r.get("cause")
            counties = sorted((c for c in by.values() if c["out"] > 0), key=lambda c: -c["out"])
            return {"counties": counties, "total": sum(c["out"] for c in counties)}

        res = self.cached("power", 300, fetch)
        links = [ln.split("|", 1) for ln in (s.get("outage_links") or "").splitlines() if "|" in ln]
        return {**(res["data"] or {"counties": [], "total": None}), "updated": res["updated"], "error": res["error"],
                "source": "ORNL ODIN (participating utilities only)",
                "links": [{"name": a.strip(), "url": b.strip()} for a, b in links if b.strip().startswith(("http://", "https://"))]}

    # -- internet: IODA (Georgia Tech) region signals & alerts --
    def internet(self):
        s = self.settings()
        IODA = "https://api.ioda.inetintel.cc.gatech.edu/v2"

        def fetch():
            out = []
            now = int(time.time())
            for st in self.states(s):
                name = STATE_NAMES.get(st)
                ent = self.cached(f"ioda-ent-{st}", 30 * DAY, lambda: next(
                    (e["code"] for e in self.get(f"{IODA}/entities/query?" + urlencode(
                        {"entityType": "region", "search": name})).get("data", [])
                     if e.get("name") == name and (e.get("attrs") or {}).get("country_code") == "US"),
                    None))["data"]
                if not ent:
                    continue
                sig = self.get(f"{IODA}/signals/raw/region/{ent}?from={now - DAY}&until={now}")
                series = {}
                for row in (sig.get("data") or [[]])[0]:
                    ds = row.get("datasource")
                    if ds not in ("bgp", "ping-slash24", "merit-nt"):
                        continue
                    vals = row.get("values") or []
                    pts = [(row["from"] + i * row["step"], v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
                    if not pts:
                        continue
                    base = sorted(v for _, v in pts)[len(pts) // 2] or 1
                    recent = [v for _, v in pts[-3:]]
                    cur = sum(recent) / len(recent)
                    step = max(1, len(pts) // 96)
                    series[ds] = {"current": cur, "baseline": base, "ratio": round(cur / base, 3),
                                  "points": [[t, v] for t, v in pts[::step]]}
                alerts = self.get(f"{IODA}/outages/alerts?" + urlencode(
                    {"entityType": "region", "entityCode": ent, "from": now - DAY, "until": now})).get("data", [])
                worst = min((x["ratio"] for k, x in series.items() if k != "merit-nt"), default=1)
                latest = {}  # an alert only counts until IODA reports that signal back to normal
                for a in sorted(alerts, key=lambda a: a.get("time") or 0):
                    latest[a.get("datasource")] = a.get("level")
                active = [d for d, lvl in latest.items() if lvl and lvl != "normal"]
                status = "outage" if worst < 0.6 else "degraded" if worst < 0.85 or active else "normal"
                out.append({"state": st, "name": name, "code": ent, "status": status, "series": series,
                            "alerts": [{k: a.get(k) for k in ("datasource", "time", "level", "condition", "value", "historyValue")}
                                       for a in alerts[-20:]],
                            "link": f"https://ioda.inetintel.cc.gatech.edu/region/{ent}"})
            return out

        res = self.cached("internet", 300, fetch)
        return {"regions": res["data"] or [], "updated": res["updated"], "error": res["error"],
                "source": "IODA, Georgia Tech Internet Intelligence Lab"}

    # -- cameras: our own list + Windy webcams + TDOT SmartWay --
    def cameras(self, con):
        s = self.settings()
        cams, errors = [], []
        for r in con.execute("SELECT * FROM cameras WHERE status IS NULL OR status != 'inactive' ORDER BY name"):
            cams.append({"id": f"db-{r['id']}", "name": r["name"], "kind": r["kind"] or "other", "lat": r["lat"],
                         "lon": r["lon"], "county": r["county"], "page": r["page_url"], "src": "TACISS",
                         "image": r["image_url"], "record_id": r["id"]})
        if s.get("windy_api_key"):
            res = self.cached("windy", 480, lambda: self._windy(s))
            cams += res["data"] or []
            if res["error"]:
                errors.append("Windy: " + res["error"])
        if s.get("tdot_api_key"):
            res = self.cached("tdot", 600, lambda: self._tdot(s))
            cams += res["data"] or []
            if res["error"]:
                errors.append("TDOT SmartWay: " + res["error"])
        with self.lock:
            for c in cams:
                if c.get("image") and str(c["image"]).startswith(("http://", "https://")):
                    self.cam_src[c["id"]] = c["image"]
        for c in cams:  # browsers load snapshots through TACISS (works for LAN clients without internet)
            c["img"] = f"/api/feeds/cam/{c['id']}" if c.pop("image", None) else None
        return {"cameras": cams, "error": "; ".join(errors) or None,
                "windy": bool(s.get("windy_api_key")), "tdot": bool(s.get("tdot_api_key"))}

    def _windy(self, s):
        q = urlencode({"nearby": f"{float(s['map_lat']):.3f},{float(s['map_lon']):.3f},250", "limit": 50,
                       "include": "images,location,urls"})
        try:
            data = self.get("https://api.windy.com/webcams/api/v3/webcams?" + q, {"x-windy-api-key": s["windy_api_key"]})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(502, "Windy rejected the API key.")
            raise
        out = []
        for w in data.get("webcams", []):
            loc, img = w.get("location") or {}, ((w.get("images") or {}).get("current") or {})
            out.append({"id": f"wy-{w.get('webcamId')}", "name": w.get("title"), "kind": "weather",
                        "lat": loc.get("latitude"), "lon": loc.get("longitude"),
                        "county": ", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
                        "page": (w.get("urls") or {}).get("detail"), "src": "Windy.com",
                        "image": img.get("preview") or img.get("thumbnail")})
        return out

    def _tdot(self, s):
        try:
            data = self.get("https://www.tdot.tn.gov/opendata/api/public/RoadwayCameras", {"ApiKey": s["tdot_api_key"]})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(502, "TDOT rejected the API key.")
            raise
        items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])

        def pick(d, *names, contains=None):
            for n in names:
                for k, v in d.items():
                    if k.lower() == n and v not in (None, ""):
                        return v
            if contains:
                for k, v in d.items():
                    if any(c in k.lower() for c in contains) and isinstance(v, str) and v.startswith("http"):
                        return v
            return None

        out = []
        for c in items[:2000]:
            if not isinstance(c, dict) or str(pick(c, "active", "enabled") or "true").lower() == "false":
                continue
            out.append({"id": f"td-{pick(c, 'id', 'cameraid') or len(out)}", "kind": "traffic", "src": "TDOT SmartWay",
                        "name": pick(c, "title", "name", "description", "location") or "TDOT camera",
                        "lat": pick(c, "lat", "latitude"), "lon": pick(c, "lng", "lon", "longitude"),
                        "county": pick(c, "county", "jurisdiction", "route") or "",
                        "page": pick(c, "httpsvideourl", "httpvideourl", "videourl", contains=("video", "stream")) or "https://smartway.tn.gov/",
                        "image": pick(c, "thumbnailurl", "imageurl", "snapshoturl", contains=("thumb", "image", "snapshot"))})
        return out

    def camera_image(self, cid):
        with self.lock:
            url = self.cam_src.get(cid)
            hit = self.img_cache.get(cid)
        if not url:
            raise ApiError(404, "Unknown camera (open the camera list first).")
        if hit and time.time() - hit[0] < 45:
            return hit[1], hit[2]
        try:
            ctype, body = self.get(url, {"Accept": "image/*"}, raw=True, timeout=12, limit=4 * 1024 * 1024)
        except Exception:
            if hit:
                return hit[1], hit[2]
            raise ApiError(502, "Camera image unavailable.")
        ctype = ctype.split(";")[0].strip().lower()
        if not ctype.startswith("image/") or ctype == "image/svg+xml":
            raise ApiError(502, "Camera did not return an image.")
        with self.lock:
            self.img_cache[cid] = (time.time(), ctype, body)
            if len(self.img_cache) > 150:
                for k, _ in sorted(self.img_cache.items(), key=lambda kv: kv[1][0])[:50]:
                    del self.img_cache[k]
        return ctype, body

    # -- background: poll storm reports so new ones pop up on every war-room screen --
    def run(self):
        SHUTDOWN.wait(5)
        while not SHUTDOWN.is_set():
            try:
                if self.settings().get("feeds_enabled", "1") == "1":
                    ids = {r["id"] for r in self.storm_reports()["reports"]}
                    if self.seen_storm is not None and ids - self.seen_storm:
                        BUS.publish({"entity": "feeds", "action": "storm", "public": False, "new": len(ids - self.seen_storm)})
                    self.seen_storm = ids if self.seen_storm is None else self.seen_storm | ids
            except Exception:
                traceback.print_exc()
            SHUTDOWN.wait(120)

    def start(self):
        threading.Thread(target=self.run, name="feeds-poller", daemon=True).start()


FEEDS = FeedService()


# --------------------------------------------------------------------------
# APRS: live APRS-IS feed for the area, aprs.fi lookups, and sending packets
# (positions, objects, messages) into APRS-IS under a user's own callsign.
# aprs.fi shows whatever flows through APRS-IS, so anything sent appears there.
# --------------------------------------------------------------------------
APRS_TOCALL = "APZTAC"  # APZxxx = experimental software tocall
APRS_SYMBOLS = {  # symbol (table + code) -> label, offered in the send forms
    "/-": "House / QTH", "/>": "Car", "/[": "Person", "/k": "Truck", "/v": "Van", "/j": "Jeep",
    "/o": "EOC", "/a": "Ambulance", "/f": "Fire truck", "/!": "Police", "/+": "Red Cross",
    "/h": "Hospital", "/r": "Antenna", "/y": "Yagi at QTH", "/_": "Weather station", "/;": "Campground / portable",
    "\\z": "Shelter", "\\'": "Crash site", "\\T": "Thunderstorm", "\\@": "Hurricane / storm", "\\t": "Tornado",
    "\\F": "Flooding", "\\X": "Hazard", "/'": "Small aircraft", "/X": "Helicopter",
}
CALL_RE = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}$")      # amateur callsign (base, no SSID)
APRS_ADDR_RE = re.compile(r"^[A-Z0-9]{1,6}(-[0-9]{1,2})?$")  # callsign with optional SSID
WX_WIDTHS = {"c": 3, "s": 3, "g": 3, "t": 3, "r": 3, "p": 3, "P": 3, "h": 2, "b": 5, "L": 3, "l": 3}


def public_prefs(prefs):
    """A user's prefs as sent to the browser: the APRS passcode never leaves the server."""
    out = {k: v for k, v in prefs.items() if k != "aprs_passcode"}
    out["aprs_passcode_set"] = bool(prefs.get("aprs_passcode"))
    return out


def aprs_passcode(call):
    """The standard APRS-IS passcode for a callsign (SSID ignored)."""
    h = 0x73E2
    c = call.upper().split("-")[0]
    for i in range(0, len(c), 2):
        h ^= ord(c[i]) << 8
        if i + 1 < len(c):
            h ^= ord(c[i + 1])
    return h & 0x7FFF


def _b91(s):
    v = 0
    for ch in s:
        v = v * 91 + ord(ch) - 33
    return v


def _wx_fields(s):
    """Parse APRS weather fields ('c220s004g005t077r000...'). Returns (dict, rest of text)."""
    out, i = {}, 0
    while i < len(s) and s[i] in WX_WIDTHS:
        k, w = s[i], WX_WIDTHS[s[i]]
        val = s[i + 1:i + 1 + w]
        if not re.fullmatch(r"-?\d+|[ .]+", val or "x"):
            break
        if val.strip(" .") not in ("", "-"):
            n = int(val)
            out.update({"c": {"wind_dir": n}, "s": {"wind_mph": n}, "g": {"gust_mph": n}, "t": {"temp_f": n},
                        "r": {"rain_1h_in": n / 100}, "p": {"rain_24h_in": n / 100},
                        "P": {"rain_midnight_in": n / 100}, "h": {"humidity": 100 if n == 0 else n},
                        "b": {"pressure_mb": n / 10}, "L": {"lum_wm2": n}, "l": {"lum_wm2": n + 1000}}[k])
        i += 1 + w
    return out, s[i:]


def _pos_uncompressed(b):
    """'DDMM.mmN/DDDMM.mmW>' -> (lat, lon, symbol, rest) or None."""
    m = re.match(r"(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)", b)
    if not m:
        return None
    lat = int(m[1]) + float(m[2].replace(" ", "0")) / 60
    lon = int(m[5]) + float(m[6].replace(" ", "0")) / 60
    lat, lon = (-lat if m[3] == "S" else lat), (-lon if m[7] == "W" else lon)
    return lat, lon, m[4] + m[8], b[19:]


def _pos_compressed(b):
    if len(b) < 13 or not re.match(r"[/\\A-Za-j]", b[0]):
        return None
    try:
        lat = 90 - _b91(b[1:5]) / 380926
        lon = -180 + _b91(b[5:9]) / 190463
    except (ValueError, IndexError):
        return None
    out = {"lat": lat, "lon": lon, "sym": b[0] + b[9]}
    c, s = b[10], b[11]
    if c != " " and 0 <= ord(c) - 33 <= 89 and ord(s) >= 33:
        out["course"] = (ord(c) - 33) * 4
        out["speed_kmh"] = round((1.08 ** (ord(s) - 33) - 1) * 1.852, 1)
    return out, b[13:]


def _comment_ext(rec, comment):
    """Course/speed, altitude and weather hidden in a position comment."""
    m = re.match(r"(\d{3})/(\d{3})", comment)
    if m and rec.get("sym", "")[1:] == "_":  # weather station: wind dir/speed then wx fields
        wx, rest = _wx_fields(comment[7:])
        wx.update(wind_dir=int(m[1]), wind_mph=int(m[2]))
        rec["wx"], comment = wx, rest
    elif m:
        if int(m[1]) or int(m[2]):
            rec["course"], rec["speed_kmh"] = int(m[1]) % 360, round(int(m[2]) * 1.852, 1)
        comment = comment[7:]
    a = re.search(r"/A=(-?\d{6})", comment)
    if a:
        rec["alt_m"] = round(int(a[1]) * 0.3048)
        comment = comment.replace(a[0], "")
    rec["comment"] = comment.strip()[:200]
    return rec


def _position(rec, body):
    p = _pos_uncompressed(body)
    if p:
        rec.update(lat=p[0], lon=p[1], sym=p[2])
        return _comment_ext(rec, p[3])
    c = _pos_compressed(body)
    if c:
        rec.update(c[0])
        if rec["sym"][1:] == "_":  # compressed weather station: wx fields follow directly
            rec["wx"], rest = _wx_fields(c[1])
            rec["comment"] = rest.strip()[:200]
            return rec
        return _comment_ext(rec, c[1])
    return None


def _mic_e(rec, dst, b):
    dst = dst.split("-")[0]
    if len(dst) < 6 or len(b) < 8:
        return None
    digits = ""
    for ch in dst[:6]:
        if ch.isdigit():
            digits += ch
        elif "A" <= ch <= "J":
            digits += str(ord(ch) - 65)
        elif "P" <= ch <= "Y":
            digits += str(ord(ch) - 80)
        elif ch in "KLZ":
            digits += "0"
        else:
            return None
    lat = int(digits[:2]) + float(digits[2:4] + "." + digits[4:6]) / 60
    north, lon_off, west = dst[3] >= "P", 100 if dst[4] >= "P" else 0, dst[5] >= "P"
    d = ord(b[0]) - 28 + lon_off
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    m = ord(b[1]) - 28
    if m >= 60:
        m -= 60
    lon = d + (m + (ord(b[2]) - 28) / 100) / 60
    sp, dc, se = ord(b[3]) - 28, ord(b[4]) - 28, ord(b[5]) - 28
    speed, course = sp * 10 + dc // 10, (dc % 10) * 100 + se
    speed -= 800 if speed >= 800 else 0
    course -= 400 if course >= 400 else 0
    if not (-90 <= lat <= 90 and 0 <= lon <= 180):
        return None
    rec.update(lat=lat if north else -lat, lon=-lon if west else lon, sym=b[7] + b[6])
    if speed:
        rec.update(speed_kmh=round(speed * 1.852, 1), course=course % 360)
    comment = b[8:]
    if len(comment) >= 4 and comment[3] == "}":
        rec["alt_m"] = _b91(comment[:3]) - 10000
        comment = comment[4:]
    elif comment[:1] in ">]`'" and len(comment) >= 5 and comment[4] == "}":
        rec["alt_m"] = _b91(comment[1:4]) - 10000
        comment = comment[5:]
    rec["comment"] = comment.lstrip(">]`'").strip()[:200]
    return rec


def parse_aprs(line):
    """One APRS-IS line -> dict (type: position|object|item|message|status|weather|other) or None."""
    line = line.rstrip("\r\n")
    if not line or line[0] == "#" or ":" not in line or ">" not in line.split(":", 1)[0]:
        return None
    head, payload = line.split(":", 1)
    src, rest = head.split(">", 1)
    dst, *path = rest.split(",")
    if not payload:
        return None
    if payload[0] == "}":  # third-party traffic: parse the inner packet
        inner = parse_aprs(payload[1:])
        if inner:
            inner["raw"] = line[:300]
        return inner
    rec = {"src": src.upper(), "dst": dst, "path": ",".join(path), "raw": line[:300], "call": src.upper()}
    t, b = payload[0], payload[1:]
    try:
        if t in "!=":
            r = _position(rec, b)
            return dict(r, type="position") if r else None
        if t in "/@":
            r = _position(rec, b[7:])
            return dict(r, type="position") if r else None
        if t == ";" and len(b) >= 17:
            name, live = b[:9].strip(), b[9]
            r = _position(dict(rec, call=name, by=rec["src"]), b[17:])
            return dict(r, type="object", killed=live == "_") if r else None
        if t == ")":
            m = re.match(r"(.{3,9}?)([!_])", b)
            if m:
                r = _position(dict(rec, call=m[1].strip(), by=rec["src"]), b[m.end():])
                return dict(r, type="item", killed=m[2] == "_") if r else None
            return None
        if t in "`'":
            r = _mic_e(rec, dst, b)
            return dict(r, type="position") if r else None
        if t == ":" and len(b) >= 10 and b[9] == ":":
            to, text = b[:9].strip().upper(), b[10:]
            msg = dict(rec, type="message", to=to, text=text[:200])
            if re.fullmatch(r"(ack|rej)[A-Za-z0-9]{1,5}\}?.*", text):
                msg.update(ack=text[:3], msgno=re.sub(r"[^A-Za-z0-9].*", "", text[3:]))
            elif "{" in text:
                body, no = text.rsplit("{", 1)
                msg.update(text=body[:200], msgno=no.split("}")[0][:5])
            return msg
        if t == ">":
            return dict(rec, type="status", comment=b[:200])
        if t == "_" and len(b) >= 8:
            wx, _ = _wx_fields(b[8:])
            return dict(rec, type="weather", wx=wx) if wx else None
    except (ValueError, IndexError, TypeError):
        return None
    return dict(rec, type="other")


def aprs_coord(lat, lon):
    """Decimal degrees -> APRS 'DDMM.mmN', 'DDDMM.mmW'."""
    la, lo = abs(lat), abs(lon)
    lad, lam = int(la), (la - int(la)) * 60
    lod, lom = int(lo), (lo - int(lo)) * 60
    if round(lam, 2) >= 60:
        lad, lam = lad + 1, 0
    if round(lom, 2) >= 60:
        lod, lom = lod + 1, 0
    return f"{lad:02d}{lam:05.2f}{'N' if lat >= 0 else 'S'}", f"{lod:03d}{lom:05.2f}{'E' if lon >= 0 else 'W'}"


def aprs_clean(s, n):
    """Printable ASCII only (APRS is 7-bit), trimmed to n characters."""
    return re.sub(r"[^\x20-\x7e]", "", str(s or "").replace("|", "/").replace("~", "-"))[:n]


class AprsService:
    """Keeps a read-only APRS-IS connection with an area filter and remembers what it hears."""
    UA = f"TACISS/{VERSION} (+https://github.com/amorilab/taciss)"
    MAX_STATIONS = 4000
    KEEP_HOURS = 24

    def __init__(self):
        self.lock = threading.Lock()
        self.stations = {}  # call/object name -> latest record
        self.packets = collections.deque(maxlen=400)
        self.messages = collections.deque(maxlen=600)
        self.status = {"connected": False, "server": None, "since": None, "error": None, "packets": 0,
                       "filter": None}
        self.sock = None
        self.want_filter = None
        self.dirty = False
        self.wake = threading.Event()
        self.api_cache = {}
        self.last_tx = {}   # username -> [timestamps]

    # ---- settings / filter
    @staticmethod
    def settings():
        return FeedService.settings()

    def watch_calls(self):
        """Callsigns whose messages we want (every user's own callsign) plus users' watch lists."""
        calls, buddies = set(), set()
        con = connect()
        try:
            for r in con.execute("SELECT callsign, prefs FROM users WHERE active = 1"):
                if r["callsign"] and CALL_RE.match(r["callsign"].upper()):
                    calls.add(r["callsign"].upper())
                try:
                    for c in (json.loads(r["prefs"] or "{}").get("aprs_watch") or [])[:30]:
                        buddies.add(c)
                except ValueError:
                    pass
        finally:
            con.close()
        return sorted(calls), sorted(buddies)

    def build_filter(self, s=None):
        s = s or self.settings()
        try:
            lat, lon = float(s.get("map_lat") or 35.86), float(s.get("map_lon") or -86.35)
            km = max(5, min(int(float(s.get("aprs_range_km") or 150)), 2000))
        except ValueError:
            lat, lon, km = 35.86, -86.35, 150
        parts = [f"r/{lat:.3f}/{lon:.3f}/{km}"]
        calls, buddies = self.watch_calls()
        if calls:
            parts.append("g/" + "/".join(calls[:60]))       # messages to our users
            parts.append("b/" + "/".join(c + "*" for c in calls[:60]))  # our users' own stations, any SSID
        if buddies:
            parts.append("b/" + "/".join(buddies[:60]))
        extra = re.sub(r"[^\x20-\x7e]", "", s.get("aprs_filter_extra") or "").strip()
        if extra:
            parts.append(extra[:300])
        return " ".join(parts)

    def refilter(self):
        """Settings or watch lists changed: push a new filter to the open connection."""
        self.want_filter = None
        self.wake.set()

    # ---- receive loop
    def run(self):
        backoff = 5
        while True:
            s = self.settings()
            if s.get("aprs_enabled") != "1":
                with self.lock:
                    self.status.update(connected=False, error="APRS is turned off in Admin settings.")
                self.wake.wait(30)
                self.wake.clear()
                continue
            try:
                self.session(*self.server_of(s))
                backoff = 5
            except (OSError, ValueError) as e:
                with self.lock:
                    self.status.update(connected=False, error=f"{type(e).__name__}: {e}"[:200])
            finally:
                self.sock = None
            self.wake.wait(backoff)
            self.wake.clear()
            backoff = min(backoff * 2, 300)

    def session(self, host, port):
        import socket
        flt = self.build_filter()
        sock = socket.create_connection((host, port), timeout=30)
        self.sock = sock
        buf = b""
        try:
            sock.sendall(f"user {self.rx_login()} pass -1 vers TACISS {VERSION} filter {flt}\r\n".encode())
            self.want_filter = flt
            with self.lock:
                self.status.update(connected=True, since=now_iso(), error=None, filter=flt, server=host)
            last_push = last_ka = time.time()
            sock.settimeout(5)
            while True:
                if self.wake.is_set():  # settings changed
                    self.wake.clear()
                    s = self.settings()
                    if s.get("aprs_enabled") != "1" or self.server_of(s) != (host, port):
                        return  # turned off, or a different server: run() reconnects
                if self.want_filter is None:  # area or watch list changed: tell the server
                    flt = self.build_filter()
                    sock.sendall(f"#filter {flt}\r\n".encode())
                    self.want_filter = flt
                    with self.lock:
                        self.status["filter"] = flt
                try:
                    data = sock.recv(65536)
                    if not data:
                        raise ConnectionError("server closed the connection")
                    buf += data
                except socket.timeout:
                    pass
                if len(buf) > 1_000_000:
                    buf = b""
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.decode("latin-1", "replace").strip()
                    if line.startswith("# logresp"):
                        m = re.search(r"server (\S+)", line)
                        with self.lock:
                            self.status["server"] = f"{m[1] if m else host} ({host})"
                    elif line.startswith("# Login") or "not allowed" in line:
                        with self.lock:  # e.g. "# Login by user not allowed": keep it for the status line
                            self.status["error"] = line[2:200]
                    elif line and not line.startswith("#"):
                        self.ingest(line)
                now = time.time()
                if now - last_ka > 120:  # keepalive so the server keeps the session
                    sock.sendall(b"#keepalive\r\n")
                    last_ka = now
                if self.dirty and now - last_push > 4:
                    self.dirty = False
                    last_push = now
                    BUS.publish({"entity": "aprs", "action": "update", "public": False})
        finally:
            with self.lock:
                self.status["connected"] = False
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def rx_login():
        """Read-only APRS-IS login. Servers refuse N0CALL and drop duplicate logins, so each
        TACISS node uses its own name derived from its mesh node ID (e.g. TACAB12CD)."""
        nid = re.sub(r"[^0-9A-F]", "", str(NODE.get("id") or "").upper())
        return "TAC" + (nid[:6] or secrets.token_hex(3).upper())

    @staticmethod
    def server_of(s):
        host, _, port = (s.get("aprs_server") or "rotate.aprs2.net:14580").partition(":")
        return host.strip(), int(port or 14580)

    def ingest(self, line, now=None):
        rec = parse_aprs(line)
        ts = now or now_iso()
        with self.lock:
            self.status["packets"] += 1
            self.packets.append({"t": ts, "raw": line[:300], "type": rec["type"] if rec else "unparsed"})
            if not rec:
                return rec
            if rec["type"] == "message":
                self.messages.append({"t": ts, "src": rec["src"], "to": rec["to"], "text": rec.get("text"),
                                      "msgno": rec.get("msgno"), "ack": rec.get("ack")})
            elif rec.get("lat") is not None or rec["type"] in ("status", "weather"):
                key = rec["call"]
                old = self.stations.get(key, {})
                if rec.get("killed"):
                    self.stations.pop(key, None)
                else:
                    new = {k: v for k, v in old.items() if k in ("lat", "lon", "sym", "first", "count", "wx",
                                                                 "comment", "status")}
                    for k in ("lat", "lon", "sym", "course", "speed_kmh", "alt_m", "wx", "by", "path", "type"):
                        if rec.get(k) is not None:
                            new[k] = rec[k]
                    if rec["type"] == "status":
                        new["status"] = rec.get("comment")
                        new.setdefault("type", "position")
                    elif rec.get("comment") is not None:
                        new["comment"] = rec["comment"]
                    new.update(call=key, last=ts, first=old.get("first") or ts, count=old.get("count", 0) + 1,
                               raw=rec["raw"])
                    if new.get("lat") is not None:
                        self.stations[key] = new
                if len(self.stations) > self.MAX_STATIONS:
                    for k, _ in sorted(self.stations.items(), key=lambda kv: kv[1]["last"])[:500]:
                        self.stations.pop(k, None)
            self.dirty = True
        return rec

    def snapshot(self, hours=2):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0.1, min(hours, self.KEEP_HOURS)))) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            return [dict(v) for v in self.stations.values() if v["last"] >= cutoff]

    def start(self):
        threading.Thread(target=self.run, name="aprs-is", daemon=True).start()

    # ---- aprs.fi API
    def aprsfi(self, params, ttl=60):
        key = self.settings_secret("aprsfi_api_key")
        if not key:
            raise ApiError(400, "An aprs.fi API key isn't set. An admin can add one in Admin -> Settings "
                                "(free from your aprs.fi account page).")
        ck = json.dumps(params, sort_keys=True)
        hit = self.api_cache.get(ck)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        url = "https://api.aprs.fi/api/get?" + urlencode(dict(params, apikey=key, format="json"))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": self.UA}), timeout=15) as r:
                data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
        except (OSError, ValueError) as e:
            raise ApiError(502, f"Couldn't reach aprs.fi: {e}")
        if data.get("result") != "ok":
            raise ApiError(502, f"aprs.fi: {data.get('description') or data.get('code') or 'request failed'}")
        self.api_cache[ck] = (time.time(), data)
        if len(self.api_cache) > 300:
            self.api_cache.clear()
        return data

    @staticmethod
    def settings_secret(k):
        con = connect()
        try:
            r = con.execute("SELECT value FROM settings WHERE key = ?", (k,)).fetchone()
            return r[0] if r else ""
        finally:
            con.close()

    # ---- transmit
    def send(self, call, passcode, packet, host=None, port=None):
        """Log in to APRS-IS as `call` and send one packet line. Returns the server's login reply."""
        import socket
        s = self.settings()
        if not host:
            host, port = self.server_of(s)
        with socket.create_connection((host.strip(), port), timeout=15) as sock:
            f = sock.makefile("rb")
            f.readline()
            sock.sendall(f"user {call} pass {passcode} vers TACISS {VERSION}\r\n".encode())
            reply = ""
            deadline = time.time() + 10
            while time.time() < deadline:
                line = f.readline().decode("latin-1", "replace").strip()
                if line.startswith("# logresp"):
                    reply = line
                    break
                if not line:
                    break
            if " verified" not in reply or "unverified" in reply:
                raise ApiError(400, f"APRS-IS didn't accept the login for {call}. Check the passcode. "
                                    f"({reply or 'no reply'})")
            sock.sendall(packet.encode("ascii") + b"\r\n")
            time.sleep(1)  # let it go out before closing
        self.ingest(packet)  # show it locally right away
        return reply


APRS = AprsService()


# --------------------------------------------------------------------------
# MeshCore: LoRa mesh traffic as seen by internet-connected observers.
#  * Node directory for our area from the public MeshCore map (map.meshcore.dev).
#  * Live packets, observers, nodes and public-channel messages from a CoreScope
#    analyzer (open source; run by a regional group or yourself) if one is configured.
#  * Links (and an optional embedded view) for other web tools such as LetsMesh.
# Everything is fetched on demand when someone opens the tab and cached (also on
# disk, so the last data stays viewable offline).
# --------------------------------------------------------------------------
MC_NODE_TYPES = {1: "companion", 2: "repeater", 3: "room", 4: "sensor"}
MC_PAYLOADS = {0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 3: "ACK", 4: "ADVERT", 5: "GRP_TXT", 6: "GRP_DATA",
               7: "ANON_REQ", 8: "PATH", 9: "TRACE", 10: "MULTIPART", 11: "CONTROL", 15: "RAW_CUSTOM"}


def _bbox(s, key):
    try:
        so, w, n, e = (float(x) for x in (s.get(key) or "").split(","))
        return so, w, n, e
    except ValueError:
        return 34.9, -90.4, 36.7, -81.6


def _in_box(lat, lon, box):
    try:
        return box[0] <= float(lat) <= box[2] and box[1] <= float(lon) <= box[3]
    except (TypeError, ValueError):
        return False


class MeshcoreService:
    UA = f"TACISS/{VERSION} (+https://github.com/amorilab/taciss)"
    MAX_MAP_BYTES = 400 * 1024 * 1024  # decompressed safety cap

    def __init__(self):
        self.map_lock = threading.Lock()  # one download of the big node list at a time

    def map_nodes(self, s):
        """Nodes in our box from the public MeshCore map. The list is worldwide (~55 MB of JSON), so it's
        streamed and filtered object by object instead of loaded whole (fine on a Raspberry Pi)."""
        url = s.get("meshcore_map_url") or SETTINGS_DEFAULTS["meshcore_map_url"]
        box = _bbox(s, "meshcore_bbox")
        key = "meshcore-map-" + hashlib.sha1(f"{url}|{box}".encode()).hexdigest()[:10]

        def fetch():
            req = urllib.request.Request(url, headers={"User-Agent": self.UA, "Accept-Encoding": "gzip"})
            out, seen = [], 0
            with urllib.request.urlopen(req, timeout=120) as r:
                gz = zlib.decompressobj(16 + zlib.MAX_WBITS) if r.headers.get("Content-Encoding") == "gzip" else None
                text = codecs_decoder()
                dec = json.JSONDecoder()
                buf, started = "", False
                while True:
                    chunk = r.read(256 * 1024)
                    if chunk:
                        data = gz.decompress(chunk) if gz else chunk
                        seen += len(data)
                        if seen > self.MAX_MAP_BYTES:
                            raise ValueError("node list too large")
                        buf += text.decode(data)
                    i = 0
                    while True:
                        while i < len(buf) and buf[i] in " \t\r\n,":
                            i += 1
                        if not started:
                            if i < len(buf) and buf[i] == "[":
                                started, i = True, i + 1
                                continue
                            if i < len(buf):
                                raise ValueError("unexpected node list format")
                            break
                        if i >= len(buf) or buf[i] == "]":
                            break
                        try:
                            obj, j = dec.raw_decode(buf, i)
                        except ValueError:
                            break  # object continues in the next chunk
                        i = j
                        n = self.compact_map_node(obj, box)
                        if n:
                            out.append(n)
                    buf = buf[i:]
                    if not chunk:
                        break
            return out

        with self.map_lock:
            return FEEDS.cached(key, 3600, fetch)

    @staticmethod
    def compact_map_node(o, box):
        if not isinstance(o, dict) or not _in_box(o.get("adv_lat"), o.get("adv_lon"), box):
            return None
        last = str(o.get("last_advert") or "")[:19] + "Z" if o.get("last_advert") else None
        if last and last > (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"):
            last = str(o.get("updated_date") or "")[:19] + "Z" or None  # node clock is wrong; use when the map saw it
        p = o.get("params") or {}
        radio = f"{p.get('freq')} MHz SF{p.get('sf')} BW{p.get('bw')} CR{p.get('cr')}" if p.get("freq") else ""
        return {"key": str(o.get("public_key") or "")[:64], "name": str(o.get("adv_name") or "")[:60],
                "type": MC_NODE_TYPES.get(o.get("type"), "other"), "lat": round(float(o["adv_lat"]), 5),
                "lon": round(float(o["adv_lon"]), 5), "last": last, "radio": radio, "source": "map"}

    # ---- CoreScope analyzer
    def analyzer(self, s, path, ttl):
        base = (s.get("meshcore_analyzer_url") or "").rstrip("/")
        if not base:
            raise ApiError(400, "No MeshCore analyzer is set up. An admin can add one in Admin -> Settings.")
        key = "meshcore-an-" + hashlib.sha1(f"{base}{path}".encode()).hexdigest()[:12]

        def fetch():
            req = urllib.request.Request(base + path, headers={"User-Agent": self.UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read(20 * 1024 * 1024).decode("utf-8", "replace"))
        return FEEDS.cached(key, ttl, fetch)

    @staticmethod
    def channel_key(name):
        return str(name or "").strip().lstrip("#").lower()

    def channel_ok(self, s, name):
        """True if this public channel is on the admin's list (or there is no list)."""
        allow = {self.channel_key(c) for c in (s.get("meshcore_channels") or "").split(",") if c.strip()}
        return not allow or self.channel_key(name) in allow

    @staticmethod
    def regions(s):
        return ",".join(c for c in re.split(r"[\s,]+", (s.get("meshcore_regions") or "").upper()) if c)

    def packets(self, s):
        q = "/api/packets?limit=150&order=desc" + (f"&region={quote(self.regions(s))}" if self.regions(s) else "")
        res = self.analyzer(s, q, 15)
        rows = []
        for p in ((res.get("data") or {}).get("packets") or [])[:150]:
            try:
                d = json.loads(p.get("decoded_json") or "{}")
            except ValueError:
                d = {}
            kind = d.get("type") or MC_PAYLOADS.get(p.get("payload_type"), str(p.get("payload_type")))
            summary = ""
            if kind == "ADVERT":
                f = d.get("flags") or {}
                role = next((k for k in ("repeater", "room", "sensor", "chat") if f.get(k)), "")
                summary = f"{d.get('name') or '?'}" + (f" ({'companion' if role == 'chat' else role})" if role else "")
            elif kind in ("GRP_TXT", "GRP_DATA"):
                if (s.get("meshcore_channels") or "").strip() and not (d.get("channel") and self.channel_ok(s, d["channel"])):
                    continue  # channel list set: skip other channels (and encrypted ones we can't name)
                summary =(f"#{d['channel']}" if d.get("channel") and not str(d["channel"]).startswith("#") else
                           d.get("channel") or f"channel {d.get('channelHashHex', '?')}")
                if d.get("decryptionStatus") != "decrypted":
                    summary += " (encrypted)"
            elif kind == "CONTROL":
                summary = str(d.get("ctrlSubtype") or "")
            path = p.get("_parsedPath") or []
            rows.append({"t": p.get("timestamp") or p.get("first_seen"), "type": kind, "summary": summary[:120],
                         "observer": p.get("observer_name"), "iata": p.get("observer_iata"),
                         "hops": len(path) if isinstance(path, list) else None, "snr": p.get("snr"),
                         "rssi": p.get("rssi"), "seen": p.get("observation_count"), "hash": p.get("hash"),
                         "lat": d.get("lat") if kind == "ADVERT" else None, "lon": d.get("lon") if kind == "ADVERT" else None})
        return {"updated": res["updated"], "error": res["error"], "data": rows}

    def observers(self, s):
        res = self.analyzer(s, "/api/observers", 120)
        regs, box = set(self.regions(s).split(",")) - {""}, _bbox(s, "meshcore_bbox")
        keep = []
        for o in ((res.get("data") or {}).get("observers") or []):
            if (regs and o.get("iata") in regs) or (not regs and _in_box(o.get("lat"), o.get("lon"), box)) or \
                    (regs and _in_box(o.get("lat"), o.get("lon"), box)):
                keep.append({k: o.get(k) for k in ("id", "name", "iata", "last_seen", "packetsLastHour", "model",
                                                   "lat", "lon", "battery_mv", "noise_floor", "nodeRole")})
        return {"updated": res["updated"], "error": res["error"], "data": keep}

    def nodes(self, s):
        res = self.analyzer(s, "/api/nodes?limit=5000", 300)
        box = _bbox(s, "meshcore_bbox")
        keep = [{"key": n.get("public_key"), "name": n.get("name"), "type": n.get("role") or n.get("type") or "node",
                 "lat": n.get("lat"), "lon": n.get("lon"), "last": n.get("last_heard") or n.get("last_seen"),
                 "source": "analyzer"}
                for n in ((res.get("data") or {}).get("nodes") or []) if _in_box(n.get("lat"), n.get("lon"), box)]
        return {"updated": res["updated"], "error": res["error"], "data": keep}

    def channels(self, s):
        res = self.analyzer(s, "/api/channels", 60)
        rows = [{k: c.get(k) for k in ("hash", "name", "lastActivity", "lastSender", "lastMessage", "messageCount")}
                for c in ((res.get("data") or {}).get("channels") or []) if self.channel_ok(s, c.get("name") or c.get("hash"))]
        rows.sort(key=lambda c: c.get("lastActivity") or "", reverse=True)
        return {"updated": res["updated"], "error": res["error"], "data": rows[:60]}

    def messages(self, s, channel):
        if not self.channel_ok(s, channel):
            raise ApiError(403, "That channel isn't one of the channels this server shows.")
        q = f"/api/channels/{quote(channel, safe='')}/messages?limit=60" + \
            (f"&region={quote(self.regions(s))}" if self.regions(s) else "")
        res = self.analyzer(s, q, 30)
        rows = [{k: m.get(k) for k in ("timestamp", "sender", "text", "hops", "snr", "observers", "repeats")}
                for m in ((res.get("data") or {}).get("messages") or [])]
        rows.sort(key=lambda m: m.get("timestamp") or "", reverse=True)  # newest first
        return {"updated": res["updated"], "error": res["error"], "data": rows}


_EMBED = {"t": 0.0, "origin": ""}


def embed_origin():
    """https origin of the configured MeshCore embed page (re-read at most every 30 s)."""
    if time.time() - _EMBED["t"] > 30:
        origin = ""
        try:
            con = connect()
            try:
                r = con.execute("SELECT key, value FROM settings WHERE key IN ('meshcore_embed_url', 'meshcore_enabled')")
                s = dict(SETTINGS_DEFAULTS, **{k: v for k, v in r})
            finally:
                con.close()
            u = urlparse(s.get("meshcore_embed_url") or "")
            if s.get("meshcore_enabled") == "1" and u.scheme == "https" and u.hostname and \
                    re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", u.netloc):
                origin = f"https://{u.netloc}"
        except sqlite3.Error:
            pass
        _EMBED.update(t=time.time(), origin=origin)
    return _EMBED["origin"]


def codecs_decoder():
    import codecs
    return codecs.getincrementaldecoder("utf-8")("replace")


MESHCORE = MeshcoreService()


def clean_record(entity, data, partial):
    if not isinstance(data, dict):
        raise ApiError(400, "Expected a JSON object.")
    spec = ENTITIES[entity]
    out = {}
    for f in spec["fields"]:
        n = f["name"]
        if f.get("hidden"):
            continue
        if n not in data:
            if not partial:
                if f["default"] is not None:
                    out[n] = f["default"]
                elif f["required"]:
                    raise ApiError(400, f"{f['label']} is required.")
            continue
        v = data[n]
        if f["type"] == "refs":
            if isinstance(v, str):
                v = [x for x in re.split(r"[,\s]+", v) if x]
            if v is None:
                v = []
            try:
                ids = sorted({int(x) for x in v}) if isinstance(v, list) else None
            except (TypeError, ValueError):
                ids = None
            if ids is None or len(ids) > 200:
                raise ApiError(400, f"{f['label']} must be a list of ids.")
            out[n] = json.dumps(ids)
            continue
        if v is None or (isinstance(v, str) and v.strip() == ""):
            if f["required"]:
                raise ApiError(400, f"{f['label']} is required.")
            out[n] = None
            continue
        t = f["type"]
        try:
            if t in ("text", "textarea"):
                v = str(v).strip()[:MAX_TEXT]
                if f["choices"] and v not in f["choices"]:
                    raise ApiError(400, f"{f['label']} must be one of: {', '.join(f['choices'])}.")
            elif t == "real":
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError
                if n == "lat" and not -90 <= v <= 90 or n == "lon" and not -180 <= v <= 180:
                    raise ApiError(400, f"{f['label']} is out of range.")
            elif t in ("int", "ref"):
                v = int(v)
        except (TypeError, ValueError):
            raise ApiError(400, f"{f['label']} is not a valid {t}.")
        out[n] = v
    return out


# --------------------------------------------------------------------------
# Mesh: node-to-node sync between TACISS installs (Raspberry Pi field kits, EOC
# servers...). Works over any IP link - LAN/Wi-Fi, AREDN, Tailscale/WireGuard, internet.
#
#  * Every shared record gets a global id (mesh_uid), the node that created it
#    (mesh_origin) and a version (hybrid clock "ms-node") that orders edits.
#  * Each node keeps a change log (mesh_log) holding the latest state of every record
#    it knows about - its own AND ones it received - so changes hop A -> B -> C even
#    when A and C never talk directly (store-and-forward).
#  * Peers pull ("what changed since #N?") and push; newer version wins, equal/older
#    is ignored, so repeats and loops are harmless.
#  * Requests and replies are HMAC-signed with the pairing secret: tamper-proof and
#    authenticated, but NOT encrypted (AREDN / amateur radio must stay readable).
#    Encryption comes from the link: Tailscale/WireGuard or HTTPS.
# --------------------------------------------------------------------------
MESH_ENTITIES = ["programs", "agencies", "incidents", "activity", "checkins", "reports",
                 "resources", "cameras", "messages"]  # parents before children
MESH_LINKS = {
    "lan": "Local network (LAN / Wi-Fi)",
    "aredn": "AREDN ham mesh (signed, not encrypted - Part 97)",
    "vpn": "Tailscale / WireGuard VPN",
    "internet": "Internet (HTTPS)",
}
MESH_PAGE_ITEMS = 300
MESH_PAGE_BYTES = 400_000
MESH_MAX_BODY = 4 * 1024 * 1024        # compressed request/response cap
MESH_MAX_JSON = 24 * 1024 * 1024       # after decompression
MESH_UID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{12}")
MESH_VER_RE = re.compile(r"\d{13}-[0-9a-f]{16}")
MESH_NODE_RE = re.compile(r"[0-9a-f]{16}")
NODE = {"id": None, "name": None}
_HLC = {"last": 0}
_HLC_LOCK = threading.Lock()


class MeshPending(Exception):
    """A change refers to a record we don't have yet (it will arrive later)."""


def mesh_setting(con, key):
    r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return (r[0] if r and r[0] is not None else SETTINGS_DEFAULTS.get(key, "")).strip()


def mesh_init(con):
    """Node identity + clock; stamp records created before mesh existed (or by the CLI)."""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS mesh_meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS mesh_log (
        uid TEXT PRIMARY KEY, seq INTEGER NOT NULL, entity TEXT NOT NULL, origin TEXT,
        version TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, payload TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_log_seq ON mesh_log(seq);
    CREATE TABLE IF NOT EXISTS mesh_nodes (node_id TEXT PRIMARY KEY, name TEXT, seen_at TEXT);
    CREATE TABLE IF NOT EXISTS mesh_peers (
        id INTEGER PRIMARY KEY, node_id TEXT UNIQUE NOT NULL, name TEXT, url TEXT,
        secret TEXT NOT NULL, link TEXT NOT NULL DEFAULT 'lan',
        share_restricted INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
        pull_cursor INTEGER NOT NULL DEFAULT 0, push_cursor INTEGER NOT NULL DEFAULT 0,
        last_ok TEXT, last_try TEXT, last_error TEXT, last_seen TEXT, clock_skew REAL,
        received INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS mesh_invites (
        id INTEGER PRIMARY KEY, secret TEXT NOT NULL, note TEXT, link TEXT NOT NULL DEFAULT 'lan',
        share_restricted INTEGER NOT NULL DEFAULT 0, created_by TEXT, created_at TEXT NOT NULL,
        expires_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS mesh_pending (
        uid TEXT PRIMARY KEY, version TEXT NOT NULL, item TEXT NOT NULL, received_at REAL NOT NULL);
    """)
    r = con.execute("SELECT value FROM mesh_meta WHERE key = 'node_id'").fetchone()
    if r:
        NODE["id"] = r[0]
    else:
        NODE["id"] = secrets.token_hex(8)
        con.execute("INSERT INTO mesh_meta(key, value) VALUES ('node_id', ?)", (NODE["id"],))
    mesh_refresh_name(con)
    for name in MESH_ENTITIES:
        existing = {r["name"] for r in con.execute(f"PRAGMA table_info({name})")}
        for col in ("mesh_uid", "mesh_origin", "mesh_version"):
            if col not in existing:
                con.execute(f"ALTER TABLE {name} ADD COLUMN {col} TEXT")
        con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{name}_mesh_uid ON {name}(mesh_uid)")
    top = con.execute("SELECT MAX(version) FROM mesh_log").fetchone()[0]
    if top:
        mesh_observe(top)
    for name in MESH_ENTITIES:  # parents first so references resolve
        for (rid,) in con.execute(f"SELECT id FROM {name} WHERE mesh_uid IS NULL ORDER BY id").fetchall():
            mesh_stamp(con, name, rid)
    con.execute("DELETE FROM mesh_invites WHERE expires_at < ?", (time.time(),))
    con.execute("DELETE FROM mesh_pending WHERE received_at < ?", (time.time() - 7 * DAY,))


def mesh_refresh_name(con):
    import socket
    NODE["name"] = (mesh_setting(con, "mesh_node_name") or socket.gethostname() or "TACISS")[:60]
    con.execute("INSERT INTO mesh_nodes(node_id, name, seen_at) VALUES (?,?,?) ON CONFLICT(node_id) "
                "DO UPDATE SET name = excluded.name", (NODE["id"], NODE["name"], now_iso()))


def mesh_version():
    with _HLC_LOCK:
        ms = max(int(time.time() * 1000), _HLC["last"] + 1)
        _HLC["last"] = ms
    return f"{ms:013d}-{NODE['id']}"


def mesh_observe(version):
    """Hybrid clock: never issue a version older than one we've seen (Pis often lack a
    battery clock). Ignore anything absurdly far in the future."""
    try:
        ms = int(str(version)[:13])
    except ValueError:
        return
    if ms > time.time() * 1000 + 7 * DAY * 1000:
        return
    with _HLC_LOCK:
        _HLC["last"] = max(_HLC["last"], ms)


def mesh_new_uid():
    return f"{NODE['id'][:8]}-{secrets.token_hex(6)}"


def mesh_uid_of(con, table, rid):
    if rid is None:
        return None
    r = con.execute(f"SELECT mesh_uid FROM {table} WHERE id = ?", (rid,)).fetchone()
    return r[0] if r else None


def mesh_local_id(con, table, uid):
    if not isinstance(uid, str):
        return None
    r = con.execute(f"SELECT id FROM {table} WHERE mesh_uid = ?", (uid,)).fetchone()
    return r[0] if r else None


def mesh_qualify(who):
    """Usernames are per-node: 'alice' here travels as 'alice@Knox EOC'."""
    if not who:
        return who
    return who if "@" in who else f"{who}@{NODE['name']}"


def mesh_unqualify(who):
    if not isinstance(who, str):
        return None
    who = who.strip()[:120]
    suffix = "@" + NODE["name"]
    return who[:-len(suffix)] if who.endswith(suffix) else who


def mesh_payload(con, entity, row):
    """Row -> portable dict (local ids replaced by mesh uids)."""
    d = {}
    for f in ENTITIES[entity]["fields"]:
        n, v = f["name"], row[f["name"]]
        if f["type"] == "ref":
            v = mesh_uid_of(con, f["ref"], v)
        elif f["type"] == "refs":
            v = [u for u in (mesh_uid_of(con, f["ref"], i) for i in parse_refs(v)) if u]
        elif entity == "messages" and n == "channel":
            iid = incident_channel(v)
            if iid is not None:
                v = "incident:" + (mesh_uid_of(con, "incidents", iid) or "")
        d[n] = v
    for k in ("created_at", "updated_at"):
        d[k] = row[k]
    for k in ("created_by", "updated_by"):
        d[k] = mesh_qualify(row[k])
    return d


def mesh_log_put(con, uid, entity, origin, version, deleted, payload):
    con.execute(
        "INSERT INTO mesh_log(uid, seq, entity, origin, version, deleted, payload) "
        "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM mesh_log), ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET seq = excluded.seq, entity = excluded.entity, origin = excluded.origin, "
        "version = excluded.version, deleted = excluded.deleted, payload = excluded.payload",
        (uid, entity, origin, version, 1 if deleted else 0,
         None if payload is None else json.dumps(payload, separators=(",", ":"), default=str)))


def mesh_stamp(con, entity, rid):
    """A record changed on THIS node: new version + into the change log."""
    if entity not in MESH_ENTITIES:
        return
    row = con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
    if not row:
        return
    uid = row["mesh_uid"] or mesh_new_uid()
    origin = row["mesh_origin"] or NODE["id"]
    ver = mesh_version()
    con.execute(f"UPDATE {entity} SET mesh_uid = ?, mesh_origin = ?, mesh_version = ? WHERE id = ?",
                (uid, origin, ver, rid))
    row = con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
    mesh_log_put(con, uid, entity, origin, ver, False, mesh_payload(con, entity, row))


def mesh_tombstone(con, entity, row):
    if entity in MESH_ENTITIES and row is not None and row["mesh_uid"]:
        mesh_log_put(con, row["mesh_uid"], entity, row["mesh_origin"], mesh_version(), True, None)


def mesh_children(con, iid):
    """(entity, id) of everything hanging off an incident."""
    out = []
    for name in MESH_ENTITIES:
        if any(f["name"] == "incident_id" for f in ENTITIES[name]["fields"]):
            out += [(name, r[0]) for r in con.execute(f"SELECT id FROM {name} WHERE incident_id = ?", (iid,))]
    out += [("messages", r[0]) for r in con.execute("SELECT id FROM messages WHERE channel = ?", (f"incident-{iid}",))]
    return out


def delete_record(con, entity, rid, log=True):
    """Delete a record and tidy everything pointing at it. log=True records the deletes /
    detaches as mesh changes (a local delete); False = quiet (applying a remote delete,
    whose origin already logged its own cascade). Returns [(entity, row)] removed."""
    row = con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
    if not row:
        return []
    removed = []
    for other, ospec in ENTITIES.items():
        for f in ospec["fields"]:
            if f.get("ref") != entity:
                continue
            col = f["name"]
            if f["type"] == "refs":
                for r in con.execute(f"SELECT id, {col} FROM {other} WHERE {col} LIKE ?", (f"%{rid}%",)).fetchall():
                    ids = parse_refs(r[1])
                    if rid in ids:
                        con.execute(f"UPDATE {other} SET {col} = ? WHERE id = ?",
                                    (json.dumps([i for i in ids if i != rid]), r[0]))
                        if log:
                            mesh_stamp(con, other, r[0])
            elif ospec.get("append_only") or f["required"]:  # owned by this record: remove
                kids = con.execute(f"SELECT * FROM {other} WHERE {col} = ?", (rid,)).fetchall()
                con.execute(f"DELETE FROM {other} WHERE {col} = ?", (rid,))
                removed += [(other, k) for k in kids]
                if log:
                    for k in kids:
                        mesh_tombstone(con, other, k)
            else:
                ids = [r[0] for r in con.execute(f"SELECT id FROM {other} WHERE {col} = ?", (rid,))]
                con.execute(f"UPDATE {other} SET {col} = NULL WHERE {col} = ?", (rid,))
                if log:
                    for i in ids:
                        mesh_stamp(con, other, i)
    if entity in MEMBER_KINDS:
        con.execute("DELETE FROM memberships WHERE kind = ? AND ref_id = ?", (entity, rid))
    if entity == "incidents":
        kids = con.execute("SELECT * FROM messages WHERE channel = ?", (f"incident-{rid}",)).fetchall()
        con.execute("DELETE FROM messages WHERE channel = ?", (f"incident-{rid}",))
        removed += [("messages", k) for k in kids]
        if log:
            for k in kids:
                mesh_tombstone(con, "messages", k)
    con.execute(f"DELETE FROM {entity} WHERE id = ?", (rid,))
    removed.append((entity, row))
    if log:
        mesh_tombstone(con, entity, row)
    return removed


def scope_of(con, entity, row):
    """The incident (as a dict) a record belongs to, for access checks."""
    iid = incident_id_of(entity, row)
    if iid is None:
        return None
    if entity == "incidents":
        return dict(row)
    r = con.execute("SELECT id, visibility, created_by, programs, agencies FROM incidents WHERE id = ?",
                    (iid,)).fetchone()
    return dict(r) if r else None


def change_event(con, entity, action, rid, row, prev=None):
    """Live (SSE) event. Records of a restricted incident carry its access list so the stream
    only tells people who may see it (no record data either way)."""
    evt = {"entity": entity, "action": action, "id": rid, "public": ENTITIES[entity]["public"]}
    scopes = [s for s in (scope_of(con, entity, r) for r in (row, prev) if r is not None) if s]
    if scopes and all(s.get("visibility") == "restricted" for s in scopes):
        evt["public"] = False
        evt["acl"] = [{k: s.get(k) for k in ("visibility", "created_by", "programs", "agencies")} for s in scopes]
    return evt


def mesh_is_restricted(con, entity, data):
    if entity == "incidents":
        return (data or {}).get("visibility") == "restricted"
    inc = (data or {}).get("incident_id")
    if entity == "messages" and str((data or {}).get("channel") or "").startswith("incident:"):
        inc = data["channel"][9:]
    if not inc:
        return False
    r = con.execute("SELECT visibility FROM incidents WHERE mesh_uid = ?", (inc,)).fetchone()
    return bool(r and r[0] == "restricted")


def mesh_outgoing(con, peer, since, max_items=MESH_PAGE_ITEMS, max_bytes=MESH_PAGE_BYTES):
    """Changes after log position `since`, as this peer is allowed to see them."""
    rows = con.execute("SELECT * FROM mesh_log WHERE seq > ? ORDER BY seq LIMIT ?", (since, max_items)).fetchall()
    items, size, last = [], 0, since
    for r in rows:
        last = r["seq"]
        if r["version"].endswith("-" + peer["node_id"]):
            continue  # they made this exact change; no need to echo it back
        it = {"uid": r["uid"], "entity": r["entity"], "origin": r["origin"], "version": r["version"]}
        if r["deleted"]:
            it["op"] = "del"
        else:
            data = json.loads(r["payload"])
            if not peer["share_restricted"] and mesh_is_restricted(con, r["entity"], data):
                it["op"] = "hide"  # locked incident: not shared with this peer (and removed if it was)
            else:
                it["op"], it["data"] = "put", data
        items.append(it)
        size += len(r["payload"] or "") + 120
        if size >= max_bytes:
            break
    top = con.execute("SELECT COALESCE(MAX(seq), 0) FROM mesh_log").fetchone()[0]
    return items, last, last < top, top


def mesh_localize(con, entity, data):
    """Portable dict -> validated local record (uids -> local ids)."""
    if not isinstance(data, dict):
        raise ApiError(400, "bad record")
    d = dict(data)
    for f in ENTITIES[entity]["fields"]:
        n, v = f["name"], d.get(f["name"])
        if f["type"] == "ref" and v is not None:
            lid = mesh_local_id(con, f["ref"], v)
            if lid is None:
                raise MeshPending(v)
            d[n] = lid
        elif f["type"] == "refs":
            ids = []
            for u in (v if isinstance(v, list) else [])[:200]:
                lid = mesh_local_id(con, f["ref"], u)
                if lid is None:
                    raise MeshPending(u)
                ids.append(lid)
            d[n] = ids
    if entity == "messages":
        ch = str(d.get("channel") or "")
        if ch.startswith("incident:"):
            lid = mesh_local_id(con, "incidents", ch[9:])
            if lid is None:
                raise MeshPending(ch)
            d["channel"] = f"incident-{lid}"
    rec = clean_record(entity, d, partial=False)
    for f in ENTITIES[entity]["fields"]:
        if f.get("hidden"):
            v = d.get(f["name"])
            rec[f["name"]] = None if v is None else str(v)[:MAX_TEXT]
    for k in ("created_at", "updated_at"):
        v = d.get(k)
        rec[k] = v[:40] if isinstance(v, str) and v else now_iso()
    for k in ("created_by", "updated_by"):
        rec[k] = mesh_unqualify(d.get(k))
    return rec


def mesh_apply(con, it, events):
    """Apply one incoming change. Returns 'applied' | 'old' | 'hidden'; raises MeshPending."""
    entity, uid, ver, op = it.get("entity"), it.get("uid"), it.get("version"), it.get("op")
    origin = it.get("origin")
    if entity not in MESH_ENTITIES or op not in ("put", "del", "hide") or \
            not isinstance(uid, str) or not MESH_UID_RE.fullmatch(uid) or \
            not isinstance(ver, str) or not MESH_VER_RE.fullmatch(ver) or \
            not isinstance(origin, str) or not MESH_NODE_RE.fullmatch(origin):
        raise ApiError(400, "malformed change")
    mesh_observe(ver)
    cur = con.execute("SELECT version, entity FROM mesh_log WHERE uid = ?", (uid,)).fetchone()
    if cur and cur["entity"] != entity:
        raise ApiError(400, "uid/entity mismatch")
    row = con.execute(f"SELECT * FROM {entity} WHERE mesh_uid = ?", (uid,)).fetchone()

    if op == "hide":
        # The sender no longer shares this (incident locked): drop our copy quietly and
        # forget it, so we neither relay it nor block it if it's reopened later.
        if cur and ver < cur["version"]:
            return "old"
        if row:
            events.append(change_event(con, entity, "delete", row["id"], None, row))
            for ent, r in delete_record(con, entity, row["id"], log=False):
                if r["mesh_uid"]:
                    con.execute("DELETE FROM mesh_log WHERE uid = ?", (r["mesh_uid"],))
        con.execute("DELETE FROM mesh_log WHERE uid = ?", (uid,))
        return "hidden"

    if cur and ver <= cur["version"]:
        return "old"
    if op == "del":
        if row:
            events.append(change_event(con, entity, "delete", row["id"], None, row))
            delete_record(con, entity, row["id"], log=False)
        mesh_log_put(con, uid, entity, origin, ver, True, None)
        return "applied"

    data = it.get("data")
    rec = mesh_localize(con, entity, data)
    rec.update(mesh_uid=uid, mesh_origin=origin, mesh_version=ver)
    if row:
        con.execute(f"UPDATE {entity} SET {', '.join(c + ' = ?' for c in rec)} WHERE id = ?", (*rec.values(), row["id"]))
        rid, action = row["id"], "update"
    else:
        cols = list(rec)
        rid = con.execute(f"INSERT INTO {entity} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          [rec[c] for c in cols]).lastrowid
        action = "create"
    new = con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
    mesh_log_put(con, uid, entity, origin, ver, False, data)  # relay exactly what we got
    events.append(change_event(con, entity, action, rid, new, row))
    return "applied"


def mesh_apply_batch(con, items, nodes=None):
    stats = {"applied": 0, "old": 0, "pending": 0, "errors": 0}
    events = []
    if isinstance(nodes, dict):
        for nid, name in list(nodes.items())[:500]:
            if isinstance(nid, str) and MESH_NODE_RE.fullmatch(nid) and isinstance(name, str) and nid != NODE["id"]:
                con.execute("INSERT INTO mesh_nodes(node_id, name, seen_at) VALUES (?,?,?) ON CONFLICT(node_id) "
                            "DO UPDATE SET name = excluded.name, seen_at = excluded.seen_at",
                            (nid, name.strip()[:60], now_iso()))

    def one(it):
        con.execute("SAVEPOINT m")
        try:
            res = mesh_apply(con, it, events)
            con.execute("RELEASE m")
            return res
        except MeshPending:
            con.execute("ROLLBACK TO m")
            con.execute("RELEASE m")
            old = con.execute("SELECT version FROM mesh_pending WHERE uid = ?", (it["uid"],)).fetchone()
            if not old or old[0] < it["version"]:
                con.execute("INSERT OR REPLACE INTO mesh_pending(uid, version, item, received_at) VALUES (?,?,?,?)",
                            (it["uid"], it["version"], json.dumps(it, default=str), time.time()))
            return "pending"
        except (ApiError, KeyError, TypeError, ValueError, AttributeError, sqlite3.IntegrityError) as e:
            con.execute("ROLLBACK TO m")
            con.execute("RELEASE m")
            sys.stderr.write(f"{now_iso()} mesh: skipped {it.get('entity')} {it.get('uid')}: {e}\n")
            return "errors"

    for it in (items or [])[:5000]:
        if not isinstance(it, dict):
            stats["errors"] += 1
            continue
        res = one(it)
        stats["applied" if res == "hidden" else res] += 1
    # retry anything that was waiting on a parent record
    for _ in range(5):
        progress = False
        for p in con.execute("SELECT uid, item FROM mesh_pending").fetchall():
            it = json.loads(p["item"])
            res = one(it) if res_ok(con, it) else "old"
            if res != "pending":
                con.execute("DELETE FROM mesh_pending WHERE uid = ? AND version = ?", (p["uid"], it["version"]))
                if res == "applied":
                    stats["applied"] += 1
                    stats["pending"] = max(0, stats["pending"] - 1)
                    progress = True
        if not progress:
            break
    con.commit()
    for e in events:
        BUS.publish(e)
    if stats["applied"]:
        MESH.kick()  # pass it on to our other peers
    return stats


def res_ok(con, it):
    """A pending item is still worth applying (nothing newer arrived meanwhile)."""
    cur = con.execute("SELECT version FROM mesh_log WHERE uid = ?", (it.get("uid"),)).fetchone()
    return not cur or cur[0] < it.get("version", "")


def mesh_sign(secret, method, path, ts, body):
    msg = f"{method}\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def mesh_unzip(raw, encoding):
    if "gzip" not in (encoding or "").lower():
        return raw
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, MESH_MAX_JSON)
    if d.unconsumed_tail:
        raise ApiError(413, "Mesh payload too large.")
    return out


def mesh_new_code():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L - easy to read over the radio
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def mesh_norm_code(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def mesh_check_url(url, link):
    if link not in MESH_LINKS:
        raise ApiError(400, "Link type must be one of: " + ", ".join(MESH_LINKS))
    if not url:
        return ""
    url = url.strip().rstrip("/")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ApiError(400, "Peer address must look like http://10.1.2.3:8080 or https://eoc.example.org")
    if link == "aredn" and u.scheme == "https":
        raise ApiError(400, "AREDN is amateur radio: Part 97 forbids encrypting traffic, so use http://. "
                            "TACISS still signs every sync so it can't be forged or altered.")
    if link == "internet" and u.scheme != "https":
        raise ApiError(400, "Internet links must use https:// (put Caddy/nginx in front of TACISS). "
                            "Using Tailscale or WireGuard? Pick the VPN link type instead.")
    return url


def mesh_request(url, secret, method, path, obj=None, timeout=25):
    """Signed call to a peer. Returns (json, server_time)."""
    body = b""
    headers = {"X-Mesh-Node": NODE["id"], "Accept-Encoding": "gzip", "User-Agent": f"TACISS-mesh/{VERSION}",
               "Accept": "application/json"}
    if obj is not None:
        body = gzip.compress(json.dumps(obj, separators=(",", ":"), default=str).encode(), 6)
        headers.update({"Content-Type": "application/json", "Content-Encoding": "gzip"})
    ts = str(int(time.time()))
    headers.update({"X-Mesh-Time": ts, "X-Mesh-Sig": mesh_sign(secret, method, path, ts, body)})
    req = urllib.request.Request(url.rstrip("/") + path, data=body if method != "GET" else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MESH_MAX_BODY + 1)
            rh = r.headers
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read(4096) or b"{}").get("error")
        except ValueError:
            msg = None
        raise RuntimeError(f"peer said {e.code}: {msg or e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"can't reach peer ({getattr(e, 'reason', e)})")
    if len(raw) > MESH_MAX_BODY:
        raise RuntimeError("peer reply too large")
    sig = rh.get("X-Mesh-Sig") or ""
    if not hmac.compare_digest(sig, mesh_sign(secret, "RESP", path, rh.get("X-Mesh-Time") or "", raw)):
        raise RuntimeError("peer reply failed signature check (wrong pairing or tampered)")
    try:
        data = json.loads(mesh_unzip(raw, rh.get("Content-Encoding")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zlib.error):
        raise RuntimeError("peer sent bad data")
    try:
        st = float(rh.get("X-Mesh-Time") or 0)
    except ValueError:
        st = 0
    return data, st


def mesh_nodes_map(con):
    return {r[0]: r[1] for r in con.execute("SELECT node_id, name FROM mesh_nodes")}


class MeshService:
    """Background sync with every enabled peer: pull their changes, push ours."""

    def __init__(self):
        self.wake = threading.Event()
        self.locks = {}
        self.guard = threading.Lock()

    def kick(self):
        self.wake.set()

    def run(self):
        SHUTDOWN.wait(3)
        while not SHUTDOWN.is_set():
            try:
                con = connect()
                try:
                    enabled = mesh_setting(con, "mesh_enabled") == "1"
                    interval = int(mesh_setting(con, "mesh_interval") or 30)
                    peers = [r["id"] for r in con.execute(
                        "SELECT id FROM mesh_peers WHERE enabled = 1 AND url IS NOT NULL AND url != ''")]
                finally:
                    con.close()
                if enabled:
                    for pid in peers:
                        self.sync_peer(pid)
            except Exception:
                traceback.print_exc()
                interval = 30
            self.wake.wait(max(5, interval))
            if self.wake.is_set():
                self.wake.clear()
                SHUTDOWN.wait(1)  # let a burst of edits settle into one sync

    def start(self):
        threading.Thread(target=self.run, name="mesh-sync", daemon=True).start()

    def sync_peer(self, pid):
        with self.guard:
            lock = self.locks.setdefault(pid, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"busy": True}
        con = connect()
        result = {"pulled": 0, "pushed": 0}
        try:
            p = con.execute("SELECT * FROM mesh_peers WHERE id = ?", (pid,)).fetchone()
            if not p or not p["url"]:
                return {"error": "no address for this peer"}
            con.execute("UPDATE mesh_peers SET last_try = ? WHERE id = ?", (now_iso(), pid))
            con.commit()
            try:
                skew = None
                cursor = p["pull_cursor"]
                for _ in range(100):  # pull
                    data, st = mesh_request(p["url"], p["secret"], "GET", f"/api/mesh/changes?since={cursor}")
                    if st:
                        skew = st - time.time()
                    if int(data.get("max_seq") or 0) < cursor:  # peer restored from an older backup
                        cursor = 0
                        continue
                    stats = mesh_apply_batch(con, data.get("items") or [], data.get("nodes"))
                    result["pulled"] += stats["applied"]
                    cursor = int(data.get("next") or cursor)
                    con.execute("UPDATE mesh_peers SET pull_cursor = ?, received = received + ? WHERE id = ?",
                                (cursor, stats["applied"], pid))
                    con.commit()
                    if not data.get("more"):
                        break
                pos = p["push_cursor"]
                for _ in range(100):  # push
                    p = con.execute("SELECT * FROM mesh_peers WHERE id = ?", (pid,)).fetchone()
                    items, last, more, top = mesh_outgoing(con, p, pos)
                    if top < pos:
                        pos = 0
                        continue
                    if items:
                        mesh_request(p["url"], p["secret"], "POST", "/api/mesh/changes",
                                     {"items": items, "nodes": mesh_nodes_map(con)})
                        result["pushed"] += len(items)
                    pos = last
                    con.execute("UPDATE mesh_peers SET push_cursor = ?, sent = sent + ? WHERE id = ?",
                                (pos, len(items), pid))
                    con.commit()
                    if not more:
                        break
                con.execute("UPDATE mesh_peers SET last_ok = ?, last_error = NULL, clock_skew = ? WHERE id = ?",
                            (now_iso(), skew, pid))
                con.commit()
            except RuntimeError as e:
                result["error"] = str(e)
                con.execute("UPDATE mesh_peers SET last_error = ? WHERE id = ?", (str(e)[:300], pid))
                con.commit()
            if result["pulled"] or result["pushed"] or bool(result.get("error")) != bool(p["last_error"]):
                BUS.publish({"entity": "mesh", "action": "sync", "public": False, "admin": True})
            return result
        finally:
            con.close()
            lock.release()


MESH = MeshService()


# --------------------------------------------------------------------------
# Mesh discovery: find nearby TACISS nodes so nobody has to type IP addresses.
# Discovery only fills in the address - pairing still needs the code, so a
# stranger on the network can't join just by being seen.
#   LAN:       UDP broadcast + multicast beacon on port 47474
#   AREDN:     the local AREDN node's advertised-services list (sysinfo.json)
#   Tailscale: `tailscale status --json`, then probe each online peer
# --------------------------------------------------------------------------
DISC_PORT = 47474
DISC_GROUP = "239.255.84.67"
DISC_TTL = 600            # forget nodes not heard from in 10 minutes
DISC_MAX = 200
AREDN_SYSINFO = "http://localnode.local.mesh/cgi-bin/sysinfo.json?services=1"


def mesh_whoami(con):
    return {"taciss": 1, "id": NODE["id"], "name": NODE["name"], "v": VERSION,
            "port": CONFIG["port"], "url": mesh_setting(con, "mesh_url") or ""}


def mesh_probe(base, timeout=3):
    """GET <base>/api/mesh/whoami - returns the node's card or None."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/mesh/whoami",
                                     headers={"User-Agent": f"TACISS-mesh/{VERSION}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read(4096).decode("utf-8"))
        if d.get("taciss") == 1 and MESH_NODE_RE.fullmatch(str(d.get("id") or "")):
            return d
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        pass
    return None


class MeshDiscovery:
    def __init__(self):
        self.found = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.sock = None
        self.status = {"lan": "off", "aredn": "not found", "tailscale": "not installed"}

    # -- bookkeeping --------------------------------------------------------
    def add(self, card, via, addr):
        nid = str(card.get("id") or "")
        if not MESH_NODE_RE.fullmatch(nid) or nid == NODE["id"]:
            return
        url = str(card.get("url") or "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+", url):
            try:
                port = int(card.get("port") or 8080)
            except (TypeError, ValueError):
                port = 8080
            url = f"http://{addr}:{port}" if addr else ""
        if not url:
            return
        with self.lock:
            old = self.found.get(nid)
            if old is None and len(self.found) >= DISC_MAX:
                return
            new = {"node_id": nid, "name": str(card.get("name") or nid)[:60], "url": url[:200],
                   "via": via, "version": str(card.get("v") or "")[:20], "seen": time.time()}
            if old is None:
                BUS.publish({"entity": "mesh", "action": "found", "public": False, "admin": True})
            self.found[nid] = new

    def list(self):
        cut = time.time() - DISC_TTL
        with self.lock:
            for k in [k for k, v in self.found.items() if v["seen"] < cut]:
                del self.found[k]
            return sorted(self.found.values(), key=lambda d: d["name"].lower())

    # -- LAN beacon ---------------------------------------------------------
    def open_socket(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", DISC_PORT))
        mreq = socket.inet_aton(DISC_GROUP) + socket.inet_aton("0.0.0.0")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.settimeout(1.0)
        return s

    def beacon(self, kind="hello"):
        if not self.sock:
            return
        con = connect()
        try:
            if mesh_setting(con, "mesh_enabled") != "1" or mesh_setting(con, "mesh_discoverable") != "1":
                if kind == "hello":
                    return
                card = {"taciss": 1, "id": NODE["id"]}  # still allowed to ask, just not announce
            else:
                card = mesh_whoami(con)
        finally:
            con.close()
        card["t"] = kind
        data = json.dumps(card, separators=(",", ":")).encode()
        for dest in (DISC_GROUP, "255.255.255.255"):
            try:
                self.sock.sendto(data, (dest, DISC_PORT))
            except OSError:
                pass

    def listen(self):
        while not SHUTDOWN.is_set():
            try:
                data, (addr, _port) = self.sock.recvfrom(2048)
            except OSError:
                continue
            try:
                card = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(card, dict) or card.get("taciss") != 1 or card.get("id") == NODE["id"]:
                continue
            if card.get("t") == "probe":
                self.beacon("hello")  # answer so the asker sees us right away
                if not card.get("name"):
                    continue
            if card.get("name"):
                self.add(card, "lan", addr)

    # -- AREDN ----------------------------------------------------------------
    def scan_aredn(self):
        try:
            req = urllib.request.Request(AREDN_SYSINFO,
                                         headers={"User-Agent": f"TACISS-mesh/{VERSION}"})
            with urllib.request.urlopen(req, timeout=4) as r:
                info = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            self.status["aredn"] = "not found"
            return
        svcs = [s for s in (info.get("services") or []) if isinstance(s, dict)
                and "taciss" in str(s.get("name") or "").lower()]
        self.status["aredn"] = f"{info.get('node') or 'AREDN node'}: {len(svcs)} TACISS service(s) advertised"
        for s in svcs[:50]:
            u = urlparse(str(s.get("link") or ""))
            if u.scheme != "http" or not u.hostname:
                continue
            base = f"http://{u.hostname}" + (f":{u.port}" if u.port else "")
            card = mesh_probe(base)
            if card:
                card["url"] = card.get("url") or base
                self.add(card, "aredn", u.hostname)

    # -- Tailscale ------------------------------------------------------------
    def scan_tailscale(self):
        import shutil
        import subprocess
        exe = shutil.which("tailscale")
        if not exe:
            self.status["tailscale"] = "not installed"
            return
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=8).stdout
            st = json.loads(out.decode("utf-8", "replace"))
        except (OSError, ValueError, subprocess.SubprocessError):
            self.status["tailscale"] = "not running"
            return
        peers = [p for p in (st.get("Peer") or {}).values() if isinstance(p, dict) and p.get("Online")]
        self.status["tailscale"] = f"{len(peers)} peer(s) online"
        threads = []
        for p in peers[:64]:
            ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
            if not ips:
                continue
            host = (p.get("DNSName") or "").rstrip(".") or ips[0]

            def probe(ip=ips[0], host=host):
                card = mesh_probe(f"http://{ip}:{CONFIG['port']}", timeout=3)
                if card:
                    if not card.get("url"):
                        card["url"] = f"http://{host}:{card.get('port') or CONFIG['port']}"
                    self.add(card, "tailscale", ip)
            t = threading.Thread(target=probe, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(5)

    # -- loop -----------------------------------------------------------------
    def scan(self):
        con = connect()
        try:
            on = mesh_setting(con, "mesh_enabled") == "1"
        finally:
            con.close()
        if not on:
            return
        self.beacon("probe")
        for fn in (self.scan_aredn, self.scan_tailscale):
            try:
                fn()
            except Exception:
                traceback.print_exc()

    def run(self):
        try:
            self.sock = self.open_socket()
            self.status["lan"] = f"listening on UDP {DISC_PORT}"
            threading.Thread(target=self.listen, name="mesh-listen", daemon=True).start()
        except OSError as e:
            self.status["lan"] = f"unavailable ({e})"
        SHUTDOWN.wait(2)
        n = 0
        while not SHUTDOWN.is_set():
            if n % 4 == 0:  # full scan every 2 minutes, beacon every 30 s
                self.scan()
            else:
                self.beacon("hello")
            n += 1
            if self.wake.wait(30):
                self.wake.clear()
                n = 0

    def start(self):
        threading.Thread(target=self.run, name="mesh-discovery", daemon=True).start()


DISCOVERY = MeshDiscovery()


# --------------------------------------------------------------------------
# Update notice: once a day, ask GitHub for the newest release and tell admins.
# Nothing is downloaded or installed; `sudo taciss update` does that.
# --------------------------------------------------------------------------
def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


class UpdateChecker:
    def __init__(self):
        self.latest = None

    def check(self):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", CONFIG["repo"]):
            return
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{CONFIG['repo']}/releases/latest",
                                         headers={"User-Agent": f"TACISS/{VERSION}",
                                                  "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read(1_000_000).decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return  # offline is normal for field kits
        tag = str(d.get("tag_name") or "")
        if re.fullmatch(r"v?\d+(\.\d+){0,3}", tag):
            self.latest = {"version": tag.lstrip("v"), "url": f"https://github.com/{CONFIG['repo']}/releases/tag/{tag}"}

    def notice(self):
        if self.latest and version_tuple(self.latest["version"]) > version_tuple(VERSION):
            return self.latest
        return None

    def run(self):
        SHUTDOWN.wait(60)
        while not SHUTDOWN.is_set():
            self.check()
            SHUTDOWN.wait(24 * 3600)

    def start(self):
        if CONFIG["update_check"]:
            threading.Thread(target=self.run, name="update-check", daemon=True).start()


UPDATES = UpdateChecker()


# --------------------------------------------------------------------------
# Debrief kit: a PDF packet of everything recorded for a closed incident.
# Small stdlib PDF writer (the standard Helvetica/Courier fonts every PDF reader
# has built in), so it works offline with no extra packages.
# --------------------------------------------------------------------------
# Character widths (1/1000 em) for ASCII 32..126, from the Adobe font metrics
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    278, 278, 584, 584, 584, 556, 1015,
    667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667,
    944, 667, 667, 611, 278, 278, 278, 469, 556, 333,
    556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500,
    722, 500, 500, 500, 334, 260, 334, 584]
_HELVB = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    333, 333, 584, 584, 584, 611, 975,
    722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667,
    944, 667, 667, 611, 333, 278, 333, 584, 556, 333,
    556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556,
    778, 556, 556, 500, 389, 280, 389, 584]

INK, MUTED_INK, RULE = (0.09, 0.11, 0.16), (0.36, 0.40, 0.47), (0.82, 0.85, 0.89)
NAVY_RGB, ORANGE_RGB, ZEBRA = (0.043, 0.067, 0.125), (0.976, 0.451, 0.086), (0.955, 0.965, 0.98)


class PdfDoc:
    W, H, M = 612, 792, 54  # US Letter, 3/4 inch margins
    FONTS = {"R": ("F1", "Helvetica", _HELV), "B": ("F2", "Helvetica-Bold", _HELVB),
             "I": ("F3", "Helvetica-Oblique", _HELV), "C": ("F4", "Courier", None)}

    def __init__(self, title, footer):
        self.title, self.footer = title, footer
        self.pages, self.ops, self.y = [], None, 0
        self.new_page()

    @property
    def inner(self):
        return self.W - 2 * self.M

    @staticmethod
    def enc(s):
        b = str(s).replace("\t", "    ").encode("cp1252", "replace")
        return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").decode("latin-1")

    def width(self, s, f="R", size=10):
        table = self.FONTS[f][2]
        if table is None:
            return 0.6 * size * len(s)
        return size / 1000 * sum(table[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in s)

    def wrap(self, text, f="R", size=10, maxw=None):
        maxw = maxw or self.inner
        out = []
        for para in str(text if text is not None else "").replace("\r\n", "\n").split("\n"):
            line = ""
            for word in para.split(" "):
                cand = word if not line else line + " " + word
                if self.width(cand, f, size) <= maxw:
                    line = cand
                    continue
                if line:
                    out.append(line)
                while self.width(word, f, size) > maxw:  # a single word longer than the line
                    cut = len(word)
                    while cut > 1 and self.width(word[:cut], f, size) > maxw:
                        cut -= 1
                    out.append(word[:cut])
                    word = word[cut:]
                line = word
            out.append(line)
        return out or [""]

    # ---- drawing primitives
    def new_page(self):
        self.ops = []
        self.pages.append(self.ops)
        self.y = self.H - self.M - 12

    def ensure(self, h):
        if self.y - h < self.M + 26:
            self.new_page()

    def text(self, x, y, s, f="R", size=10, color=INK):
        self.ops.append(f"BT /{self.FONTS[f][0]} {size} Tf {color[0]} {color[1]} {color[2]} rg "
                        f"{x:.2f} {y:.2f} Td ({self.enc(s)}) Tj ET")

    def rect(self, x, y, w, h, color):
        self.ops.append(f"{color[0]} {color[1]} {color[2]} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def hline(self, y, color=RULE, x0=None, x1=None, width=0.6):
        x0 = self.M if x0 is None else x0
        x1 = self.W - self.M if x1 is None else x1
        self.ops.append(f"{color[0]} {color[1]} {color[2]} RG {width} w {x0:.2f} {y:.2f} m {x1:.2f} {y:.2f} l S")

    # ---- layout helpers
    def space(self, h):
        self.y -= h

    def para(self, s, f="R", size=10, indent=0, color=INK, gap=4):
        lead = size * 1.32
        for ln in self.wrap(s, f, size, self.inner - indent):
            self.ensure(lead)
            self.y -= lead
            self.text(self.M + indent, self.y, ln, f, size, color)
        self.y -= gap

    def heading(self, s, sub=None):
        self.ensure(70)
        self.y -= 22
        self.rect(self.M, self.y - 3, 4, 17, ORANGE_RGB)
        self.text(self.M + 12, self.y, s, "B", 15)
        if sub:
            self.text(self.W - self.M - self.width(sub, "R", 9), self.y, sub, "R", 9, MUTED_INK)
        self.y -= 9
        self.hline(self.y)
        self.y -= 6

    def subheading(self, s):
        self.ensure(40)
        self.y -= 16
        self.text(self.M, self.y, s, "B", 11.5)
        self.y -= 5

    def kv(self, rows, label_w=150, size=9.5):
        lead = size * 1.32
        for label, value in rows:
            lines = self.wrap(value if value not in (None, "") else "-", "R", size, self.inner - label_w)
            labels = self.wrap(label, "B", size - 0.5, label_w - 10)
            self.ensure(lead * min(max(len(lines), len(labels)), 3) + 3)
            for k in range(max(len(lines), len(labels))):
                self.ensure(lead)
                self.y -= lead
                if k < len(labels):
                    self.text(self.M, self.y, labels[k], "B", size - 0.5, MUTED_INK)
                if k < len(lines):
                    self.text(self.M + label_w, self.y, lines[k], "R", size)
            self.y -= 3

    def table(self, cols, rows, size=8.5):
        """cols: [(title, fraction of width)]; rows: lists of cell strings. Header repeats on each page."""
        widths = [frac * self.inner for _, frac in cols]
        lead, pad = size * 1.28, 4
        max_lines = int((self.H - 2 * self.M - 90) / lead)  # tallest row that fits on one page

        def header():
            self.ensure(lead + 2 * pad + 14)
            h = lead + 2 * pad
            self.rect(self.M, self.y - h, self.inner, h, NAVY_RGB)
            x = self.M
            for (title, _), w in zip(cols, widths):
                self.text(x + pad, self.y - pad - size, title, "B", size, (1, 1, 1))
                x += w
            self.y -= h

        if not rows:
            self.para("None recorded.", "I", 9.5, color=MUTED_INK)
            return
        header()
        for i, row in enumerate(rows):
            cells = [self.wrap(c if c not in (None, "") else "-", "R", size, w - 2 * pad) for c, w in zip(row, widths)]
            while cells:  # a very long cell continues in a follow-on row on the next page
                part = [c[:max_lines] for c in cells]
                rest = [c[max_lines:] for c in cells]
                n = max(len(c) for c in part)
                h = n * lead + 2 * pad
                if self.y - h < self.M + 26:
                    self.new_page()
                    header()
                if i % 2:
                    self.rect(self.M, self.y - h, self.inner, h, ZEBRA)
                x = self.M
                for c, w in zip(part, widths):
                    for k, ln in enumerate(c):
                        self.text(x + pad, self.y - pad - size - k * lead + 1, ln, "R", size)
                    x += w
                self.y -= h
                self.hline(self.y, width=0.4)
                cells = rest if any(rest) else None
        self.y -= 6

    def lines_to_fill(self, label, n=4):
        self.subheading(label)
        for _ in range(n):
            self.ensure(24)
            self.y -= 22
            self.hline(self.y, color=(0.7, 0.73, 0.78))
        self.y -= 4

    # ---- output
    def render(self, producer):
        n = len(self.pages)
        objs = []

        def add(body):
            objs.append(body if isinstance(body, bytes) else body.encode("latin-1"))
            return len(objs)

        add("<< /Type /Catalog /Pages 2 0 R >>")
        add("")  # page tree, filled in below once the page ids are known
        font_ids = {}
        for fid, base, _ in self.FONTS.values():
            enc = "" if base == "Courier" else " /Encoding /WinAnsiEncoding"
            font_ids[fid] = add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base}{enc} >>")
        fonts = " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items())
        stamp = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info = add(f"<< /Title ({self.enc(self.title)}) /Producer ({self.enc(producer)}) /Creator (TACISS) "
                   f"/CreationDate ({stamp}) >>")
        mi = MUTED_INK
        kids = []
        for i, ops in enumerate(self.pages, 1):
            fy = self.M - 22
            right = f"Page {i} of {n}"
            extra = [f"{RULE[0]} {RULE[1]} {RULE[2]} RG 0.6 w {self.M} {fy + 12} m {self.W - self.M} {fy + 12} l S"]
            for x, s in ((self.M, self.footer), (self.W - self.M - self.width(right, "R", 8), right)):
                extra.append(f"BT /F1 8 Tf {mi[0]} {mi[1]} {mi[2]} rg {x:.2f} {fy} Td ({self.enc(s)}) Tj ET")
            if i > 1:  # running title on every page after the cover
                extra.append(f"BT /F2 8 Tf {mi[0]} {mi[1]} {mi[2]} rg {self.M} {self.H - self.M + 14} Td "
                             f"({self.enc(self.title)}) Tj ET")
            data = zlib.compress("\n".join(ops + extra).encode("latin-1"))
            cid = add(f"<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream")
            kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W} {self.H}] "
                            f"/Resources << /Font << {fonts} >> >> /Contents {cid} 0 R >>"))
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {n} >>".encode()
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)


LOGO_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MAX_LOGO = 1_500_000


def logo_type(data):
    """Image type from the file's first bytes (never trust the name or the browser's label)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _ts(v):
    """Stored UTC timestamp -> '2026-11-09 14:03Z'."""
    if not v:
        return "-"
    s = str(v)
    return s[:16].replace("T", " ") + "Z" if len(s) >= 16 and s[10:11] == "T" else s


def _count(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _nice(v):
    return str(v).replace("_", " ").capitalize() if v not in (None, "") else "-"


def _duration(a, b):
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        sec = int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds())
    except (TypeError, ValueError):
        return "-"
    d, rem = divmod(max(sec, 0), 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d} d " if d else "") + f"{h} h {rem // 60} min"


def build_debrief(con, inc, user, settings):
    """Everything recorded for one incident as a PDF debrief packet. Returns (pdf bytes, file name)."""
    iid = inc["id"]

    def q(sql, *a):
        return [dict(r) for r in con.execute(sql, a)]

    progs = {r["id"]: r for r in q("SELECT * FROM programs")}
    agencies = {r["id"]: r for r in q("SELECT * FROM agencies")}
    people = {r["username"].lower(): r for r in q("SELECT username, display_name, callsign, role FROM users")}
    log = q("SELECT * FROM activity WHERE incident_id = ? ORDER BY created_at, id", iid)
    reports = q("SELECT * FROM reports WHERE incident_id = ? ORDER BY created_at, id", iid)
    checkins = q("SELECT * FROM checkins WHERE incident_id = ? ORDER BY created_at, id", iid)
    resources = q("SELECT * FROM resources WHERE incident_id = ? ORDER BY name", iid)
    chat = q("SELECT * FROM messages WHERE channel = ? ORDER BY created_at, id", f"incident-{iid}")
    related = {"incidents": [iid], "activity": [r["id"] for r in log], "reports": [r["id"] for r in reports],
               "checkins": [r["id"] for r in checkins], "resources": [r["id"] for r in resources],
               "messages": [r["id"] for r in chat]}
    history = []
    for ent, ids in related.items():
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            history += q(f"SELECT * FROM audit WHERE entity = ? AND entity_id IN ({','.join('?' * len(chunk))})",
                         ent, *chunk)
    history.sort(key=lambda r: (r["ts"], r["id"]))
    status_changes = [h for h in history if h["entity"] == "incidents" and h["action"] == "update"
                      and "status" in (h["detail"] or "")]
    closed_at = status_changes[-1]["ts"] if status_changes else inc.get("updated_at")

    def ref_names(ids, table):
        return ", ".join(table[i]["name"] for i in parse_refs(ids) if i in table) or "-"

    def who(username):
        if not username:
            return "-"
        p = people.get(str(username).lower())
        if not p:
            return str(username)
        name = p.get("display_name") or p["username"]
        return " ".join(b for b in (p.get("callsign"), name) if b) + ("" if name == p["username"] else f" ({p['username']})")

    site = settings.get("site_name") or "TACISS"
    generated = now_iso()
    doc = PdfDoc(f"Debrief Kit - Incident #{iid}: {inc.get('title') or ''}",
                 f"{site} incident #{iid} debrief kit  |  generated {_ts(generated)} by {user['username']}  |  times UTC")

    # ---- cover
    band = 190
    doc.rect(0, doc.H - band, doc.W, band, NAVY_RGB)
    doc.rect(0, doc.H - band - 6, doc.W, 6, ORANGE_RGB)
    doc.text(doc.M, doc.H - 70, site.upper(), "B", 13, ORANGE_RGB)
    doc.text(doc.M, doc.H - 88, settings.get("org_name") or "", "R", 9.5, (0.75, 0.79, 0.86))
    doc.text(doc.M, doc.H - 132, "INCIDENT DEBRIEF KIT", "B", 28, (1, 1, 1))
    doc.text(doc.M, doc.H - 156, f"Incident #{iid}  |  Closed {_ts(closed_at)}", "R", 11, (0.85, 0.88, 0.93))
    doc.y = doc.H - band - 34
    doc.para(inc.get("title") or "(untitled incident)", "B", 20, gap=10)
    if (inc.get("visibility") or "open") == "restricted":
        doc.ensure(40)
        doc.rect(doc.M, doc.y - 30, doc.inner, 30, (0.99, 0.91, 0.91))
        doc.text(doc.M + 10, doc.y - 19, "RESTRICTED INCIDENT - share this packet only with its assigned programs "
                                         "and served agencies.", "B", 9.5, (0.6, 0.1, 0.1))
        doc.space(40)
    doc.kv([("Type", _nice(inc.get("kind"))), ("Priority", _nice(inc.get("priority"))),
            ("Status", _nice(inc.get("status"))), ("Access", _nice(inc.get("visibility") or "open")),
            ("Opened", f"{_ts(inc.get('created_at'))} by {who(inc.get('created_by'))}"),
            ("Closed", _ts(closed_at)), ("Duration", _duration(inc.get("created_at"), closed_at)),
            ("Location", inc.get("location")),
            ("Assigned programs", ref_names(inc.get("programs"), progs)),
            ("Served agencies", ref_names(inc.get("agencies"), agencies)),
            ("Incident commander / NCS", inc.get("commander")), ("Radio net / frequency", inc.get("radio_net"))])
    doc.subheading("In this packet")
    n_events = len(log) + len(reports) + len(checkins) + len(chat) + 1 + \
        sum(1 for c in checkins if c.get("checked_out_at")) + sum(1 for h in history if h["action"] != "create")
    for name, count in (("1. Incident details", ""), ("2. People involved", ""), ("3. Timeline", _count(n_events, "event")),
                        ("4. Activity log", _count(len(log), "entry", "entries")), ("5. Reports", _count(len(reports), "report")),
                        ("6. Net check-ins", _count(len(checkins), "check-in")), ("7. Resources", f"{len(resources)} assigned"),
                        ("8. Incident chat", _count(len(chat), "message")), ("9. Change history", _count(len(history), "change")),
                        ("10. Debrief worksheet", "to fill in at the debrief")):
        doc.ensure(14)
        doc.space(13.5)
        doc.text(doc.M + 8, doc.y, name, "R", 10)
        if count:
            doc.text(doc.M + 230, doc.y, count, "R", 10, MUTED_INK)
    doc.space(14)
    doc.para(f"This packet contains everything recorded in {site} for this incident as of {_ts(generated)}: its "
             "details, log, reports, check-ins, resources, chat and change history. It was generated by "
             f"{who(user['username'])}. All times are UTC unless a field says otherwise.", "I", 8.5, color=MUTED_INK)

    # ---- 1. details
    doc.new_page()
    doc.heading("1. Incident details", f"Incident #{iid}")
    rows = []
    for f in ENTITIES["incidents"]["fields"]:
        if f["name"] == "description":
            continue
        v = inc.get(f["name"])
        if f["type"] == "refs":
            v = ref_names(v, progs if f["ref"] == "programs" else agencies)
        elif f["choices"]:
            v = _nice(v)
        rows.append((f["label"], "" if v is None else str(v)))
    rows += [("Created", f"{_ts(inc.get('created_at'))} by {who(inc.get('created_by'))}"),
             ("Last updated", f"{_ts(inc.get('updated_at'))} by {who(inc.get('updated_by'))}")]
    doc.kv(rows)
    doc.subheading("Description")
    doc.para(inc.get("description") or "No description recorded.", "R", 10,
             color=INK if inc.get("description") else MUTED_INK)
    prog_items = [progs[i] for i in parse_refs(inc.get("programs")) if i in progs]
    if prog_items:
        doc.subheading("Assigned programs")
        doc.table([("Program", .28), ("Type", .1), ("Coordinator", .24), ("Counties", .18), ("Frequencies", .2)],
                  [[p["name"], p.get("kind"), " ".join(x for x in (p.get("coordinator"), p.get("coordinator_call")) if x),
                    p.get("counties"), p.get("frequencies")] for p in prog_items])
    agency_items = [agencies[i] for i in parse_refs(inc.get("agencies")) if i in agencies]
    if agency_items:
        doc.subheading("Served agencies")
        doc.table([("Agency", .32), ("Type", .22), ("County", .14), ("Phone", .14), ("Email", .18)],
                  [[a["name"], a.get("kind"), a.get("county"), a.get("phone"), a.get("email")] for a in agency_items])

    # ---- 2. people
    doc.heading("2. People involved")
    tally = {}
    for src, recs in (("incident", [inc]), ("log", log), ("reports", reports), ("check-ins", checkins),
                      ("resources", resources), ("chat", chat)):
        for r in recs:
            for k in ("created_by", "updated_by"):
                if r.get(k):
                    t = tally.setdefault(r[k].lower(), {"name": r[k], "what": {}})
                    t["what"][src] = t["what"].get(src, 0) + (k == "created_by")
    doc.subheading("TACISS users who entered or changed information")
    doc.table([("User", .38), ("Role", .12), ("Contributed", .5)],
              [[who(t["name"]), (people.get(k) or {}).get("role") or "-",
                ", ".join(f"{src} ({n})" if n else f"{src} (edits)" for src, n in t["what"].items())]
               for k, t in sorted(tally.items())])
    doc.subheading("Stations on the net")
    doc.table([("Callsign", .16), ("Name", .24), ("Assignment", .32), ("Final status", .14), ("Traffic", .14)],
              [[c.get("callsign"), c.get("name"), c.get("assignment"), _nice(c.get("status")), _nice(c.get("traffic"))]
               for c in checkins])

    # ---- 3. timeline
    doc.heading("3. Timeline", "everything, oldest first")
    events = [(inc.get("created_at"), "Incident", who(inc.get("created_by")), f"Incident opened: {inc.get('title')}")]
    events += [(r["created_at"], "Log", who(r.get("created_by")), r.get("body")) for r in log]
    events += [(r["created_at"], "Report", who(r.get("created_by")),
                f"{_nice(r.get('kind'))} ({_nice(r.get('severity'))}): {r.get('title')}") for r in reports]
    events += [(r["created_at"], "Check-in", who(r.get("created_by")),
                f"{r.get('callsign')} {r.get('name') or ''} checked in".replace("  ", " ")
                + (f" - {r.get('assignment')}" if r.get("assignment") else "")) for r in checkins]
    events += [(r["checked_out_at"], "Check-out", "-", f"{r.get('callsign')} checked out")
               for r in checkins if r.get("checked_out_at")]
    events += [(r["created_at"], "Chat", who(r.get("created_by")), r.get("body")) for r in chat]
    events += [(h["ts"], "Change", who(h.get("username")),
                f"{h['action']} {'log entry' if h['entity'] == 'activity' else h['entity'].rstrip('s')} #{h['entity_id']}"
                + (f": {h['detail']}" if h.get("detail") else "")) for h in history if h["action"] != "create"]
    events.sort(key=lambda e: str(e[0] or ""))
    doc.table([("Time (UTC)", .17), ("Source", .1), ("By", .2), ("What happened", .53)],
              [[_ts(t), s, b, w] for t, s, b, w in events])

    # ---- 4. activity log
    doc.heading("4. Activity log", _count(len(log), "entry", "entries"))
    doc.table([("Time (UTC)", .17), ("By", .2), ("Entry", .63)],
              [[_ts(r["created_at"]), who(r.get("created_by")), r.get("body")] for r in log])

    # ---- 5. reports
    doc.heading("5. Reports", _count(len(reports), "report"))
    if not reports:
        doc.para("None recorded.", "I", 9.5, color=MUTED_INK)
    for r in reports:
        doc.subheading(f"Report #{r['id']}: {r.get('title') or ''}")
        rrows = []
        for f in ENTITIES["reports"]["fields"]:
            v = r.get(f["name"])
            if f["name"] in ("title", "incident_id", "body") or v in (None, ""):
                continue
            rrows.append((f["label"], _nice(v) if f["choices"] else str(v)))
        rrows.append(("Submitted", f"{_ts(r.get('created_at'))} by {who(r.get('created_by'))}"))
        if r.get("updated_at") != r.get("created_at"):
            rrows.append(("Last updated", f"{_ts(r.get('updated_at'))} by {who(r.get('updated_by'))}"))
        doc.kv(rrows, label_w=170, size=9)
        if r.get("body"):
            doc.para(r["body"], "R", 9.5, indent=12)
        doc.space(4)

    # ---- 6. check-ins
    doc.heading("6. Net check-ins", _count(len(checkins), "check-in"))
    doc.table([("Callsign", .11), ("Name", .14), ("Assignment", .17), ("Status", .1), ("Traffic", .09),
               ("In (UTC)", .13), ("Out", .11), ("Notes", .15)],
              [[c.get("callsign"), c.get("name"), c.get("assignment"), _nice(c.get("status")), _nice(c.get("traffic")),
                _ts(c.get("created_at")), _ts(c.get("checked_out_at")) if c.get("checked_out_at") else "-",
                c.get("notes")] for c in checkins])

    # ---- 7. resources
    doc.heading("7. Resources", "still assigned to this incident")
    doc.table([("Resource", .22), ("Type", .12), ("Status", .12), ("Location", .18), ("Contact", .16), ("Notes", .2)],
              [[r.get("name"), _nice(r.get("kind")), _nice(r.get("status")), r.get("location"), r.get("contact"),
                r.get("notes")] for r in resources])

    # ---- 8. chat
    doc.heading("8. Incident chat", f"#incident-{iid}, " + _count(len(chat), "message"))
    doc.table([("Time (UTC)", .17), ("From", .2), ("Message", .63)],
              [[_ts(m["created_at"]), who(m.get("created_by")), m.get("body")] for m in chat])

    # ---- 9. change history
    doc.heading("9. Change history", "from the audit log")
    doc.table([("Time (UTC)", .17), ("User", .18), ("Action", .1), ("Record", .17), ("Fields / detail", .38)],
              [[_ts(h["ts"]), who(h.get("username")), h["action"], f"{h['entity']} #{h['entity_id']}", h.get("detail")]
               for h in history])

    # ---- 10. worksheet
    doc.new_page()
    doc.heading("10. Debrief worksheet", "fill in at the after-action meeting")
    for label in ("Debrief date", "Facilitator", "Attendees"):
        doc.space(22)
        doc.text(doc.M, doc.y, label, "B", 9.5, MUTED_INK)
        doc.hline(doc.y - 2, color=(0.7, 0.73, 0.78), x0=doc.M + 110)
    doc.space(6)
    for label, n in (("What went well", 4), ("What could be improved", 4), ("Communications and equipment issues", 3),
                     ("Action items (what / who / by when)", 5)):
        doc.lines_to_fill(label, n)
    doc.ensure(60)
    doc.space(44)
    for x, w, label in ((doc.M, doc.inner * 0.5, "Debrief lead signature"),
                        (doc.M + doc.inner * 0.62, doc.inner * 0.38, "Date")):
        doc.hline(doc.y, color=INK, x0=x, x1=x + w, width=0.8)
        doc.text(x, doc.y - 11, label, "R", 8.5, MUTED_INK)

    slug = re.sub(r"[^a-z0-9]+", "-", (inc.get("title") or "incident").lower()).strip("-")[:40] or "incident"
    return doc.render(f"TACISS {VERSION}"), f"debrief-incident-{iid}-{slug}.pdf"


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TACISS/" + VERSION
    sys_version = ""

    # ---- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (now_iso(), self.client_ip(), fmt % args))

    def client_ip(self):
        if CONFIG["trust_proxy"]:
            fwd = self.headers.get("X-Forwarded-For") if hasattr(self, "headers") and self.headers else None
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def is_https(self):
        if CONFIG["secure_cookies"]:
            return True
        return CONFIG["trust_proxy"] and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        embed = embed_origin()  # the one outside page the MeshCore tab may show inside a frame
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob: https: http:; connect-src 'self'; "
                         + (f"frame-src 'self' {embed}; " if embed else "") +
                         "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if self.is_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Request too large.")
        if length == 0:
            return {}
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            raise ApiError(415, "Content-Type must be application/json.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON.")

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        self.con = None
        try:
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                peer_call = u.path in ("/api/mesh/hello", "/api/mesh/changes") and self.headers.get("X-Mesh-Node")
                if method not in ("GET", "HEAD") and self.headers.get("X-Requested-With") != "TACISS"                         and not peer_call:  # peer calls carry an HMAC signature instead of a cookie
                    raise ApiError(403, "Missing request header.")  # CSRF guard
                self.con = connect()
                self.route_api(method, u.path, parse_qs(u.query))
            elif method in ("GET", "HEAD"):
                self.serve_static(u.path)
            else:
                raise ApiError(405, "Method not allowed.")
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "Internal server error."}, 500)
            except Exception:
                pass
        finally:
            if self.con:
                self.con.close()

    # ---- static -----------------------------------------------------------
    def public_base(self):
        """https://taciss.com style address of this server, for link previews (TACISS_PUBLIC_URL wins)."""
        if CONFIG["public_url"]:
            return CONFIG["public_url"]
        host = (self.headers.get("X-Forwarded-Host") if CONFIG["trust_proxy"] else None) or self.headers.get("Host") or ""
        host = host.split(",")[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?|\[[0-9a-fA-F:]+\](:\d{1,5})?", host):
            host = f"{CONFIG['host']}:{CONFIG['port']}"
        return f"{'https' if self.is_https() else 'http'}://{host}"

    def share_tags(self):
        """Open Graph / Twitter tags so Facebook, Messenger, Discord, Slack, etc. show a preview card."""
        import html as _html
        s = FeedService.settings()
        name = s.get("site_name") or "TACISS"
        title = f"{name} - {s.get('org_name')}" if s.get("org_name") else name
        desc = ("Incident and information sharing for ARES, RACES, Skywarn, CERT and AUXCOMM groups: incidents, "
                "net check-ins, reports, NWS weather, APRS and MeshCore on one board.")
        base = self.public_base()
        e = lambda v: _html.escape(str(v), quote=True)
        image = f"{base}/static/share.png?v=1"
        tags = [("name", "description", desc), ("property", "og:type", "website"), ("property", "og:site_name", name),
                ("property", "og:title", title), ("property", "og:description", desc), ("property", "og:url", base + "/"),
                ("property", "og:image", image), ("property", "og:image:secure_url", image) if image.startswith("https") else None,
                ("property", "og:image:type", "image/png"), ("property", "og:image:width", "1200"),
                ("property", "og:image:height", "630"), ("property", "og:image:alt", f"{name} logo"),
                ("name", "twitter:card", "summary_large_image"), ("name", "twitter:title", title),
                ("name", "twitter:description", desc), ("name", "twitter:image", image)]
        return "".join(f'<meta {k}="{e(n)}" content="{e(v)}">\n' for k, n, v in filter(None, tags))

    def serve_static(self, path):
        if path in ("/", "") or not path.startswith("/static/"):
            # SPA: any non-API, non-static path serves the app shell
            rel = "index.html"
        else:
            rel = path[len("/static/"):]
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Not found.")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        if rel == "index.html":
            data = data.replace(b"</head>", self.share_tags().encode() + b"</head>", 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if rel == "index.html" else "public, max-age=300")
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---- auth helpers -----------------------------------------------------
    def current_user(self):
        if hasattr(self, "_user"):
            return self._user
        self._user = None
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)taciss_session=([A-Za-z0-9_\-]+)", cookie)
        if m:
            row = self.con.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                (token_hash(m.group(1)), time.time())).fetchone()
            if row:
                self._user = dict(row)
        return self._user

    def require(self, role):
        u = self.current_user()
        if not u:
            raise ApiError(401, "Sign in required.")
        if ROLE_RANK.get(u["role"], 0) < ROLE_RANK[role]:
            raise ApiError(403, f"Requires {role} role.")
        return u

    def can_read(self, entity_public):
        if self.current_user():
            return True
        return CONFIG["public_read"] and entity_public

    # ---- restricted incidents ---------------------------------------------
    # An incident with visibility=restricted (and everything hanging off it: log, check-ins,
    # reports, resources, its chat channel) is only visible to admins, its creator, members
    # of one of its assigned programs, and users of one of its served agencies.
    def access(self):
        if not hasattr(self, "_access"):
            self._access = access_for(self.con, self.current_user())
        return self._access

    def hidden_incidents(self):
        if not hasattr(self, "_hidden"):
            acc = self.access()
            self._hidden = set() if acc["admin"] else {
                r["id"] for r in self.con.execute(
                    "SELECT id, visibility, created_by, programs, agencies FROM incidents WHERE visibility = 'restricted'")
                if not incident_allowed(dict(r), acc)}
        return self._hidden

    def record_hidden(self, entity, row):
        hid = self.hidden_incidents()
        if not hid:
            return False
        return incident_id_of(entity, row) in hid

    def scope_of(self, entity, row):
        return scope_of(self.con, entity, row)

    # ---- who may change a record ------------------------------------------
    # Server admins: anything. Otherwise only:
    #   * the person who submitted it (records created on this node, not synced from another), or
    #   * a program admin of a program the record belongs to (the program itself, incidents assigned
    #     to it, and the log/reports/check-ins/resources/chat of those incidents), or
    #   * for net check-ins, the person who opened the incident (net control runs the net).
    # Log entries and chat are append-only: server admins only.
    def incident_row(self, iid):
        cache = self.__dict__.setdefault("_inc_rows", {})
        if iid not in cache:
            r = self.con.execute("SELECT * FROM incidents WHERE id = ?", (iid,)).fetchone()
            cache[iid] = dict(r) if r else None
        return cache[iid]

    def record_programs(self, entity, row):
        """(program ids the record belongs to, its incident row or None)."""
        if entity == "programs":
            return {row["id"]}, None
        if entity == "incidents":
            return set(parse_refs(row["programs"])), dict(row)
        iid = incident_id_of(entity, row)
        inc = self.incident_row(iid) if iid else None
        return (set(parse_refs(inc["programs"])) if inc else set()), inc

    def owns(self, row, u):
        row = dict(row)
        local = not row.get("mesh_origin") or row.get("mesh_origin") == NODE["id"]
        return bool(local and row.get("created_by") and u and row["created_by"].lower() == u["username"].lower())

    def can_modify(self, entity, row):
        u = self.current_user()
        if not u:
            return False
        if u["role"] == "admin":
            return True
        if ENTITIES[entity].get("append_only"):
            return False
        if self.owns(row, u):
            return True
        pa = self.access()["program_admin"]
        if entity == "programs":
            return row["id"] in pa
        progs, inc = self.record_programs(entity, row)
        # A program admin manages records of their program that its own members opened or submitted.
        # (Putting your program on someone else's incident only tags it; it doesn't hand you their records.)
        for pid in progs & pa:
            if self.member_of(inc and inc.get("created_by"), pid) or self.member_of(dict(row).get("created_by"), pid):
                return True
        return entity == "checkins" and inc is not None and self.owns(inc, u)

    def member_of(self, username, pid):
        if not username:
            return False
        cache = self.__dict__.setdefault("_member_of", {})
        key = (username.lower(), pid)
        if key not in cache:
            cache[key] = bool(self.con.execute(
                "SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE u.username = ? AND m.kind = 'programs' AND m.ref_id = ?", (username, pid)).fetchone())
        return cache[key]

    def can_delete(self, entity, row):
        """Same as editing, except whole programs and agencies are deleted by server admins only."""
        if entity in ("programs", "agencies"):
            u = self.current_user()
            return bool(u and u["role"] == "admin")
        return self.can_modify(entity, row)

    def row_out(self, entity, row):
        return dict(out_row(entity, row), can_edit=self.can_modify(entity, row), can_delete=self.can_delete(entity, row))

    def require_modify(self, entity, row):
        self.require("viewer")
        if not self.can_modify(entity, row):
            what = ENTITIES[entity]["singular"].lower()
            raise ApiError(403, f"Only the person who submitted this {what}, a program admin for its program, "
                                "or a server admin can change it.")

    def publish(self, entity, action, rid, row, prev=None):
        BUS.publish(change_event(self.con, entity, action, rid, row, prev))
        MESH.kick()

    def check_refs(self, entity, rec):
        hidden = self.hidden_incidents()
        for f in ENTITIES[entity]["fields"]:
            v = rec.get(f["name"])
            if v is None:
                continue
            if f["type"] == "ref":
                if not self.con.execute(f"SELECT 1 FROM {f['ref']} WHERE id = ?", (v,)).fetchone() or \
                        f["ref"] == "incidents" and v in hidden:
                    raise ApiError(400, f"{f['label']} does not exist.")
            elif f["type"] == "refs":
                ids = parse_refs(v)
                if ids:
                    found = {r[0] for r in self.con.execute(
                        f"SELECT id FROM {f['ref']} WHERE id IN ({','.join('?' * len(ids))})", ids)}
                    missing = [i for i in ids if i not in found]
                    if missing:
                        raise ApiError(400, f"{f['label']}: #{missing[0]} does not exist.")
        if entity == "messages" and incident_channel(rec.get("channel")) in hidden:
            raise ApiError(404, "Channel not found.")

    def audit(self, action, entity=None, entity_id=None, detail=None):
        u = self.current_user()
        self.con.execute(
            "INSERT INTO audit(ts, username, ip, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), u["username"] if u else None, self.client_ip(), action, entity, entity_id,
             (detail or "")[:500]))

    def cookie(self, value, max_age):
        parts = [f"taciss_session={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.is_https():
            parts.append("Secure")
        return "; ".join(parts)

    # ---- router -----------------------------------------------------------
    def route_api(self, method, path, qs):
        parts = [p for p in path.split("/") if p][1:]  # strip "api"
        if not parts:
            raise ApiError(404, "Not found.")
        head = parts[0]
        if method == "HEAD":
            method = "GET"

        if head == "health":
            return self.send_json({"ok": True, "version": VERSION, "time": now_iso()})
        if head == "login" and method == "POST":
            return self.api_login()
        if head == "setup" and method == "POST":
            return self.api_setup()
        if head == "logout" and method == "POST":
            return self.api_logout()
        if head == "me":
            if len(parts) == 2 and parts[1] == "password" and method == "POST":
                return self.api_change_password()
            if len(parts) == 2 and parts[1] == "prefs" and method == "PUT":
                return self.api_prefs()
            needs_setup = self.con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            u = self.current_user()
            user = self.user_out(u)
            if user:
                try:
                    user["prefs"] = public_prefs(json.loads(u.get("prefs") or "{}"))
                except ValueError:
                    user["prefs"] = {}
                # the user's own program(s): shown in the top bar and dashboard so the board
                # reads as "their" program's TACISS
                ids = user.get("programs") or []
                user["program_info"] = [dict(r) for r in self.con.execute(
                    "SELECT id, name, kind, status, counties, region, coordinator, coordinator_call, frequencies, logo "
                    f"FROM programs WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", ids)] if ids else []
            return self.send_json({"user": user, "needs_setup": needs_setup,
                                   "public_read": CONFIG["public_read"], "version": VERSION,
                                   "update": UPDATES.notice() if u and u["role"] == "admin" else None})
        if head == "weather":
            return self.api_weather(method, parts[1:], qs)
        if head == "aprs":
            return self.api_aprs(method, parts[1:], qs)
        if head == "meshcore":
            return self.api_meshcore(method, parts[1:], qs)
        if head == "feeds":
            return self.api_feeds(method, parts[1:])
        if head == "reports" and len(parts) == 3 and parts[2] == "nws":
            return self.api_report_nws(method, parts[1])
        if head == "schema":
            return self.send_json({"entities": ENTITIES, "seoc_levels": SEOC_LEVELS,
                                   "roles": list(ROLE_RANK)})
        if head == "settings":
            return self.api_settings(method)
        if head == "stream":
            return self.api_stream()
        if head == "users":
            return self.api_users(method, parts[1:])
        if head == "members":
            return self.api_members(method, parts[1:])
        if head == "mesh":
            return self.api_mesh(method, parts[1:], qs)
        if head == "audit":
            self.require("admin")
            limit = min(int((qs.get("limit") or ["300"])[0]), 2000)
            rows = self.con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return self.send_json([dict(r) for r in rows])
        if head == "export":
            return self.api_export()
        if head == "stats":
            return self.api_stats()
        if head in ENTITIES:
            return self.api_entity(method, head, parts[1:], qs)
        raise ApiError(404, "Not found.")

    # ---- auth endpoints ---------------------------------------------------
    @staticmethod
    def public_user(u):
        if not u:
            return None
        return {k: u[k] for k in ("id", "username", "display_name", "callsign", "role", "active",
                                  "created_at", "last_login")}

    def user_out(self, u):
        """public_user plus the programs / served agencies the user belongs to."""
        out = self.public_user(u)
        if out:
            acc = access_for(self.con, u)
            for k in MEMBER_KINDS:
                out[k] = sorted(acc[k])
            out["program_admin"] = sorted(acc["program_admin"])
        return out

    def set_memberships(self, uid, kind, ids, admin_ids=None):
        if not isinstance(ids, list) or len(ids) > 500:
            raise ApiError(400, f"{kind} must be a list of ids.")
        try:
            ids = sorted({int(x) for x in ids})
        except (TypeError, ValueError):
            raise ApiError(400, f"{kind} must be a list of ids.")
        if ids:
            found = {r[0] for r in self.con.execute(
                f"SELECT id FROM {kind} WHERE id IN ({','.join('?' * len(ids))})", ids)}
            if len(found) != len(ids):
                raise ApiError(400, f"Unknown {kind[:-1].replace('ie', 'y')} id.")
        if kind == "programs" and admin_ids is not None:
            admins = set(self.ids_list(admin_ids, "program_admin")) & set(ids)
        else:  # keep existing program-admin roles for programs the user stays in
            admins = {r[0] for r in self.con.execute(
                "SELECT ref_id FROM memberships WHERE user_id = ? AND kind = ? AND role = 'admin'", (uid, kind))}
        self.con.execute("DELETE FROM memberships WHERE user_id = ? AND kind = ?", (uid, kind))
        self.con.executemany("INSERT INTO memberships(user_id, kind, ref_id, role) VALUES (?,?,?,?)",
                             [(uid, kind, i, "admin" if i in admins else "member") for i in ids])

    @staticmethod
    def ids_list(v, label):
        if not isinstance(v, list) or len(v) > 2000:
            raise ApiError(400, f"{label} must be a list of ids.")
        try:
            return sorted({int(x) for x in v})
        except (TypeError, ValueError):
            raise ApiError(400, f"{label} must be a list of ids.")

    def api_members(self, method, rest):
        """/api/members/<programs|agencies>/<id>: who belongs to a program or served agency.
        Server admins manage any roster; a program admin manages their own program's roster
        (members and program admins). /api/members/people lists users to pick from."""
        if rest == ["people"] and method == "GET":
            u = self.require("viewer")
            if u["role"] != "admin" and not self.access()["program_admin"]:
                raise ApiError(403, "Only server admins and program admins can list users.")
            rows = self.con.execute("SELECT id, username, display_name, callsign, role FROM users "
                                    "WHERE active = 1 ORDER BY username").fetchall()
            return self.send_json([dict(r) for r in rows])
        if len(rest) != 2 or rest[0] not in MEMBER_KINDS:
            raise ApiError(404, "Not found.")
        kind = rest[0]
        try:
            rid = int(rest[1])
        except ValueError:
            raise ApiError(404, "Not found.")
        if not self.con.execute(f"SELECT 1 FROM {kind} WHERE id = ?", (rid,)).fetchone():
            raise ApiError(404, "Not found.")
        u = self.require("viewer")
        can_manage = u["role"] == "admin" or (kind == "programs" and rid in self.access()["program_admin"])
        if method == "GET":
            rows = self.con.execute(
                "SELECT u.id, u.username, u.display_name, u.callsign, u.role, u.active, m.role AS member_role "
                "FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.kind = ? AND m.ref_id = ? "
                "ORDER BY m.role = 'admin' DESC, u.username", (kind, rid)).fetchall()
            out = [dict(r) for r in rows]
            if self.headers.get("X-Members-Detail"):
                return self.send_json({"members": out, "can_manage": can_manage})
            return self.send_json(out)
        if method == "PUT":
            if not can_manage:
                raise ApiError(403, "Only a server admin or this program's program admins can change its members.")
            body = self.read_json()
            ids = self.ids_list(body.get("user_ids"), "user_ids")
            admins = set(self.ids_list(body.get("admin_ids") or [], "admin_ids")) & set(ids) if kind == "programs" else set()
            if ids:
                found = {r[0] for r in self.con.execute(
                    f"SELECT id FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)}
                if len(found) != len(ids):
                    raise ApiError(400, "Unknown user id.")
            if u["role"] != "admin" and u["id"] not in admins:
                raise ApiError(400, "You can't remove your own program-admin role. Ask another program admin "
                                    "or a server admin.")
            self.con.execute("DELETE FROM memberships WHERE kind = ? AND ref_id = ?", (kind, rid))
            self.con.executemany("INSERT INTO memberships(user_id, kind, ref_id, role) VALUES (?,?,?,?)",
                                 [(i, kind, rid, "admin" if i in admins else "member") for i in ids])
            self.audit("members", kind, rid, f"{len(ids)} user(s), {len(admins)} program admin(s)")
            self.con.commit()
            BUS.publish({"entity": "members", "action": "update", "public": False})
            return self.api_members("GET", rest)
        raise ApiError(405, "Method not allowed.")

    def api_login(self):
        ip = self.client_ip()
        if LIMITER.blocked(ip):
            raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
        body = self.read_json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        row = self.con.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        ok = verify_password(password, row["password_hash"] if row else DUMMY_HASH) and row is not None
        if not ok:
            LIMITER.fail(ip)
            self.audit("login_failed", detail=username)
            self.con.commit()
            raise ApiError(401, "Invalid username or password.")
        LIMITER.clear(ip)
        self.start_session(row, "login")

    def start_session(self, row, action):
        token = secrets.token_urlsafe(32)
        max_age = CONFIG["session_hours"] * 3600
        self.con.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, ip) VALUES (?,?,?,?,?)",
                         (token_hash(token), row["id"], now_iso(), time.time() + max_age, self.client_ip()))
        self.con.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), row["id"]))
        self._user = dict(row)
        self.audit(action)
        self.con.commit()
        self.send_json({"user": self.public_user(dict(row))}, extra_headers=[("Set-Cookie", self.cookie(token, max_age))])

    def api_setup(self):
        """First-run: create the first admin, proven by the one-time code shown on the server console."""
        ip = self.client_ip()
        if LIMITER.blocked(ip):
            raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
        body = self.read_json()
        username = str(body.get("username", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", username):
            raise ApiError(400, "Username must be 2-40 letters, numbers, dot, dash or underscore.")
        check_password_policy(body.get("password"))
        self.con.execute("BEGIN IMMEDIATE")  # serialize concurrent setup attempts
        if self.con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            self.con.rollback()
            raise ApiError(409, "Setup is already complete. Please sign in.")
        try:
            with open(setup_code_path()) as fh:
                code = fh.read().strip()
        except FileNotFoundError:
            self.con.rollback()
            raise ApiError(400, "No setup code on the server. Run: sudo taciss setup-code")
        given = str(body.get("code", "")).strip().upper()
        if not hmac.compare_digest(given.encode(), code.encode()):
            self.con.rollback()
            LIMITER.fail(ip)
            raise ApiError(403, "Wrong setup code. It is shown on the server's console screen.")
        cur = self.con.execute(
            "INSERT INTO users(username, display_name, callsign, role, password_hash, created_at) VALUES (?,?,?,?,?,?)",
            (username, str(body.get("display_name") or username)[:100], str(body.get("callsign") or "").upper()[:20],
             "admin", hash_password(body["password"]), now_iso()))
        row = self.con.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        try:
            os.remove(setup_code_path())
        except OSError:
            pass
        self.start_session(row, "setup_first_admin")

    def api_logout(self):
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)taciss_session=([A-Za-z0-9_\-]+)", cookie)
        if m:
            self.con.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(m.group(1)),))
            self.con.commit()
        self.send_json({"ok": True}, extra_headers=[("Set-Cookie", self.cookie("", 0))])

    def api_change_password(self):
        u = self.require("viewer")
        body = self.read_json()
        if not verify_password(str(body.get("current", "")), u["password_hash"]):
            raise ApiError(400, "Current password is incorrect.")
        check_password_policy(body.get("new"))
        self.con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body["new"]), u["id"]))
        self.audit("password_changed", "users", u["id"])
        self.con.commit()
        self.send_json({"ok": True})

    def api_prefs(self):
        u = self.require("viewer")
        body = self.read_json()
        try:
            prefs = json.loads(u.get("prefs") or "{}")
        except ValueError:
            prefs = {}
        if "wx_counties" in body:
            c = body["wx_counties"]
            if not isinstance(c, list) or len(c) > 300 or not all(isinstance(x, str) and re.fullmatch(r"\d{6}", x) for x in c):
                raise ApiError(400, "wx_counties must be a list of 6-digit county codes.")
            prefs["wx_counties"] = sorted(set(c))
        if "wx_sound" in body:
            prefs["wx_sound"] = bool(body["wx_sound"])
        if "wx_station" in body:
            v = str(body["wx_station"] or "").strip().upper()
            if v and not re.fullmatch(r"[A-Z0-9]{3,12}", v):
                raise ApiError(400, "Unknown weather station.")
            prefs["wx_station"] = v
        refilter = False
        if "aprs_ssid" in body:
            v = str(body["aprs_ssid"] or "").strip().lstrip("-")
            if v and not (v.isdigit() and 1 <= int(v) <= 15):
                raise ApiError(400, "SSID must be blank or 1-15 (e.g. 9 for a mobile).")
            prefs["aprs_ssid"] = v
        if "aprs_passcode" in body:
            v = str(body["aprs_passcode"] or "").strip()
            if v:
                call = (u.get("callsign") or "").upper()
                if not CALL_RE.match(call):
                    raise ApiError(400, "Your account has no amateur callsign yet. Ask an admin to add it "
                                        "(Admin -> Users) before setting an APRS passcode.")
                if not v.isdigit() or int(v) != aprs_passcode(call):
                    raise ApiError(400, f"That isn't the APRS-IS passcode for {call}. Use the passcode you use "
                                        "in your other APRS apps (APRSdroid, Xastir, etc.).")
            prefs["aprs_passcode"] = v
        if "aprs_symbol" in body:
            v = str(body["aprs_symbol"] or "")
            if v and v not in APRS_SYMBOLS:
                raise ApiError(400, "Unknown symbol.")
            prefs["aprs_symbol"] = v
        if "aprs_comment" in body:
            prefs["aprs_comment"] = aprs_clean(body["aprs_comment"], 43)
        if "aprs_watch" in body:
            w = body["aprs_watch"]
            if not isinstance(w, list) or len(w) > 30:
                raise ApiError(400, "Watch list must be up to 30 callsigns.")
            w = sorted({str(c).strip().upper() for c in w if str(c).strip()})
            bad = [c for c in w if not APRS_ADDR_RE.match(c.rstrip("*"))]
            if bad:
                raise ApiError(400, f"Not a callsign: {bad[0]}")
            prefs["aprs_watch"] = w
            refilter = True
        self.con.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps(prefs), u["id"]))
        self.con.commit()
        if refilter:
            APRS.refilter()
        self.send_json(public_prefs(prefs))

    # ---- APRS -------------------------------------------------------------
    def aprs_me(self, u):
        """This user's APRS identity and whether they can send (with the reason if not)."""
        try:
            prefs = json.loads(u.get("prefs") or "{}")
        except ValueError:
            prefs = {}
        call = (u.get("callsign") or "").upper().strip()
        ssid = prefs.get("aprs_ssid") or ""
        s = FeedService.settings()
        reason = None
        if s.get("aprs_tx_enabled") != "1":
            reason = "An admin has turned off sending to APRS."
        elif not CALL_RE.match(call):
            reason = "Your account needs your amateur callsign. An admin adds it in Admin -> Users."
        elif not prefs.get("aprs_passcode"):
            reason = "Enter your APRS-IS passcode under My APRS setup."
        elif int(prefs["aprs_passcode"]) != aprs_passcode(call):
            reason = "Your saved passcode doesn't match your callsign any more. Enter it again."
        me = {"callsign": call, "ssid": ssid, "from": call + (f"-{ssid}" if ssid else ""),
              "symbol": prefs.get("aprs_symbol") or "/-", "comment": prefs.get("aprs_comment") or "",
              "watch": prefs.get("aprs_watch") or [], "passcode_set": bool(prefs.get("aprs_passcode")),
              "can_send": reason is None, "reason": reason}
        return me, prefs

    def api_aprs(self, method, rest, qs):
        u = self.require("viewer")
        sub = rest[0] if rest else ""
        arg = lambda k, d="": (qs.get(k) or [d])[0]
        if method == "GET" and sub == "status":
            s = FeedService.settings()
            with APRS.lock:
                st = dict(APRS.status, stations=len(APRS.stations))
            me, _ = self.aprs_me(u)
            return self.send_json(dict(st, enabled=s.get("aprs_enabled") == "1", range_km=s.get("aprs_range_km"),
                                       center=[float(s.get("map_lat") or 0), float(s.get("map_lon") or 0)],
                                       aprsfi=bool(APRS.settings_secret("aprsfi_api_key")),
                                       symbols=APRS_SYMBOLS, me=me))
        if method == "GET" and sub == "stations":
            try:
                hours = float(arg("hours", "2"))
            except ValueError:
                hours = 2
            return self.send_json(APRS.snapshot(hours))
        if method == "GET" and sub == "packets":
            with APRS.lock:
                return self.send_json(list(APRS.packets)[-250:][::-1])
        if method == "GET" and sub == "messages":
            me, _ = self.aprs_me(u)
            base = me["callsign"]
            with APRS.lock:
                msgs = list(APRS.messages)
            acks = {(m["src"].split("-")[0], m["msgno"]) for m in msgs if m.get("ack") == "ack" and m.get("msgno")}
            out = []
            for m in msgs:
                if m.get("ack"):
                    continue
                d = dict(m, acked=(m["to"].split("-")[0], m.get("msgno")) in acks if m.get("msgno") else None)
                d["mine"] = bool(base) and base in (m["src"].split("-")[0], m["to"].split("-")[0])
                out.append(d)
            return self.send_json({"mine": [m for m in out if m["mine"]][::-1][:200],
                                   "heard": [m for m in out if not m["to"].startswith(("BLN", "NWS"))][::-1][:150],
                                   "bulletins": [m for m in out if m["to"].startswith(("BLN", "NWS"))][::-1][:50]})
        if method == "GET" and sub == "lookup":
            names = [n.strip().upper() for n in arg("name").split(",") if n.strip()][:20]
            if not names or not all(re.fullmatch(r"[A-Z0-9\-_.]{1,12}", n) for n in names):
                raise ApiError(400, "Enter up to 20 callsigns or object names, separated by commas.")
            what = arg("what", "loc")
            if what not in ("loc", "wx"):
                raise ApiError(400, "what must be loc or wx.")
            return self.send_json(APRS.aprsfi({"name": ",".join(names), "what": what}).get("entries", []))
        if method == "GET" and sub == "inbox":
            me, _ = self.aprs_me(u)
            if not CALL_RE.match(me["callsign"]):
                raise ApiError(400, "Your account has no amateur callsign yet.")
            calls = sorted({me["callsign"], me["from"]})
            return self.send_json(APRS.aprsfi({"what": "msg", "dst": ",".join(calls)}, ttl=30).get("entries", []))
        if method == "POST" and sub == "send":
            return self.api_aprs_send(u)
        raise ApiError(404, "Not found.")

    def api_aprs_send(self, u):
        me, prefs = self.aprs_me(u)
        if not me["can_send"]:
            raise ApiError(400, me["reason"])
        now = time.time()
        recent = [t for t in APRS.last_tx.get(u["username"], []) if now - t < 3600]
        if recent and now - recent[-1] < 10:
            raise ApiError(429, "Please wait a few seconds between APRS packets.")
        if len(recent) >= 60:
            raise ApiError(429, "That's 60 APRS packets in the last hour. Please wait before sending more.")
        body = self.read_json()
        kind = body.get("type")
        src = me["from"]
        sym = body.get("symbol") or me["symbol"]
        if sym not in APRS_SYMBOLS:
            raise ApiError(400, "Unknown symbol.")
        comment = aprs_clean(body.get("comment"), 43)
        if kind in ("position", "object"):
            try:
                lat, lon = float(body.get("lat")), float(body.get("lon"))
                assert -90 <= lat <= 90 and -180 <= lon <= 180
            except (TypeError, ValueError, AssertionError):
                raise ApiError(400, "Pick a location: enter latitude and longitude, or click the map.")
            la, lo = aprs_coord(lat, lon)
        if kind == "position":
            payload = f"={la}{sym[0]}{lo}{sym[1]}{comment}"
            label = src
        elif kind == "object":
            name = aprs_clean(body.get("name"), 9).strip()
            if not name:
                raise ApiError(400, "Give the object a name (up to 9 characters).")
            stamp = datetime.now(timezone.utc).strftime("%d%H%Mz")
            payload = f";{name:<9}{'_' if body.get('kill') else '*'}{stamp}{la}{sym[0]}{lo}{sym[1]}{comment}"
            label = name
        elif kind == "message":
            to = str(body.get("to") or "").strip().upper()
            if not (APRS_ADDR_RE.match(to) or re.fullmatch(r"BLN[0-9A-Z]{1,6}", to)):
                raise ApiError(400, "Send to a callsign, e.g. KD4XYZ or KD4XYZ-9.")
            text = aprs_clean(body.get("text"), 67).strip()
            if not text:
                raise ApiError(400, "Type a message (up to 67 characters).")
            msgno = "" if to.startswith("BLN") else "{" + str(int(now) % 100000)
            payload = f":{to:<9}:{text}{msgno}"
            label = to
        else:
            raise ApiError(400, "type must be position, object or message.")
        packet = f"{src}>{APRS_TOCALL},TCPIP*:{payload}"
        try:
            reply = APRS.send(src, prefs["aprs_passcode"], packet)
        except OSError as e:
            raise ApiError(502, f"Couldn't reach the APRS-IS server: {e}")
        APRS.last_tx[u["username"]] = recent + [now]
        self.audit("aprs_send", detail=packet)
        self.con.commit()
        return self.send_json({"ok": True, "packet": packet, "server": reply,
                               "aprsfi": "https://aprs.fi/info/a/" + quote(label.strip())})

    # ---- MeshCore ---------------------------------------------------------
    def api_meshcore(self, method, rest, qs):
        self.require("viewer")
        if method != "GET":
            raise ApiError(405, "Method not allowed.")
        s = FeedService.settings()
        sub = rest[0] if rest else "status"
        if s.get("meshcore_enabled") != "1":
            raise ApiError(400, "MeshCore is turned off in Admin -> Settings.")
        if sub == "status":
            links = []
            for line in (s.get("meshcore_links") or "").splitlines():
                name, _, url = line.partition("|")
                if url.strip().startswith(("http://", "https://")):
                    links.append({"name": name.strip()[:60], "url": url.strip()})
            return self.send_json({"bbox": _bbox(s, "meshcore_bbox"), "days": int(s.get("meshcore_days") or 14),
                                   "analyzer": s.get("meshcore_analyzer_url") or "", "regions": MESHCORE.regions(s),
                                   "channels": s.get("meshcore_channels") or "",
                                   "embed": s.get("meshcore_embed_url") or "", "links": links})
        if sub == "nodes":
            res = MESHCORE.map_nodes(s)
            try:
                days = max(1, min(int((qs.get("days") or [s.get("meshcore_days") or "14"])[0]), 3650))
            except ValueError:
                days = 14
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = [n for n in (res["data"] or []) if (n.get("last") or "") >= cutoff]
            return self.send_json(dict(res, data=data, total_in_area=len(res["data"] or [])))
        handlers = {"packets": MESHCORE.packets, "observers": MESHCORE.observers, "live-nodes": MESHCORE.nodes,
                    "channels": MESHCORE.channels}
        if sub in handlers:
            return self.send_json(handlers[sub](s))
        if sub == "messages":
            ch = (qs.get("channel") or [""])[0]
            if not ch or len(ch) > 80:
                raise ApiError(400, "Pick a channel.")
            return self.send_json(MESHCORE.messages(s, ch))
        raise ApiError(404, "Not found.")

    # ---- weather ----------------------------------------------------------
    def api_weather(self, method, rest, qs):
        if not self.can_read(True):
            raise ApiError(401, "Sign in required.")
        sub = rest[0] if rest else ""
        if sub == "alerts" and method == "GET":
            return self.send_json(WX.snapshot())
        if sub == "counties" and method == "GET":
            return self.send_json(WX.counties())
        if sub == "forecast" and method == "GET":
            s = WX.settings()
            try:
                lat = float((qs.get("lat") or [s["map_lat"]])[0])
                lon = float((qs.get("lon") or [s["map_lon"]])[0])
            except ValueError:
                raise ApiError(400, "lat/lon must be numbers.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ApiError(400, "lat/lon out of range.")
            return self.send_json(WX.forecast(lat, lon))
        if sub == "stations" and method == "GET":
            out = WX.stations()
            for a in APRS.snapshot(3):  # APRS / CWOP home weather stations heard in the last 3 hours
                if a.get("wx") and a.get("lat") is not None:
                    out.append({"id": a["call"], "name": (a.get("comment") or "")[:60], "lat": a["lat"], "lon": a["lon"],
                                "source": "aprs", "wx": a["wx"], "last": a["last"]})
            return self.send_json(out)
        if sub == "station" and method == "GET":
            sid = (qs.get("id") or [""])[0].upper()
            if not re.fullmatch(r"[A-Z0-9]{3,12}", sid):
                raise ApiError(400, "Unknown station.")
            return self.send_json(WX.observation(sid))
        if sub == "now" and method == "GET":
            # current conditions for the signed-in user: their chosen station, else the one nearest the map center
            u = self.current_user()
            try:
                prefs = json.loads((u or {}).get("prefs") or "{}")
            except ValueError:
                prefs = {}
            sid = prefs.get("wx_station")
            if not sid:
                s = WX.settings()
                sid = WX.nearest_station(float(s["map_lat"]), float(s["map_lon"]))
            return self.send_json(dict(WX.observation(sid), chosen=bool(prefs.get("wx_station"))))
        if sub == "product" and method == "GET":
            ptype = (qs.get("type") or ["HWO"])[0].upper()
            office = (qs.get("office") or [""])[0].upper()
            if ptype not in WX_PRODUCTS or not re.fullmatch(r"[A-Z]{3}", office):
                raise ApiError(400, "Unknown product or office.")
            return self.send_json(WX.product(ptype, office))
        if sub == "test":
            u = self.require("operator")
            if method == "DELETE":
                WX.clear_tests()
                self.audit("weather_exercise_clear")
                self.con.commit()
                return self.send_json({"ok": True})
            if method == "POST":
                body = self.read_json()
                event = str(body.get("event") or "Tornado Warning").strip()[:80]
                same = body.get("same") or []
                if not isinstance(same, list) or not same or not all(isinstance(x, str) and re.fullmatch(r"\d{6}", x) for x in same):
                    raise ApiError(400, "Pick at least one county.")
                minutes = max(1, min(int(body.get("minutes") or 15), 240))
                area = str(body.get("area") or "selected counties")[:300]
                alert = WX.add_test(event, same[:50], area, minutes, u["username"])
                self.audit("weather_exercise", detail=f"{event} for {area} ({minutes} min)")
                self.con.commit()
                return self.send_json(alert, 201)
        raise ApiError(404, "Not found.")

    # ---- war-room feeds -----------------------------------------------------
    def api_feeds(self, method, rest):
        if method != "GET":
            raise ApiError(405, "Method not allowed.")
        if not self.can_read(True):
            raise ApiError(401, "Sign in required.")
        sub = rest[0] if rest else ""
        if self.get_settings().get("feeds_enabled", "1") != "1" and sub != "cam":
            raise ApiError(503, "Live feeds are turned off in Admin settings.")
        if sub == "storm":
            return self.send_json(FEEDS.storm_reports())
        if sub == "power":
            return self.send_json(FEEDS.power())
        if sub == "internet":
            return self.send_json(FEEDS.internet())
        if sub == "cameras":
            return self.send_json(FEEDS.cameras(self.con))
        if sub == "cam" and len(rest) == 2 and re.fullmatch(r"[a-z]{2}-[\w.\-]{1,80}", rest[1]):
            ctype, body = FEEDS.camera_image(rest[1])
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=30")
            self.security_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        raise ApiError(404, "Not found.")

    def api_report_nws(self, method, rid):
        """Record that a SITREP storm/damage report was passed to the NWS (mPING, e-mail, phone, social)."""
        if method != "POST":
            raise ApiError(405, "Method not allowed.")
        u = self.require("viewer")
        try:
            row = self.con.execute("SELECT * FROM reports WHERE id = ?", (int(rid),)).fetchone()
        except ValueError:
            row = None
        if not row or self.record_hidden("reports", row):
            raise ApiError(404, "Report not found.")
        if not self.can_modify("reports", row):
            raise ApiError(403, "Only the reporter, a program admin for its program, or a server admin can mark this as sent.")
        via = str(self.read_json().get("via") or "").strip()[:60] or "other"
        note = f"{via} by {u['callsign'] or u['username']} at {now_iso()}"
        self.con.execute("UPDATE reports SET nws_sent = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                         (note, now_iso(), u["username"], row["id"]))
        mesh_stamp(self.con, "reports", row["id"])
        self.audit("report_to_nws", "reports", row["id"], note)
        self.con.commit()
        new = self.con.execute("SELECT * FROM reports WHERE id = ?", (row["id"],)).fetchone()
        self.publish("reports", "update", row["id"], new, row)
        return self.send_json(out_row("reports", new))

    # ---- mesh ---------------------------------------------------------------
    def read_raw(self, limit=MESH_MAX_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError(413, "Request too large.")
        return self.rfile.read(length) if length else b""

    def mesh_reply(self, secret, obj, status=200):
        """Signed (and gzip'd when it helps) reply to a peer."""
        body = json.dumps(obj, separators=(",", ":"), default=str).encode()
        gz = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, 6)
        ts = str(int(time.time()))
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Mesh-Time", ts)
        self.send_header("X-Mesh-Sig", mesh_sign(secret, "RESP", self.path, ts, body))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def mesh_sig_ok(self, secret, method, raw):
        return hmac.compare_digest(self.headers.get("X-Mesh-Sig") or "",
                                   mesh_sign(secret, method, self.path, self.headers.get("X-Mesh-Time") or "", raw))

    def mesh_peer_api(self, method, sub, qs):
        """Calls from other nodes (HMAC-signed, no login)."""
        if mesh_setting(self.con, "mesh_enabled") != "1":
            raise ApiError(503, "Mesh sync is turned off on this node.")
        nid = self.headers.get("X-Mesh-Node") or ""
        if not MESH_NODE_RE.fullmatch(nid):
            raise ApiError(403, "Bad node id.")
        raw = self.read_raw()
        peer = self.con.execute("SELECT * FROM mesh_peers WHERE node_id = ?", (nid,)).fetchone()

        if sub == "hello" and method == "POST":
            secret, invite = None, None
            if peer and self.mesh_sig_ok(peer["secret"], method, raw):
                secret = peer["secret"]
            else:
                for inv in self.con.execute("SELECT * FROM mesh_invites WHERE expires_at > ?", (time.time(),)):
                    if self.mesh_sig_ok(inv["secret"], method, raw):
                        secret, invite = inv["secret"], inv
                        break
            if not secret:
                raise ApiError(403, "Pairing code not recognized (wrong or expired).")
            try:
                body = json.loads(mesh_unzip(raw, self.headers.get("Content-Encoding")) or b"{}")
            except (ValueError, zlib.error):
                raise ApiError(400, "Invalid JSON.")
            if nid == NODE["id"]:
                raise ApiError(400, "That pairing code belongs to this same node.")
            name = str(body.get("name") or nid)[:60]
            url = str(body.get("url") or "").strip()
            link = invite["link"] if invite else peer["link"]
            try:
                url = mesh_check_url(url, link) if url else ""
            except ApiError:
                url = ""  # their address doesn't fit the link type; they can still pull/push to us
            if peer:
                self.con.execute("UPDATE mesh_peers SET name = ?, url = COALESCE(NULLIF(?, ''), url), secret = ?, "
                                 "last_seen = ? WHERE id = ?", (name, url, secret, now_iso(), peer["id"]))
            else:
                self.con.execute(
                    "INSERT INTO mesh_peers(node_id, name, url, secret, link, share_restricted, last_seen, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (nid, name, url, secret, link, invite["share_restricted"], now_iso(), now_iso()))
            if invite:
                self.con.execute("DELETE FROM mesh_invites WHERE id = ?", (invite["id"],))
                self.audit("mesh_pair", detail=f"paired with {name} ({nid}) via invite")
            self.con.execute("INSERT INTO mesh_nodes(node_id, name, seen_at) VALUES (?,?,?) ON CONFLICT(node_id) "
                             "DO UPDATE SET name = excluded.name, seen_at = excluded.seen_at", (nid, name, now_iso()))
            self.con.commit()
            BUS.publish({"entity": "mesh", "action": "pair", "public": False, "admin": True})
            MESH.kick()
            return self.mesh_reply(secret, {"node_id": NODE["id"], "name": NODE["name"], "version": VERSION,
                                            "url": mesh_setting(self.con, "mesh_url")})

        if not peer or not peer["enabled"] or not self.mesh_sig_ok(peer["secret"], method, raw):
            raise ApiError(403, "Unknown node or bad signature.")
        self.con.execute("UPDATE mesh_peers SET last_seen = ? WHERE id = ?", (now_iso(), peer["id"]))
        self.con.commit()
        if sub == "changes" and method == "GET":
            try:
                since = max(0, int((qs.get("since") or ["0"])[0]))
            except ValueError:
                since = 0
            items, last, more, top = mesh_outgoing(self.con, peer, since)
            return self.mesh_reply(peer["secret"], {"items": items, "next": last, "more": more, "max_seq": top,
                                                    "nodes": mesh_nodes_map(self.con), "time": time.time()})
        if sub == "changes" and method == "POST":
            try:
                body = json.loads(mesh_unzip(raw, self.headers.get("Content-Encoding")) or b"{}")
            except (ValueError, zlib.error):
                raise ApiError(400, "Invalid JSON.")
            stats = mesh_apply_batch(self.con, body.get("items") or [], body.get("nodes"))
            self.con.execute("UPDATE mesh_peers SET received = received + ? WHERE id = ?",
                             (stats["applied"], peer["id"]))
            self.con.commit()
            return self.mesh_reply(peer["secret"], {"ok": True, **stats})
        raise ApiError(404, "Not found.")

    @staticmethod
    def peer_out(p):
        d = {k: p[k] for k in p.keys() if k != "secret"}
        d["enabled"], d["share_restricted"] = bool(p["enabled"]), bool(p["share_restricted"])
        return d

    def api_mesh(self, method, rest, qs):
        sub = rest[0] if rest else ""
        if sub in ("hello", "changes") and self.headers.get("X-Mesh-Node"):
            return self.mesh_peer_api(method, sub, qs)
        if sub == "whoami" and method == "GET":  # discovery probe: public name card, no data
            if mesh_setting(self.con, "mesh_enabled") != "1" or mesh_setting(self.con, "mesh_discoverable") != "1":
                raise ApiError(404, "Not found.")
            return self.send_json(mesh_whoami(self.con))
        if sub == "nodes" and method == "GET":  # names for "from Knox EOC" badges
            self.require("viewer")
            return self.send_json({"self": NODE["id"], "nodes": mesh_nodes_map(self.con)})
        u = self.require("admin")
        con = self.con
        if not sub and method == "GET":
            peers = [self.peer_out(p) for p in con.execute("SELECT * FROM mesh_peers ORDER BY name")]
            invites = [{"id": r["id"], "note": r["note"], "link": r["link"],
                        "share_restricted": bool(r["share_restricted"]),
                        "created_by": r["created_by"], "expires_at": r["expires_at"]}
                       for r in con.execute("SELECT * FROM mesh_invites WHERE expires_at > ? ORDER BY id",
                                            (time.time(),))]
            log = con.execute("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM mesh_log").fetchone()
            return self.send_json({
                "node": {"id": NODE["id"], "name": NODE["name"], "url": mesh_setting(con, "mesh_url"),
                         "enabled": mesh_setting(con, "mesh_enabled") == "1",
                         "interval": mesh_setting(con, "mesh_interval"),
                         "discoverable": mesh_setting(con, "mesh_discoverable") == "1"},
                "discovered": DISCOVERY.list(), "discovery": DISCOVERY.status,
                "links": MESH_LINKS, "peers": peers, "invites": invites,
                "log": {"records": log[0], "seq": log[1]},
                "pending": con.execute("SELECT COUNT(*) FROM mesh_pending").fetchone()[0]})

        if sub == "scan" and method == "POST":  # look for nearby nodes now
            DISCOVERY.scan()
            time.sleep(1.5)  # give LAN replies a moment
            return self.send_json({"discovered": DISCOVERY.list(), "discovery": DISCOVERY.status})

        if sub == "invites":
            if method == "POST" and len(rest) == 1:
                body = self.read_json()
                link = str(body.get("link") or "lan")
                if link not in MESH_LINKS:
                    raise ApiError(400, "Unknown link type.")
                try:
                    hours = max(1, min(int(body.get("hours") or 24), 168))
                except (TypeError, ValueError):
                    hours = 24
                code = mesh_new_code()
                cur = con.execute(
                    "INSERT INTO mesh_invites(secret, note, link, share_restricted, created_by, created_at, expires_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (mesh_norm_code(code), str(body.get("note") or "")[:120], link,
                     1 if body.get("share_restricted") else 0, u["username"], now_iso(), time.time() + hours * 3600))
                self.audit("mesh_invite", detail=f"invite #{cur.lastrowid} ({link}, {hours}h)")
                con.commit()
                return self.send_json({"id": cur.lastrowid, "code": code, "hours": hours,
                                       "node": {"id": NODE["id"], "name": NODE["name"],
                                                "url": mesh_setting(con, "mesh_url")}}, 201)
            if method == "DELETE" and len(rest) == 2:
                con.execute("DELETE FROM mesh_invites WHERE id = ?", (rest[1],))
                self.audit("mesh_invite_cancel", detail=f"invite #{rest[1]}")
                con.commit()
                return self.send_json({"ok": True})

        if sub == "peers":
            if method == "POST" and len(rest) == 1:  # join another node using its pairing code
                body = self.read_json()
                link = str(body.get("link") or "lan")
                url = mesh_check_url(str(body.get("url") or ""), link)
                if not url:
                    raise ApiError(400, "Enter the other node's address.")
                secret = mesh_norm_code(body.get("code"))
                if len(secret) < 16:
                    raise ApiError(400, "Enter the pairing code shown on the other node.")
                try:
                    info, _ = mesh_request(url, secret, "POST", "/api/mesh/hello",
                                           {"node_id": NODE["id"], "name": NODE["name"],
                                            "url": mesh_setting(con, "mesh_url"), "version": VERSION}, timeout=15)
                except RuntimeError as e:
                    raise ApiError(502, f"Pairing failed: {e}")
                nid = str(info.get("node_id") or "")
                if not MESH_NODE_RE.fullmatch(nid) or nid == NODE["id"]:
                    raise ApiError(502, "Pairing failed: the other side isn't a different TACISS node.")
                name = str(info.get("name") or nid)[:60]
                share = 1 if body.get("share_restricted") else 0
                con.execute(
                    "INSERT INTO mesh_peers(node_id, name, url, secret, link, share_restricted, last_seen, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET name = excluded.name, "
                    "url = excluded.url, secret = excluded.secret, link = excluded.link, "
                    "share_restricted = excluded.share_restricted, enabled = 1",
                    (nid, name, url, secret, link, share, now_iso(), now_iso()))
                con.execute("INSERT INTO mesh_nodes(node_id, name, seen_at) VALUES (?,?,?) ON CONFLICT(node_id) "
                            "DO UPDATE SET name = excluded.name", (nid, name, now_iso()))
                self.audit("mesh_pair", detail=f"joined {name} ({nid}) at {url} [{link}]")
                con.commit()
                pid = con.execute("SELECT id FROM mesh_peers WHERE node_id = ?", (nid,)).fetchone()[0]
                result = MESH.sync_peer(pid)  # first sync right away
                p = con.execute("SELECT * FROM mesh_peers WHERE id = ?", (pid,)).fetchone()
                return self.send_json({"peer": self.peer_out(p), "sync": result}, 201)
            if len(rest) >= 2:
                p = con.execute("SELECT * FROM mesh_peers WHERE id = ?", (rest[1],)).fetchone()
                if not p:
                    raise ApiError(404, "Peer not found.")
                if len(rest) == 3 and rest[2] == "sync" and method == "POST":
                    result = MESH.sync_peer(p["id"])
                    p = con.execute("SELECT * FROM mesh_peers WHERE id = ?", (p["id"],)).fetchone()
                    return self.send_json({"peer": self.peer_out(p), "sync": result})
                if len(rest) == 2 and method == "PUT":
                    body = self.read_json()
                    link = str(body.get("link", p["link"]))
                    url = mesh_check_url(str(body.get("url", p["url"]) or ""), link)
                    share = bool(body.get("share_restricted", bool(p["share_restricted"])))
                    con.execute("UPDATE mesh_peers SET url = ?, link = ?, share_restricted = ?, enabled = ?, "
                                "name = ? WHERE id = ?",
                                (url, link, 1 if share else 0, 1 if body.get("enabled", bool(p["enabled"])) else 0,
                                 str(body.get("name") or p["name"])[:60], p["id"]))
                    if share != bool(p["share_restricted"]):  # resend everything under the new rules
                        con.execute("UPDATE mesh_peers SET push_cursor = 0 WHERE id = ?", (p["id"],))
                    self.audit("mesh_peer", detail=f"updated {p['name']}")
                    con.commit()
                    MESH.kick()
                    return self.send_json(self.peer_out(con.execute("SELECT * FROM mesh_peers WHERE id = ?",
                                                                    (p["id"],)).fetchone()))
                if len(rest) == 2 and method == "DELETE":
                    con.execute("DELETE FROM mesh_peers WHERE id = ?", (p["id"],))
                    self.audit("mesh_unpair", detail=f"removed {p['name']} ({p['node_id']})")
                    con.commit()
                    return self.send_json({"ok": True})
        raise ApiError(404, "Not found.")

    # ---- settings ---------------------------------------------------------
    def get_settings(self):
        s = {r["key"]: r["value"] for r in self.con.execute("SELECT key, value FROM settings")}
        for k in SECRET_SETTINGS:  # API keys stay on the server
            s[k] = SECRET_MASK if s.get(k) else ""
        return s

    def api_settings(self, method):
        if method == "GET":
            return self.send_json(self.get_settings())
        if method != "PUT":
            raise ApiError(405, "Method not allowed.")
        u = self.require("operator")
        body = self.read_json()
        changed = []
        for k, v in body.items():
            if k not in SETTINGS_DEFAULTS:
                raise ApiError(400, f"Unknown setting: {k}")
            if k not in SETTINGS_OPERATOR_KEYS and u["role"] != "admin":
                raise ApiError(403, f"Only admins can change {k}.")
            v = "" if v is None else str(v).strip()[:4000]
            if k in SECRET_SETTINGS and v == SECRET_MASK:
                continue  # unchanged
            if k == "feeds_bbox":
                try:
                    s_, w_, n_, e_ = (float(x) for x in v.split(","))
                    assert -90 <= s_ < n_ <= 90 and -180 <= w_ < e_ <= 180
                except (ValueError, AssertionError):
                    raise ApiError(400, "feeds_bbox must be south,west,north,east, e.g. 34.9,-90.4,36.7,-81.6")
            if k == "lsr_hours" and not (v.isdigit() and 1 <= int(v) <= 168):
                raise ApiError(400, "lsr_hours must be 1-168.")
            if k == "mesh_url" and v:
                u_ = urlparse(v)
                if u_.scheme not in ("http", "https") or not u_.hostname:
                    raise ApiError(400, "mesh_url must look like http://10.1.2.3:8080")
                v = v.rstrip("/")
            if k == "mesh_interval" and not (v.isdigit() and 5 <= int(v) <= 3600):
                raise ApiError(400, "mesh_interval must be 5-3600 seconds.")
            if k == "mesh_node_name":
                v = v[:60]
            if k == "aprs_range_km" and not (v.isdigit() and 5 <= int(v) <= 2000):
                raise ApiError(400, "aprs_range_km must be 5-2000.")
            if k == "aprs_server" and not re.fullmatch(r"[A-Za-z0-9.-]+:\d{2,5}", v):
                raise ApiError(400, "aprs_server must look like rotate.aprs2.net:14580")
            if k == "meshcore_bbox":
                try:
                    s_, w_, n_, e_ = (float(x) for x in v.split(","))
                    assert -90 <= s_ < n_ <= 90 and -180 <= w_ < e_ <= 180
                except (ValueError, AssertionError):
                    raise ApiError(400, "meshcore_bbox must be south,west,north,east, e.g. 34.9,-90.4,36.7,-81.6")
            if k == "meshcore_days" and not (v.isdigit() and 1 <= int(v) <= 365):
                raise ApiError(400, "meshcore_days must be 1-365.")
            if k in ("meshcore_map_url", "meshcore_analyzer_url", "meshcore_embed_url") and v:
                u_ = urlparse(v)
                if u_.scheme not in ("http", "https") or not u_.hostname:
                    raise ApiError(400, f"{k} must be a web address like https://analyzer.example.org")
                if k == "meshcore_embed_url" and u_.scheme != "https":
                    raise ApiError(400, "The embedded MeshCore view must be an https:// address.")
                if k == "meshcore_analyzer_url":
                    v = v.rstrip("/")
            if k == "meshcore_channels":
                names = [c.strip() for c in v.split(",") if c.strip()]
                if len(names) > 50 or any(len(c) > 40 or not re.fullmatch(r"#?[\w .:+/-]+", c) for c in names):
                    raise ApiError(400, "meshcore_channels must be channel names separated by commas, e.g. #weather,#wx,Public")
                v = ",".join(names)
            if k == "meshcore_regions":
                v = ",".join(c for c in re.split(r"[\s,]+", v.upper()) if c)
                if v and not re.fullmatch(r"[A-Z0-9-]{2,10}(,[A-Z0-9-]{2,10})*", v):
                    raise ApiError(400, "meshcore_regions must be area codes like BNA,TYS,CHA.")
            if k in ("feeds_enabled", "mesh_enabled", "mesh_discoverable", "aprs_enabled", "aprs_tx_enabled",
                     "meshcore_enabled") and v not in ("0", "1"):
                raise ApiError(400, f"{k} must be 0 or 1.")
            if k == "seoc_level" and v not in SEOC_LEVELS:
                raise ApiError(400, "Invalid SEOC level.")
            if k in ("map_lat", "map_lon", "map_zoom"):
                try:
                    float(v)
                except ValueError:
                    raise ApiError(400, f"{k} must be a number.")
            if k == "wx_states":
                v = re.sub(r"\s", "", v.upper())
                if not re.fullmatch(r"[A-Z]{2}(,[A-Z]{2})*", v) or any(s not in STATE_FIPS for s in v.split(",")):
                    raise ApiError(400, "Weather states must be 2-letter codes, e.g. TN or TN,KY.")
            if k == "wx_offices":
                v = re.sub(r"\s", "", v.upper())
                if not re.fullmatch(r"[A-Z]{3}(,[A-Z]{3})*", v):
                    raise ApiError(400, "NWS offices must be 3-letter codes, e.g. MRX,OHX,MEG.")
            if k == "wx_enabled" and v not in ("0", "1"):
                raise ApiError(400, "wx_enabled must be 0 or 1.")
            self.con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
            changed.append(f"{k}=(updated)" if k in SECRET_SETTINGS else f"{k}={v[:200]}")
        self.audit("settings", detail="; ".join(changed))
        if any(c.startswith("mesh_node_name=") for c in changed):
            mesh_refresh_name(self.con)
        self.con.commit()
        if any(c.split("=")[0].startswith(("aprs", "map_lat", "map_lon")) for c in changed):
            APRS.refilter()
        if any(c.startswith("meshcore_") for c in changed):
            _EMBED["t"] = 0.0  # pick up a new embed address right away
        BUS.publish({"entity": "settings", "action": "update"})
        if any(c.startswith(("wx_states=", "wx_enabled=")) for c in changed):
            threading.Thread(target=WX.poll, daemon=True).start()  # refresh alerts for the new area now
        if any(c.startswith(("wx_states=", "feeds_", "lsr_", "windy_", "tdot_")) for c in changed):
            FEEDS.flush()
        self.send_json(self.get_settings())

    # ---- users ------------------------------------------------------------
    def api_users(self, method, rest):
        me = self.require("admin")
        if method == "GET" and not rest:
            rows = self.con.execute("SELECT * FROM users ORDER BY username").fetchall()
            return self.send_json([self.user_out(dict(r)) for r in rows])
        if method == "POST" and not rest:
            body = self.read_json()
            username = str(body.get("username", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", username):
                raise ApiError(400, "Username must be 2-40 letters, numbers, dot, dash or underscore.")
            role = body.get("role", "viewer")
            if role not in ROLE_RANK:
                raise ApiError(400, "Invalid role.")
            check_password_policy(body.get("password"))
            try:
                cur = self.con.execute(
                    "INSERT INTO users(username, display_name, callsign, role, password_hash, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (username, str(body.get("display_name") or "")[:100], str(body.get("callsign") or "").upper()[:20],
                     role, hash_password(body["password"]), now_iso()))
            except sqlite3.IntegrityError:
                raise ApiError(409, "That username already exists.")
            for kind in MEMBER_KINDS:
                if kind in body:
                    self.set_memberships(cur.lastrowid, kind, body[kind], body.get("program_admin"))
            self.audit("create", "users", cur.lastrowid, username)
            self.con.commit()
            if any(k in body for k in MEMBER_KINDS):
                BUS.publish({"entity": "members", "action": "update", "public": False})
            row = self.con.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self.send_json(self.user_out(dict(row)), 201)
        if len(rest) == 1:
            try:
                uid = int(rest[0])
            except ValueError:
                raise ApiError(404, "Not found.")
            row = self.con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not row:
                raise ApiError(404, "User not found.")
            if method == "PUT":
                body = self.read_json()
                sets, args = [], []
                for k in ("display_name", "callsign"):
                    if k in body:
                        v = str(body[k] or "")[:100]
                        sets.append(f"{k} = ?")
                        args.append(v.upper() if k == "callsign" else v)
                if "role" in body:
                    if body["role"] not in ROLE_RANK:
                        raise ApiError(400, "Invalid role.")
                    if uid == me["id"] and body["role"] != "admin":
                        raise ApiError(400, "You cannot remove your own admin role.")
                    sets.append("role = ?")
                    args.append(body["role"])
                if "active" in body:
                    if uid == me["id"] and not body["active"]:
                        raise ApiError(400, "You cannot deactivate yourself.")
                    sets.append("active = ?")
                    args.append(1 if body["active"] else 0)
                if body.get("password"):
                    check_password_policy(body["password"])
                    sets.append("password_hash = ?")
                    args.append(hash_password(body["password"]))
                if "program_admin" in body and "programs" not in body:  # only the admin flags changed
                    body["programs"] = sorted(access_for(self.con, dict(row))["programs"])
                member_changes = [k for k in MEMBER_KINDS if k in body]
                for kind in member_changes:
                    self.set_memberships(uid, kind, body[kind], body.get("program_admin"))
                if sets or member_changes:
                    if sets:
                        self.con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, uid))
                    if "active" in body and not body["active"] or body.get("password"):
                        self.con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                    self.audit("update", "users", uid, ", ".join([s.split(" ")[0] for s in sets] + member_changes))
                    self.con.commit()
                    if member_changes:
                        BUS.publish({"entity": "members", "action": "update", "public": False})
                row = self.con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                return self.send_json(self.user_out(dict(row)))
            if method == "DELETE":
                if uid == me["id"]:
                    raise ApiError(400, "You cannot delete yourself.")
                self.con.execute("DELETE FROM users WHERE id = ?", (uid,))
                self.audit("delete", "users", uid, row["username"])
                self.con.commit()
                return self.send_json({"ok": True})
        raise ApiError(405, "Method not allowed.")

    # ---- generic entities ---------------------------------------------------
    def api_debrief(self, rid):
        """GET /api/incidents/<id>/debrief.pdf - the debrief kit for a closed incident."""
        u = self.require("viewer")
        try:
            iid = int(rid)
        except ValueError:
            raise ApiError(404, "Not found.")
        row = self.con.execute("SELECT * FROM incidents WHERE id = ?", (iid,)).fetchone()
        if not row or self.record_hidden("incidents", row):
            raise ApiError(404, "Not found.")
        if row["status"] != "closed":
            raise ApiError(409, "The debrief kit is available once the incident is closed.")
        pdf, name = build_debrief(self.con, dict(row), u, self.get_settings())
        self.audit("debrief_pdf", "incidents", iid, name)
        self.con.commit()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(pdf)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(pdf)

    def api_program_logo(self, method, rid):
        """GET/PUT/DELETE /api/programs/<id>/logo. Upload body is the raw image (PNG, JPEG, GIF or WebP)."""
        try:
            pid = int(rid)
        except ValueError:
            raise ApiError(404, "Not found.")
        row = self.con.execute("SELECT * FROM programs WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise ApiError(404, "Program not found.")
        folder = os.path.join(CONFIG["data_dir"], "program-logos")
        if method == "GET":
            if not self.can_read(ENTITIES["programs"]["public"]):
                raise ApiError(401, "Sign in required.")
            ext = (row["logo"] or "").split(":")[0]
            path = os.path.join(folder, f"{pid}.{ext}")
            if ext not in LOGO_MIME or not os.path.isfile(path):
                raise ApiError(404, "This program has no logo.")
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", LOGO_MIME[ext])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=604800")  # URL carries the version
            self.security_headers()
            self.end_headers()
            self.wfile.write(data)
            return
        if method not in ("PUT", "DELETE"):
            raise ApiError(405, "Method not allowed.")
        self.require_modify("programs", row)
        u = self.current_user()
        value = None
        if method == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_LOGO:
                raise ApiError(413, "Logo is too big. Use an image under 1.5 MB.")
            data = self.rfile.read(length) if length else b""
            ext = logo_type(data)
            if not ext:
                raise ApiError(415, "Upload a PNG, JPEG, GIF or WebP image.")
            value = f"{ext}:{hashlib.sha1(data).hexdigest()[:12]}"
        os.makedirs(folder, exist_ok=True)
        for old in LOGO_MIME:
            try:
                os.remove(os.path.join(folder, f"{pid}.{old}"))
            except OSError:
                pass
        if value:
            with open(os.path.join(folder, f"{pid}.{ext}.tmp"), "wb") as fh:
                fh.write(data)
            os.replace(os.path.join(folder, f"{pid}.{ext}.tmp"), os.path.join(folder, f"{pid}.{ext}"))
        self.con.execute("UPDATE programs SET logo = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                         (value, now_iso(), u["username"], pid))
        mesh_stamp(self.con, "programs", pid)
        self.audit("program_logo", "programs", pid, "uploaded" if value else "removed")
        self.con.commit()
        new = self.con.execute("SELECT * FROM programs WHERE id = ?", (pid,)).fetchone()
        self.publish("programs", "update", pid, new, row)
        return self.send_json(self.row_out("programs", new))

    def api_entity(self, method, entity, rest, qs):
        if entity == "incidents" and method == "GET" and len(rest) == 2 and rest[1] == "debrief.pdf":
            return self.api_debrief(rest[0])
        if entity == "programs" and len(rest) == 2 and rest[1] == "logo":
            return self.api_program_logo(method, rest[0])
        spec = ENTITIES[entity]
        fields = {f["name"] for f in spec["fields"]}
        refs_fields = {f["name"] for f in spec["fields"] if f["type"] == "refs"}
        if method == "GET" and not rest:
            if not self.can_read(spec["public"]):
                raise ApiError(401, "Sign in required.")
            where, args = [], []
            for k, vals in qs.items():
                if k in fields and k not in refs_fields:
                    where.append(f"{k} = ?")
                    args.append(vals[0])
                elif k == "since_id":
                    where.append("id > ?")
                    args.append(int(vals[0]))
            try:
                limit = max(1, min(int((qs.get("limit") or ["1000"])[0]), 5000))
            except ValueError:
                limit = 1000
            sql = f"SELECT * FROM {entity}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            rows = self.con.execute(sql, (*args, limit)).fetchall()
            return self.send_json([self.row_out(entity, r) for r in rows if not self.record_hidden(entity, r)])

        if method == "POST" and not rest:
            u = self.require("viewer")
            via_pa = ROLE_RANK[u["role"]] < ROLE_RANK[spec["create"]]  # creating only because they're a program admin
            pa = self.access()["program_admin"]
            if via_pa and not (spec.get("program_admin_create") and pa):
                raise ApiError(403, f"Requires {spec['create']} role.")
            rec = clean_record(entity, self.read_json(), partial=False)
            if via_pa and entity == "incidents":
                progs = set(parse_refs(rec.get("programs")))
                if not progs & pa:
                    if len(pa) != 1:
                        raise ApiError(400, "Assign the incident to your program.")
                    progs |= pa  # their one program
                rec["programs"] = json.dumps(sorted(progs))
            elif via_pa and rec.get("incident_id") is not None:
                inc = self.incident_row(rec["incident_id"])
                if not inc or not set(parse_refs(inc["programs"])) & pa:
                    raise ApiError(403, "Program admins can add these only to incidents assigned to their program.")
            self.check_refs(entity, rec)
            ts = now_iso()
            rec.update(created_at=ts, updated_at=ts, created_by=u["username"], updated_by=u["username"])
            cols = list(rec)
            cur = self.con.execute(
                f"INSERT INTO {entity} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [rec[c] for c in cols])
            rid = cur.lastrowid
            mesh_stamp(self.con, entity, rid)
            self.audit("create", entity, rid, str(rec.get("title") or rec.get("name") or rec.get("body") or "")[:120])
            self.con.commit()
            row = self.con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
            self.publish(entity, "create", rid, row)
            return self.send_json(self.row_out(entity, row), 201)

        if len(rest) != 1:
            raise ApiError(404, "Not found.")
        try:
            rid = int(rest[0])
        except ValueError:
            raise ApiError(404, "Not found.")
        row = self.con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
        if not row:
            raise ApiError(404, f"{spec['singular']} not found.")
        if not self.can_read(spec["public"]):
            raise ApiError(401, "Sign in required.")
        if self.record_hidden(entity, row):
            # restricted incident the caller isn't assigned to: same answer as "doesn't exist"
            raise ApiError(404, f"{spec['singular']} not found.")

        if method == "GET":
            return self.send_json(self.row_out(entity, row))

        if method == "PUT":
            body = self.read_json()
            if entity == "incidents" and not self.can_modify(entity, row) and set(body) == {"programs"}:
                # a program admin may put their own program on (or take it off) any incident they can see
                u = self.require("viewer")
                old, new = set(parse_refs(row["programs"])), set(parse_refs(clean_record(entity, body, partial=True)["programs"]))
                if not self.access()["program_admin"] or not (old ^ new) <= self.access()["program_admin"]:
                    raise ApiError(403, "You can only add or remove your own program on this incident.")
            else:
                self.require_modify(entity, row)
            u = self.current_user()
            rec = clean_record(entity, body, partial=True)
            if not rec:
                return self.send_json(self.row_out(entity, row))
            self.check_refs(entity, rec)
            rec.update(updated_at=now_iso(), updated_by=u["username"])
            self.con.execute(f"UPDATE {entity} SET {', '.join(c + ' = ?' for c in rec)} WHERE id = ?",
                             (*rec.values(), rid))
            mesh_stamp(self.con, entity, rid)
            if entity == "incidents" and "visibility" in rec and rec["visibility"] != row["visibility"]:
                for ent, cid in mesh_children(self.con, rid):  # re-share / un-share its log, reports, chat...
                    mesh_stamp(self.con, ent, cid)
            self.audit("update", entity, rid, ", ".join(k for k in rec if k not in ("updated_at", "updated_by")))
            self.con.commit()
            new = self.con.execute(f"SELECT * FROM {entity} WHERE id = ?", (rid,)).fetchone()
            self.publish(entity, "update", rid, new, row)
            return self.send_json(self.row_out(entity, new))

        if method == "DELETE":
            self.require_modify(entity, row)
            if not self.can_delete(entity, row):
                raise ApiError(403, f"Only a server admin can delete a {spec['singular'].lower()}.")
            scope = self.scope_of(entity, row)  # for the live event, before the incident row is gone
            delete_record(self.con, entity, rid, log=True)
            self.audit("delete", entity, rid, str(row["title"] if "title" in row.keys() else
                                                  row["name"] if "name" in row.keys() else "")[:120])
            self.con.commit()
            evt = {"entity": entity, "action": "delete", "id": rid, "public": spec["public"]}
            if scope and scope.get("visibility") == "restricted":
                evt.update(public=False, acl=[{k: scope.get(k) for k in ("visibility", "created_by", "programs", "agencies")}])
            BUS.publish(evt)
            MESH.kick()
            return self.send_json({"ok": True})
        raise ApiError(405, "Method not allowed.")

    def api_stats(self):
        if not self.can_read(True):
            raise ApiError(401, "Sign in required.")
        out = {}
        for name, spec in ENTITIES.items():
            if not self.can_read(spec["public"]):
                continue
            if self.hidden_incidents() and (name in ("incidents", "messages") or
                                            any(f["name"] == "incident_id" for f in spec["fields"])):
                rows = [r for r in self.con.execute(f"SELECT * FROM {name}") if not self.record_hidden(name, r)]
                if any(f["name"] == "status" for f in spec["fields"]):
                    counts = {}
                    for r in rows:
                        counts[r["status"] or "unknown"] = counts.get(r["status"] or "unknown", 0) + 1
                    out[name] = counts
                else:
                    out[name] = {"total": len(rows)}
                continue
            if any(f["name"] == "status" for f in spec["fields"]):
                out[name] = {r[0] or "unknown": r[1] for r in
                             self.con.execute(f"SELECT status, COUNT(*) FROM {name} GROUP BY status")}
            else:
                out[name] = {"total": self.con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]}
        out["_connected"] = BUS.count()
        self.send_json(out)

    def api_export(self):
        self.require("admin")
        out = {"exported_at": now_iso(), "version": VERSION, "settings": self.get_settings(),
               "users": [self.public_user(dict(r)) for r in self.con.execute("SELECT * FROM users")],
               "memberships": [dict(r) for r in self.con.execute("SELECT * FROM memberships")]}
        for name in ENTITIES:
            out[name] = [dict(r) for r in self.con.execute(f"SELECT * FROM {name} ORDER BY id")]
        self.audit("export")
        self.con.commit()
        body = json.dumps(out, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition",
                         f'attachment; filename="taciss-export-{datetime.now():%Y%m%d-%H%M}.json"')
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---- live stream ------------------------------------------------------
    def api_stream(self):
        user = self.current_user()
        if not user and not CONFIG["public_read"]:
            raise ApiError(401, "Sign in required.")
        self.con.close()
        self.con = None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # nginx: don't buffer
        self.security_headers()
        self.end_headers()
        self.close_connection = True
        q = BUS.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n: connected\n\n")
            self.wfile.flush()
            while not SHUTDOWN.is_set():
                try:
                    evt = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if not user and not evt.get("public", evt["entity"] == "settings"):
                    continue
                if evt.get("admin") and (not user or user["role"] != "admin"):
                    continue
                if evt.get("acl"):
                    if not stream_allowed(user, evt["acl"]):
                        continue
                    evt = {k: v for k, v in evt.items() if k != "acl"}
                self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_serve(args):
    if args.host:
        CONFIG["host"] = args.host
    if args.port:
        CONFIG["port"] = args.port
    init_db()
    con = connect()
    n_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    httpd = Server((CONFIG["host"], CONFIG["port"]), Handler)
    WX.start()
    FEEDS.start()
    if CONFIG["aprs"]:
        APRS.start()
    else:
        APRS.status["error"] = "APRS is turned off for this server (TACISS_APRS=0)."
    MESH.start()
    DISCOVERY.start()
    UPDATES.start()

    def stop(*_):
        SHUTDOWN.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    print(f"TACISS {VERSION} listening on http://{CONFIG['host']}:{CONFIG['port']}  data={os.path.abspath(db_path())}")
    print(f"  public_read={CONFIG['public_read']} trust_proxy={CONFIG['trust_proxy']}")
    if n_users == 0:
        print(f"  No accounts yet. Open the site to run first-time setup. Setup code: {ensure_setup_code()}")
        print("  (or create one here:  python3 taciss.py create-user admin --role admin)")
    sys.stdout.flush()
    httpd.serve_forever()
    httpd.server_close()


def read_new_password(provided=None):
    if provided:
        check_password_policy(provided)
        return provided
    pw = getpass.getpass("New password (10+ chars): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    check_password_policy(pw)
    return pw


def cmd_create_user(args):
    init_db()
    try:
        pw = read_new_password(args.password)
    except ApiError as e:
        sys.exit(e.message)
    con = connect()
    try:
        con.execute("INSERT INTO users(username, display_name, callsign, role, password_hash, created_at) "
                    "VALUES (?,?,?,?,?,?)", (args.username, args.name or args.username, (args.callsign or "").upper(),
                                             args.role, hash_password(pw), now_iso()))
        con.commit()
    except sqlite3.IntegrityError:
        sys.exit("User already exists. Use reset-password instead.")
    finally:
        con.close()
    ensure_setup_code()  # removes the one-time web setup code now that an account exists
    print(f"Created {args.role} '{args.username}'.")


def cmd_reset_password(args):
    init_db()
    try:
        pw = read_new_password(args.password)
    except ApiError as e:
        sys.exit(e.message)
    con = connect()
    cur = con.execute("UPDATE users SET password_hash = ?, active = 1 WHERE username = ?", (hash_password(pw), args.username))
    con.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username = ?)", (args.username,))
    con.commit()
    con.close()
    print("Password reset." if cur.rowcount else "No such user.")


def cmd_setup_code(args):
    code = ensure_setup_code()
    if args.quiet:
        if code:
            print(code)
        return
    print(f"First-time setup code: {code}" if code else "Setup already complete (an account exists).")


def cmd_backup(args):
    init_db()
    src = connect()
    dst = sqlite3.connect(args.dest)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"Backup written to {args.dest}")


def cmd_seed(args):
    init_db()
    con = connect()
    if con.execute("SELECT COUNT(*) FROM programs").fetchone()[0]:
        sys.exit("Database already has programs; refusing to seed.")
    ts = now_iso()
    base = dict(created_at=ts, updated_at=ts, created_by="seed", updated_by="seed")

    def ins(table, **kw):
        kw.update(base)
        cur = con.execute(f"INSERT INTO {table} ({', '.join(kw)}) VALUES ({', '.join('?' * len(kw))})", list(kw.values()))
        return cur.lastrowid

    ins("programs", name="Example County ARES", kind="ARES", status="operational", counties="Davidson",
        region="Middle TN", coordinator="Jane Doe", coordinator_call="N0CALL", frequencies="146.520 simplex",
        lat=36.16, lon=-86.78)
    ins("programs", name="Example Skywarn Net", kind="Skywarn", status="standby", counties="Knox, Blount",
        region="East TN", coordinator="John Roe", coordinator_call="K0ABC", frequencies="147.000 (+) 100.0",
        lat=35.96, lon=-83.92)
    ins("programs", name="Example RACES", kind="RACES", status="inactive", counties="Hamilton",
        region="Southeast TN", lat=35.05, lon=-85.31)
    ins("agencies", name="Example County OEM", kind="Emergency Management", status="active", county="Davidson",
        phone="555-0100", lat=36.17, lon=-86.77)
    ins("agencies", name="NWS Example Office", kind="NWS", status="active", county="Knox", lat=36.0, lon=-83.9)
    inc = ins("incidents", title="Exercise: Severe Weather Drill", kind="exercise", status="active", priority="normal",
              location="Middle TN", lat=36.0, lon=-86.5, commander="NCS N0CALL", radio_net="146.520",
              description="Training exercise. Check-ins on the net every 30 minutes.")
    ins("activity", incident_id=inc, body="Net opened. 6 stations checked in.")
    ins("reports", title="Wind damage - trees down", kind="storm_report", severity="minor", incident_id=inc,
        location="Example Rd", lat=36.05, lon=-86.6, body="Several trees across roadway. No injuries.")
    ins("resources", name="Go-Kit Alpha", kind="go_kit", status="available", location="EOC", contact="N0CALL")
    ins("messages", channel="general", body="Welcome to TACISS.")
    con.commit()
    con.close()
    print("Demo data loaded.")


def main():
    p = argparse.ArgumentParser(description="TACISS self-hosted server")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("create-user", help="create a user")
    s.add_argument("username")
    s.add_argument("--role", choices=list(ROLE_RANK), default="viewer")
    s.add_argument("--name")
    s.add_argument("--callsign")
    s.add_argument("--password", help="non-interactive (avoid; shows in shell history)")
    s.set_defaults(fn=cmd_create_user)
    s = sub.add_parser("reset-password", help="reset a user's password")
    s.add_argument("username")
    s.add_argument("--password")
    s.set_defaults(fn=cmd_reset_password)
    s = sub.add_parser("setup-code", help="show the one-time web setup code (only while no accounts exist)")
    s.add_argument("--quiet", action="store_true", help="print only the code, or nothing")
    s.set_defaults(fn=cmd_setup_code)
    s = sub.add_parser("backup", help="hot backup of the database")
    s.add_argument("dest")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("seed-demo", help="load example data")
    s.set_defaults(fn=cmd_seed)
    args = p.parse_args()
    if not args.cmd:
        args = p.parse_args(["serve"])
    args.fn(args)


if __name__ == "__main__":
    main()
