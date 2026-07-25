"""Shared listing poller and per-user email alert worker."""

import os
import smtplib
import time
from email.message import EmailMessage

import saas
import house_finder as hf


def sync_catalogue():
    return saas.scrape_catalogue()


def queue_matches():
    db = saas.get_db()
    users = db.execute("SELECT id FROM users").fetchall()
    listings = db.execute("SELECT * FROM listings").fetchall()
    queued = 0
    for user in users:
        prefs = saas.preferences_for(user["id"])
        if prefs["alerts"] != "instant":
            continue
        for listing in listings:
            if db.execute(
                "SELECT 1 FROM notifications WHERE user_id=? AND listing_id=? AND channel='email'",
                (user["id"], listing["id"]),
            ).fetchone():
                continue
            score, _reasons, _warnings = saas.score_listing(listing, prefs)
            status = "pending" if score >= 65 else "filtered"
            db.execute(
                """INSERT INTO notifications
                   (user_id, listing_id, channel, status, created_at) VALUES (?, ?, 'email', ?, ?)""",
                (user["id"], listing["id"], status, saas.now_iso()),
            )
            queued += status == "pending"
    db.commit()
    return queued


def deliver_pending():
    host = os.environ.get("SMTP_HOST")
    if not host:
        return 0
    port = int(os.environ.get("SMTP_PORT", "587"))
    username, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("ALERT_FROM", username or "alerts@localhost")
    app_url = os.environ.get("APP_URL", "http://127.0.0.1:5051").rstrip("/")
    db = saas.get_db()
    rows = db.execute(
        """SELECT n.id, u.email, u.name, l.* FROM notifications n
           JOIN users u ON u.id=n.user_id JOIN listings l ON l.id=n.listing_id
           WHERE n.status='pending' ORDER BY n.id LIMIT 100"""
    ).fetchall()
    sent = 0
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.environ.get("SMTP_TLS", "true").lower() == "true":
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        for row in rows:
            message = EmailMessage()
            message["From"], message["To"] = sender, row["email"]
            message["Subject"] = f"New rental match: {row['title']} - EUR {row['price']:,}"
            message.set_content(
                f"Hi {row['name']},\n\nA new listing matches your Keyturn profile.\n\n"
                f"{row['title']}\nEUR {row['price']:,}/month - {row['area']} m2 - "
                f"{row['interior'] or 'interior not confirmed'}\n{row['location']}\n\n"
                f"Open in Keyturn: {app_url}/\nSource: {row['url']}\n"
            )
            try:
                smtp.send_message(message)
                db.execute("UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
                           (saas.now_iso(), row["id"]))
                sent += 1
            except Exception as exc:
                db.execute("UPDATE notifications SET status='failed', error=? WHERE id=?",
                           (str(exc)[:500], row["id"]))
    db.commit()
    return sent


def run_once():
    with saas.app.app_context():
        added = sync_catalogue()
        queued = queue_matches()
        sent = deliver_pending()
        print(f"catalogue +{added}; alerts queued {queued}; sent {sent}", flush=True)


if __name__ == "__main__":
    interval = max(60, int(os.environ.get("WATCH_INTERVAL_SECONDS", "240")))
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
        time.sleep(interval)
