"""Self-hosted, multi-user rental search built around the Pararius watcher."""

from __future__ import annotations

import csv
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, current_app, g, jsonify, request, session
from dotenv import load_dotenv
import jwt
import pandas as pd
from werkzeug.security import check_password_hash, generate_password_hash

import geo
import house_detail
import house_finder as hf


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT)).expanduser()
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("RENTAL_DB_PATH", STATE_DIR / "rentals.db"))
PAGE = (ROOT / "saas.html").read_text(encoding="utf-8")
DEFAULT_SCRAPE_CONFIG = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

DEFAULT_PREFERENCES = {
    "cities": ["rotterdam"],
    "min_price": 1000,
    "max_price": 2300,
    "min_area": 40,
    "max_bedrooms": 2,
    "interiors": ["Furnished", "Upholstered"],
    "districts": [],
    "income_gross_monthly": 0,
    "move_in": "",
    "alerts": "instant",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=str(DB_PATH),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()
        if not os.environ.get("SECRET_KEY"):
            secret = setting("installation_secret")
            if not secret:
                secret = secrets.token_hex(32)
                get_db().execute(
                    "INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                    ("installation_secret", json.dumps(secret), now_iso()),
                )
                get_db().commit()
            app.config["SECRET_KEY"] = secret
        if not app.config.get("SKIP_LEGACY_IMPORT"):
            import_legacy_state()

    @app.errorhandler(403)
    @app.errorhandler(503)
    def handled_http_error(error):
        return api_error(error.description, error.code)

    @app.before_request
    def protect_state_changes():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return api_error("Cross-origin request rejected.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.get("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.post("/api/auth/register")
    def register():
        body = json_body()
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        name = body.get("name", "").strip()
        invite_token = body.get("invite_token", "").strip()
        if not name or "@" not in email or len(password) < 8:
            return api_error("Enter a name, valid email, and password of at least 8 characters.")
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return api_error("An account with this email already exists.", 409)
        has_users = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        invite = None
        if has_users:
            invite = db.execute(
                """SELECT * FROM invites
                   WHERE token = ? AND used_at IS NULL AND (email IS NULL OR lower(email) = ?)""",
                (invite_token, email),
            ).fetchone()
            if not invite:
                return api_error("A valid invite is required for this installation.", 403)
        try:
            cur = db.execute(
                """INSERT INTO users
                   (name, email, password_hash, role, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, email, generate_password_hash(password, method="pbkdf2:sha256:600000"),
                 "member" if has_users else "admin", now_iso()),
            )
            user_id = cur.lastrowid
            if invite:
                db.execute(
                    "UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?",
                    (now_iso(), user_id, invite["id"]),
                )
            db.execute(
                "INSERT INTO preferences (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
                (user_id, json.dumps(DEFAULT_PREFERENCES), now_iso()),
            )
            db.execute(
                """INSERT OR IGNORE INTO notifications
                   (user_id, listing_id, channel, status, created_at)
                   SELECT ?, id, 'email', 'baseline', ? FROM listings""",
                (user_id, now_iso()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return api_error("An account with this email already exists.", 409)
        session.clear()
        session["user_id"] = user_id
        return jsonify({"user": user_payload(user_id)}), 201

    @app.get("/api/setup")
    def setup_status():
        count = get_db().execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
        return jsonify({"needs_admin": count == 0, "invite_required": count > 0})

    @app.post("/api/auth/login")
    def login():
        body = json_body()
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (body.get("email", "").strip().lower(),)
        ).fetchone()
        if not user or not check_password_hash(user["password_hash"], body.get("password", "")):
            return api_error("Email or password is incorrect.", 401)
        session.clear()
        session["user_id"] = user["id"]
        return jsonify({"user": user_payload(user["id"])})

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/me")
    @login_required
    def me():
        return jsonify({"user": user_payload(g.user["id"])})

    @app.get("/api/preferences")
    @login_required
    def get_preferences():
        return jsonify({"preferences": preferences_for(g.user["id"])})

    @app.get("/api/mapkit-token")
    @login_required
    def mapkit_token():
        token = os.environ.get("MAPKIT_TOKEN", "").strip()
        if not token:
            team_id = os.environ.get("MAPKIT_TEAM_ID", "").strip()
            key_id = os.environ.get("MAPKIT_KEY_ID", "").strip()
            key_path = os.environ.get("MAPKIT_PRIVATE_KEY_PATH", "").strip()
            private_key = os.environ.get("MAPKIT_PRIVATE_KEY", "").strip().replace("\\n", "\n")
            origin = os.environ.get("MAPKIT_ORIGIN", "").strip() or request.host.split(":", 1)[0]
            if not all((team_id, key_id, origin)) or not (private_key or key_path):
                return api_error("Apple Maps is not configured yet.", 503)
            try:
                if not private_key:
                    private_key = Path(key_path).expanduser().read_text(encoding="utf-8")
                issued_at = int(time.time())
                token = jwt.encode(
                    {
                        "iss": team_id,
                        "iat": issued_at,
                        "exp": issued_at + 15 * 60,
                        "scope": "mapkit_js",
                        "origin": origin,
                    },
                    private_key,
                    algorithm="ES256",
                    headers={"kid": key_id, "typ": "JWT"},
                )
            except (OSError, ValueError):
                app.logger.exception("Unable to create MapKit token")
                return api_error("Apple Maps token generation failed.", 503)
        return jsonify({"token": token})

    @app.put("/api/preferences")
    @login_required
    def put_preferences():
        prefs = normalize_preferences(json_body())
        get_db().execute(
            """INSERT INTO preferences (user_id, settings_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json,
               updated_at=excluded.updated_at""",
            (g.user["id"], json.dumps(prefs), now_iso()),
        )
        get_db().commit()
        return jsonify({"preferences": prefs})

    @app.get("/api/listings")
    @login_required
    def listings():
        prefs = preferences_for(g.user["id"])
        rows = get_db().execute(
            """SELECT l.*, COALESCE(us.status, 'new') AS user_status,
                      us.notes AS user_notes, us.updated_at AS status_updated_at
               FROM listings l
               LEFT JOIN user_listing_state us
                 ON us.listing_id = l.id AND us.user_id = ?
               ORDER BY l.first_seen_at DESC, l.id DESC""",
            (g.user["id"],),
        ).fetchall()
        items = [listing_payload(row, prefs) for row in rows]
        items = [item for item in items if item["score"] >= 20 or item["status"] != "new"]
        return jsonify({"listings": items, "count": len(items)})

    @app.post("/api/listings/scrape")
    @login_required
    def scrape():
        return jsonify({"ok": True, "added": scrape_catalogue()})

    @app.get("/api/listings/<int:listing_id>")
    @login_required
    def listing_detail(listing_id):
        row = get_db().execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            return api_error("Listing not found.", 404)
        info = json.loads(row["detail_json"] or "{}")
        if not info:
            try:
                info = house_detail.listing_details(row["url"])
                get_db().execute(
                    "UPDATE listings SET detail_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(info), now_iso(), listing_id),
                )
                get_db().commit()
            except Exception:
                info = {}
        return jsonify({"listing": listing_payload(row, preferences_for(g.user["id"])), "detail": info})

    @app.put("/api/listings/<int:listing_id>/status")
    @login_required
    def update_status(listing_id):
        body = json_body()
        status = body.get("status", "")
        allowed = {"new", "saved", "applied", "viewing", "replied", "rejected", "ignored"}
        if status not in allowed:
            return api_error("Unknown listing status.")
        if not get_db().execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone():
            return api_error("Listing not found.", 404)
        get_db().execute(
            """INSERT INTO user_listing_state (user_id, listing_id, status, notes, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, listing_id) DO UPDATE SET status=excluded.status,
               notes=excluded.notes, updated_at=excluded.updated_at""",
            (g.user["id"], listing_id, status, body.get("notes", "")[:2000], now_iso()),
        )
        get_db().commit()
        return jsonify({"ok": True, "status": status})

    @app.post("/api/listings/<int:listing_id>/draft")
    @login_required
    def draft_response(listing_id):
        row = get_db().execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            return api_error("Listing not found.", 404)
        prefs = preferences_for(g.user["id"])
        user = g.user
        move = prefs.get("move_in") or "as soon as possible"
        income = prefs.get("income_gross_monthly") or 0
        income_line = (f"My gross monthly income is EUR {income:,.0f}, and I can provide my "
                       "employment contract and identification immediately.") if income else (
                       "I can provide my employment and income documents immediately."
        )
        message = f"""Hello,

I would like to request a viewing for {row['title']}.

My name is {user['name']}. I will be the sole tenant, with no children or pets. {income_line} I am looking to move {move} and am interested in a comfortable long-term home.

Please let me know whether a viewing is available and which documents you require.

Kind regards,
{user['name']}"""
        return jsonify({"message": message})

    @app.get("/api/admin/users")
    @login_required
    @admin_required
    def admin_users():
        rows = get_db().execute(
            "SELECT id, name, email, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return jsonify({"users": [dict(row) for row in rows]})

    @app.get("/api/admin/invites")
    @login_required
    @admin_required
    def admin_invites():
        rows = get_db().execute(
            """SELECT i.id, i.email, i.token, i.created_at, i.used_at, i.used_by,
                      u.email AS used_by_email
               FROM invites i LEFT JOIN users u ON u.id = i.used_by
               ORDER BY i.id DESC"""
        ).fetchall()
        return jsonify({"invites": [dict(row) for row in rows]})

    @app.post("/api/admin/invites")
    @login_required
    @admin_required
    def create_invite():
        email = json_body().get("email", "").strip().lower() or None
        if email and "@" not in email:
            return api_error("Enter a valid email or leave it blank.")
        token = secrets.token_urlsafe(24)
        cur = get_db().execute(
            "INSERT INTO invites (email, token, created_by, created_at) VALUES (?, ?, ?, ?)",
            (email, token, g.user["id"], now_iso()),
        )
        get_db().commit()
        return jsonify({"invite": {
            "id": cur.lastrowid, "email": email, "token": token,
            "url": f"{public_origin()}/?invite={token}", "created_at": now_iso(),
        }}), 201

    @app.delete("/api/admin/invites/<int:invite_id>")
    @login_required
    @admin_required
    def delete_invite(invite_id):
        get_db().execute("DELETE FROM invites WHERE id = ? AND used_at IS NULL", (invite_id,))
        get_db().commit()
        return jsonify({"ok": True})

    return app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_database())
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def current_database():
    from flask import current_app
    return current_app.config["DATABASE"]


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    get_db().executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'member',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preferences (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          settings_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          location TEXT NOT NULL DEFAULT '',
          price INTEGER NOT NULL DEFAULT 0,
          area INTEGER NOT NULL DEFAULT 0,
          rooms INTEGER NOT NULL DEFAULT 0,
          interior TEXT NOT NULL DEFAULT '',
          image TEXT NOT NULL DEFAULT '',
          district TEXT,
          source TEXT NOT NULL DEFAULT 'Pararius',
          detail_json TEXT NOT NULL DEFAULT '{}',
          latitude REAL,
          longitude REAL,
          geocoded_at TEXT,
          first_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_listing_state (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          notes TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          PRIMARY KEY (user_id, listing_id)
        );
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invites (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT,
          token TEXT NOT NULL UNIQUE,
          created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          used_at TEXT,
          used_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
          channel TEXT NOT NULL DEFAULT 'email',
          status TEXT NOT NULL DEFAULT 'pending',
          error TEXT,
          created_at TEXT NOT NULL,
          sent_at TEXT,
          UNIQUE(user_id, listing_id, channel)
        );
        CREATE INDEX IF NOT EXISTS listings_seen_idx ON listings(first_seen_at DESC);
        CREATE INDEX IF NOT EXISTS listing_state_user_idx ON user_listing_state(user_id, status);
        CREATE INDEX IF NOT EXISTS notification_status_idx ON notifications(status, created_at);
        CREATE INDEX IF NOT EXISTS invite_token_idx ON invites(token);
        """
    )
    ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'member'")
    ensure_column("listings", "latitude", "REAL")
    ensure_column("listings", "longitude", "REAL")
    ensure_column("listings", "geocoded_at", "TEXT")
    if not get_db().execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
        first = get_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first:
            get_db().execute("UPDATE users SET role = 'admin' WHERE id = ?", (first["id"],))
    get_db().execute(
        """INSERT OR IGNORE INTO app_settings (key, value_json, updated_at)
           VALUES ('scrape_config', ?, ?)""",
        (json.dumps(DEFAULT_SCRAPE_CONFIG), now_iso()),
    )
    get_db().commit()


def ensure_column(table, name, declaration):
    columns = {row["name"] for row in get_db().execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        get_db().execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def setting(key, default=None):
    row = get_db().execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def import_legacy_state():
    if setting("legacy_import_complete", False):
        return 0
    path = ROOT / "data.csv"
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else setting(
        "scrape_config", DEFAULT_SCRAPE_CONFIG
    )
    if cfg_path.exists():
        get_db().execute(
            "UPDATE app_settings SET value_json = ?, updated_at = ? WHERE key = 'scrape_config'",
            (json.dumps(cfg), now_iso()),
        )
    districts = cfg.get("criteria", {}).get("districts", {})
    count = 0
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                count += upsert_listing(row, districts)
    coordinate_path = ROOT / "geocache.json"
    if coordinate_path.exists():
        for url, coords in json.loads(coordinate_path.read_text(encoding="utf-8")).items():
            if coords and len(coords) == 2:
                get_db().execute(
                    """UPDATE listings SET latitude = ?, longitude = ?, geocoded_at = ?
                       WHERE url = ?""",
                    (coords[0], coords[1], now_iso(), url),
                )
    get_db().execute(
        """INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, 'true', ?)
           ON CONFLICT(key) DO UPDATE SET value_json = 'true', updated_at = excluded.updated_at""",
        ("legacy_import_complete", now_iso()),
    )
    get_db().commit()
    return count


def upsert_listing(row, districts):
    url = str(row.get("Link", "")).strip()
    if not url:
        return 0
    seen = now_iso()
    get_db().execute(
        """INSERT INTO listings
           (url, title, location, price, area, rooms, interior, image, district,
            first_seen_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET title=excluded.title, location=excluded.location,
           price=excluded.price, area=excluded.area, rooms=excluded.rooms,
           interior=excluded.interior, image=excluded.image, district=excluded.district,
           updated_at=excluded.updated_at""",
        (url, row.get("Title", ""), row.get("Location", ""), hf._int(row.get("Price")),
         hf._int(row.get("Surface Area")), hf._int(row.get("Rooms")),
         row.get("Interior", ""), row.get("Image", ""),
         hf.district_of(row.get("Location", ""), districts), seen, seen),
    )
    return 1


def catalogue_frame():
    rows = get_db().execute(
        "SELECT title, location, price, area, url, image, rooms, interior FROM listings"
    ).fetchall()
    return pd.DataFrame([{
        "Title": row["title"], "Location": row["location"], "Price": row["price"],
        "Surface Area": row["area"], "Link": row["url"], "Image": row["image"],
        "Rooms": row["rooms"], "Interior": row["interior"], "New": False, "Contacted": False,
    } for row in rows], columns=hf.COLUMNS)


def scrape_catalogue():
    cfg = setting("scrape_config", DEFAULT_SCRAPE_CONFIG)
    df = catalogue_frame()
    before = set(df["Link"]) if not df.empty else set()
    profiles = get_db().execute("SELECT settings_json FROM preferences").fetchall()
    searches = {
        (city, prefs["min_price"], prefs["max_price"])
        for profile in profiles
        for prefs in [{**DEFAULT_PREFERENCES, **json.loads(profile["settings_json"])}]
        for city in prefs["cities"]
    }
    if not searches:
        search = cfg["search"]
        searches = {
            (city, search["min_price"], search["max_price"]) for city in search["cities"]
        }
    for city, min_price, max_price in sorted(searches):
        for url in hf.build_search_urls([city], min_price, max_price):
            hf.scrape_pararius(df, url)
    hf.enrich(df, cfg)
    districts = cfg.get("criteria", {}).get("districts", {})
    for row in df.to_dict(orient="records"):
        upsert_listing(row, districts)
    get_db().commit()
    return len(set(df["Link"]) - before)


def normalize_preferences(body):
    prefs = dict(DEFAULT_PREFERENCES)
    prefs["cities"] = [str(x).lower()[:60] for x in body.get("cities", prefs["cities"]) if str(x).strip()][:5]
    prefs["min_price"] = max(0, min(int(body.get("min_price", 0)), 20000))
    prefs["max_price"] = max(prefs["min_price"], min(int(body.get("max_price", 2300)), 20000))
    prefs["min_area"] = max(0, min(int(body.get("min_area", 0)), 1000))
    prefs["max_bedrooms"] = max(0, min(int(body.get("max_bedrooms", 2)), 20))
    prefs["interiors"] = [x for x in body.get("interiors", []) if x in {"Furnished", "Upholstered", "Unfurnished"}]
    prefs["districts"] = [str(x)[:80] for x in body.get("districts", [])][:20]
    prefs["income_gross_monthly"] = max(0, min(int(body.get("income_gross_monthly", 0)), 1000000))
    prefs["move_in"] = str(body.get("move_in", ""))[:40]
    prefs["alerts"] = body.get("alerts") if body.get("alerts") in {"instant", "daily", "off"} else "instant"
    return prefs


def preferences_for(user_id):
    row = get_db().execute("SELECT settings_json FROM preferences WHERE user_id = ?", (user_id,)).fetchone()
    return {**DEFAULT_PREFERENCES, **(json.loads(row["settings_json"]) if row else {})}


def listing_payload(row, prefs):
    score, reasons, warnings = score_listing(row, prefs)
    coords = listing_coordinates(row)
    return {
        "id": row["id"], "url": row["url"], "title": row["title"],
        "location": row["location"], "price": row["price"], "area": row["area"],
        "rooms": row["rooms"], "bedrooms": max(row["rooms"] - 1, 0) if row["rooms"] else None,
        "interior": row["interior"], "image": row["image"], "district": row["district"],
        "source": row["source"], "status": row["user_status"] if "user_status" in row.keys() else "new",
        "notes": row["user_notes"] if "user_notes" in row.keys() else "",
        "score": score, "reasons": reasons, "warnings": warnings, "coords": coords,
        "first_seen_at": row["first_seen_at"],
    }


def listing_coordinates(row):
    if row["latitude"] is not None and row["longitude"] is not None:
        return [row["latitude"], row["longitude"]]
    if row["geocoded_at"]:
        return None
    if current_app.config.get("DISABLE_GEOCODING"):
        return None
    coords = geo.geocode(row["url"], row["title"], row["location"])
    get_db().execute(
        "UPDATE listings SET latitude = ?, longitude = ?, geocoded_at = ? WHERE id = ?",
        (coords[0] if coords else None, coords[1] if coords else None, now_iso(), row["id"]),
    )
    get_db().commit()
    return coords


def score_listing(row, prefs):
    score, reasons, warnings = 50, [], []
    price, area = row["price"], row["area"]
    if prefs["min_price"] <= price <= prefs["max_price"]:
        score += 18; reasons.append("Within budget")
    else:
        score -= min(35, int(abs(price - max(prefs["min_price"], min(price, prefs["max_price"]))) / 40))
        warnings.append("Outside budget")
    if area >= prefs["min_area"]:
        score += 12; reasons.append(f"{area} m2")
    else:
        score -= 18; warnings.append(f"Below {prefs['min_area']} m2")
    interior = (row["interior"] or "").lower()
    if any(value.lower() in interior for value in prefs["interiors"]):
        score += 10; reasons.append(row["interior"])
    elif interior:
        score -= 8; warnings.append(row["interior"])
    if prefs["districts"]:
        if row["district"] in prefs["districts"]:
            score += 10; reasons.append(row["district"])
        else:
            score -= 10; warnings.append("Outside preferred districts")
    bedrooms = max(row["rooms"] - 1, 0) if row["rooms"] else None
    if bedrooms is not None and bedrooms > prefs["max_bedrooms"]:
        score -= 12; warnings.append("Too many bedrooms")
    if prefs["income_gross_monthly"] and price * 3.5 > prefs["income_gross_monthly"]:
        warnings.append("May exceed 3.5x income rule"); score -= 12
    elif prefs["income_gross_monthly"]:
        reasons.append("Likely income fit"); score += 6
    return max(0, min(100, score)), reasons[:3], warnings[:3]


def user_payload(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "role": user["role"], "access_active": True,
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
        if not user:
            return api_error("Sign in required.", 401)
        g.user = user
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            return api_error("Administrator access required.", 403)
        return view(*args, **kwargs)
    return wrapped


def public_origin():
    configured = os.environ.get("APP_URL", "").rstrip("/")
    if configured:
        return configured
    parsed = urlparse(request.host_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def json_body():
    return request.get_json(silent=True) or {}


def api_error(message, status=400):
    return jsonify({"error": message}), status


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5051")), debug=False)
