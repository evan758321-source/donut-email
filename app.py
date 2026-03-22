import os
import sqlite3
import smtplib
import threading
import time
import json
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, session
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "donutsmp-super-secret-2024-key")
app.permanent_session_lifetime = timedelta(hours=12)

# ─── Configuration ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "pp88")
API_BASE = "https://api.donutsmp.net"
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

API_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

LEADERBOARD_TYPES = [
    "brokenblocks", "deaths", "kills", "mobskilled",
    "money", "placedblocks", "playtime", "sell", "shards", "shop"
]

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "donutsmp.db")

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            search_term TEXT,
            seller_name TEXT,
            threshold_price REAL,
            email_to TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_triggered TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER,
            alert_name TEXT,
            triggered_at TEXT NOT NULL,
            message TEXT NOT NULL,
            item_name TEXT,
            price REAL,
            seller TEXT
        );

        CREATE TABLE IF NOT EXISTS auction_snapshot (
            snapshot_key TEXT PRIMARY KEY,
            item_name TEXT NOT NULL,
            price REAL NOT NULL,
            seller TEXT NOT NULL,
            count INTEGER,
            enchants TEXT,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at TEXT NOT NULL,
            items_found INTEGER DEFAULT 0,
            alerts_triggered INTEGER DEFAULT 0,
            error TEXT
        );
    """)
    db.commit()
    db.close()

init_db()

# ─── Auth Decorators ──────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Unauthorized", "status": 401}), 401
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"error": "Admin privileges required", "status": 403}), 403
        return f(*args, **kwargs)
    return decorated

# ─── Email ────────────────────────────────────────────────────────────────────
def send_email(to_addr, subject, html_body):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("[EMAIL] SMTP not configured, skipping email")
        return False, "SMTP not configured"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🍩 DonutSMP | {subject}"
        msg["From"] = SMTP_USERNAME
        msg["To"] = to_addr
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, to_addr, msg.as_string())
        return True, "OK"
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False, str(e)

def build_email_html(title, rows, extra=""):
    rows_html = "".join(
        f"<tr><td style='padding:8px 12px;color:#aaa;border-bottom:1px solid #333'>{k}</td>"
        f"<td style='padding:8px 12px;color:#fff;border-bottom:1px solid #333'><b>{v}</b></td></tr>"
        for k, v in rows
    )
    return f"""<!DOCTYPE html>
<html><body style='margin:0;padding:0;background:#0f0f0f;font-family:system-ui,sans-serif;'>
<div style='max-width:560px;margin:32px auto;background:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #333;'>
  <div style='background:linear-gradient(135deg,#FF6B35,#FF8C42);padding:24px 28px;'>
    <h1 style='margin:0;color:#fff;font-size:22px;'>🍩 DonutSMP Alert</h1>
    <p style='margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:14px;'>{title}</p>
  </div>
  <table style='width:100%;border-collapse:collapse;margin-top:0;'>{rows_html}</table>
  {extra}
  <div style='padding:16px 28px;color:#555;font-size:12px;border-top:1px solid #333;'>
    Sent by DonutSMP Dashboard • {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
  </div>
</div>
</body></html>"""

# ─── API Helpers ──────────────────────────────────────────────────────────────
def api_get(path, json_body=None):
    try:
        kwargs = {"headers": API_HEADERS, "timeout": 10}
        if json_body:
            kwargs["json"] = json_body
        r = requests.get(f"{API_BASE}{path}", **kwargs)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e), "status": 500}, 500

def api_put(path, json_body=None):
    try:
        r = requests.put(f"{API_BASE}{path}", headers=API_HEADERS, json=json_body, timeout=10)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e), "status": 500}, 500

# ─── Background Poller ────────────────────────────────────────────────────────
poll_lock = threading.Lock()
last_poll_info = {"time": None, "items": 0, "alerts": 0, "error": None}

def fetch_all_auction_items():
    all_items = []
    for page in range(1, 11):
        data, code = api_get(f"/v1/auction/list/{page}")
        if code != 200 or not data.get("result"):
            break
        items = data["result"]
        if not items:
            break
        all_items.extend(items)
    return all_items

def format_enchants(enchants):
    if not enchants:
        return ""
    try:
        levels = enchants.get("enchantments", {}).get("levels", {})
        parts = [f"{k} {v}" for k, v in levels.items()]
        return ", ".join(parts)
    except Exception:
        return ""

def build_snapshot_key(item):
    return f"{item['seller']['name']}::{item['item']['display_name']}::{item['price']}"

def check_alerts_and_poll():
    global last_poll_info
    db = None
    try:
        db = get_db()
        alerts = db.execute("SELECT * FROM alerts WHERE enabled = 1").fetchall()
        alerts = [dict(a) for a in alerts]

        all_items = fetch_all_auction_items()
        if not all_items:
            with poll_lock:
                last_poll_info = {"time": datetime.now().isoformat(), "items": 0, "alerts": 0, "error": "No items returned"}
            return

        # Build current snapshot keys
        current_keys = set()
        current_by_name = {}  # display_name -> list of (price, seller, key)
        for item in all_items:
            key = build_snapshot_key(item)
            current_keys.add(key)
            name = item["item"]["display_name"]
            if name not in current_by_name:
                current_by_name[name] = []
            current_by_name[name].append({
                "key": key,
                "price": item["price"],
                "seller": item["seller"]["name"],
                "count": item["item"]["count"],
                "enchants": format_enchants(item["item"].get("enchants")),
                "time_left": item.get("time_left", 0)
            })

        # Get known snapshot keys from DB
        known_rows = db.execute("SELECT snapshot_key FROM auction_snapshot").fetchall()
        known_keys = {r["snapshot_key"] for r in known_rows}
        new_keys = current_keys - known_keys

        alerts_triggered = 0

        for alert in alerts:
            triggered = False
            trigger_msg = ""
            trigger_item = ""
            trigger_price = 0.0
            trigger_seller = ""

            atype = alert["type"]
            search = (alert.get("search_term") or "").lower()
            seller_filter = (alert.get("seller_name") or "").lower()
            threshold = alert.get("threshold_price")

            if atype == "price_drop":
                # Alert when item matching search is listed below threshold price
                if search and threshold is not None:
                    for name, entries in current_by_name.items():
                        if search in name.lower():
                            for entry in entries:
                                if entry["price"] <= threshold:
                                    triggered = True
                                    trigger_item = name
                                    trigger_price = entry["price"]
                                    trigger_seller = entry["seller"]
                                    trigger_msg = (
                                        f"'{name}' is listed at ${entry['price']:,.2f} "
                                        f"(threshold: ${threshold:,.2f}) by {entry['seller']}"
                                    )
                                    break
                        if triggered:
                            break

            elif atype == "price_decrease":
                # Alert when an item's price decreases from what we last saw
                if search:
                    for name, entries in current_by_name.items():
                        if search in name.lower():
                            for entry in entries:
                                old_row = db.execute(
                                    "SELECT price FROM auction_snapshot WHERE snapshot_key = ?",
                                    (entry["key"],)
                                ).fetchone()
                                # Look for any older listing of same item at higher price
                                old_best = db.execute(
                                    "SELECT MIN(price) as min_price FROM auction_snapshot WHERE item_name = ?",
                                    (name,)
                                ).fetchone()
                                if old_best and old_best["min_price"] and entry["price"] < old_best["min_price"]:
                                    triggered = True
                                    trigger_item = name
                                    trigger_price = entry["price"]
                                    trigger_seller = entry["seller"]
                                    trigger_msg = (
                                        f"'{name}' price dropped! New low: ${entry['price']:,.2f} "
                                        f"(was ${old_best['min_price']:,.2f}) by {entry['seller']}"
                                    )
                                    break
                        if triggered:
                            break

            elif atype == "seller_alert":
                # Alert when specific seller lists anything new
                if seller_filter:
                    for key in new_keys:
                        for item in all_items:
                            if build_snapshot_key(item) == key:
                                if item["seller"]["name"].lower() == seller_filter:
                                    triggered = True
                                    trigger_item = item["item"]["display_name"]
                                    trigger_price = item["price"]
                                    trigger_seller = item["seller"]["name"]
                                    trigger_msg = (
                                        f"{trigger_seller} listed '{trigger_item}' "
                                        f"for ${trigger_price:,.2f}"
                                    )
                                    break
                        if triggered:
                            break

            elif atype == "new_item_listing":
                # Alert when any new listing matches search term
                if search:
                    for key in new_keys:
                        for item in all_items:
                            if build_snapshot_key(item) == key:
                                if search in item["item"]["display_name"].lower():
                                    triggered = True
                                    trigger_item = item["item"]["display_name"]
                                    trigger_price = item["price"]
                                    trigger_seller = item["seller"]["name"]
                                    trigger_msg = (
                                        f"New listing: '{trigger_item}' "
                                        f"for ${trigger_price:,.2f} by {trigger_seller}"
                                    )
                                    break
                        if triggered:
                            break

            elif atype == "any_new_listing":
                # Alert on any new listing
                if new_keys:
                    key = next(iter(new_keys))
                    for item in all_items:
                        if build_snapshot_key(item) == key:
                            triggered = True
                            trigger_item = item["item"]["display_name"]
                            trigger_price = item["price"]
                            trigger_seller = item["seller"]["name"]
                            trigger_msg = (
                                f"New AH listing: '{trigger_item}' "
                                f"for ${trigger_price:,.2f} by {trigger_seller}"
                            )
                            break

            if triggered:
                now_str = datetime.now().isoformat()
                email_html = build_email_html(
                    alert["name"],
                    [
                        ("Alert Type", atype.replace("_", " ").title()),
                        ("Item", trigger_item),
                        ("Price", f"${trigger_price:,.2f}"),
                        ("Seller", trigger_seller),
                        ("Triggered At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        ("Message", trigger_msg),
                    ]
                )
                send_email(alert["email_to"], alert["name"], email_html)
                db.execute(
                    "UPDATE alerts SET last_triggered = ? WHERE id = ?",
                    (now_str, alert["id"])
                )
                db.execute(
                    """INSERT INTO alert_history
                       (alert_id, alert_name, triggered_at, message, item_name, price, seller)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (alert["id"], alert["name"], now_str, trigger_msg, trigger_item, trigger_price, trigger_seller)
                )
                alerts_triggered += 1

        # Update snapshot table
        now_str = datetime.now().isoformat()
        for item in all_items:
            key = build_snapshot_key(item)
            db.execute(
                """INSERT OR REPLACE INTO auction_snapshot
                   (snapshot_key, item_name, price, seller, count, enchants, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    item["item"]["display_name"],
                    item["price"],
                    item["seller"]["name"],
                    item["item"]["count"],
                    format_enchants(item["item"].get("enchants")),
                    now_str
                )
            )

        # Prune stale snapshot entries (not seen in >1 hour)
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        db.execute("DELETE FROM auction_snapshot WHERE last_seen < ?", (cutoff,))

        db.execute(
            "INSERT INTO poll_log (polled_at, items_found, alerts_triggered) VALUES (?, ?, ?)",
            (now_str, len(all_items), alerts_triggered)
        )
        db.commit()

        with poll_lock:
            last_poll_info = {
                "time": now_str,
                "items": len(all_items),
                "alerts": alerts_triggered,
                "error": None
            }

    except Exception as e:
        err = str(e)
        print(f"[POLL ERROR] {err}")
        if db:
            try:
                db.execute(
                    "INSERT INTO poll_log (polled_at, items_found, alerts_triggered, error) VALUES (?, 0, 0, ?)",
                    (datetime.now().isoformat(), err)
                )
                db.commit()
            except Exception:
                pass
        with poll_lock:
            last_poll_info["error"] = err
    finally:
        if db:
            db.close()

def poller_loop():
    while True:
        check_alerts_and_poll()
        time.sleep(10)

poller_thread = threading.Thread(target=poller_loop, daemon=True)
poller_thread.start()

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    password = data.get("password", "")
    if ADMIN_PASSWORD and password == ADMIN_PASSWORD:
        session.permanent = True
        session["authenticated"] = True
        session["admin"] = True
        return jsonify({"success": True, "role": "admin"})
    elif DASHBOARD_PASSWORD and password == DASHBOARD_PASSWORD:
        session.permanent = True
        session["authenticated"] = True
        session["admin"] = False
        return jsonify({"success": True, "role": "user"})
    return jsonify({"success": False, "error": "Invalid password"}), 401

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/auth/status")
def auth_status():
    return jsonify({
        "authenticated": bool(session.get("authenticated")),
        "admin": bool(session.get("admin"))
    })

# ─── Dashboard / Utility Routes ───────────────────────────────────────────────
@app.route("/api/dashboard/status")
@require_auth
def dashboard_status():
    db = get_db()
    snapshot_count = db.execute("SELECT COUNT(*) as c FROM auction_snapshot").fetchone()["c"]
    active_alerts = db.execute("SELECT COUNT(*) as c FROM alerts WHERE enabled = 1").fetchone()["c"]
    total_alerts = db.execute("SELECT COUNT(*) as c FROM alerts").fetchone()["c"]
    history_count = db.execute("SELECT COUNT(*) as c FROM alert_history").fetchone()["c"]
    recent_history = db.execute(
        "SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT 5"
    ).fetchall()
    poll_logs = db.execute(
        "SELECT * FROM poll_log ORDER BY polled_at DESC LIMIT 10"
    ).fetchall()
    db.close()
    with poll_lock:
        info = dict(last_poll_info)
    return jsonify({
        "snapshot_count": snapshot_count,
        "active_alerts": active_alerts,
        "total_alerts": total_alerts,
        "history_count": history_count,
        "recent_history": [dict(r) for r in recent_history],
        "poll_logs": [dict(r) for r in poll_logs],
        "last_poll": info
    })

# ─── Auction Routes ───────────────────────────────────────────────────────────
@app.route("/api/auction/list/<int:page>", methods=["GET", "POST"])
@require_auth
def auction_list(page):
    body = request.get_json() if request.method == "POST" else None
    data, code = api_get(f"/v1/auction/list/{page}", json_body=body)
    return jsonify(data), code

@app.route("/api/auction/transactions/<int:page>")
@require_auth
def auction_transactions(page):
    data, code = api_get(f"/v1/auction/transactions/{page}")
    return jsonify(data), code

# ─── Leaderboard Routes ───────────────────────────────────────────────────────
@app.route("/api/leaderboards/<lb_type>/<int:page>")
@require_auth
def leaderboard(lb_type, page):
    if lb_type not in LEADERBOARD_TYPES:
        return jsonify({"error": "Invalid leaderboard type"}), 400
    data, code = api_get(f"/v1/leaderboards/{lb_type}/{page}")
    return jsonify(data), code

# ─── Lookup & Stats Routes ────────────────────────────────────────────────────
@app.route("/api/lookup/<user>")
@require_auth
def lookup(user):
    data, code = api_get(f"/v1/lookup/{user}")
    return jsonify(data), code

@app.route("/api/stats/<user>")
@require_auth
def stats(user):
    data, code = api_get(f"/v1/stats/{user}")
    return jsonify(data), code

# ─── Shield Routes (Admin Only) ───────────────────────────────────────────────
@app.route("/api/shield/metrics/<service>")
@require_auth
@require_admin
def shield_metrics(service):
    data, code = api_get(f"/v1/shield/metrics/{service}")
    return jsonify(data), code

@app.route("/api/shield/stats/<service>")
@require_auth
@require_admin
def shield_stats_route(service):
    data, code = api_get(f"/v1/shield/stats/{service}")
    return jsonify(data), code

@app.route("/api/shield/bedrock/config/<service>", methods=["GET", "PUT"])
@require_auth
@require_admin
def shield_bedrock_config(service):
    if request.method == "GET":
        data, code = api_get(f"/v1/shield/bedrock/config/{service}")
    else:
        data, code = api_put(f"/v1/shield/bedrock/config/{service}", json_body=request.get_json())
    return jsonify(data), code

@app.route("/api/shield/java/config/<service>", methods=["GET", "PUT"])
@require_auth
@require_admin
def shield_java_config(service):
    if request.method == "GET":
        data, code = api_get(f"/v1/shield/java/config/{service}")
    else:
        data, code = api_put(f"/v1/shield/java/config/{service}", json_body=request.get_json())
    return jsonify(data), code

# ─── Alert CRUD Routes ────────────────────────────────────────────────────────
@app.route("/api/alerts", methods=["GET"])
@require_auth
def get_alerts():
    db = get_db()
    rows = db.execute("SELECT * FROM alerts ORDER BY created_at DESC").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts", methods=["POST"])
@require_auth
def create_alert():
    data = request.get_json() or {}
    required = ["name", "type", "email_to"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"Field '{f}' is required"}), 400
    if data["type"] not in ["price_drop", "price_decrease", "seller_alert", "new_item_listing", "any_new_listing"]:
        return jsonify({"error": "Invalid alert type"}), 400
    db = get_db()
    db.execute(
        """INSERT INTO alerts (name, type, search_term, seller_name, threshold_price, email_to, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            data["name"], data["type"],
            data.get("search_term") or None,
            data.get("seller_name") or None,
            data.get("threshold_price") or None,
            data["email_to"],
            datetime.now().isoformat()
        )
    )
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@require_auth
def delete_alert(alert_id):
    db = get_db()
    db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    db.execute("DELETE FROM alert_history WHERE alert_id = ?", (alert_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts/<int:alert_id>", methods=["PUT"])
@require_auth
def update_alert(alert_id):
    data = request.get_json() or {}
    db = get_db()
    db.execute(
        """UPDATE alerts SET name=?, type=?, search_term=?, seller_name=?,
           threshold_price=?, email_to=? WHERE id=?""",
        (
            data.get("name"), data.get("type"),
            data.get("search_term") or None,
            data.get("seller_name") or None,
            data.get("threshold_price") or None,
            data.get("email_to"),
            alert_id
        )
    )
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts/<int:alert_id>/toggle", methods=["POST"])
@require_auth
def toggle_alert(alert_id):
    db = get_db()
    db.execute("UPDATE alerts SET enabled = 1 - enabled WHERE id = ?", (alert_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/alerts/history", methods=["GET"])
@require_auth
def alert_history():
    page = int(request.args.get("page", 1))
    offset = (page - 1) * 25
    db = get_db()
    rows = db.execute(
        "SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT 25 OFFSET ?",
        (offset,)
    ).fetchall()
    total = db.execute("SELECT COUNT(*) as c FROM alert_history").fetchone()["c"]
    db.close()
    return jsonify({"history": [dict(r) for r in rows], "total": total, "page": page})

@app.route("/api/alerts/history/clear", methods=["DELETE"])
@require_auth
def clear_alert_history():
    db = get_db()
    db.execute("DELETE FROM alert_history")
    db.commit()
    db.close()
    return jsonify({"success": True})

# ─── Settings / Email Routes ──────────────────────────────────────────────────
@app.route("/api/email/test", methods=["POST"])
@require_auth
def test_email():
    data = request.get_json() or {}
    to_addr = data.get("email", SMTP_USERNAME)
    if not to_addr:
        return jsonify({"success": False, "error": "No email address provided"}), 400
    html = build_email_html(
        "Test Email — Everything is working!",
        [
            ("Status", "✅ Connected"),
            ("SMTP Host", "smtp.gmail.com:587"),
            ("Sender", SMTP_USERNAME or "Not configured"),
            ("Sent At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ],
        extra="<div style='padding:16px 28px;color:#aaa;font-size:13px;'>Your DonutSMP Dashboard email alerts are configured and working correctly.</div>"
    )
    ok, err = send_email(to_addr, "Test Email", html)
    return jsonify({"success": ok, "error": err if not ok else None})

@app.route("/api/settings/info")
@require_auth
def settings_info():
    return jsonify({
        "api_key": API_KEY[:4] + "****" if len(API_KEY) > 4 else "****",
        "smtp_configured": bool(SMTP_USERNAME and SMTP_PASSWORD),
        "smtp_username": SMTP_USERNAME if SMTP_USERNAME else None,
        "api_base": API_BASE,
        "poll_interval_seconds": 10
    })

# ─── Poll Logs ────────────────────────────────────────────────────────────────
@app.route("/api/poll/logs")
@require_auth
def poll_logs():
    db = get_db()
    logs = db.execute(
        "SELECT * FROM poll_log ORDER BY polled_at DESC LIMIT 20"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in logs])

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
