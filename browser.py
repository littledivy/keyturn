"""Page-fetch + browser helpers.

Pararius sits behind Cloudflare, which 403s plain ``requests``/``urllib``.
``curl_cffi`` impersonates Chrome's TLS fingerprint (JA3), so it passes the
challenge with a plain HTTP request in ~1s — no browser needed. All read-only
page fetches go through that fast path; Selenium is kept only for the
interactive contact/login forms.
"""

import os
import time

from curl_cffi import requests as _creq
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from fake_useragent import UserAgent

# curl_cffi impersonation profiles to try in order if one gets blocked.
_IMPERSONATE = ("chrome", "chrome124", "safari")

# Pararius' origin can be slow; only the HTML matters for parsing, so don't wait
# on images/subresources. Fail a stuck load after this many seconds.
PAGE_LOAD_TIMEOUT = 45

# Persistent Chrome profile for logged-in actions (contacting agents). Log in
# once in this profile and the Pararius session cookie is reused on later runs.
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome-profile")


def make_driver(headless=True, profile=False):
    """Create a Chrome WebDriver with a randomised user-agent string.

    When *profile* is True a persistent on-disk profile is used so a Pararius
    login survives across runs. A random UA would break session reuse, so the
    profiled driver keeps Chrome's default UA.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    # Return control at DOMContentLoaded instead of full load (skips slow images).
    options.page_load_strategy = "eager"
    if profile:
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    else:
        options.add_argument(f"--user-agent={UserAgent().random}")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def dismiss_cookie_banner(driver):
    """Reject the OneTrust cookie popup if it appears."""
    try:
        driver.find_element(By.ID, "onetrust-reject-all-handler").click()
    except NoSuchElementException:
        pass  # banner not present — nothing to do


def _looks_blocked(html):
    """Detect a Cloudflare interstitial ('Just a moment...' challenge page)."""
    head = html[:4000].lower()
    return "just a moment" in head or "cf-challenge" in head or "checking your browser" in head


def get_html(url, driver=None, wait=4.0, retries=2):
    """Return the rendered HTML for *url*.

    If *driver* is given it is reused (cheaper across many pages); otherwise a
    throw-away headless driver is created and closed for this single call.
    Retries with a longer wait if Cloudflare serves a challenge page.
    """
    # Fast path: curl_cffi passes Cloudflare via TLS impersonation, no browser.
    html = fetch_html(url)
    if html and not _looks_blocked(html):
        return html

    # Fallback: real browser (rarely needed — e.g. curl_cffi itself gets blocked).
    own = driver is None
    if own:
        driver = make_driver()
    try:
        try:
            driver.get(url)
        except TimeoutException:
            pass
        time.sleep(wait)
        for _ in range(retries):
            if not _looks_blocked(driver.page_source):
                break
            time.sleep(6)
        dismiss_cookie_banner(driver)
        return driver.page_source
    finally:
        if own:
            driver.quit()


def fetch_html(url, timeout=30):
    """Fetch page HTML via curl_cffi (Chrome TLS impersonation). No browser.

    Tries each impersonation profile until one returns an unblocked 200.
    Returns the last response body even if blocked, so callers can inspect it.
    """
    last = ""
    for imp in _IMPERSONATE:
        try:
            r = _creq.get(url, impersonate=imp, timeout=timeout)
            last = r.text
            if r.status_code == 200 and not _looks_blocked(last):
                return last
        except Exception:
            continue
    return last
