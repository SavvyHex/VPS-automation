#!/usr/bin/env python3
"""
VFS Global Guinea-Bissau → Portugal  ·  Unified Multi-Client Booking Bot
=========================================================================
Uses undetected-chromedriver (UC) to bypass Cloudflare and automates
the full 5-step VFS booking flow for every row in clients.csv.
Multiple clients are booked in parallel — each in its own Chrome window.

FIRST RUN  ── warm the session so Cloudflare cookies are saved:
    python unified_booking.py --warmup

BOOKING RUN  ── run when the appointment window opens:
    python unified_booking.py

OPTIONAL FLAGS:
    --max-clients 3
    --proxy socks5://user:pass@host:port
    --headless          (not recommended — CF blocks headless)
    --sequential        (book one client at a time instead of parallel)
    --csv path/to/clients.csv

CLIENTS CSV columns (see clients.csv):
  first_name, last_name, date_of_birth, email, password,
  mobile_country_code, mobile_number, passport_number,
  visa_type, application_center, service_center, trip_reason,
  gender, current_nationality, passport_expiry
"""

from __future__ import annotations

import argparse
import csv
import glob
import logging
import os
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import undetected_chromedriver as uc
except ImportError:
    print("ERROR: undetected-chromedriver not installed.")
    print("       Run:  pip install undetected-chromedriver selenium")
    sys.exit(1)

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementNotInteractableException,
    StaleElementReferenceException,
)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

VFS_BASE        = "https://visa.vfsglobal.com/gnb/en/prt"
VFS_LOGIN_URL   = f"{VFS_BASE}/login"
VFS_DASHBOARD   = f"{VFS_BASE}/dashboard"           # post-login landing
VFS_BOOKING_URL = f"{VFS_BASE}/book-an-appointment" # fallback URL

VFS_USERNAME = os.getenv("VFS_USERNAME", "brunovfs2k@gmail.com")
VFS_PASSWORD = os.getenv("VFS_PASSWORD", "Bissau300@")

CLIENTS_CSV    = Path(__file__).resolve().parent / "clients.csv"
RESULTS_CSV    = Path(__file__).resolve().parent / "logs" / "booking_results.csv"
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "logs" / "screenshots"
CHROME_PROFILE = os.path.expanduser("~/.vfs_chrome_profile")
MAX_CLIENTS    = 5

# Timeouts (seconds)
CF_POLL_MAX    = 120   # max wait for Cloudflare challenge to clear
ELEMENT_WAIT   = 30    # wait for a DOM element
DROPDOWN_WAIT  = 5     # wait for mat-option list to populate
STEP_WAIT      = 3     # pause between form steps
PAGE_NAV_WAIT  = 6     # wait after page navigation

# ─────────────────────────────────────────────────────────────
# SELECTORS
# ─────────────────────────────────────────────────────────────

SEL = {
    # ── Login page ──────────────────────────────────────────
    "login_email":    ["input[id='mat-input-0']", "input[type='email']",
                       "input[formcontrolname='username']",
                       "input[formcontrolname='email']",
                       "input[placeholder*='mail' i]"],
    "login_password": ["input[id='mat-input-1']", "input[type='password']",
                       "input[formcontrolname='password']",
                       "input[placeholder*='assword' i]"],
    "login_button":   ["button[type='submit']", "button[id*='login' i]"],

    # ── Dashboard ────────────────────────────────────────────
    "start_booking":  ["button[class*='new-booking' i]",
                       "button[routerlink*='book' i]",
                       "a[routerlink*='book' i]",
                       "button:has-text('Start New Booking')",
                       "[class*='start-new-booking']"],

    # ── Step 1: Appointment Details (/application-detail) ────
    # Application Centre dropdown
    "app_centre":     ["mat-select[formcontrolname='selectedCentre']",
                       "mat-select[formcontrolname='centre']",
                       "mat-select[formcontrolname='applicationCenter']",
                       "mat-select[id*='centre' i]",
                       "mat-select[id*='center' i]"],
    # Appointment category (visa type)
    "appt_category":  ["mat-select[formcontrolname='visaCategory']",
                       "mat-select[formcontrolname='visaType']",
                       "mat-select[formcontrolname='category']",
                       "mat-select[formcontrolname='appointmentCategory']"],
    # Sub-category (service type)
    "appt_subcategory": ["mat-select[formcontrolname='visaSubCategory']",
                         "mat-select[formcontrolname='subCategory']",
                         "mat-select[formcontrolname='serviceType']",
                         "mat-select[formcontrolname='service']"],
    # Purpose of travel (trip reason) — appears on some centre configs
    "trip_reason":    ["mat-select[formcontrolname='purposeOfTravel']",
                       "mat-select[formcontrolname='tripReason']",
                       "mat-select[formcontrolname='reasonForVisit']"],
    "step1_continue": ["button[type='submit']", "button[class*='continue' i]",
                       "button[class*='next' i]", "button:last-of-type"],

    # ── Step 2: Your Details (/your-details) ────────────────
    "first_name":           "input[formcontrolname='firstName']",
    "last_name":            "input[formcontrolname='lastName']",
    "date_of_birth":        "input[formcontrolname='dateOfBirth']",
    "email":                "input[formcontrolname='email']",
    "mobile_country_code":  "input[formcontrolname='mobileCountryCode']",
    "mobile_number":        "input[formcontrolname='mobileNumber']",
    "passport_number":      "input[formcontrolname='passportNumber']",
    "passport_expiry":      ["input[formcontrolname='passportExpiryDate']",
                             "input[formcontrolname='passportExpiry']"],
    "gender":               ["mat-select[formcontrolname='gender']",
                             "mat-select[id*='gender' i]"],
    "current_nationality":  ["mat-select[formcontrolname='nationality']",
                             "mat-select[formcontrolname='currentNationality']"],
    "step2_continue": ["button[type='submit']", "button[class*='continue' i]",
                       "button[class*='next' i]"],

    # ── Step 3: Book Appointment slot (/book-appointment) ────
    "first_slot":     [".available-slot:first-of-type",
                       "[class*='slot'][class*='available']:first-of-type",
                       "td[class*='available']:first-of-type",
                       "button[class*='slot']:not([disabled]):first-of-type"],
    "step3_continue": ["button[type='submit']", "button[class*='continue' i]",
                       "button[class*='next' i]"],

    # ── Step 4: Services (/services) ────────────────────────
    "step4_continue": ["button[type='submit']", "button[class*='continue' i]",
                       "button[class*='next' i]"],

    # ── Step 5: Review (/review) ─────────────────────────────
    "confirm_button": ["button[class*='confirm' i]", "button[class*='submit' i]",
                       "button[type='submit']"],

    # ── Confirmation reference ────────────────────────────────
    "confirm_ref":    [".booking-reference", "[class*='reference']",
                       "[class*='confirmation']", "[class*='booking-id']",
                       "strong", "h2", "h3"],
}

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).resolve().parent / "logs" / "booking.log",
            encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("vfs")

# ─────────────────────────────────────────────────────────────
# UTILITY HELPERS
# ─────────────────────────────────────────────────────────────

def _norm_date(raw: str) -> str:
    """Normalise any date to DD/MM/YYYY."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    # Already DD/MM/YYYY
    if len(raw) == 10 and raw[2] == "/" and raw[5] == "/":
        d, m, y = raw[:2], raw[3:5], raw[6:]
        return raw if int(d) <= 31 and int(m) <= 12 else f"{m}/{d}/{y}"
    # YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        y, m, d = raw[:4], raw[5:7], raw[8:]
        return f"{d}/{m}/{y}"
    return raw


def _norm_code(code: str) -> str:
    code = str(code or "").strip()
    return ("+" + code) if code and not code.startswith("+") else code


def _wait(driver, selectors, timeout: float = ELEMENT_WAIT):
    """Wait until any of the given CSS selectors is visible; return (el, sel)."""
    if isinstance(selectors, str):
        selectors = [selectors]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    return el, sel
            except Exception:
                pass
        time.sleep(0.15)
    return None, None


def _wait_xpath(driver, xpaths, timeout: float = ELEMENT_WAIT):
    """Wait until any XPath expression matches a visible element; return (el, xpath)."""
    if isinstance(xpaths, str):
        xpaths = [xpaths]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for xp in xpaths:
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    return el, xp
            except Exception:
                pass
        time.sleep(0.15)
    return None, None


def _wait_cf(driver) -> bool:
    """Block until Cloudflare challenge clears (or timeout)."""
    log.info("  [CF] Watching for Cloudflare challenge (max %ds)…", CF_POLL_MAX)
    deadline = time.monotonic() + CF_POLL_MAX
    while time.monotonic() < deadline:
        time.sleep(0.8)
        try:
            title = (driver.title or "").lower()
            src   = driver.page_source.lower()
            if ("just a moment" not in title
                    and "checking your browser" not in title
                    and "ddos-guard" not in src
                    and "cf-browser-verification" not in src):
                log.info("  [CF] Clear — URL: %s", driver.current_url)
                return True
        except Exception:
            pass
    log.warning("  [CF] Timed out — continuing anyway.")
    return False


def _js_fill(driver, selector, value: str, label: str = "") -> bool:
    """Fill an Angular <input> via JS native setter so reactive forms pick it up."""
    if not value:
        return False
    if isinstance(selector, list):
        sels = selector
    else:
        sels = [selector]

    el, matched = _wait(driver, sels)
    if not el:
        log.warning("  [warn] field not found: %s", label or sels)
        return False
    try:
        ok = driver.execute_script(
            """
            var s = arguments[0], val = arguments[1];
            var el = document.querySelector(s);
            if (!el) return false;
            var setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,'value').set;
            setter.call(el, val);
            ['input','change','blur'].forEach(function(e){
                el.dispatchEvent(new Event(e,{bubbles:true}));
            });
            return true;
            """,
            matched if matched else sels[0],
            str(value),
        )
        if ok:
            log.info("  [fill] %-25s = %s", label or matched, value)
            return True
    except Exception:
        pass
    # fallback: clear then send_keys char by char
    try:
        driver.execute_script("arguments[0].value = '';", el)
        el.send_keys(Keys.CONTROL + "a")
        el.send_keys(Keys.DELETE)
        for ch in str(value):
            el.send_keys(ch)
            time.sleep(random.uniform(0.03, 0.09))
        log.info("  [fill-sk] %-22s = %s", label or matched, value)
        return True
    except Exception as e:
        log.error("  [error] %s: %s", label, e)
        return False


def _fill_date(driver, selectors, value: str, label: str = "") -> bool:
    """Fill a date picker input. Tries JS native setter first, then direct key entry.
    VFS date pickers accept DD/MM/YYYY typed directly into the input."""
    if not value:
        return False
    if isinstance(selectors, str):
        selectors = [selectors]
    el, matched = _wait(driver, selectors)
    if not el:
        log.warning("  [warn] date field not found: %s", label)
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        # Try JS setter first
        ok = driver.execute_script(
            """
            var el = arguments[0], val = arguments[1];
            var setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,'value').set;
            setter.call(el, val);
            ['input','change','blur'].forEach(function(e){
                el.dispatchEvent(new Event(e,{bubbles:true}));
            });
            return true;
            """,
            el, str(value),
        )
        if ok:
            log.info("  [date] %-25s = %s", label, value)
            return True
    except Exception:
        pass
    # Fallback: click field, select-all, type
    try:
        el.click()
        time.sleep(0.2)
        el.send_keys(Keys.CONTROL + "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(str(value))
        el.send_keys(Keys.TAB)
        log.info("  [date-sk] %-23s = %s", label, value)
        return True
    except Exception as e:
        log.error("  [error] date fill %s: %s", label, e)
        return False


def _native_select(driver, selectors, text: str, label: str = "") -> bool:
    """Handle a plain HTML <select> element by matching visible option text."""
    if not text:
        return False
    if isinstance(selectors, str):
        selectors = [selectors]
    el, _ = _wait(driver, selectors, timeout=5)
    if not el:
        return False
    try:
        from selenium.webdriver.support.ui import Select as SeleniumSelect
        sel_obj = SeleniumSelect(el)
        target = text.strip().lower()
        for opt in sel_obj.options:
            if opt.text.strip().lower() == target or target in opt.text.strip().lower():
                sel_obj.select_by_visible_text(opt.text)
                log.info("  [sel-native] %-22s = %s", label, opt.text)
                return True
        log.warning("  [warn] native select no match '%s' for %s", text, label)
        return False
    except Exception as e:
        log.error("  [error] native-select %s: %s", label, e)
        return False


def _mat_select(driver, selectors, text: str, label: str = "") -> bool:
    """Click a mat-select and choose the option whose text matches `text`.
    Falls back to native <select> if mat-select is not found."""
    if not text:
        return False
    if isinstance(selectors, str):
        selectors = [selectors]

    el, sel_used = _wait(driver, selectors)
    if not el:
        # Try native <select> fallback — swap mat-select prefix for plain select
        native_sels = [s.replace("mat-select", "select") for s in selectors]
        if _native_select(driver, native_sels, text, label):
            return True
        log.warning("  [warn] mat-select not found: %s  (%s)", label, selectors)
        return False
    try:
        # If element is a plain <select>, delegate immediately
        tag = el.tag_name.lower()
        if tag == "select":
            return _native_select(driver, [sel_used], text, label)

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.click()
        # Wait for overlay panel to populate
        options = []
        deadline = time.monotonic() + DROPDOWN_WAIT
        while time.monotonic() < deadline:
            try:
                options = driver.find_elements(By.CSS_SELECTOR, "mat-option")
                if options:
                    break
            except Exception:
                pass
            time.sleep(0.1)

        target = text.strip().lower()
        for strict in (True, False):
            for opt in options:
                try:
                    opt_text = (opt.get_attribute("innerText") or "").strip()
                    match = (opt_text.lower() == target) if strict else (target in opt_text.lower())
                    if match:
                        driver.execute_script("arguments[0].click();", opt)
                        log.info("  [sel]  %-25s = %s", label, opt_text)
                        time.sleep(0.25)
                        return True
                except StaleElementReferenceException:
                    continue
        available = [opt.get_attribute("innerText") for opt in options[:8]]
        log.warning("  [warn] no option matched '%s' for %s (available: %s)",
                    text, label, available)
        # Close overlay with Escape
        try:
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))"
            )
        except Exception:
            pass
        return False
    except Exception as e:
        log.error("  [error] mat-select %s: %s", label, e)
        return False


def _click(driver, selectors, label: str = "") -> bool:
    if isinstance(selectors, str):
        selectors = [selectors]
    el, _ = _wait(driver, selectors, timeout=10)
    if not el:
        log.warning("  [warn] button not found: %s", label)
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        try:
            el.click()
        except ElementNotInteractableException:
            driver.execute_script("arguments[0].click();", el)
        log.info("  [click] %s", label)
        return True
    except Exception as e:
        log.error("  [error] click %s: %s", label, e)
        return False


def _current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _screenshot(driver, slug: str) -> None:
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        path = SCREENSHOTS_DIR / f"{ts}_{slug}.png"
        driver.save_screenshot(str(path))
        log.info("  [screenshot] %s", path.name)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# STEP HELPERS
# ─────────────────────────────────────────────────────────────

def do_login(driver) -> bool:
    """Navigate to login page and submit credentials."""
    log.info("[Login] Loading login page…")
    driver.get(VFS_LOGIN_URL)
    _wait_cf(driver)

    email_el, _ = _wait(driver, SEL["login_email"])
    pwd_el, _   = _wait(driver, SEL["login_password"])

    if not email_el or not pwd_el:
        log.error("[Login] Could not find login form fields.")
        _screenshot(driver, "login_fail")
        return False

    try:
        email_el.clear()
    except Exception:
        pass
    for ch in VFS_USERNAME:
        email_el.send_keys(ch)
        time.sleep(random.uniform(0.03, 0.09))
    time.sleep(0.3)

    try:
        pwd_el.clear()
    except Exception:
        pass
    for ch in VFS_PASSWORD:
        pwd_el.send_keys(ch)
        time.sleep(random.uniform(0.03, 0.09))
    time.sleep(0.3)

    _click(driver, SEL["login_button"], "login submit")

    # Wait for dashboard or booking landing
    post, _ = _wait(driver, [
        "app-dashboard", "app-home",
        "[class*='dashboard']",
        "button[class*='new-booking' i]",
        "a[href*='book-an-appointment']",
        "[routerlink*='book']",
    ], timeout=15)

    if post:
        log.info("[Login] Logged in successfully.")
        return True

    url = _current_url(driver)
    if "/login" in url:
        log.error("[Login] Still on login page. Check credentials.")
        _screenshot(driver, "login_failed")
        return False

    log.info("[Login] Could not confirm dashboard — URL: %s. Continuing anyway.", url)
    return True


def do_step1_appointment_details(driver, client: dict) -> bool:
    """
    Step 1 — /application-detail
    Fill: Application Centre, Appointment Category, Sub-category (+ trip reason if present).
    Then click Continue.
    """
    log.info("  [Step 1] Appointment Details…")

    centre_el, _ = _wait(driver, SEL["app_centre"], timeout=ELEMENT_WAIT)
    if not centre_el:
        url = _current_url(driver)
        log.error("  [Step 1] Application Centre dropdown not found. URL: %s", url)
        _screenshot(driver, "step1_fail")
        return False

    # Skip centre selection if already pre-filled (only 1 centre available)
    try:
        current_val = (centre_el.get_attribute("innerText") or "").strip()
    except Exception:
        current_val = ""
    if current_val and "select" not in current_val.lower() and "choose" not in current_val.lower():
        log.info("  [Step 1] Application Centre already set: %s", current_val)
    else:
        _mat_select(driver, SEL["app_centre"],
                    client.get("application_center", ""), "Application Centre")
    time.sleep(0.5)

    _mat_select(driver, SEL["appt_category"],
                client.get("visa_type", ""), "Appointment Category")
    time.sleep(0.5)

    _mat_select(driver, SEL["appt_subcategory"],
                client.get("service_center", ""), "Sub-category")
    time.sleep(0.5)

    if client.get("trip_reason"):
        el, _ = _wait(driver, SEL["trip_reason"], timeout=3)
        if el:
            _mat_select(driver, SEL["trip_reason"],
                        client.get("trip_reason", ""), "Purpose of Travel")
            time.sleep(0.3)

    _screenshot(driver, "step1_filled")
    clicked = _click(driver, SEL["step1_continue"], "Continue (Step 1)")
    time.sleep(STEP_WAIT)
    return clicked


def do_step2_your_details(driver, client: dict) -> bool:
    """
    Step 2 — /your-details
    Fill all personal info fields and click Continue.
    """
    log.info("  [Step 2] Your Details…")

    first_el, _ = _wait(driver, SEL["first_name"], timeout=ELEMENT_WAIT)
    if not first_el:
        url = _current_url(driver)
        log.error("  [Step 2] Personal info fields not found. URL: %s", url)
        _screenshot(driver, "step2_fail")
        return False

    dob    = _norm_date(client.get("date_of_birth", ""))
    expiry = _norm_date(client.get("passport_expiry", ""))
    cc     = _norm_code(client.get("mobile_country_code", ""))

    _js_fill(driver, SEL["first_name"],         client.get("first_name", ""),    "First Name")
    _js_fill(driver, SEL["last_name"],           client.get("last_name", ""),     "Last Name")
    _fill_date(driver, SEL["date_of_birth"],     dob,                             "Date of Birth")
    _js_fill(driver, SEL["passport_number"],     client.get("passport_number", ""), "Passport Number")
    _fill_date(driver, SEL["passport_expiry"],   expiry,                          "Passport Expiry")
    _js_fill(driver, SEL["mobile_country_code"], cc,                              "Country Code")
    _js_fill(driver, SEL["mobile_number"],       client.get("mobile_number", ""), "Mobile Number")

    _mat_select(driver, SEL["gender"],
                client.get("gender", ""), "Gender")
    _mat_select(driver, SEL["current_nationality"],
                client.get("current_nationality", ""), "Nationality")

    time.sleep(0.5)
    _screenshot(driver, "step2_filled")
    clicked = _click(driver, SEL["step2_continue"], "Continue (Step 2)")
    time.sleep(STEP_WAIT)
    return clicked


def do_step3_book_appointment(driver) -> bool:
    """Step 3 — /book-appointment: pick the first available slot."""
    log.info("  [Step 3] Book Appointment slot…")
    url = _current_url(driver)
    log.info("  [Step 3] URL: %s", url)

    slot_el, _ = _wait(driver, SEL["first_slot"], timeout=15)
    if slot_el:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", slot_el)
            slot_el.click()
            log.info("  [Step 3] Slot selected.")
            time.sleep(1)
        except Exception as e:
            log.warning("  [Step 3] Could not click slot: %s", e)
    else:
        log.warning("  [Step 3] No available slot element found — form may handle selection differently.")

    _screenshot(driver, "step3")
    clicked = _click(driver, SEL["step3_continue"], "Continue (Step 3)")
    time.sleep(STEP_WAIT)
    return clicked


def do_step4_services(driver) -> bool:
    """Step 4 — /services: accept defaults and continue."""
    log.info("  [Step 4] Services…")
    time.sleep(2)
    _screenshot(driver, "step4")
    clicked = _click(driver, SEL["step4_continue"], "Continue (Step 4)")
    time.sleep(STEP_WAIT)
    return clicked


def do_step5_review_confirm(driver) -> Optional[str]:
    """
    Step 5 — /review: click Confirm and grab reference number.
    Returns the booking reference string, or None.
    """
    log.info("  [Step 5] Review & Confirm…")
    time.sleep(2)
    _screenshot(driver, "step5_review")

    _click(driver, SEL["confirm_button"], "Confirm Booking")
    time.sleep(4)

    _screenshot(driver, "step5_confirmed")

    for sel in SEL["confirm_ref"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            text = (el.get_attribute("innerText") or "").strip()
            if text:
                log.info("  [Ref] %s", text)
                return text
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────────────────────
# PER-CLIENT BOOKING
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# BROWSER LAUNCH
# ─────────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        *glob.glob(os.path.expanduser("~/.cache/selenium/chrome/linux64/*/chrome")),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return (shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or shutil.which("chromium-browser"))


def launch_driver(headless: bool = False, proxy: str = "", profile_dir: str = "") -> uc.Chrome:
    """Launch an undetected-chromedriver Chrome instance."""
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1366,768")
    options.add_argument("--lang=en-US,en;q=0.9")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")

    if proxy:
        options.add_argument(f"--proxy-server={proxy}")
        options.add_argument("--proxy-bypass-list=localhost,127.0.0.1")
        log.info("[Browser] Proxy: %s", proxy)

    chrome_bin = _find_chrome()
    driver = uc.Chrome(
        options=options,
        headless=headless,
        use_subprocess=True,
        browser_executable_path=chrome_bin if chrome_bin else None,
    )
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(0)
    log.info("[Browser] undetected-chromedriver started (headless=%s)", headless)
    return driver


# ─────────────────────────────────────────────────────────────
# PER-CLIENT BOOKING  (runs in its own thread + browser)
# ─────────────────────────────────────────────────────────────

def book_client(client: dict, idx: int, total: int,
                headless: bool = False, proxy: str = "") -> dict:
    """
    Launch a dedicated Chrome window, log in, and complete the full 5-step form.
    Each client runs in its own thread with its own browser instance so all
    bookings happen in parallel without interfering with each other.
    Returns a result dict.
    """
    name = f"{client.get('first_name','')  } {client.get('last_name','')}".strip()
    log.info("\n%s", "="*60)
    log.info("[Client %d/%d] %s", idx, total, name)
    log.info("="*60)

    result = {
        "name": name,
        "email": client.get("email", ""),
        "status": "FAILED",
        "reference": "",
        "error": "",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    # Each client gets an isolated Chrome profile so sessions don't collide
    profile = os.path.join(CHROME_PROFILE, f"client_{idx}")
    driver = None
    try:
        driver = launch_driver(headless=headless, proxy=proxy, profile_dir=profile)

        # ── Login ────────────────────────────────────────────
        logged_in = do_login(driver)
        if not logged_in:
            result["error"] = "Login failed"
            return result

        # ── Navigate to booking form ─────────────────────────
        time.sleep(1.5)
        start_url = _current_url(driver)
        log.info("  [nav] Dashboard URL: %s", start_url)

        # Try CSS selectors, then XPath text-match for the button
        start_btn, _ = _wait(driver, SEL["start_booking"], timeout=6)
        if not start_btn:
            start_btn, _ = _wait_xpath(driver, [
                "//button[normalize-space()='Start New Booking']",
                "//button[contains(text(),'Start New Booking')]",
                "//a[contains(text(),'Start New Booking')]",
            ], timeout=6)
        if start_btn:
            log.info("  [nav] Clicking 'Start New Booking'…")
            try:
                driver.execute_script("arguments[0].click();", start_btn)
            except Exception:
                pass
            time.sleep(PAGE_NAV_WAIT)
        else:
            log.warning("  [nav] 'Start New Booking' not found — navigating directly.")
            driver.get(VFS_BOOKING_URL)
            _wait_cf(driver)
            time.sleep(PAGE_NAV_WAIT)

        log.info("  [nav] Booking URL: %s", _current_url(driver))

        # ── Step 1: Appointment Details ──────────────────────
        ok = do_step1_appointment_details(driver, client)
        if not ok:
            result["error"] = "Step 1 (Appointment Details) failed"
            return result

        # ── Step 2: Your Details ─────────────────────────────
        ok = do_step2_your_details(driver, client)
        if not ok:
            result["error"] = "Step 2 (Your Details) failed"
            return result

        # ── Step 3: Book Appointment ─────────────────────────
        do_step3_book_appointment(driver)

        # ── Step 4: Services ─────────────────────────────────
        do_step4_services(driver)

        # ── Step 5: Review & Confirm ─────────────────────────
        ref = do_step5_review_confirm(driver)

        if ref:
            result["status"]    = "BOOKED"
            result["reference"] = ref
            log.info("[Client %d] ✓  BOOKED — reference: %s", idx, ref)
        else:
            result["status"] = "SUBMITTED"
            log.info("[Client %d] ✓  Form submitted — no reference text found. Check browser.", idx)

        # Keep the window open briefly so the user can verify
        time.sleep(5)

    except Exception as exc:
        log.exception("[Client %d] Unhandled error: %s", idx, exc)
        result["error"] = str(exc)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────────────────────
# RESULTS WRITER
# ─────────────────────────────────────────────────────────────

def save_results(results: list[dict]) -> None:
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["name", "email", "status", "reference", "error", "timestamp"]
    write_header = not RESULTS_CSV.exists()
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerows(results)
    log.info("[Results] Written to %s", RESULTS_CSV)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="VFS Global Guinea-Bissau → Portugal unified booking bot"
    )
    ap.add_argument("--warmup", action="store_true",
                    help="Open browser for manual CF + login warm-up. Run BEFORE the booking window.")
    ap.add_argument("--max-clients", type=int, default=MAX_CLIENTS, metavar="N",
                    help="Max number of clients to book (default %(default)s).")
    ap.add_argument("--proxy", default=os.getenv("VFS_PROXY", ""),
                    help="Proxy URL e.g. socks5://user:pass@host:port  (env: VFS_PROXY)")
    ap.add_argument("--headless", action="store_true",
                    help="Run Chrome in headless mode (not recommended — CF blocks this).")
    ap.add_argument("--sequential", action="store_true",
                    help="Book clients one at a time instead of launching parallel windows.")
    ap.add_argument("--csv", default=str(CLIENTS_CSV), metavar="PATH",
                    help="Path to clients CSV (default: %(default)s).")
    args = ap.parse_args()

    # Ensure log dirs exist
    (Path(__file__).parent / "logs").mkdir(exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load clients
    df = pd.read_csv(args.csv, dtype=str).fillna("")
    clients = df.to_dict(orient="records")[: args.max_clients]
    log.info("[Main] Loaded %d client(s) from %s", len(clients), args.csv)

    if not args.proxy:
        log.warning("[Main] No proxy set — if geo-blocked you may receive 403. "
                    "Set via --proxy or VFS_PROXY env var.")

    # ── WARMUP MODE ─────────────────────────────────────────
    if args.warmup:
        log.info("\n%s", "="*60)
        log.info("[Warmup] MANUAL SESSION SETUP")
        log.info("="*60)
        log.info("  The browser is open at: %s", VFS_LOGIN_URL)
        log.info("  1. Solve any Cloudflare challenge.")
        log.info("  2. Log in with your VFS credentials.")
        log.info("  3. Navigate to 'Start New Booking' so CF cookies warm up.")
        log.info("  4. Come back here and press ENTER.")
        log.info("="*60)
        profile = os.path.join(CHROME_PROFILE, "warmup")
        driver = launch_driver(headless=False, proxy=args.proxy, profile_dir=profile)
        driver.get(VFS_LOGIN_URL)
        _wait_cf(driver)
        try:
            input("  >>> Press ENTER once done: ")
        except EOFError:
            time.sleep(20)
        log.info("[Warmup] Warming booking page…")
        driver.get(VFS_BOOKING_URL)
        _wait_cf(driver)
        el, _ = _wait(driver, SEL["app_centre"], timeout=60)
        if el:
            log.info("[Warmup] ✓ Booking form loaded — session warmed.")
        else:
            log.warning("[Warmup] ✗ Booking form did NOT load. Stuck at: %s", _current_url(driver))
        time.sleep(3)
        driver.quit()
        log.info("[Warmup] Profile saved to: %s", CHROME_PROFILE)
        log.info("[Warmup] Run without --warmup when the booking window opens.")
        return

    # ── BOOKING MODE ─────────────────────────────────────────
    log.info("\n[Main] Starting %d booking(s)  [mode: %s]…",
             len(clients), "sequential" if args.sequential else "parallel")
    t0 = time.monotonic()
    results = []

    if args.sequential:
        for i, client in enumerate(clients, start=1):
            r = book_client(client, i, len(clients),
                            headless=args.headless, proxy=args.proxy)
            results.append(r)
    else:
        # Each client gets its own thread and its own Chrome window
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            futures = {
                pool.submit(book_client, client, i, len(clients),
                            args.headless, args.proxy): i
                for i, client in enumerate(clients, start=1)
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    idx = futures[future]
                    log.error("[Client %d] Thread raised: %s", idx, exc)
                    results.append({
                        "name": f"Client {idx}", "email": "", "status": "FAILED",
                        "reference": "", "error": str(exc),
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                    })

    elapsed = time.monotonic() - t0
    log.info("\n[Main] All %d client(s) done in %.1fs.", len(clients), elapsed)

    # ── Summary ──────────────────────────────────────────────
    log.info("\n%s  RESULTS  %s", "─"*25, "─"*25)
    for r in results:
        log.info("  %-30s  %-10s  %s", r["name"], r["status"],
                 r.get("reference", r.get("error", "")))
    log.info("─"*61)

    save_results(results)


if __name__ == "__main__":
    main()
