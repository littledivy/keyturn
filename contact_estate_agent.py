"""Automate contacting estate agents on Pararius using Selenium.

Provides two actions:
  - send_message_to_agent(): submit the agent contact form.
  - set_viewing(): submit the viewing request form (also selects all available days).
"""

import time
from dataclasses import dataclass
from random import uniform

from selenium.common.exceptions import (
    NoSuchElementException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

import browser


@dataclass
class ContactDetails:
    """User contact information for agent forms."""

    firstname: str
    lastname: str
    email: str
    phone: str
    message: str


# -- Shared helpers -----------------------------------------------------------

def _create_driver():
    """Create a visible Chrome WebDriver with a randomised user-agent string."""
    return browser.make_driver(headless=False)


def _dismiss_cookie_banner(driver):
    """Reject the OneTrust cookie popup if it appears."""
    try:
        driver.find_element(By.ID, "onetrust-reject-all-handler").click()
    except NoSuchElementException:
        pass  # banner not present — nothing to do


def _fill_contact_form(driver, contact):
    """Locate and fill the standard Pararius contact form fields.

    Adds small random delays between inputs to mimic human typing.
    """
    fields = {
        "listing_contact_agent_form[first_name]": contact.firstname,
        "listing_contact_agent_form[last_name]": contact.lastname,
        "listing_contact_agent_form[email]": contact.email,
        "listing_contact_agent_form[phone]": contact.phone,
        "listing_contact_agent_form[message]": contact.message,
    }

    for field_name, value in fields.items():
        driver.find_element(By.NAME, field_name).send_keys(value)
        time.sleep(uniform(1, 2))


def _submit_form(driver):
    """Click the form submit button and wait for the response."""
    driver.find_element(By.CSS_SELECTOR, ".form .form__button--submit").click()
    time.sleep(5)


# -- Public API ---------------------------------------------------------------

def send_message_to_agent(listing_url, contact, driver=None):
    """Fill and submit the agent contact form for a listing.

    Args:
        listing_url: Full URL to the Pararius contact-agent page.
        contact: ContactDetails instance with user info.
        driver: optional shared Selenium driver to reuse (kept open by caller).

    Returns:
        True if the form was submitted successfully, False otherwise.
    """
    own = driver is None
    if own:
        driver = _create_driver()
    try:
        driver.get(listing_url)
        time.sleep(5)

        _dismiss_cookie_banner(driver)
        _fill_contact_form(driver, contact)
        _submit_form(driver)

        print("Message sent successfully!")
        return True
    except (NoSuchElementException, WebDriverException) as exc:
        print(f"Failed to send message: {exc}")
        return False
    finally:
        if own:
            driver.quit()


def set_viewing(listing_url, contact, driver=None):
    """Fill and submit the viewing request form for a listing.

    Same as send_message_to_agent but also selects all available
    day-of-week checkboxes before submitting.

    Args:
        listing_url: Full URL to the Pararius viewing-request page.
        contact: ContactDetails instance with user info.
        driver: optional shared Selenium driver to reuse (kept open by caller).

    Returns:
        True if the form was submitted successfully, False otherwise.
    """
    own = driver is None
    if own:
        driver = _create_driver()
    try:
        driver.get(listing_url)
        time.sleep(5)

        _dismiss_cookie_banner(driver)
        _fill_contact_form(driver, contact)

        # Select all available day-of-week checkboxes
        checkboxes = driver.find_elements(By.CLASS_NAME, "checkbox-control__label")
        for checkbox in checkboxes:
            checkbox.click()
            time.sleep(uniform(1, 2))

        _submit_form(driver)

        print("Viewing request sent successfully!")
        return True
    except (NoSuchElementException, WebDriverException) as exc:
        print(f"Failed to request viewing: {exc}")
        return False
    finally:
        if own:
            driver.quit()
