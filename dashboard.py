"""Local house-hunting dashboard.

Serves an interactive map + listing board backed by the Pararius scraper, with
buttons to re-scrape, inspect a listing's details, and apply to an agent.

Run:  ../ParariusBot/.venv/bin/python dashboard.py   then open http://127.0.0.1:5000
"""
import io
import threading

from flask import Flask, jsonify, request, send_file, Response
import requests

import house_finder as hf
import house_detail
import contact_estate_agent
import browser
import geo

app = Flask(__name__)
_scrape_lock = threading.Lock()


def enrich(row):
    """Turn a raw candidate row into the shape the front-end wants."""
    price, area = hf._int(row["Price"]), hf._int(row["Surface Area"])
    return {
        "title": row["Title"],
        "location": row["Location"],
        "price": price,
        "area": area,
        "ppm": round(price / area) if area else 0,
        "kind": hf.kind_of(row["Link"]),
        "rooms": hf._int(row.get("Rooms")),
        "beds": max(hf._int(row.get("Rooms")) - 1, 0) if hf._int(row.get("Rooms")) else None,
        "interior": row.get("Interior") or "",
        "district": row.get("district"),
        "url": row["Link"],
        "image": row.get("Image") or "",
        "is_new": str(row.get("New")).strip().lower() in ("true", "1"),
        "contacted": str(row.get("Contacted")).strip().lower() in ("true", "1"),
        "coords": geo.geocode(row["Link"], row["Title"], row["Location"]),
    }


def candidates():
    cfg = hf.load_config()
    return [enrich(r) for r in hf.matching(hf.load_data(), cfg)]


@app.route("/api/candidates")
def api_candidates():
    return jsonify(candidates())


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if not _scrape_lock.acquire(blocking=False):
        return jsonify({"error": "scrape already running"}), 409
    try:
        cfg = hf.load_config()
        df = hf.load_data()
        before = len(df)
        s = cfg["search"]
        for url in hf.build_search_urls(s["cities"], s["min_price"], s["max_price"]):
            hf.scrape_pararius(df, url)  # curl_cffi fast path
        hf.enrich(df, cfg)  # backfill rooms + interior from detail pages
        df.to_csv(hf.DATA_FILE, index=False)
        return jsonify({"added": len(df) - before, "candidates": candidates()})
    finally:
        _scrape_lock.release()


@app.route("/api/detail", methods=["POST"])
def api_detail():
    url = request.json["url"]
    info = house_detail.listing_details(url)  # curl_cffi fast path
    # Empty result = Pararius origin didn't serve the page (currently slow/504).
    if not info.get("features") and not info.get("title"):
        return jsonify({"error": "Pararius couldn't serve this listing page right now "
                                 "(their server is slow). Try again in a bit."})
    return jsonify(info)


@app.route("/api/contact", methods=["POST"])
def api_contact():
    body = request.json
    url, message = body["url"], body["message"]
    dry = body.get("dry_run", False)
    cfg = hf.load_config()
    c = cfg["contact"]
    contact = contact_estate_agent.ContactDetails(
        c["firstname"], c["lastname"], c["email"], c["phone"], message)

    d = browser.make_driver(headless=False, profile=True)
    try:
        link = house_detail.house_details_scraper(url, driver=d)
        if not link:
            return jsonify({"ok": False, "error": "no contact link found"}), 400
        kind, contact_url = link
        if dry:
            sent = False
        elif kind == "viewing":
            sent = contact_estate_agent.set_viewing(contact_url, contact, driver=d)
        else:
            sent = contact_estate_agent.send_message_to_agent(contact_url, contact, driver=d)
    finally:
        d.quit()

    if sent:
        df = hf.load_data()
        df.loc[df["Link"] == url, "Contacted"] = True
        df.to_csv(hf.DATA_FILE, index=False)
    return jsonify({"ok": bool(sent), "kind": kind, "dry_run": dry})


@app.route("/img")
def img_proxy():
    """Proxy a Pararius CDN image (keeps the front-end simple, dodges hotlink quirks)."""
    src = request.args.get("u", "")
    if "fastly" not in src and "pararius" not in src:
        return "", 400
    r = requests.get(src, timeout=15)
    return send_file(io.BytesIO(r.content), mimetype=r.headers.get("content-type", "image/jpeg"))


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = open(__file__.replace("dashboard.py", "dashboard.html")).read()


if __name__ == "__main__":
    # 0.0.0.0 so phone/tablet on the tailnet can reach it (ntfy tap-to-apply).
    # Port 5050 — macOS AirPlay Receiver squats on 5000.
    app.run(host="0.0.0.0", port=5050, debug=False)
