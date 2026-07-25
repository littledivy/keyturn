"""Scrape an individual Pararius listing page for contact links.

Given a listing URL, this module finds the appropriate contact link
(either a direct agent contact form or a viewing request form).
"""

from bs4 import BeautifulSoup

import browser


def house_details_scraper(url, driver=None):
    """Extract the contact or viewing-request link from a listing page.

    Args:
        url: Full URL of a Pararius listing page.
        driver: optional shared Selenium driver to reuse.

    Returns:
        A tuple of (contact_type, full_url) where contact_type is either
        "agent" or "viewing". Returns None if the page can't be fetched.
    """
    html = browser.get_html(url, driver=driver)
    soup = BeautifulSoup(html, "html.parser")

    sidebar_sections = soup.find_all("section", class_="page__sidebar")

    agent_link = ""
    viewing_link = ""

    for section in sidebar_sections:
        # Try to find the direct agent contact link
        try:
            agent_link = section.find("a", class_="agent-summary__agent-contact-request")["href"]
        except (TypeError, KeyError):
            print(f"No agent contact link found for {url}")

        # Try to find the viewing request link
        try:
            viewing_class = (
                "agent-summary__agent-viewing-request "
                "agent-summary__agent-viewing-request--ghost"
            )
            viewing_link = section.find("a", class_=viewing_class)["href"]
        except (TypeError, KeyError):
            print(f"No viewing link found for {url}")
            viewing_link = ""

    # Prefer the viewing link when available, fall back to agent contact
    if viewing_link:
        return ("viewing", "https://www.pararius.com" + viewing_link)
    if agent_link:
        return ("agent", "https://www.pararius.com" + agent_link)
    return None


def listing_details(url, driver=None):
    """Fetch a listing page and return fields useful for judging fit.

    Returns a dict with the title, description, and the feature/label pairs
    (surface area, rooms, deposit, income requirement, interior, etc.).
    """
    html = browser.get_html(url, driver=driver)
    soup = BeautifulSoup(html, "html.parser")

    def text_of(selector):
        el = soup.select_one(selector)
        return el.get_text(" ", strip=True) if el else ""

    # Feature dl lists: <dt>label</dt><dd>value</dd>
    features = {}
    for item in soup.select("dl.listing-features__list, .listing-features__list"):
        terms = item.find_all("dt")
        defs = item.find_all("dd")
        for dt, dd in zip(terms, defs):
            key = dt.get_text(" ", strip=True)
            val = dd.get_text(" ", strip=True)
            if key and val:
                features[key] = val

    return {
        "url": url,
        "title": text_of("h1.listing-detail-summary__title, h1"),
        "price": text_of(".listing-detail-summary__price"),
        "description": text_of(".listing-detail-description__additional, .listing-detail-description"),
        "features": features,
    }
