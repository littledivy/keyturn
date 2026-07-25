"""Geocode a listing address.

The self-hosted app persists results in SQLite. This module only performs the
network lookup so callers can choose their own storage.
"""
import re
import time

from geopy.geocoders import Nominatim

_geo = Nominatim(user_agent="rotterdam-housing-board")


def _street(title):
    t = re.sub(r"^(Flat|Studio|Apartment|House|Room)\s+", "", title or "")
    m = re.match(r"(.+?\s+\d+)", t)
    return m.group(1) if m else t


def _postcode(loc):
    m = re.match(r"(\d{4}\s?[A-Z]{2})", loc or "")
    return m.group(1) if m else ""


def geocode(url, title, location):
    """Return ``[latitude, longitude]`` for a listing, or ``None``."""
    street = _street(title)
    for query in (f"{street}, {_postcode(location)} Rotterdam, Netherlands",
                  f"{street}, Rotterdam, Netherlands"):
        try:
            hit = _geo.geocode(query, timeout=10)
        except Exception:
            hit = None
        time.sleep(1.1)  # Nominatim rate limit
        if hit:
            return [hit.latitude, hit.longitude]
    return None
