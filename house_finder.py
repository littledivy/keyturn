"""Scrape Pararius apartment listings and auto-contact estate agents.

This is the main entry point. It scrapes each configured city/price
combination, stores new listings in a CSV tracker, and contacts agents
for any listing that hasn't been contacted yet.
"""

import os
from bs4 import BeautifulSoup
import pandas as pd
from house import House
from contact_estate_agent import ContactDetails
import house_detail
import contact_estate_agent
import browser

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
import urllib.parse
import urllib.request

BASE_URL = "https://www.pararius.com/apartments"
CONFIG_FILE = "config.json"
DATA_FILE = "data.csv"
COLUMNS = ["Title", "Location", "Price", "Surface Area", "Link", "Image", "Rooms", "Interior", "New", "Contacted"]


def load_config(path=CONFIG_FILE):
    """Load search + contact settings from config.json."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_data():
    """Load the listing tracker CSV, or an empty frame with the right columns."""
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE, keep_default_na=False)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = False if col in ("Contacted", "New") else (0 if col == "Rooms" else "")
        df = df[COLUMNS]
        return df[COLUMNS]
    return pd.DataFrame(columns=COLUMNS)


def build_search_urls(cities, min_price, max_price):
    """Build Pararius search URLs from the city list and price range."""
    return [f"{BASE_URL}/{city}/{min_price}-{max_price}" for city in cities]


def scrape_pararius(dataframe, url, driver=None):
    """Scrape a single Pararius search page and append new listings to *dataframe*.

    Args:
        dataframe: pandas DataFrame that tracks all known listings.
        url: Pararius search URL to scrape.
        driver: optional shared Selenium driver to reuse across pages.

    Returns:
        The updated DataFrame (rows are added in-place as well).
    """
    html = browser.get_html(url, driver=driver)
    soup = BeautifulSoup(html, "html.parser")
    listings = soup.find_all("section", class_="listing-search-item")

    if not listings:
        print(f"No listings parsed for {url} (blocked or empty)")
        return dataframe

    for listing in listings:
        title = listing.find("a", class_="listing-search-item__link--title").text.strip()
        location = listing.find("div", class_="listing-search-item__sub-title").text.strip()
        price = listing.find("div", class_="listing-search-item__price").text.strip()
        surface_area = listing.find(
            "li",
            class_="illustrated-features__item illustrated-features__item--surface-area",
        ).text.strip()
        link = listing.find("a", class_="listing-search-item__link")["href"]

        img_el = listing.find("img")
        image = ""
        if img_el:
            image = img_el.get("src") or img_el.get("data-src") or ""
            if not image and img_el.get("srcset"):
                image = img_el["srcset"].split()[0]

        is_new = listing.find(class_="listing-label--new") is not None

        rooms_el = listing.find(class_="illustrated-features__item--number-of-rooms")
        rooms = _int(rooms_el.get_text()) if rooms_el else 0

        house = House(
            title,
            location,
            price,
            surface_area,
            "https://www.pararius.com" + link,
            image=image,
            rooms=rooms,
            is_new=is_new,
        )

        mask = dataframe["Link"] == house.link
        if mask.any():
            # Already tracked: refresh metadata but keep the Contacted flag.
            idx = dataframe.index[mask][0]
            for col, val in zip(
                ["Price", "Surface Area", "Image", "Rooms", "New"],
                [house.price, house.surface_area, house.image, house.rooms, house.is_new],
            ):
                dataframe.at[idx, col] = val
        else:
            dataframe.loc[len(dataframe)] = house.to_list()

    return dataframe


def _int(text):
    """First integer in a string ('€1,066 pcm' -> 1066), or 0."""
    if pd.isna(text):
        return 0
    m = re.search(r"\d[\d.,]*", str(text))
    return int(re.sub(r"[.,]", "", m.group(0))) if m else 0


def _text(value):
    """Normalized text for optional CSV fields; pandas may read blanks as NaN."""
    if pd.isna(value):
        return ""
    return str(value).strip()


def _truthy(value):
    """Parse persisted bool-ish CSV values without treating 'False' as true."""
    return _text(value).lower() in ("true", "1", "yes")


def _street(title):
    """Strip the type prefix from a listing title ('Flat Statenweg 79 B' -> 'Statenweg 79 B')."""
    return re.sub(r"^(Flat|Studio|Apartment|House|Room)\s+", "", title or "").strip()


def district_of(location, districts):
    """Classify a listing location into one of the configured districts, or None."""
    loc = (location or "").lower()
    for name, hoods in districts.items():
        if any(h.lower() in loc for h in hoods):
            return name
    return None


def kind_of(link):
    """Derive listing kind from its Pararius URL slug."""
    if "/room-for-rent/" in link:
        return "room"
    if "/studio-for-rent/" in link:
        return "studio"
    if "/house-for-rent/" in link:
        return "house"
    return "apartment"


def passes_criteria(row, cfg):
    """True if a listing row meets the configured kind/area/district filters."""
    crit = cfg.get("criteria", {})
    if kind_of(row["Link"]) in crit.get("exclude_kinds", []):
        return False
    if crit.get("min_area_m2") and _int(row.get("Surface Area")) < crit["min_area_m2"]:
        return False
    # Bedrooms aren't on the search list, but rooms are; bedrooms ≈ rooms − 1
    # (one room is the living room). Only filter when we actually know the count.
    max_bed = crit.get("max_bedrooms")
    rooms = _int(row.get("Rooms"))
    if max_bed is not None and rooms and (rooms - 1) > max_bed:
        return False
    # Interior comes from the detail page (backfilled by enrich); only filter
    # once known, so un-enriched rows still show up.
    allowed = crit.get("interior_allowed")
    interior = _text(row.get("Interior")).lower()
    if allowed and interior and not any(a.lower() in interior for a in allowed):
        return False
    districts = crit.get("districts")
    if districts and district_of(row.get("Location"), districts) is None:
        return False
    return True


def uncontacted(df, cfg=None):
    """Return not-yet-contacted rows as dicts, filtered by criteria when cfg given."""
    if df.empty:
        return []
    rows = df[~df["Contacted"].apply(_truthy)][COLUMNS[:-1]].to_dict(orient="records")
    if cfg is None:
        return rows
    out = []
    for r in rows:
        if passes_criteria(r, cfg):
            r["district"] = district_of(r.get("Location"), cfg["criteria"].get("districts", {}))
            out.append(r)
    return out


def enrich(df, cfg, force=False):
    """Backfill exact rooms + interior from each candidate's detail page.

    Only fetches uncontacted rows that pass the non-interior filters and still
    lack an Interior value (unless force). curl_cffi makes each fetch ~1s.
    """
    for idx, row in df.iterrows():
        if _truthy(row.get("Contacted")):
            continue
        if not force and _text(row.get("Interior")):
            continue
        if not passes_criteria(row, cfg):  # skip ones already excluded by price/district/etc.
            continue
        try:
            info = house_detail.listing_details(row["Link"])
        except Exception:
            continue
        feats = info.get("features", {})
        rooms = _int(feats.get("Number of rooms"))
        interior = (feats.get("Interior") or "").split(" More")[0].strip()
        if rooms:
            df.at[idx, "Rooms"] = rooms
        if interior:
            df.at[idx, "Interior"] = interior
    return df


def matching(df, cfg):
    """Return all criteria-matching rows (contacted included), each with district set."""
    if df.empty:
        return []
    out = []
    for r in df[COLUMNS].to_dict(orient="records"):
        if passes_criteria(r, cfg):
            r["district"] = district_of(r.get("Location"), cfg["criteria"].get("districts", {}))
            out.append(r)
    return out


def cmd_scrape(cfg, _args):
    """Scrape configured cities, update data.csv, emit uncontacted candidates as JSON."""
    df = load_data()
    before = len(df)
    s = cfg["search"]
    for url in build_search_urls(s["cities"], s["min_price"], s["max_price"]):
        print(f"scraping {url} ...", file=sys.stderr)
        scrape_pararius(df, url)  # curl_cffi fast path — no driver needed
    print("enriching (rooms + interior from detail pages) ...", file=sys.stderr)
    enrich(df, cfg)
    df.to_csv(DATA_FILE, index=False)
    print(f"added {len(df) - before} new, {len(df)} total tracked", file=sys.stderr)
    cands = uncontacted(df, cfg)
    print(f"{len(cands)} match criteria", file=sys.stderr)
    json.dump(cands, sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_candidates(cfg, _args):
    """Print uncontacted candidates matching criteria from data.csv as JSON (no scraping)."""
    json.dump(uncontacted(load_data(), cfg), sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_sync(_cfg, _args):
    """Mark listings you've already responded to on Pararius as contacted.

    Reads your logged-in "My responses" page (manual applies included) and flips
    the matching rows in data.csv to Contacted, so the bot never double-applies.
    Runs headed + profiled so Cloudflare's challenge can clear.
    """
    from bs4 import BeautifulSoup

    # "Mijn reacties" / "My responses" — cards link to reaction UUIDs, not the
    # listing, so we match by the street name shown on the page instead.
    pages = [
        "https://www.pararius.nl/profiel/reacties",
        "https://www.pararius.com/profile/reactions",
    ]
    page_text = ""
    driver = browser.make_driver(headless=False, profile=True)
    try:
        for page in pages:
            html = browser.get_html(page, driver=driver, wait=6)
            if browser._looks_blocked(html):
                print(f"{page}: blocked by Cloudflare", file=sys.stderr)
                continue
            page_text += " " + BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    finally:
        driver.quit()

    text_low = page_text.lower()
    df = load_data()
    marked = []
    for idx, row in df.iterrows():
        street = _street(row["Title"]).lower()
        if street and len(street) > 4 and street in text_low and not _truthy(row["Contacted"]):
            df.at[idx, "Contacted"] = True
            marked.append(row["Title"])
    df.to_csv(DATA_FILE, index=False)
    print(f"marked {len(marked)} listings contacted from your responses page", file=sys.stderr)
    json.dump(marked, sys.stdout, indent=2, ensure_ascii=False)
    print()


def ntfy_push(cfg, listing):
    """Push a new-match alert to the configured ntfy topic with a tap-to-apply link."""
    topic = cfg.get("ntfy", {}).get("topic")
    if not topic:
        return
    dash = cfg.get("dashboard_url", "").rstrip("/")
    click = f"{dash}/?apply={urllib.parse.quote(listing['Link'], safe='')}" if dash else listing["Link"]
    beds = max(_int(listing.get("Rooms")) - 1, 0)
    body = (f"{listing['Price']} · {listing['Surface Area']} · {beds} bed · "
            f"{listing.get('Interior','')} · {listing.get('district','')}\n{listing['Title']}")
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": "New Rotterdam match",
            "Priority": "high",
            "Tags": "house",
            "Click": click,
            "Actions": f"view, Open on Pararius, {listing['Link']}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)


def cmd_watch(cfg, _args):
    """Poll for new matching listings and push each to ntfy. Runs until stopped.

    Interval is jittered around config watch.interval_seconds to avoid a
    clockwork request pattern (looks human, stays under Cloudflare's radar).
    """
    w = cfg.get("watch", {})
    base, jitter = w.get("interval_seconds", 240), w.get("jitter_seconds", 60)
    known = set(load_data()["Link"])
    print(f"watching {cfg['search']['cities']} every ~{base}s (±{jitter}s); "
          f"{len(known)} listings already known", file=sys.stderr)
    while True:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df = load_data()
        before = len(df)
        s = cfg["search"]
        print(f"[{started}] poll start: {len(df)} known listings", file=sys.stderr, flush=True)
        for url in build_search_urls(s["cities"], s["min_price"], s["max_price"]):
            scrape_pararius(df, url)
        enrich(df, cfg)
        df.to_csv(DATA_FILE, index=False)

        fresh = [r for r in uncontacted(df, cfg) if r["Link"] not in known]
        for r in fresh:
            print(f"  NEW: {r['Title']} {r['Price']}", file=sys.stderr, flush=True)
            ntfy_push(cfg, r)
        known |= set(df["Link"])

        delay = base + random.randint(-jitter, jitter)
        finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{finished}] poll done: {len(df)} known ({len(df) - before:+d}), "
            f"{len(fresh)} new matches; next in {max(60, delay)}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(max(60, delay))


def cmd_login(_cfg, _args):
    """Open Pararius in the persistent profile so you can log in once.

    The session cookie is saved to the profile and reused by later `contact`
    runs. Press Enter here after you've finished logging in.
    """
    driver = browser.make_driver(headless=False, profile=True)
    try:
        driver.get("https://www.pararius.com/login")
        browser.dismiss_cookie_banner(driver)
        input("Log in in the browser window, then press Enter here to save the session... ")
        print("session saved to profile", file=sys.stderr)
    finally:
        driver.quit()


def cmd_detail(_cfg, args):
    """Fetch a listing page and emit description + features text for judging."""
    driver = browser.make_driver()
    try:
        info = house_detail.listing_details(args.url, driver=driver)
    finally:
        driver.quit()
    json.dump(info, sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_contact(cfg, args):
    """Contact a single listing with the given message; mark it contacted in data.csv."""
    c = cfg["contact"]
    contact = ContactDetails(
        c["firstname"], c["lastname"], c["email"], c["phone"], args.message
    )

    driver = browser.make_driver(headless=False, profile=True)
    try:
        link = house_detail.house_details_scraper(args.url, driver=driver)
        if not link:
            print("could not resolve contact link", file=sys.stderr)
            sys.exit(1)
        kind, contact_url = link
        print(f"contact type: {kind} -> {contact_url}", file=sys.stderr)

        if args.dry_run:
            print("dry-run: not submitting", file=sys.stderr)
            sent = False
        elif kind == "viewing":
            sent = contact_estate_agent.set_viewing(contact_url, contact, driver=driver)
        else:
            sent = contact_estate_agent.send_message_to_agent(contact_url, contact, driver=driver)
    finally:
        driver.quit()

    if sent:
        df = load_data()
        df.loc[df["Link"] == args.url, "Contacted"] = True
        df.to_csv(DATA_FILE, index=False)
        print("marked contacted", file=sys.stderr)
    print(json.dumps({"url": args.url, "contacted": bool(sent)}))


# --- Main execution ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pararius scrape + LLM-driven apply tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scrape", help="scrape cities, update data.csv, print candidates JSON")
    sub.add_parser("candidates", help="print uncontacted candidates JSON (no scrape)")
    sub.add_parser("login", help="open Pararius to log in once; session persists in profile")
    sub.add_parser("sync", help="mark listings you've already responded to on Pararius as contacted")
    sub.add_parser("watch", help="poll for new matches and push them to ntfy (runs until stopped)")

    p_detail = sub.add_parser("detail", help="fetch one listing's description/features")
    p_detail.add_argument("--url", required=True)

    p_contact = sub.add_parser("contact", help="apply to one listing with a message")
    p_contact.add_argument("--url", required=True)
    p_contact.add_argument("--message", required=True)
    p_contact.add_argument("--dry-run", action="store_true", help="fill but do not submit")

    args = parser.parse_args()
    cfg = load_config()

    {
        "scrape": cmd_scrape,
        "candidates": cmd_candidates,
        "login": cmd_login,
        "sync": cmd_sync,
        "watch": cmd_watch,
        "detail": cmd_detail,
        "contact": cmd_contact,
    }[args.command](cfg, args)


if __name__ == "__main__":
    main()
