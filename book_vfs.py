#!/usr/bin/env python3
"""
VFS Global Guinea-Bissau → Portugal Appointment Booking Bot
=============================================================
Standalone local script.  No OTP.  Supports up to 5 clients.
Cloudflare bypass via Playwright stealth + human-like interaction.

Usage:
    python book_vfs.py                        # visible browser, default settings
    python book_vfs.py --headless             # headless (backgrounded)
    python book_vfs.py --max-clients 3        # book at most 3 clients
    python book_vfs.py --monitor-minutes 6    # extend the slot-watch window

Clients are read from  clients.csv  in the same directory.
Credentials come from constants below (or VFS_EMAIL / VFS_PASSWORD env vars).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, WebDriverException,
)

# ──────────────────────────────────────────────────────────────
# CONFIGURATION  (edit here or use environment variables)
# ──────────────────────────────────────────────────────────────
LOGIN_EMAIL    = os.environ.get("VFS_EMAIL",    "brunovfs2k@gmail.com")
LOGIN_PASSWORD = os.environ.get("VFS_PASSWORD", "Bissau300@")

BASE_URL   = "https://visa.vfsglobal.com"
LOGIN_URL  = "https://visa.vfsglobal.com/gnb/en/prt/login"
BOOK_URL   = "https://visa.vfsglobal.com/gnb/en/prt/book-appointment"
DASH_URL   = "https://visa.vfsglobal.com/gnb/en/prt/dashboard"

MAX_CLIENTS     = 5
MONITOR_MINUTES = 4       # how long to poll for slots
CHECK_INTERVAL  = 20      # seconds between availability polls

# CSV file with client data
CLIENTS_CSV = Path(__file__).resolve().parent / "clients.csv"

# ──────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/book_vfs.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("vfs_bot")


# ──────────────────────────────────────────────────────────────
# Client dataclass + CSV loader
# ──────────────────────────────────────────────────────────────
@dataclass
class Client:
    first_name: str
    last_name: str
    date_of_birth: str          # DD/MM/YYYY
    email: str
    password: str
    mobile_country_code: str
    mobile_number: str
    passport_number: str
    visa_type: str
    application_center: str
    service_center: str
    trip_reason: str
    gender: str
    current_nationality: str
    passport_expiry: str        # DD/MM/YYYY


def load_clients(csv_path: Path = CLIENTS_CSV, max_n: int = MAX_CLIENTS) -> List[Client]:
    """Load clients from CSV file, up to max_n."""
    if not csv_path.exists():
        log.error(f"clients.csv not found at {csv_path}")
        return []
    clients: List[Client] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stripped = {k.strip(): v.strip() for k, v in row.items()}
            try:
                clients.append(Client(**stripped))
                if len(clients) >= max_n:
                    break
            except TypeError as exc:
                log.warning(f"Skipping malformed row ({exc}): {stripped}")
    log.info(f"Loaded {len(clients)} client(s) from {csv_path}")
    return clients


# ──────────────────────────────────────────────────────────────
# Stealth : realistic User-Agents  (undetected-chromedriver takes
# care of all navigator.webdriver / CDP fingerprint patches)
# ──────────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",

    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _delay(lo: float = 1.2, hi: float = 3.0) -> None:
    """Human-like random pause."""
    time.sleep(random.uniform(lo, hi))


def _find_el(driver, selector: str, timeout: float = 3.0):
    """
    Find a single visible element.  Selector may be:
      - A CSS selector (default)
      - An XPath expression (prefix '//' or './/') 
    Returns the element or None.
    """
    by = (
        By.XPATH
        if selector.startswith("//") or selector.startswith(".//")
        else By.CSS_SELECTOR
    )
    try:
        return WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((by, selector))
        )
    except Exception:
        return None


def _find_els(driver, selector: str) -> list:
    """Return all matching elements; CSS or XPath."""
    by = (
        By.XPATH
        if selector.startswith("//") or selector.startswith(".//")
        else By.CSS_SELECTOR
    )
    try:
        return driver.find_elements(by, selector)
    except Exception:
        return []


def _human_type(element, text: str) -> None:
    """Type each character with a realistic inter-key delay."""
    try:
        element.click()
        element.clear()
    except Exception:
        pass
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.045, 0.13))
    _delay(0.2, 0.6)


def _click_first_visible(driver, selectors: List[str], description: str = "") -> bool:
    """Click the first selector (CSS or XPath) that is visible and enabled."""
    for sel in selectors:
        try:
            el = _find_el(driver, sel, timeout=2.0)
            if el and el.is_enabled():
                el.click()
                log.info(f"  Clicked: {description or sel}")
                return True
        except Exception:
            pass
    return False


# ──────────────────────────────────────────────────────────────
# VFS Booking Bot  (undetected-chromedriver backend)
# ──────────────────────────────────────────────────────────────
class VFSBot:
    """
    Selenium + undetected-chromedriver bot.
    UC patches ChromeDriver at the binary level so Cloudflare's
    bot-detection heuristics see a genuine Chrome process.
    """

    def __init__(self, headless: bool = False) -> None:
        self.headless   = headless
        self.driver: uc.Chrome = None  # type: ignore[assignment]
        self.wait: WebDriverWait = None  # type: ignore[assignment]
        self._logged_in = False

    # ── Browser lifecycle ──────────────────────────────────────

    def start(self) -> None:
        log.info("Launching undetected Chrome…")
        ua = random.choice(_USER_AGENTS)

        opts = uc.ChromeOptions()
        if self.headless:
            # "new" headless is still detectable by some checks;
            # for best bypass results run headed (default).
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1366,768")
        opts.add_argument("--lang=en-US,en")
        opts.add_argument(f"--user-agent={ua}")
        opts.add_argument("--disable-blink-features=AutomationControlled")

        self.driver = uc.Chrome(
            options=opts,
            use_subprocess=True,   # keeps the chromedriver process isolated
        )
        self.wait = WebDriverWait(self.driver, 15)
        self.driver.set_page_load_timeout(60)

        # Inject extra stealth on top of what UC already does
        self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
            "headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        })
        log.info("Browser ready (undetected-chromedriver).")

    def stop(self) -> None:
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        log.info("Browser closed.")

    # ── Cloudflare handling ────────────────────────────────────

    def _is_cf_page(self) -> bool:
        """Return True if the current page is a Cloudflare challenge."""
        try:
            src = self.driver.page_source.lower()
            return any(kw in src for kw in [
                "checking your browser",
                "cf-browser-verification",
                "cloudflare",
                "ddos protection",
                "just a moment",
                "enable javascript",
            ])
        except Exception:
            return False

    def _wait_cf(self, timeout_s: int = 60) -> bool:
        """Wait for Cloudflare JS challenge to auto-resolve."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self._is_cf_page():
                log.info("Cloudflare cleared ✓")
                return True
            log.info(f"  CF still active, waiting… ({int(deadline - time.time())}s left)")
            time.sleep(4)
        return False

    def _goto(self, url: str, ms: int = 60_000) -> None:
        """Navigate to URL, wait for CF to clear if needed."""
        self.driver.set_page_load_timeout(ms / 1000)
        try:
            self.driver.get(url)
        except WebDriverException as exc:
            # Timeout is a common outcome on heavy CF pages; log but continue
            log.warning(f"  Page load timed-out ({exc.__class__.__name__}) – proceeding anyway.")
        _delay(2, 4)   # Give Angular/CF scripts time to execute
        if self._is_cf_page():
            log.warning("Cloudflare challenge detected – waiting for auto-pass (up to 60s)…")
            if not self._wait_cf(60):
                log.warning("CF auto-pass failed. Pausing 90s for manual CAPTCHA solve…")
                time.sleep(90)

    # ── Login ──────────────────────────────────────────────────

    def login(self, email: str, password: str) -> bool:
        """
        Log in to the VFS Global English portal.
        Tries formcontrolname Angular selectors first, with fallbacks.
        """
        log.info(f"→ Logging in as {email}")
        self._goto(LOGIN_URL)
        _delay(1.5, 3)

        # ── Accept cookies banner (if shown) ──────────────────
        for sel in [
            "#onetrust-accept-btn-handler",
            "//button[contains(., 'Accept All')]",
            "//button[contains(., 'Accept')]",
            ".accept-cookies",
            "[aria-label='Close']",
        ]:
            try:
                el = _find_el(self.driver, sel, timeout=2.0)
                if el and el.is_enabled():
                    el.click()
                    _delay(0.5, 1)
                    log.info("  Accepted cookies.")
                    break
            except Exception:
                pass

        # ── Email field ────────────────────────────────────────
        email_selectors = [
            'input[formcontrolname="email"]',
            'input[formcontrolname="username"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[type="email"]',
            '#mat-input-0',
        ]
        email_el = None
        for sel in email_selectors:
            email_el = _find_el(self.driver, sel, timeout=4.0)
            if email_el:
                break

        if email_el is None:
            log.error("Could not locate the email field on the login page.")
            return False

        _human_type(email_el, email)
        log.info("  Email entered.")

        # ── Password field ─────────────────────────────────────
        pwd_selectors = [
            'input[formcontrolname="password"]',
            'input[name="password"]',
            'input[type="password"]',
            '#mat-input-1',
        ]
        pwd_el = None
        for sel in pwd_selectors:
            pwd_el = _find_el(self.driver, sel, timeout=4.0)
            if pwd_el:
                break

        if pwd_el is None:
            log.error("Could not locate the password field.")
            return False

        _human_type(pwd_el, password)
        log.info("  Password entered.")
        _delay(0.8, 1.5)

        # ── Submit ─────────────────────────────────────────────
        submit_selectors = [
            'button[type="submit"]',
            "//button[contains(., 'Sign In')]",
            "//button[contains(., 'Log In')]",
            "//button[contains(., 'Login')]",
            "//button[contains(., 'Submit')]",
            '.sign-in-btn',
            '.login-btn',
            'app-login button',
        ]
        if not _click_first_visible(self.driver, submit_selectors, "Sign In button"):
            pwd_el.send_keys(Keys.RETURN)
            log.info("  Pressed Enter to submit.")

        # ── Wait for post-login navigation ─────────────────────
        _delay(3, 6)
        current = self.driver.current_url
        log.info(f"  Post-submit URL: {current}")

        if "/login" not in current:
            log.info("✅ Login successful.")
            self._logged_in = True
            return True

        # Check for error messages
        for err_sel in [
            ".error-message", ".alert-danger", ".mat-error",
            "mat-error", "[role='alert']",
        ]:
            el = _find_el(self.driver, err_sel, timeout=1.0)
            if el:
                log.error(f"  Login error: '{el.text.strip()}'")
                return False

        # One last wait for slow Angular router
        _delay(3, 5)
        if "/login" not in self.driver.current_url:
            log.info("✅ Login successful (delayed navigation).")
            self._logged_in = True
            return True

        log.error(f"Login failed. Still on: {self.driver.current_url}")
        return False

    # ── Booking navigation ─────────────────────────────────────

    def navigate_to_booking(self) -> bool:
        """Go to the book-appointment page (requires active session)."""
        log.info("→ Navigating to booking page…")
        try:
            self._goto(BOOK_URL)
            _delay(2, 4)
            current = self.driver.current_url
            log.info(f"  Current URL: {current}")
            if "/login" in current:
                log.warning("Redirected back to login – session expired or not established.")
                return False
            return True
        except Exception as exc:
            log.error(f"  Navigation to booking page failed: {exc}")
            return False

    # ── Slot detection ─────────────────────────────────────────

    def _count_slots(self) -> int:
        """
        Count available booking slots visible on the current page.
        Returns 0 when the site says no appointments, or no slot elements found.
        """
        try:
            src_low = self.driver.page_source.lower()
        except Exception:
            return 0

        no_avail_phrases = [
            "no appointments available", "no slots available", "fully booked",
            "there are no available", "appointments are not available",
            "não existem", "no appointment",
        ]
        if any(p in src_low for p in no_avail_phrases):
            return 0

        slot_selectors = [
            "mat-calendar .mat-calendar-body-cell:not(.mat-calendar-body-disabled)",
            "mat-calendar button:not([disabled]):not(.mat-calendar-body-disabled)",
            ".vfs-slot-available",
            ".slot-available",
            ".appointment-slot",
            'input[type="radio"][name*="slot"]:not([disabled])',
            'input[type="radio"][name*="date"]:not([disabled])',
            ".available-date",
            ".available-slot",
            ".calendar-day.available",
            ".calendar-day.bookable",
        ]
        for sel in slot_selectors:
            els = _find_els(self.driver, sel)
            if els:
                return len(els)

        positive_phrases = [
            "select a date", "choose appointment", "available appointment",
            "book appointment", "select time slot", "available dates",
        ]
        if any(p in src_low for p in positive_phrases):
            return 1

        return 0

    # ── Availability monitoring loop ───────────────────────────

    def wait_for_availability(self, minutes: int = MONITOR_MINUTES) -> bool:
        """
        Refresh the booking page every CHECK_INTERVAL seconds for up to `minutes`.
        Returns True as soon as at least one slot is detected.
        """
        log.info(f"⏳ Watching for slots ({minutes}-min window, polling every {CHECK_INTERVAL}s)…")
        deadline = datetime.now() + timedelta(minutes=minutes)
        poll = 0
        while datetime.now() < deadline:
            poll += 1
            remaining = int((deadline - datetime.now()).total_seconds() // 60)
            log.info(f"  Poll #{poll} | ~{remaining} min left")
            try:
                self.driver.refresh()
                _delay(2, 3)
                if self._is_cf_page():
                    self._wait_cf()

                n = self._count_slots()
                if n > 0:
                    log.info(f"🎉  Slots detected ({n})!")
                    return True
                log.info(f"  No slots yet. Next poll in {CHECK_INTERVAL}s…")
            except Exception as exc:
                log.warning(f"  Poll error: {exc}")

            time.sleep(CHECK_INTERVAL)

        log.warning("⌛ Monitoring window expired. No slots found.")
        return False

    # ── Booking form helpers ───────────────────────────────────

    def _select_mat(self, selectors: List[str], value: str, label: str = "") -> bool:
        """
        Handle Angular Material mat-select and native <select>.
        Opens the dropdown and clicks the matching option.
        """
        for sel in selectors:
            el = _find_el(self.driver, sel, timeout=3.0)
            if el is None:
                continue
            try:
                tag = el.tag_name.lower()
                if tag == "select":
                    Select(el).select_by_visible_text(value)
                    log.info(f"  Selected (native) '{value}' for {label}")
                    return True
                else:
                    # mat-select or custom dropdown — click to open
                    el.click()
                    _delay(0.4, 0.9)
                    for opt_sel in [
                        f'//mat-option[contains(., "{value}")]',
                        f'//li[contains(., "{value}")]',
                        f'//*[@role="option"][contains(., "{value}")]',
                    ]:
                        opt = _find_el(self.driver, opt_sel, timeout=2.0)
                        if opt:
                            opt.click()
                            _delay(0.3, 0.8)
                            log.info(f"  Selected (mat) '{value}' for {label}")
                            return True
            except Exception:
                pass
        log.warning(f"  Could not select '{value}' for {label}")
        return False

    def _fill_input(self, selectors: List[str], value: str, label: str = "") -> bool:
        """Fill a text/email input using the first matching visible selector."""
        for sel in selectors:
            el = _find_el(self.driver, sel, timeout=2.0)
            if el:
                _human_type(el, value)
                log.info(f"  Filled '{value}' for {label}")
                return True
        log.warning(f"  Could not fill '{label}'")
        return False

    def _next_step(self) -> None:
        """Click Continue / Next / Proceed to advance the wizard."""
        _click_first_visible(self.driver, [
            "//button[contains(., 'Continue')]",
            "//button[contains(., 'Next')]",
            "//button[contains(., 'Proceed')]",
            "//button[contains(., 'Go')]",
            '.continue-btn', '.next-btn',
            'button[type="submit"]:not([disabled])',
        ], "Continue / Next")
        _delay(1.5, 3)

    # ── Full booking flow for one client ───────────────────────

    def fill_booking_form(self, client: Client, slot_index: int = 0) -> bool:
        """
        Walk through the VFS Angular booking wizard for a single client.

        VFS Global wizard steps (Guinea-Bissau → Portugal English portal):
          1. Appointment category (visa type)
          2. Sub-category / service / trip reason
          3. Calendar → pick first available date
          4. Time-slot selection
          5. Applicant personal details
          6. Review & submit
        """
        log.info(f"  Filling form for: {client.first_name} {client.last_name} ({client.email})")
        try:
            # ── STEP 1: Appointment category ───────────────────
            self._select_mat([
                'mat-select[formcontrolname="appointmentCategory"]',
                'mat-select[formcontrolname="category"]',
                'mat-select[formcontrolname="visaCategory"]',
                'mat-select[formcontrolname="serviceType"]',
                'select[name="appointmentCategory"]',
                'select[name="category"]',
                'mat-select',
                '#appointmentCategory',
            ], client.visa_type, "Appointment Category")
            _delay(1, 2)

            # ── STEP 2: Sub-category / trip reason ─────────────
            self._select_mat([
                'mat-select[formcontrolname="appointmentSubCategory"]',
                'mat-select[formcontrolname="subCategory"]',
                'mat-select[formcontrolname="serviceCategory"]',
                'mat-select[formcontrolname="purposeOfVisit"]',
                'select[name="subCategory"]',
            ], client.trip_reason, "Trip Reason / Sub-category")
            _delay(1, 2)

            self._next_step()

            # ── STEP 3: Calendar – pick first available day ─────
            _delay(1.5, 3)
            for cal_sel in [
                "mat-calendar .mat-calendar-body-cell:not(.mat-calendar-body-disabled)",
                "mat-calendar button:not([disabled]):not(.mat-calendar-body-disabled)",
                ".calendar-day.available",
                ".available-date",
            ]:
                days = _find_els(self.driver, cal_sel)
                if days:
                    days[0].click()
                    log.info(f"  Clicked first available calendar day ({len(days)} found).")
                    _delay(1, 2)
                    break
            else:
                log.warning("  Could not find an available calendar day.")

            # ── STEP 4: Time-slot selection ─────────────────────
            for slot_sel in [
                'input[type="radio"][name*="slot"]:not([disabled])',
                'input[type="radio"][name*="time"]:not([disabled])',
                '.time-slot:not(.disabled):not(.unavailable)',
                '.slot-item:not(.disabled)',
                'mat-radio-button:not([disabled])',
            ]:
                slots = _find_els(self.driver, slot_sel)
                if slots:
                    idx = slot_index % len(slots)
                    slots[idx].click()
                    log.info(f"  Selected time slot #{idx + 1} of {len(slots)}.")
                    _delay(0.8, 1.5)
                    break

            self._next_step()

            # ── STEP 5: Applicant personal details ─────────────
            _delay(1, 2)

            self._fill_input([
                'input[formcontrolname="firstName"]',
                'input[formcontrolname="givenName"]',
                'input[name="firstName"]',
                '#firstName',
            ], client.first_name, "First Name")

            self._fill_input([
                'input[formcontrolname="lastName"]',
                'input[formcontrolname="surname"]',
                'input[name="lastName"]',
                '#lastName',
            ], client.last_name, "Last Name")

            self._fill_input([
                'input[formcontrolname="dateOfBirth"]',
                'input[formcontrolname="dob"]',
                'input[name="dateOfBirth"]',
                'input[type="date"]',
                '#dateOfBirth', '#dob',
            ], client.date_of_birth, "Date of Birth")

            self._fill_input([
                'input[formcontrolname="passportNumber"]',
                'input[formcontrolname="passport"]',
                'input[name="passportNumber"]',
                '#passportNumber',
            ], client.passport_number, "Passport Number")

            self._fill_input([
                'input[formcontrolname="passportExpiry"]',
                'input[formcontrolname="expiryDate"]',
                'input[formcontrolname="passportExpiryDate"]',
                'input[name="passportExpiry"]',
                '#passportExpiry',
            ], client.passport_expiry, "Passport Expiry")

            self._fill_input([
                'input[formcontrolname="email"]',
                'input[formcontrolname="emailAddress"]',
                'input[name="email"]',
                'input[type="email"]',
                '#email',
            ], client.email, "Email")

            self._select_mat([
                'mat-select[formcontrolname="phoneCountryCode"]',
                'mat-select[formcontrolname="countryCode"]',
                'mat-select[formcontrolname="phoneCode"]',
                'select[name="countryCode"]',
            ], f"+{client.mobile_country_code}", "Phone Country Code")

            self._fill_input([
                'input[formcontrolname="phoneNumber"]',
                'input[formcontrolname="mobile"]',
                'input[formcontrolname="phone"]',
                'input[name="phoneNumber"]',
                '#phoneNumber',
            ], client.mobile_number, "Phone Number")

            self._select_mat([
                'mat-select[formcontrolname="nationality"]',
                'mat-select[formcontrolname="currentNationality"]',
                'mat-select[formcontrolname="country"]',
                'select[name="nationality"]',
            ], client.current_nationality, "Nationality")

            # Gender (radio buttons or mat-select)
            gender_cap = client.gender.capitalize()
            found_gender = _click_first_visible(self.driver, [
                f'//mat-radio-button[contains(., "{gender_cap}")]',
                f'input[type="radio"][value="{client.gender.lower()}"]',
                f'input[type="radio"][value="{gender_cap}"]',
                f'//label[contains(., "{gender_cap}")]//input[@type="radio"]',
            ], f"Gender: {gender_cap}")
            if not found_gender:
                self._select_mat([
                    'mat-select[formcontrolname="gender"]',
                    'select[name="gender"]',
                ], gender_cap, "Gender")

            _delay(1, 2)
            self._next_step()

            # ── STEP 6: Review & submit ─────────────────────────
            _delay(1.5, 3)
            if not _click_first_visible(self.driver, [
                "//button[contains(., 'Confirm')]",
                "//button[contains(., 'Submit')]",
                "//button[contains(., 'Book')]",
                "//button[contains(., 'Pay')]",
                'button[type="submit"]:not([disabled])',
                '.confirm-btn', '.submit-btn',
            ], "Confirm / Submit"):
                log.warning("  Could not find a final submit button.")

            _delay(3, 7)

            # ── Confirmation detection ──────────────────────────
            src_low = self.driver.page_source.lower()
            confirmed = any(p in src_low for p in [
                "booking confirmed", "appointment confirmed",
                "reference number", "booking reference",
                "successfully booked", "your appointment",
            ])

            if confirmed:
                for ref_sel in [
                    ".booking-reference", ".reference-number",
                    "[class*='reference']", "[class*='confirmation']",
                    "//span[contains(., 'Reference')]",
                ]:
                    el = _find_el(self.driver, ref_sel, timeout=1.0)
                    if el:
                        log.info(f"  📋 Booking reference: {el.text.strip()}")
                        break
                log.info(f"✅ Booking confirmed for {client.first_name} {client.last_name}!")
            else:
                log.warning(
                    f"  Form submitted for {client.email} but no explicit confirmation detected. "
                    "Check the browser window."
                )

            return True

        except Exception as exc:
            log.error(f"  Form fill raised an exception for {client.email}: {exc}")
            return False

    # ── Main orchestration ─────────────────────────────────────

    def run(
        self,
        clients: List[Client],
        monitor_minutes: int = MONITOR_MINUTES,
    ) -> None:
        """
        Full booking pipeline:
          1. Start browser
          2. Login
          3. Navigate to booking page
          4. Poll for available slots
          5. Fill form for each client in sequence
        """
        try:
            self.start()

            if not self.login(LOGIN_EMAIL, LOGIN_PASSWORD):
                log.error("❌ Login failed. Halting.")
                return

            if not self.navigate_to_booking():
                log.error("❌ Could not reach the booking page. Halting.")
                return

            if not self.wait_for_availability(monitor_minutes):
                log.info("No slots became available within the monitoring window.")
                return

            booked = 0
            failed = 0
            for i, client in enumerate(clients):
                sep = "─" * 55
                log.info(f"\n{sep}")
                log.info(f"Client {i + 1}/{len(clients)}: {client.first_name} {client.last_name}")
                log.info(sep)
                ok = self.fill_booking_form(client, slot_index=i)
                if ok:
                    booked += 1
                else:
                    failed += 1
                _delay(2, 5)

            log.info(f"\n{'═' * 55}")
            log.info(f"Session done.  ✅ Booked: {booked}  ❌ Failed: {failed}")
            log.info(f"{'═' * 55}")

            summary = {
                "timestamp": datetime.now().isoformat(),
                "total": len(clients),
                "booked": booked,
                "failed": failed,
            }
            Path("logs").mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(f"logs/booking_summary_{ts}.json", "w") as fh:
                json.dump(summary, fh, indent=2)
            log.info(f"Summary saved to logs/booking_summary_{ts}.json")

        finally:
            if not self.headless:
                log.info("Keeping browser open for 30s for review…")
                time.sleep(30)
            self.stop()


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="VFS Global Guinea-Bissau → Portugal Booking Bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode (no visible browser window)",
    )
    parser.add_argument(
        "--max-clients",
        type=int, default=MAX_CLIENTS,
        metavar="N",
        help="Maximum number of clients to book per session",
    )
    parser.add_argument(
        "--monitor-minutes",
        type=int, default=MONITOR_MINUTES,
        metavar="M",
        help="How many minutes to watch for available slots",
    )
    parser.add_argument(
        "--clients-csv",
        default=str(CLIENTS_CSV),
        metavar="PATH",
        help="Path to clients.csv",
    )
    args = parser.parse_args()

    clients = load_clients(Path(args.clients_csv), args.max_clients)
    if not clients:
        log.error("No clients loaded. Ensure clients.csv exists and is properly formatted.")
        sys.exit(1)

    log.info(f"Starting VFS booking bot for {len(clients)} client(s).")
    log.info(f"Target URL : {LOGIN_URL}")
    log.info(f"Monitoring : {args.monitor_minutes} min | Poll interval: {CHECK_INTERVAL}s")

    bot = VFSBot(headless=args.headless)
    bot.run(clients, monitor_minutes=args.monitor_minutes)


if __name__ == "__main__":
    main()
