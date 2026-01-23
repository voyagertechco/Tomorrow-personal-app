#!/usr/bin/env python3
"""
app.py - Tomorrow PWA backend + Admin UI (admin UI always shows users)

Usage:
  python app.py

Environment:
  TODAY_DB           - path to sqlite DB (default: tomorrow.db)
  APP_HOST           - host to bind (default: 0.0.0.0)
  APP_PORT           - port to bind  (default: 5000)
  ADMIN_PATH         - admin path suffix (default: admin256)
  TRUST_PROXY        - if "1", consider X-Forwarded-For for client IP detection
  KEEPALIVE_ENABLE   - "1" to enable the internal keepalive thread (default "1")
  KEEPALIVE_INTERVAL - seconds between heartbeats (default 240)
  KEEPALIVE_URLS     - comma-separated external URLs to ping (optional)

Notes:
 - This variant intentionally exposes the admin UI listing and a few admin actions
   unconditionally (no LAN-only restriction) per user request. Use with care.
"""

import os
import json
import sqlite3
import logging
import threading
import time
import urllib.request
import urllib.error
import base64
import csv
import io
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
ADMIN_PATH = os.environ.get("ADMIN_PATH", "admin256")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

KEEPALIVE_ENABLE = os.environ.get("KEEPALIVE_ENABLE", "1") != "0"
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "240"))
KEEPALIVE_URLS = [u.strip() for u in (os.environ.get("KEEPALIVE_URLS", "") or "").split(",") if u.strip()]

# private networks allowed to access admin UI (kept for helper; NOT enforced)
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
CORS(app, resources={r"/api/*": {"origins": "*"}})  # adjust for production

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tomorrow-backend")

# -----------------------
# Helpers
# -----------------------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def get_client_ip():
    """
    Resolve client IP. If TRUST_PROXY, prefer X-Forwarded-For first item.
    """
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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS message_dismissals (
        message_id INTEGER NOT NULL,
        user_email TEXT NOT NULL,
        dismissed_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_email)
    )
    """)
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
    return {
        "email": row["email"],
        "name": row["name"],
        "location": row["location"],
        "country": row["country"],
        "createdAt": row["created_at"],
        "lastSeen": row["last_seen"],
        "enabled": bool(row["enabled"]),
        "disabledApps": json.loads(row["disabled_apps"] or "[]"),
        "meta": json.loads(row["meta"] or "{}"),
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
# API: register / ping / status
# -----------------------

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    location = (data.get("location") or "").strip()
    country = (data.get("country") or "").strip()
    meta = data.get("meta") or {}

    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    if not location or not country:
        return jsonify({"ok": False, "error": "location and country required"}), 400

    db = get_db()
    cur = db.cursor()
    now = now_iso()

    cur.execute("SELECT created_at FROM users WHERE email=?", (email,))
    existing = cur.fetchone()

    if existing:
        cur.execute("""
          UPDATE users
          SET name=?, location=?, country=?, last_seen=?, meta=COALESCE(?, meta)
          WHERE email=?
        """, (name, location, country, now, json.dumps(meta), email))
    else:
        cur.execute("""
          INSERT INTO users (email, name, location, country, created_at, last_seen, enabled, disabled_apps, meta)
          VALUES (?, ?, ?, ?, ?, ?, 1, '[]', ?)
        """, (email, name, location, country, now, now, json.dumps(meta)))
    db.commit()

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
    cur.execute("UPDATE users SET last_seen=? WHERE email=?", (now, email))
    db.commit()

    cur.execute("SELECT enabled, disabled_apps FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return jsonify({"ok": True, "status": {"enabled": True, "disabledApps": []}}), 200

    return jsonify({
        "ok": True,
        "status": {
            "enabled": bool(row["enabled"]),
            "disabledApps": json.loads(row["disabled_apps"] or "[]")
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

    return jsonify({
        "ok": True,
        "enabled": bool(row["enabled"]),
        "disabledApps": json.loads(row["disabled_apps"] or "[]")
    }), 200

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
    cur.execute("""SELECT * FROM messages WHERE active=1 ORDER BY id DESC LIMIT 50""")
    rows = cur.fetchall()
    cur.execute("SELECT message_id FROM message_dismissals WHERE user_email=?", (email,))
    dismissed = {r[0] for r in cur.fetchall()}

    out = []
    for r in rows:
        mid = r["id"]
        if mid in dismissed:
            continue
        audience = (r["audience"] or "all").strip()
        targets = json.loads(r["target_emails"] or "[]")
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
    cur.execute("""INSERT OR REPLACE INTO message_dismissals (message_id, user_email, dismissed_at) VALUES (?, ?, ?)""",
                (mid_int, email, now_iso()))
    db.commit()
    return jsonify({"ok": True}), 200


@app.route("/api/notifications", methods=["GET"])
def api_notifications():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("""SELECT * FROM messages WHERE active=1 ORDER BY id DESC LIMIT 50""")
    rows = cur.fetchall()
    cur.execute("SELECT message_id FROM message_dismissals WHERE user_email=?", (email,))
    dismissed = {r[0] for r in cur.fetchall()}
    out = []
    for r in rows:
        mid = r["id"]
        if mid in dismissed:
            continue
        audience = (r["audience"] or "all").strip()
        targets = json.loads(r["target_emails"] or "[]")
        applies = True
        if audience == "emails":
            applies = (email in targets)
        if applies:
            out.append({"id": mid, "title": r["title"], "body": r["body"], "createdAt": r["created_at"]})
    return jsonify({"ok": True, "notifications": out}), 200

# -----------------------
# Keepalive / heartbeat support
# -----------------------
def _db_touch_keepalive():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS keepalive (key TEXT PRIMARY KEY, last_ts TEXT, run_count INTEGER DEFAULT 0)""")
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
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(64)
    except urllib.error.HTTPError as he:
        return he.code, None
    except Exception:
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
# Admin UI (always show users unconditionally)
# -----------------------
@app.route(f"/{ADMIN_PATH}", methods=["GET", "POST"]) 
def admin_dashboard():
    # NOTE: per request, this admin UI will list users unconditionally.
    db = get_db()
    cur = db.cursor()

    # Handle POST actions (extended admin abilities)
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "toggle_user":
            email = (request.form.get("email") or "").strip().lower()
            if email:
                cur.execute("UPDATE users SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE email=?", (email,))
                db.commit()
        elif action == "set_enable":
            email = (request.form.get("email") or "").strip().lower()
            val = request.form.get("value", "1")
            if email:
                try:
                    v = 1 if str(val) in ("1", "true", "True") else 0
                    cur.execute("UPDATE users SET enabled=? WHERE email=?", (v, email))
                    db.commit()
                except Exception:
                    pass
        elif action == "set_disabled_apps":
            email = (request.form.get("email") or "").strip().lower()
            apps = (request.form.get("apps") or "").strip()
            disabled_apps = [a.strip() for a in apps.split(",") if a.strip()]
            if email:
                cur.execute("UPDATE users SET disabled_apps=? WHERE email=?", (json.dumps(disabled_apps), email))
                db.commit()
        elif action == "edit_user":
            email = (request.form.get("email") or "").strip().lower()
            name = (request.form.get("name") or "").strip()
            country = (request.form.get("country") or "").strip()
            location = (request.form.get("location") or "").strip()
            if email:
                cur.execute("UPDATE users SET name=?, country=?, location=? WHERE email=?", (name, country, location, email))
                db.commit()
        elif action == "delete_user":
            email = (request.form.get("email") or "").strip().lower()
            if email:
                cur.execute("DELETE FROM users WHERE email=?", (email,))
                db.commit()
        elif action == "create_message":
            title = (request.form.get("title") or "").strip()
            body = (request.form.get("body") or "").strip()
            audience = (request.form.get("audience") or "all").strip()
            targets = (request.form.get("targets") or "").strip()
            target_emails = [e.strip().lower() for e in targets.split(",") if e.strip()]
            if title and body:
                if audience not in ("all", "emails"):
                    audience = "all"
                cur.execute("""
                  INSERT INTO messages (title, body, created_at, active, audience, target_emails)
                  VALUES (?, ?, ?, 1, ?, ?)
                """, (title, body, now_iso(), audience, json.dumps(target_emails)))
                db.commit()
        elif action == "cancel_message":
            mid = request.form.get("message_id", "")
            if mid.isdigit():
                cur.execute("UPDATE messages SET active=0 WHERE id=?", (int(mid),))
                db.commit()
        elif action == "export_users":
            # handled below as GET redirect
            pass
        return redirect(url_for("admin_dashboard"))

    # GET: optional search/filter
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        cur.execute("SELECT * FROM users WHERE email LIKE ? OR name LIKE ? ORDER BY last_seen DESC", (like, like))
    else:
        cur.execute("SELECT * FROM users ORDER BY last_seen DESC")
    users = cur.fetchall()

    cur.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 30")
    msgs = cur.fetchall()

    # build rows
    users_rows = ""
    for u in users:
        enabled = "✅" if u["enabled"] == 1 else "⛔"
        users_rows += f"""
        <tr>
          <td>{enabled}</td>
          <td style="font-family:ui-monospace,monospace;font-size:12px">{u["email"]}</td>
          <td>{u["name"] or ""}</td>
          <td>{u["country"] or ""}</td>
          <td>{u["location"] or ""}</td>
          <td style="font-size:12px">{u["created_at"] or ""}</td>
          <td style="font-size:12px">{u["last_seen"] or ""}</td>
          <td style="font-size:12px">{u["disabled_apps"] or "[]"}</td>
          <td>
            <form method="post" style="display:inline">
              <input type="hidden" name="action" value="toggle_user">
              <input type="hidden" name="email" value="{u["email"]}">
              <button class="btn small" type="submit">Toggle</button>
            </form>
            <form method="post" style="display:inline;margin-left:6px">
              <input type="hidden" name="action" value="delete_user">
              <input type="hidden" name="email" value="{u["email"]}">
              <button class="btn small danger" type="submit" onclick="return confirm('Delete user? This is permanent')">Delete</button>
            </form>
            <form method="get" action="" style="display:inline;margin-left:6px">
              <input type="hidden" name="q" value="{u["email"]}">
              <button class="btn small" type="submit">View</button>
            </form>
          </td>
        </tr>
        """

    msgs_rows = ""
    for m in msgs:
        active = "ACTIVE" if m["active"] == 1 else "OFF"
        msgs_rows += f"""
        <tr>
          <td>{m["id"]}</td>
          <td>{active}</td>
          <td>{m["created_at"]}</td>
          <td><b>{m["title"]}</b><div style="opacity:.85;font-size:12px">{m["body"]}</div></td>
          <td>{m["audience"]}</td>
          <td style="font-size:12px">{m["target_emails"]}</td>
          <td>
            <form method="post" style="display:inline">
              <input type="hidden" name="action" value="cancel_message">
              <input type="hidden" name="message_id" value="{m["id"]}">
              <button class="btn small danger" type="submit">Cancel</button>
            </form>
          </td>
        </tr>
        """

    # admin HTML (adds search, export)
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Tomorrow Admin</title>
  <style>
    body{{font-family:system-ui,Arial;margin:0;background:#0b1220;color:#e6eef6}}
    header{{padding:14px 16px;background:linear-gradient(90deg,#0ea5a4,#3b82f6);display:flex;align-items:center;gap:12px}}
    header h1{{margin:0;font-size:18px}}
    .wrap{{max-width:1200px;margin:0 auto;padding:16px}}
    .card{{background:#111a2e;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:14px;margin-bottom:14px}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{border-bottom:1px solid rgba(255,255,255,.08);padding:10px;vertical-align:top}}
    th{{text-align:left;opacity:.85;font-size:12px}}
    .btn{{background:#4f46e5;border:0;color:white;border-radius:10px;padding:10px 12px;cursor:pointer}}
    .btn.small{{padding:8px 10px;font-size:12px}}
    .btn.danger{{background:#ef4444}}
    input,select,textarea{{width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:#0b1220;color:#e6eef6}}
    textarea{{min-height:72px}}
    .row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    .muted{{opacity:.8;font-size:12px}}
    .chip{{display:inline-block;padding:6px 10px;border:1px solid rgba(255,255,255,.15);border-radius:999px;font-size:12px;opacity:.9}}
    .tools{display:flex;gap:8px;align-items:center}
  </style>
</head>
<body>
  <header>
    <h1>Tomorrow Admin</h1>
    <div style="margin-left:auto" class="chip">Admin path: /{ADMIN_PATH}</div>
  </header>

  <div class="wrap">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <h3 style="margin:0 0 8px 0">Send announcement</h3>
          <div class="muted">Clients must be online to fetch messages. Users can dismiss messages on device.</div>
        </div>
        <div class="tools">
          <form method="get" action="" style="margin:0;display:flex;gap:8px;align-items:center">
            <input name="q" placeholder="search email or name" value="{q}">
            <button class="btn" type="submit">Search</button>
          </form>
          <form method="get" action="/{ADMIN_PATH}/export_users" style="margin:0">
            <button class="btn" type="submit">Export CSV</button>
          </form>
          <form method="get" action="/{ADMIN_PATH}/export_users.json" style="margin:0">
            <button class="btn" type="submit">Export JSON</button>
          </form>
        </div>
      </div>

      <form method="post" style="margin-top:10px">
        <input type="hidden" name="action" value="create_message">
        <div class="row">
          <div>
            <label>Title</label>
            <input name="title" placeholder="Short title">
          </div>
          <div>
            <label>Audience</label>
            <select name="audience">
              <option value="all">All users</option>
              <option value="emails">Specific emails</option>
            </select>
          </div>
        </div>
        <div style="margin-top:10px">
          <label>Body</label>
          <textarea name="body" placeholder="Message text"></textarea>
        </div>
        <div style="margin-top:10px">
          <label>Target emails (comma-separated, used only if audience=emails)</label>
          <input name="targets" placeholder="a@x.com,b@y.com">
        </div>
        <div style="margin-top:10px">
          <button class="btn" type="submit">Send</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Recent announcements</h3>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Status</th><th>Created</th><th>Content</th><th>Audience</th><th>Targets</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {msgs_rows or "<tr><td colspan='7' class='muted'>No messages yet.</td></tr>"}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Users</h3>
      <div class="muted">Enable/disable applies when users come online and your client calls /api/status or /api/ping. You can search, export, edit or delete users.</div>
      <table>
        <thead>
          <tr>
            <th>On?</th><th>Email</th><th>Name</th><th>Country</th><th>Location</th><th>Joined</th><th>Last seen</th><th>Disabled apps</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {users_rows or "<tr><td colspan='9' class='muted'>No users yet. Clients must POST /api/register.</td></tr>"}
        </tbody>
      </table>

      <hr style="border:0;border-top:1px solid rgba(255,255,255,.08);margin:14px 0">

      <h4 style="margin:0 0 8px 0">Keepalive</h4>
      <p class="muted">Interval: {KEEPALIVE_INTERVAL}s • Keepalive enabled: {KEEPALIVE_ENABLE}</p>
      <p class="muted">External pings: {KEEPALIVE_URLS or 'none'}</p>
      <p class="muted">Check <code>/api/keepalive_status</code> for last heartbeat. Direct access to <a href="/babra.html" target="_blank">/babra.html</a> opens keepalive page.</p>
    </div>
  </div>
</body>
</html>"""

    return html

# -----------------------
# Admin: export users (CSV) and JSON
# -----------------------
@app.route(f"/{ADMIN_PATH}/export_users", methods=["GET"])
def admin_export_users_csv():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users ORDER BY last_seen DESC")
    rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "name", "country", "location", "created_at", "last_seen", "enabled", "disabled_apps", "meta"])
    for r in rows:
        writer.writerow([
            r["email"], r["name"], r["country"], r["location"], r["created_at"], r["last_seen"], r["enabled"], r["disabled_apps"], r["meta"]
        ])
    csv_bytes = output.getvalue().encode("utf-8")
    return Response(csv_bytes, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=users.csv"})


@app.route(f"/{ADMIN_PATH}/export_users.json", methods=["GET"])
def admin_export_users_json():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users ORDER BY last_seen DESC")
    rows = cur.fetchall()
    out = [row_to_user_dict(r) for r in rows]
    return jsonify({"ok": True, "users": out})

# -----------------------
# Admin API: users JSON (unprotected)
# -----------------------
@app.route(f"/{ADMIN_PATH}/users.json", methods=["GET"])
def admin_users_json():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users ORDER BY last_seen DESC")
    rows = cur.fetchall()
    out = [row_to_user_dict(r) for r in rows]
    return jsonify({"ok": True, "users": out})

# -----------------------
# Health
# -----------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "time": now_iso()}), 200

# -----------------------
# babra keepalive page + pixel (unchanged)
# -----------------------
@app.route("/babra.html")
def babra_page():
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>babra — keepalive</title>
  <meta name="theme-color" content="#4F46E5"/>
  <style>
    body{font-family:system-ui,Arial,Helvetica,sans-serif;margin:0;background:#0b1220;color:#e6eef6;display:flex;align-items:center;justify-content:center;height:100vh}
    .card{background:#071022;padding:20px;border-radius:12px;max-width:720px;width:92%;box-shadow:0 10px 30px rgba(0,0,0,0.6)}
    h1{margin:0 0 8px 0;font-size:18px}
    .muted{opacity:.8;font-size:13px}
    .row{display:flex;gap:8px;align-items:center;margin-top:12px}
    button{padding:8px 12px;border-radius:8px;border:0;background:#4f46e5;color:white;cursor:pointer}
    input{padding:6px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);background:transparent;color:inherit}
    pre{background:#071022;padding:8px;border-radius:8px;overflow:auto}
    label{min-width:90px}
  </style>
</head>
<body>
  <div class="card">
    <h1>babra — keepalive</h1>
    <div class="muted">This page will periodically ping <code>/api/health</code> and update a tiny UI. Use it to keep the app receiving requests.</div>

    <div class="row" style="margin-top:12px">
      <label for="interval">Interval (sec):</label>
      <input id="interval" type="number" min="10" value="180" style="width:96px"/>
      <button id="startBtn">Start</button>
      <button id="stopBtn" disabled>Stop</button>
    </div>

    <div style="margin-top:12px">
      <div class="muted">Last ping:</div>
      <pre id="lastPing">never</pre>
      <div class="muted" style="margin-top:8px">Keepalive status (optional):</div>
      <pre id="keepalive">loading…</pre>
    </div>

    <div style="margin-top:12px" class="muted">Tip: open this page on a remote machine (or an external monitor) to produce real inbound traffic.</div>
  </div>

<script>
(() => {
  const startBtn = document.getElementById('startBtn');
  const stopBtn  = document.getElementById('stopBtn');
  const intervalEl = document.getElementById('interval');
  const lastPing = document.getElementById('lastPing');
  const keepalive = document.getElementById('keepalive');

  let timer = null;

  async function doPing(){
    try{
      const url = '/api/health?_=' + Date.now();
      const res = await fetch(url, { cache: 'no-store', mode: 'same-origin' });
      const json = await res.json().catch(()=>({ ok:false }));
      lastPing.textContent = new Date().toISOString() + '  |  status: ' + (res.status || 'n/a') + '\n' + JSON.stringify(json);
    }catch(e){
      lastPing.textContent = new Date().toISOString() + '  |  ERROR: ' + String(e);
    }

    try{
      const ku = await fetch('/api/keepalive_status?_=' + Date.now(), { cache: 'no-store' });
      const kjson = await ku.json().catch(()=>null);
      keepalive.textContent = JSON.stringify(kjson, null, 2);
    }catch(e){
      keepalive.textContent = 'error: '+ String(e);
    }
  }

  function start(){
    stop();
    const secs = Math.max(10, Number(intervalEl.value) || 180);
    doPing();
    timer = setInterval(() => {
      const img = new Image();
      img.src = '/babra-pixel?_=' + Date.now();
      doPing();
    }, secs * 1000);
    startBtn.disabled = true;
    stopBtn.disabled = false;
  }
  function stop(){
    if(timer) { clearInterval(timer); timer = null; }
    startBtn.disabled = false;
    stopBtn.disabled = true;
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', stop);

  if(new URLSearchParams(location.search).get('autostart') === '1') start();
})();
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/babra-pixel")
def babra_pixel():
    gif_b64 = b"R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=="
    gif = base64.b64decode(gif_b64)
    return (gif, 200, {"Content-Type":"image/gif", "Cache-Control":"no-store"})

# -----------------------
# Bootstrap: start keepalive thread (if enabled)
# -----------------------
def start_keepalive_thread():
    if not KEEPALIVE_ENABLE:
        logger.info("KEEPALIVE_DISABLE set; not starting keepalive thread.")
        return None
    t = threading.Thread(target=background_heartbeat_loop, name="keepalive-thread", daemon=True)
    t.start()
    return t

# -----------------------
# CLI run
# -----------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    logger.info("Starting Tomorrow backend on http://%s:%s", APP_HOST, APP_PORT)
    logger.info("Admin: http://%s:%s/%s", APP_HOST, APP_PORT, ADMIN_PATH)
    if KEEPALIVE_ENABLE:
        start_keepalive_thread()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=APP_HOST, port=APP_PORT, debug=debug_mode)
