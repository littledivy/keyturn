import os
import tempfile

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

os.environ.setdefault("RENTAL_DB_PATH", tempfile.mktemp(suffix=".db"))

import saas
import saas_worker


@pytest.fixture()
def client(tmp_path):
    app = saas.create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test-secret",
        "SKIP_LEGACY_IMPORT": True,
        "DISABLE_GEOCODING": True,
    })
    return app.test_client()


def register(client, email="person@example.com", name="Test Person"):
    return client.post("/api/auth/register", json={
        "name": name,
        "email": email,
        "password": "long-enough-password",
    })


def test_register_login_and_preferences(client):
    response = register(client)
    assert response.status_code == 201
    assert response.get_json()["user"]["access_active"] is True

    response = client.put("/api/preferences", json={
        "cities": ["Rotterdam"],
        "min_price": 1200,
        "max_price": 2100,
        "min_area": 50,
        "max_bedrooms": 1,
        "interiors": ["Furnished"],
        "districts": ["centrum"],
        "income_gross_monthly": 8000,
        "move_in": "2026-09-01",
        "alerts": "instant",
    })
    assert response.status_code == 200
    assert response.get_json()["preferences"]["max_price"] == 2100

    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={
        "email": "person@example.com", "password": "long-enough-password"
    }).status_code == 200


def test_duplicate_email_is_rejected(client):
    assert register(client).status_code == 201
    client.post("/api/auth/logout")
    assert register(client).status_code == 409


def test_mapkit_token_requires_login_and_configuration(client, monkeypatch):
    monkeypatch.delenv("MAPKIT_TOKEN", raising=False)
    monkeypatch.delenv("MAPKIT_TEAM_ID", raising=False)
    monkeypatch.delenv("MAPKIT_KEY_ID", raising=False)
    monkeypatch.delenv("MAPKIT_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("MAPKIT_PRIVATE_KEY", raising=False)
    assert client.get("/api/mapkit-token").status_code == 401
    register(client)
    assert client.get("/api/mapkit-token").status_code == 503


def test_mapkit_token_can_be_signed_dynamically(client, monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.delenv("MAPKIT_TOKEN", raising=False)
    monkeypatch.setenv("MAPKIT_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("MAPKIT_KEY_ID", "KEY1234567")
    monkeypatch.delenv("MAPKIT_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("MAPKIT_PRIVATE_KEY", private_pem.decode())
    monkeypatch.setenv("MAPKIT_ORIGIN", "127.0.0.1")
    register(client)

    response = client.get("/api/mapkit-token")

    assert response.status_code == 200
    token = response.get_json()["token"]
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY1234567"
    assert claims["scope"] == "mapkit_js"
    assert claims["origin"] == "127.0.0.1"


def test_first_user_is_admin_and_invites_next_user(client):
    first = register(client, "admin@example.com", "Admin")
    assert first.get_json()["user"]["role"] == "admin"
    invite = client.post("/api/admin/invites", json={"email": "member@example.com"})
    assert invite.status_code == 201
    token = invite.get_json()["invite"]["token"]

    client.post("/api/auth/logout")
    rejected = register(client, "member@example.com", "Member")
    assert rejected.status_code == 403
    accepted = client.post("/api/auth/register", json={
        "name": "Member",
        "email": "member@example.com",
        "password": "long-enough-password",
        "invite_token": token,
    })
    assert accepted.status_code == 201
    assert accepted.get_json()["user"]["role"] == "member"
    assert client.get("/api/admin/users").status_code == 403


def test_invite_is_one_time_and_email_scoped(client):
    register(client, "admin@example.com", "Admin")
    token = client.post(
        "/api/admin/invites", json={"email": "allowed@example.com"}
    ).get_json()["invite"]["token"]
    client.post("/api/auth/logout")
    wrong = client.post("/api/auth/register", json={
        "name": "Wrong", "email": "wrong@example.com",
        "password": "long-enough-password", "invite_token": token,
    })
    assert wrong.status_code == 403
    assert client.post("/api/auth/register", json={
        "name": "Allowed", "email": "allowed@example.com",
        "password": "long-enough-password", "invite_token": token,
    }).status_code == 201
    client.post("/api/auth/logout")
    assert client.post("/api/auth/register", json={
        "name": "Again", "email": "again@example.com",
        "password": "long-enough-password", "invite_token": token,
    }).status_code == 403


def test_listing_state_is_isolated_per_user(client):
    register(client, "one@example.com", "One")
    app = client.application
    with app.app_context():
        db = saas.get_db()
        db.execute(
            """INSERT INTO listings
               (url,title,location,price,area,rooms,interior,image,district,first_seen_at,updated_at)
               VALUES ('https://example.com/a','Apartment A','Rotterdam',1800,60,2,
                       'Furnished','','centrum',?,?)""",
            (saas.now_iso(), saas.now_iso()),
        )
        db.commit()

    listing_id = client.get("/api/listings").get_json()["listings"][0]["id"]
    assert client.put(f"/api/listings/{listing_id}/status", json={"status": "applied"}).status_code == 200
    assert client.get("/api/listings").get_json()["listings"][0]["status"] == "applied"

    client.post("/api/auth/logout")
    with app.app_context():
        token = saas.get_db().execute(
            "SELECT token FROM invites WHERE used_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not token:
            token_value = "test-invite-token"
            saas.get_db().execute(
                "INSERT INTO invites (token, created_by, created_at) VALUES (?, 1, ?)",
                (token_value, saas.now_iso()),
            )
            saas.get_db().commit()
        else:
            token_value = token["token"]
    client.post("/api/auth/register", json={
        "name": "Two", "email": "two@example.com",
        "password": "long-enough-password", "invite_token": token_value,
    })
    assert client.get("/api/listings").get_json()["listings"][0]["status"] == "new"


def test_listing_scoring_flags_income_rule(client):
    prefs = {**saas.DEFAULT_PREFERENCES, "income_gross_monthly": 5000}
    row = {"price": 2000, "area": 60, "rooms": 2, "interior": "Furnished", "district": "centrum"}
    score, reasons, warnings = saas.score_listing(row, prefs)
    assert score < 100
    assert "May exceed 3.5x income rule" in warnings


def test_worker_queues_new_match_once(client):
    register(client)
    app = client.application
    with app.app_context():
        db = saas.get_db()
        db.execute(
            """INSERT INTO listings
               (url,title,location,price,area,rooms,interior,image,district,first_seen_at,updated_at)
               VALUES ('https://example.com/new','New Home','Rotterdam',1800,60,2,
                       'Furnished','','centrum',?,?)""",
            (saas.now_iso(), saas.now_iso()),
        )
        db.commit()
        assert saas_worker.queue_matches() == 1
        assert saas_worker.queue_matches() == 0
        note = db.execute("SELECT status FROM notifications WHERE channel='email'").fetchone()
        assert note["status"] == "pending"
