#!/usr/bin/env python3
"""
app.py - Tomorrow PWA backend + Admin UI (LAN-only)

Drop-in replacement you can copy as-is. Key improvements compared to your draft:
 - ADMIN_PATH is honored everywhere (admin HTML, export path, links).
 - Robust parsing and safe storage of `meta.age` and `meta.gender`.
 - Admin shows all users (no filtering by online) and handles null/missing fields safely.
 - Extra HTML-escaping when rendering admin page to avoid template breakages from user data.
 - Defensive DB access & exception handling to avoid 500s from malformed rows.
 - /pulse_receiver accepts POST/GET pulses, updates keepalive, and stores a report row.
 - DOES NOT forward pulses back (no circular pings).
"""
import os
import json
import csv
import sqlite3
import logging
import threading
import time
import base64
import html as html_lib
from datetime import datetime
from ipaddress import ip_address, ip_network
from flask import (
    Flask, request, jsonify, g, send_from_directory, abort, redirect, url_for, Response
)
from flask_cors import CORS

# -----------------------
# Config
# -----------------------
DB_PATH = os.environ.get("TODAY_DB", "tomorrow.db")
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", 5000))
# single place to change admin path
ADMIN_PATH = os.environ.get("ADMIN_PATH", "smile").strip().lstrip("/") or "smile"

TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

KEEPALIVE_ENABLE = os.environ.get("KEEPALIVE_ENABLE", "1") != "0"
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "240"))
KEEPALIVE_URLS = [u.strip() for u in (os.environ.get("KEEPALIVE_URLS", "") or "").split(",") if u.strip()]

# private networks allowed to access admin UI
PRIVATE_NETS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
]

# -----------------------
# App init
# -----------------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tomorrow-backend")

# -----------------------
# Helpers
# -----------------------

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_client_ip():
    if TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def is_private_ip(ip_str: str) -> bool:
    try:
        ip = ip_address(ip_str)
        return any(ip in net for net in PRIVATE_NETS)
    except Exception:
        return False


def require_lan_admin():
    ip = get_client_ip()
    if not is_private_ip(ip):
        abort(403, description="Admin is LAN-only")


def safe_json_list(v, default=None):
    if default is None:
        default = []
    if v is None:
        return default
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return default


# -----------------------
# Database
# -----------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    cur = db.cursor()
    # users table (server-side metadata)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        name TEXT,
        location TEXT,
        country TEXT,
        created_at TEXT,
        last_seen TEXT,
        enabled INTEGER DEFAULT 1,
        disabled_apps TEXT DEFAULT '[]',
        meta TEXT DEFAULT '{}'
    )
    """)
    # messages table (announcements)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        audience TEXT DEFAULT 'all',
        target_emails TEXT DEFAULT '[]'
    )
    """)
    # message dismissals
    cur.execute("""
    CREATE TABLE IF NOT EXISTS message_dismissals (
        message_id INTEGER NOT NULL,
        user_email TEXT NOT NULL,
        dismissed_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_email)
    )
    """)

    # reports: clients POST here with any helpful aggregated data
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        report_type TEXT,
        payload TEXT,
        created_at TEXT,
        client_ip TEXT,
        user_agent TEXT
    )
    """)

    # commands: admin creates commands that clients poll via /api/commands
    cur.execute("""
    CREATE TABLE IF NOT EXISTS commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,
        payload TEXT DEFAULT '{}',
        audience TEXT DEFAULT 'all',
        target_emails TEXT DEFAULT '[]',
        created_at TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    # deliveries: track which client received which command (optional)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS command_deliveries (
        command_id INTEGER NOT NULL,
        client_id TEXT NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (command_id, client_id)
    )
    """)

    # keepalive (optional)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS keepalive (
        key TEXT PRIMARY KEY,
        last_ts TEXT,
        run_count INTEGER DEFAULT 0
    )
    """)
    db.commit()


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def row_to_user_dict(row):
    if row is None:
        return None
    try:
        disabled_apps = json.loads(row["disabled_apps"] or "[]")
    except Exception:
        disabled_apps = []
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    return {
        "email": row["email"],
        "name": row["name"],
        "location": row["location"],
        "country": row["country"],
        "createdAt": row["created_at"],
        "lastSeen": row["last_seen"],
        "enabled": bool(row["enabled"]),
        "disabledApps": disabled_apps,
        "meta": meta,
    }


# -----------------------
# Static file serving (PWA)
# -----------------------
@app.route("/")
def serve_index():
    if os.path.exists(os.path.join(".", "index.html")):
        return send_from_directory(".", "index.html")
    return ("Not found", 404)


@app.route("/<path:filename>")
def serve_static(filename):
    safe_path = os.path.abspath(os.path.join(".", filename))
    if not safe_path.startswith(os.path.abspath(".")):
        return ("Not found", 404)
    if not os.path.exists(safe_path):
        return ("Not found", 404)
    return send_from_directory(".", filename)


# -----------------------
# Utility: sanitize meta (age & gender)
# -----------------------
ALLOWED_GENDERS = {"female", "male", "nonbinary", "other", ""}


def sanitize_meta(raw_meta):
    """Return a minimal sanitized meta dict merging safe age/gender and preserving other non-sensitive keys."""
    meta = {}
    if raw_meta is None:
        return meta
    if isinstance(raw_meta, str):
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict):
                raw_meta = parsed
            else:
                raw_meta = {}
        except Exception:
            raw_meta = {}
    if not isinstance(raw_meta, dict):
        raw_meta = {}

    # Age
    age_val = raw_meta.get("age")
    if age_val is None or age_val == "":
        pass
    else:
        try:
            age_int = int(age_val)
            if 1 <= age_int <= 130:
                meta["age"] = age_int
        except Exception:
            # ignore invalid age, do not write
            pass

    # Gender
    gender_val = raw_meta.get("gender")
    if gender_val is None:
        pass
    else:
        gstr = str(gender_val).strip().lower()
        if gstr in ALLOWED_GENDERS:
            meta["gender"] = gstr
        else:
            # don't store unknown freeform values to avoid DB pollution; keep as empty string
            meta["gender"] = ""

    # preserve other keys that are safe-ish (non-identifying) — limited to a whitelist
    for k in ("device", "os", "locale", "app_version"):
        if k in raw_meta:
            try:
                meta[k] = raw_meta[k]
            except Exception:
                pass

    return meta


# -----------------------
# API: register / ping / status / report / commands
# -----------------------
@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    location = (data.get("location") or "").strip()
    country = (data.get("country") or "").strip()
    meta_raw = data.get("meta") or {}

    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    if not location or not country:
        return jsonify({"ok": False, "error": "location and country required"}), 400

    db = get_db()
    cur = db.cursor()
    now = now_iso()

    # sanitize meta (handles strings/dicts and extracts safe age/gender)
    meta_clean = sanitize_meta(meta_raw)

    try:
        cur.execute("SELECT meta FROM users WHERE email=?", (email,))
        existing = cur.fetchone()
        if existing:
            # merge existing meta
            try:
                old_meta = json.loads(existing["meta"] or "{}")
            except Exception:
                old_meta = {}
            merged = dict(old_meta)
            merged.update({k: v for k, v in meta_clean.items() if v is not None})
            cur.execute("""
              UPDATE users
              SET name=?, location=?, country=?, last_seen=?, meta=?
              WHERE email=?
            """, (name, location, country, now, json.dumps(merged), email))
        else:
            cur.execute("""
              INSERT INTO users (email, name, location, country, created_at, last_seen, enabled, disabled_apps, meta)
              VALUES (?, ?, ?, ?, ?, ?, 1, '[]', ?)
            """, (email, name, location, country, now, now, json.dumps(meta_clean)))
        db.commit()
    except Exception as e:
        logger.exception("Register error: %s", e)
        return jsonify({"ok": False, "error": "internal"}), 500

    cur.execute("SELECT * FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    return jsonify({"ok": True, "user": row_to_user_dict(row)}), 200


@app.route("/api/ping", methods=["POST"])
def api_ping():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    db = get_db()
    cur = db.cursor()
    now = now_iso()
    try:
        cur.execute("UPDATE users SET last_seen=? WHERE email=?", (now, email))
        db.commit()
    except Exception:
        pass

    cur.execute("SELECT enabled, disabled_apps FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return jsonify({"ok": True, "status": {"enabled": True, "disabledApps": []}}), 200

    try:
        disabled = json.loads(row["disabled_apps"] or "[]")
    except Exception:
        disabled = []

    return jsonify({
        "ok": True,
        "status": {
            "enabled": bool(row["enabled"]),
            "disabledApps": disabled
        }
    }), 200


@app.route("/api/status", methods=["GET"])
def api_status():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT enabled, disabled_apps FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return jsonify({"ok": True, "enabled": True, "disabledApps": []}), 200

    try:
        disabled = json.loads(row["disabled_apps"] or "[]")
    except Exception:
        disabled = []

    return jsonify({"ok": True, "enabled": bool(row["enabled"]), "disabledApps": disabled}), 200


# --- NEW: client -> server reports
@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(silent=True) or {}
    report_type = (data.get("reportType") or data.get("report_type") or "generic").strip()
    email = (data.get("email") or "").strip().lower() or None
    payload = data.get("payload") or data.get("data") or {}
    db = get_db()
    cur = db.cursor()
    now = now_iso()
    client_ip = get_client_ip()
    ua = request.headers.get("User-Agent", "")
    try:
        cur.execute("""
           INSERT INTO reports (email, report_type, payload, created_at, client_ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?)
        """, (email, report_type, json.dumps(payload), now, client_ip, (ua or "")[:512]))
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid}), 200
    except Exception as e:
        logger.exception("Failed to store report: %s", e)
        return jsonify({"ok": False, "error": "internal error"}), 500


# --- NEW: clients poll for commands
@app.route("/api/commands", methods=["GET"])
def api_commands():
    email = (request.args.get("email") or "").strip().lower() or None
    client_id = (request.args.get("client_id") or "").strip() or None
    if not client_id:
        client_id = get_client_ip()

    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM commands WHERE active=1 ORDER BY created_at ASC")
    rows = cur.fetchall()
    out = []
    for r in rows:
        audience = (r["audience"] or "all").strip()
        try:
            targets = json.loads(r["target_emails"] or "[]")
        except Exception:
            targets = []
        applies = False
        if audience == "all":
            applies = True
        elif audience == "emails" and email and email in [t.lower() for t in targets]:
            applies = True
        if not applies:
            continue
        cur.execute("SELECT 1 FROM command_deliveries WHERE command_id=? AND client_id=?", (r["id"], client_id))
        if cur.fetchone():
            continue
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        out.append({"id": r["id"], "command": r["command"], "payload": payload})

    for cmd in out:
        try:
            cur.execute("INSERT OR REPLACE INTO command_deliveries (command_id, client_id, delivered_at) VALUES (?, ?, ?)",
                        (cmd["id"], client_id, now_iso()))
        except Exception:
            pass
    db.commit()
    return jsonify({"ok": True, "commands": out}), 200


# -----------------------
# Messages & notifications
# -----------------------
@app.route("/api/messages", methods=["GET"])
def api_get_messages():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM messages WHERE active=1 ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    cur.execute("SELECT message_id FROM message_dismissals WHERE user_email=?", (email,))
    dismissed = {r["message_id"] for r in cur.fetchall()}
    out = []
    for r in rows:
        mid = r["id"]
        if mid in dismissed:
            continue
        audience = (r["audience"] or "all").strip()
        try:
            targets = json.loads(r["target_emails"] or "[]")
        except Exception:
            targets = []
        applies = True
        if audience == "emails":
            applies = (email in targets)
        if applies:
            out.append({"id": mid, "title": r["title"], "body": r["body"], "createdAt": r["created_at"]})
    return jsonify({"ok": True, "messages": out}), 200


@app.route("/api/messages/dismiss", methods=["POST"])
def api_dismiss_message():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    mid = data.get("messageId")
    if not email or mid is None:
        return jsonify({"ok": False, "error": "email and messageId required"}), 400
    try:
        mid_int = int(mid)
    except Exception:
        return jsonify({"ok": False, "error": "messageId must be integer"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT OR REPLACE INTO message_dismissals (message_id, user_email, dismissed_at) VALUES (?, ?, ?)",
                (mid_int, email, now_iso()))
    db.commit()
    return jsonify({"ok": True}), 200


# -----------------------
# Keepalive / heartbeat support
# -----------------------
def _db_touch_keepalive():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS keepalive (key TEXT PRIMARY KEY, last_ts TEXT, run_count INTEGER DEFAULT 0)")
        cur.execute("SELECT run_count FROM keepalive WHERE key='heartbeat'")
        row = cur.fetchone()
        run_count = (row[0] if row and row[0] is not None else 0) + 1
        cur.execute("INSERT OR REPLACE INTO keepalive (key, last_ts, run_count) VALUES (?, ?, ?)",
                    ("heartbeat", now_iso(), run_count))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.exception("Keepalive DB touch failed: %s", e)
        return False


def _http_ping(url, timeout=8):
    try:
        import urllib.request, urllib.error
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(64)
    except Exception as e:
        return None, None


def background_heartbeat_loop():
    logger.info("Keepalive thread started (interval=%ss) keepalive_enabled=%s", KEEPALIVE_INTERVAL, KEEPALIVE_ENABLE)
    self_url = f"http://127.0.0.1:{APP_PORT}/api/health"
    urls = list(KEEPALIVE_URLS)
    if self_url not in urls:
        urls.insert(0, self_url)
    while True:
        try:
            ok = _db_touch_keepalive()
            if ok:
                logger.debug("Keepalive DB updated")
            for u in urls:
                try:
                    st, _ = _http_ping(u)
                    logger.info("Keepalive ping %s -> status=%s", u, st)
                except Exception as e:
                    logger.info("Keepalive ping error %s -> %s", u, e)
        except Exception as e:
            logger.exception("Keepalive loop error: %s", e)
        time.sleep(max(10, KEEPALIVE_INTERVAL))


@app.route("/api/keepalive_status", methods=["GET"])
def api_keepalive_status():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT last_ts, run_count FROM keepalive WHERE key='heartbeat'")
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": True, "last_ts": None, "run_count": 0}), 200
        return jsonify({"ok": True, "last_ts": row[0], "run_count": int(row[1] or 0)}), 200
    except Exception as e:
        logger.exception("keepalive_status error: %s", e)
        return jsonify({"ok": False, "error": "internal error"}), 500


# -----------------------
# Admin UI (LAN-only) — route depends on ADMIN_PATH
# -----------------------
@app.route(f"/{ADMIN_PATH}", methods=["GET", "POST"])
def admin_dashboard():
    require_lan_admin()
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "request_report":
            audience = (request.form.get("audience") or "all").strip()
            targets = (request.form.get("targets") or "").strip()
            cmd_payload_raw = (request.form.get("cmd_payload") or "{}").strip()
            try:
                cmd_payload = json.loads(cmd_payload_raw)
            except Exception:
                cmd_payload = {}
            target_emails = [e.strip().lower() for e in targets.split(",") if e.strip()]
            try:
                cur.execute("""
                    INSERT INTO commands (command, payload, audience, target_emails, created_at, active)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, ("REQUEST_REPORT", json.dumps(cmd_payload), audience, json.dumps(target_emails), now_iso()))
                db.commit()
            except Exception:
                logger.exception("Failed creating command")
        elif action == "cancel_command":
            cid = request.form.get("command_id", "")
            if cid.isdigit():
                try:
                    cur.execute("UPDATE commands SET active=0 WHERE id=?", (int(cid),))
                    db.commit()
                except Exception:
                    pass
        elif action == "toggle_user":
            email = (request.form.get("email") or "").strip().lower()
            if email:
                try:
                    cur.execute("UPDATE users SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE email=?", (email,))
                    db.commit()
                except Exception:
                    pass
        return redirect(url_for("admin_dashboard"))

    # GET: fetch data (no filtering by online)
    try:
        cur.execute("SELECT * FROM users ORDER BY last_seen DESC")
        users = cur.fetchall()
    except Exception:
        users = []

    try:
        cur.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 30")
        msgs = cur.fetchall()
    except Exception:
        msgs = []

    try:
        cur.execute("SELECT * FROM commands ORDER BY id DESC LIMIT 50")
        commands = cur.fetchall()
    except Exception:
        commands = []

    try:
        cur.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 200")
        reports = cur.fetchall()
    except Exception:
        reports = []

    total_reports = len(reports)
    unique_reporters = len({r["email"] or f'ip:{r["client_ip"]}' for r in reports})

    country_counts = {}
    for r in reports:
        try:
            if r["email"]:
                u = cur.execute("SELECT country FROM users WHERE email=?", (r["email"],)).fetchone()
                c = (u["country"] if u and u["country"] else "unknown")
            else:
                c = "unknown"
        except Exception:
            c = "unknown"
        country_counts[c] = country_counts.get(c, 0) + 1

    disabled_counts = {}
    for u in users:
        try:
            arr = json.loads(u["disabled_apps"] or "[]")
            for a in arr:
                disabled_counts[a] = disabled_counts.get(a, 0) + 1
        except Exception:
            pass

    # Build rows with HTML escaping
    users_rows = ""
    for u in users:
        try:
            enabled = "✅" if u["enabled"] == 1 else "⛔"
            users_rows += f"""
            <tr>
              <td>{html_lib.escape(enabled)}</td>
              <td style=\"font-family:ui-monospace,monospace;font-size:12px\">{html_lib.escape(u['email'] or '')}</td>
              <td>{html_lib.escape(u['name'] or '')}</td>
              <td>{html_lib.escape(u['country'] or '')}</td>
              <td>{html_lib.escape(u['location'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(u['created_at'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(u['last_seen'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(u['disabled_apps'] or '[]')}</td>
              <td>
                <form method=\"post\" style=\"display:inline\">
                  <input type=\"hidden\" name=\"action\" value=\"toggle_user\"> 
                  <input type=\"hidden\" name=\"email\" value=\"{html_lib.escape(u['email'] or '')}\"> 
                  <button class=\"btn small\" type=\"submit\">Toggle</button>
                </form>
              </td>
            </tr>
            """
        except Exception:
            continue

    msgs_rows = ""
    for m in msgs:
        try:
            active = "ACTIVE" if m["active"] == 1 else "OFF"
            msgs_rows += f"""
            <tr>
              <td>{m['id']}</td>
              <td>{html_lib.escape(active)}</td>
              <td>{html_lib.escape(m['created_at'] or '')}</td>
              <td><b>{html_lib.escape(m['title'] or '')}</b><div style=\"opacity:.85;font-size:12px\">{html_lib.escape(m['body'] or '')}</div></td>
              <td>{html_lib.escape(m['audience'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(m['target_emails'] or '[]')}</td>
              <td>
                <form method=\"post\" style=\"display:inline\">
                  <input type=\"hidden\" name=\"action\" value=\"cancel_message\">
                  <input type=\"hidden\" name=\"message_id\" value=\"{m['id']}\">
                  <button class=\"btn small danger\" type=\"submit\">Cancel</button>
                </form>
              </td>
            </tr>
            """
        except Exception:
            continue

    commands_rows = ""
    for c in commands:
        try:
            active = "ACTIVE" if c["active"] == 1 else "INACTIVE"
            commands_rows += f"""
            <tr>
              <td>{c['id']}</td>
              <td>{html_lib.escape(active)}</td>
              <td>{html_lib.escape(c['created_at'] or '')}</td>
              <td><b>{html_lib.escape(c['command'] or '')}</b><div style=\"opacity:.85;font-size:12px\">{html_lib.escape(c['payload'] or '{}')}</div></td>
              <td>{html_lib.escape(c['audience'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(c['target_emails'] or '[]')}</td>
              <td>
                <form method=\"post\" style=\"display:inline\">
                  <input type=\"hidden\" name=\"action\" value=\"cancel_command\">
                  <input type=\"hidden\" name=\"command_id\" value=\"{c['id']}\">
                  <button class=\"btn small danger\" type=\"submit\">Cancel</button>
                </form>
              </td>
            </tr>
            """
        except Exception:
            continue

    reports_rows = ""
    for r in reports:
        try:
            payload_preview = (r['payload'] and (r['payload'][:240] + '...')) if r['payload'] and len(r['payload']) > 240 else (r['payload'] or "")
            reports_rows += f"""
            <tr>
              <td>{r['id']}</td>
              <td style=\"font-family:ui-monospace,monospace\">{html_lib.escape(r['email'] or '')}</td>
              <td>{html_lib.escape(r['report_type'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(r['created_at'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(r['client_ip'] or '')}</td>
              <td style=\"font-size:12px\">{html_lib.escape(payload_preview)}</td>
            </tr>
            """
        except Exception:
            continue

    country_list_html = "".join(f"<li>{html_lib.escape(k)}: {v}</li>" for k, v in sorted(country_counts.items(), key=lambda x: -x[1]))
    disabled_list_html = "".join(f"<li>{html_lib.escape(k)}: {v}</li>" for k, v in sorted(disabled_counts.items(), key=lambda x: -x[1]))

    admin_export_path = f"/{ADMIN_PATH}/export_reports"

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Tomorrow Admin</title>
  <style>
    body{{font-family:system-ui,Arial;margin:0;background:#071022;color:#e6eef6}}
    header{{padding:14px 16px;background:linear-gradient(90deg,#0ea5a4,#3b82f6);display:flex;align-items:center;gap:12px}}
    header h1{{margin:0;font-size:18px}}
    .wrap{{max-width:1200px;margin:0 auto;padding:16px}}
    .card{{background:#0b1220;border:1px solid rgba(255,255,255,.04);border-radius:14px;padding:14px;margin-bottom:14px}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{border-bottom:1px solid rgba(255,255,255,.04);padding:10px;vertical-align:top}}
    th{{text-align:left;opacity:.85;font-size:12px}}
    .btn{{background:#4f46e5;border:0;color:white;border-radius:10px;padding:10px 12px;cursor:pointer}}
    .btn.small{{padding:8px 10px;font-size:12px}}
    .btn.danger{{background:#ef4444}}
    input,select,textarea{{width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,.08);background:#071022;color:#e6eef6}}
    textarea{{min-height:72px}}
    .row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    .muted{{opacity:.8;font-size:12px}}
    .chip{{display:inline-block;padding:6px 10px;border:1px solid rgba(255,255,255,.06);border-radius:999px;font-size:12px;opacity:.95}}
    .summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
    pre{{white-space:pre-wrap;word-break:break-word;background:#071022;padding:8px;border-radius:8px}}
  </style>
</head>
<body>
  <header>
    <h1>Tomorrow Admin</h1>
    <div style="margin-left:auto" class="chip">LAN-only • /{html_lib.escape(ADMIN_PATH)}</div>
  </header>

  <div class="wrap">
    <div class="card">
      <h3 style="margin:0 0 8px 0">Request reports from clients</h3>
      <div class="muted">This creates a command that clients will receive next time they poll <code>/api/commands</code>.</div>
      <form method="post" style="margin-top:10px">
        <input type="hidden" name="action" value="request_report">
        <div class="row">
          <div>
            <label>Audience</label>
            <select name="audience">
              <option value="all">All clients</option>
              <option value="emails">Specific emails</option>
            </select>
          </div>
          <div>
            <label>Target emails (comma-separated, used only if audience=emails)</label>
            <input name="targets" placeholder="a@x.com,b@y.com">
          </div>
        </div>
        <div style="margin-top:10px">
          <label>Payload (JSON) — optional (for example: {{\"type\":\"usage_summary\",\"since\":\"2026-01-01\"}})</label>
          <textarea name="cmd_payload" placeholder='{{"type":"usage_summary"}}'></textarea>
        </div>
        <div style="margin-top:10px">
          <button class="btn" type="submit">Request reports</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Quick summary</h3>
      <div class="summary-grid">
        <div class="card"><strong>Total reports</strong><div class="muted">{total_reports}</div></div>
        <div class="card"><strong>Unique reporters</strong><div class="muted">{unique_reporters}</div></div>
        <div class="card"><strong>Countries (reports)</strong><div class="muted"><ul style="margin:6px 0;padding-left:18px">{country_list_html}</ul></div></div>
        <div class="card"><strong>Disabled apps (users)</strong><div class="muted"><ul style="margin:6px 0;padding-left:18px">{disabled_list_html}</ul></div></div>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Recent reports</h3>
      <form method="get" action="{admin_export_path}" style="margin-bottom:10px;display:flex;gap:8px;align-items:center">
        <input type="hidden" name="format" value="json">
        <button class="btn small" type="submit">Export JSON (all)</button>
        <a href="{admin_export_path}?format=csv" class="btn small" style="text-decoration:none;color:white;padding:8px 10px">Export CSV</a>
      </form>
      <table>
        <thead>
          <tr><th>ID</th><th>Reporter</th><th>Type</th><th>When</th><th>IP</th><th>Payload (preview)</th></tr>
        </thead>
        <tbody>
          {reports_rows or "<tr><td colspan='6' class='muted'>No reports yet.</td></tr>"}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Commands</h3>
      <table>
        <thead><tr><th>ID</th><th>Status</th><th>Created</th><th>Command</th><th>Audience</th><th>Targets</th><th>Action</th></tr></thead>
        <tbody>
          {commands_rows or "<tr><td colspan='7' class='muted'>No commands</td></tr>"}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Users</h3>
      <table>
        <thead>
          <tr><th>On?</th><th>Email</th><th>Name</th><th>Country</th><th>Location</th><th>Joined</th><th>Last seen</th><th>Disabled apps</th><th>Action</th></tr>
        </thead>
        <tbody>
          {users_rows or "<tr><td colspan='9' class='muted'>No users yet. Clients must POST /api/register.</td></tr>"}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Messages</h3>
      <table>
        <thead><tr><th>ID</th><th>Status</th><th>Created</th><th>Content</th><th>Audience</th><th>Targets</th><th>Action</th></tr></thead>
        <tbody>
          {msgs_rows or "<tr><td colspan='7' class='muted'>No messages yet.</td></tr>"}
        </tbody>
      </table>
    </div>

  </div>
</body>
</html>
"""

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# -----------------------
# Export reports endpoint (admin use) — uses ADMIN_PATH
# -----------------------
@app.route(f"/{ADMIN_PATH}/export_reports", methods=["GET"])
def export_reports():
    require_lan_admin()
    fmt = (request.args.get("format") or "json").lower()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM reports ORDER BY created_at DESC")
    rows = cur.fetchall()
    if fmt == "csv":
        def gen():
            header = ["id", "email", "report_type", "created_at", "client_ip", "user_agent", "payload"]
            yield ",".join(header) + "\n"
            for r in rows:
                rowvals = [
                    str(r["id"]),
                    (r["email"] or ""),
                    (r["report_type"] or ""),
                    (r["created_at"] or ""),
                    (r["client_ip"] or ""),
                    (r["user_agent"] or "").replace("\n", " "),
                    '"' + (r["payload"] or "").replace('"', '""').replace("\n", " ") + '"'
                ]
                yield ",".join(rowvals) + "\n"
        return Response(gen(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tomorrow-reports.csv"})
    else:
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = r["payload"] or {}
            out.append({
                "id": r["id"],
                "email": r["email"],
                "reportType": r["report_type"],
                "payload": payload,
                "createdAt": r["created_at"],
                "clientIp": r["client_ip"],
                "userAgent": r["user_agent"]
            })
        return jsonify({"ok": True, "reports": out}), 200


# -----------------------
# Health, keepalive pages
# -----------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "time": now_iso()}), 200

# Accept pulses from master pinger (breathe)
PULSE_SECRET = os.environ.get("PULSE_SECRET")  # set this to require X-PULSE-TOKEN

@app.route("/pulse_receiver", methods=["POST", "GET"])
def pulse_receiver():
    """
    Receive an external pulse (from breathe) and update the keepalive table.
    If PULSE_SECRET is set, require header X-PULSE-TOKEN or ?token=... to match.
    Stores a small report row for auditing. DOES NOT forward the pulse (prevents circular pings).
    Returns JSON {status, received_at}.
    """
    token = request.headers.get("X-PULSE-TOKEN") or request.args.get("token")
    if PULSE_SECRET:
        if not token or token != PULSE_SECRET:
            return jsonify({"status": "unauthorized"}), 401

    # Try to get JSON payload, otherwise form or a minimal ping
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict() or {"message": "ping"}

    now = now_iso()

    # Persist a quick keepalive touch (separate key so you can distinguish internal heartbeat)
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS keepalive (key TEXT PRIMARY KEY, last_ts TEXT, run_count INTEGER DEFAULT 0)")
        cur.execute("SELECT run_count FROM keepalive WHERE key='external_pulse'")
        row = cur.fetchone()
        run_count = (row[0] if row and row[0] is not None else 0) + 1
        cur.execute("INSERT OR REPLACE INTO keepalive (key, last_ts, run_count) VALUES (?, ?, ?)",
                    ("external_pulse", now, run_count))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("pulse_receiver DB touch failed: %s", e)

    # Store a small entry in reports table so admin UI can show incoming pulses
    try:
        db = get_db()
        cur = db.cursor()
        ua = request.headers.get("User-Agent", "")[:512]
        client_ip = get_client_ip()
        cur.execute("""
            INSERT INTO reports (email, report_type, payload, created_at, client_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (None, "external_pulse", json.dumps(payload), now, client_ip, ua))
        db.commit()
    except Exception as e:
        logger.exception("Failed to store external pulse as report: %s", e)

    # IMPORTANT: DO NOT forward the pulse automatically to avoid circular pings.

    return jsonify({"status": "ok", "received_at": now}), 200


@app.route("/babra-pixel")
def babra_pixel():
    gif_b64 = b"R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=="
    gif = base64.b64decode(gif_b64)
    return (gif, 200, {"Content-Type": "image/gif", "Cache-Control": "no-store"})


@app.route("/babra.html")
def babra_page():
    return redirect("/index.html")


# -----------------------
# Bootstrap keepalive thread
# -----------------------
def start_keepalive_thread():
    if not KEEPALIVE_ENABLE:
        logger.info("KEEPALIVE_DISABLE set; not starting keepalive thread.")
        return None
    t = threading.Thread(target=background_heartbeat_loop, name="keepalive-thread", daemon=True)
    t.start()
    return t


# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    logger.info("Starting Tomorrow backend on http://%s:%s", APP_HOST, APP_PORT)
    logger.info("Admin (LAN-only): http://%s:%s/%s", APP_HOST, APP_PORT, ADMIN_PATH)
    if KEEPALIVE_ENABLE:
        start_keepalive_thread()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=APP_HOST, port=APP_PORT, debug=debug_mode)
