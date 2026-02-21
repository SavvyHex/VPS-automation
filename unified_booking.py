#!/usr/bin/env python3
"""
VFS Global Guinea-Bissau → Portugal  ·  Unified Multi-Client Booking Bot
=========================================================================
Uses nodriver (undetectable Chrome) to bypass Cloudflare and automates
the full 5-step VFS booking flow for every row in clients.csv.

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
import asyncio
import csv
import glob
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import nodriver as uc
import pandas as pd

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


async def _wait(tab, selectors, timeout: float = ELEMENT_WAIT):
    """Wait until any of the given CSS selectors is present; return (el, sel)."""
    if isinstance(selectors, str):
        selectors = [selectors]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                el = await tab.find(sel, timeout=0.5)
                if el:
                    return el, sel
            except Exception:
                pass
        await asyncio.sleep(0.15)
    return None, None


async def _wait_cf(tab) -> bool:
    """Block until Cloudflare challenge clears (or timeout)."""
    log.info("  [CF] Watching for Cloudflare challenge (max %ds)…", CF_POLL_MAX)
    deadline = time.monotonic() + CF_POLL_MAX
    while time.monotonic() < deadline:
        await asyncio.sleep(0.8)
        try:
            title = (await tab.get_title() or "").lower()
            url   = await tab.evaluate("window.location.href")
            if "just a moment" not in title and "checking your browser" not in title:
                log.info("  [CF] Clear — URL: %s", url)
                return True
        except Exception:
            pass
    log.warning("  [CF] Timed out — continuing anyway.")
    return False


async def _js_fill(tab, selector: str | list, value: str, label: str = "") -> bool:
    """Fill an Angular <input> via JS native setter so reactive forms pick it up."""
    if not value:
        return False
    if isinstance(selector, list):
        sels = selector
    else:
        sels = [selector]

    el, matched = await _wait(tab, sels)
    if not el:
        log.warning("  [warn] field not found: %s", label or sels)
        return False
    try:
        ok = await tab.evaluate(
            f"""(function(){{
                var s = {repr(matched if matched else sels[0])};
                var el = document.querySelector(s);
                if (!el) return false;
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype,'value').set;
                setter.call(el, {repr(str(value))});
                ['input','change','blur'].forEach(function(e){{
                    el.dispatchEvent(new Event(e,{{bubbles:true}}));
                }});
                return true;
            }})()"""
        )
        if ok:
            log.info("  [fill] %-25s = %s", label or matched, value)
            return True
    except Exception:
        pass
    # fallback: send_keys
    try:
        await el.send_keys(str(value))
        log.info("  [fill-sk] %-22s = %s", label or matched, value)
        return True
    except Exception as e:
        log.error("  [error] %s: %s", label, e)
        return False


async def _mat_select(tab, selectors: str | list, text: str, label: str = "") -> bool:
    """Click a mat-select and choose the option whose text matches `text`."""
    if not text:
        return False
    if isinstance(selectors, str):
        selectors = [selectors]

    el, sel_used = await _wait(tab, selectors)
    if not el:
        log.warning("  [warn] mat-select not found: %s  (%s)", label, selectors)
        return False
    try:
        await el.click()
        # Wait for the overlay panel to populate
        options = []
        deadline = time.monotonic() + DROPDOWN_WAIT
        while time.monotonic() < deadline:
            options = await tab.find_all("mat-option")
            if options:
                break
            await asyncio.sleep(0.1)

        target = text.strip().lower()
        for strict in (True, False):
            for opt in options:
                try:
                    opt_text = (await opt.get_attribute("innerText") or "").strip()
                    match = (opt_text.lower() == target) if strict else (target in opt_text.lower())
                    if match:
                        await opt.click()
                        log.info("  [sel]  %-25s = %s", label, opt_text)
                        await asyncio.sleep(0.25)
                        return True
                except Exception:
                    continue
        log.warning("  [warn] no option matched '%s' for %s (available: %s)",
                    text, label, [await o.get_attribute("innerText") for o in options[:8]])
        # close the overlay by pressing Escape
        try:
            await tab.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        except Exception:
            pass
        return False
    except Exception as e:
        log.error("  [error] mat-select %s: %s", label, e)
        return False


async def _click(tab, selectors: str | list, label: str = "") -> bool:
    if isinstance(selectors, str):
        selectors = [selectors]
    el, _ = await _wait(tab, selectors, timeout=10)
    if not el:
        log.warning("  [warn] button not found: %s", label)
        return False
    try:
        await el.click()
        log.info("  [click] %s", label)
        return True
    except Exception as e:
        log.error("  [error] click %s: %s", label, e)
        return False


async def _current_url(tab) -> str:
    try:
        return await tab.evaluate("window.location.href") or ""
    except Exception:
        return ""


async def _screenshot(tab, slug: str) -> None:
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        path = SCREENSHOTS_DIR / f"{ts}_{slug}.png"
        await tab.save_screenshot(str(path))
        log.info("  [screenshot] %s", path.name)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# STEP HELPERS
# ─────────────────────────────────────────────────────────────

async def do_login(tab) -> bool:
    """Navigate to login page and submit credentials."""
    log.info("[Login] Loading login page…")
    await tab.get(VFS_LOGIN_URL)
    await _wait_cf(tab)

    email_el, _ = await _wait(tab, SEL["login_email"])
    pwd_el, _   = await _wait(tab, SEL["login_password"])

    if not email_el or not pwd_el:
        log.error("[Login] Could not find login form fields.")
        await _screenshot(tab, "login_fail")
        return False

    try:
        await email_el.clear_input()
    except Exception:
        pass
    await email_el.send_keys(VFS_USERNAME)
    await asyncio.sleep(0.3)

    try:
        await pwd_el.clear_input()
    except Exception:
        pass
    await pwd_el.send_keys(VFS_PASSWORD)
    await asyncio.sleep(0.3)

    await _click(tab, SEL["login_button"], "login submit")

    # Wait for dashboard or booking landing
    post, _ = await _wait(tab, [
        "app-dashboard", "app-home",
        "[class*='dashboard']",
        "button[class*='new-booking' i]",
        "a[href*='book-an-appointment']",
        "[routerlink*='book']",
    ], timeout=15)

    if post:
        log.info("[Login] Logged in successfully.")
        return True

    url = await _current_url(tab)
    if "/login" in url:
        log.error("[Login] Still on login page. Check credentials.")
        await _screenshot(tab, "login_failed")
        return False

    log.info("[Login] Could not confirm dashboard — URL: %s. Continuing anyway.", url)
    return True


async def do_step1_appointment_details(tab, client: dict) -> bool:
    """
    Step 1 — /application-detail
    Fill: Application Centre, Appointment Category, Sub-category (+ trip reason if present).
    Then click Continue.
    """
    log.info("  [Step 1] Appointment Details…")

    # The page should already be open; wait for the centre dropdown
    centre_el, _ = await _wait(tab, SEL["app_centre"], timeout=ELEMENT_WAIT)
    if not centre_el:
        url = await _current_url(tab)
        log.error("  [Step 1] Application Centre dropdown not found. URL: %s", url)
        await _screenshot(tab, "step1_fail")
        return False

    # Application Centre
    await _mat_select(tab, SEL["app_centre"],
                      client.get("application_center", ""), "Application Centre")
    await asyncio.sleep(0.5)

    # Appointment Category (visa_type column maps here)
    await _mat_select(tab, SEL["appt_category"],
                      client.get("visa_type", ""), "Appointment Category")
    await asyncio.sleep(0.5)

    # Sub-category (service_center column)
    await _mat_select(tab, SEL["appt_subcategory"],
                      client.get("service_center", ""), "Sub-category")
    await asyncio.sleep(0.5)

    # Purpose of travel (optional — not always present)
    if client.get("trip_reason"):
        el, _ = await _wait(tab, SEL["trip_reason"], timeout=3)
        if el:
            await _mat_select(tab, SEL["trip_reason"],
                              client.get("trip_reason", ""), "Purpose of Travel")
            await asyncio.sleep(0.3)

    await _screenshot(tab, "step1_filled")

    clicked = await _click(tab, SEL["step1_continue"], "Continue (Step 1)")
    await asyncio.sleep(STEP_WAIT)
    return clicked


async def do_step2_your_details(tab, client: dict) -> bool:
    """
    Step 2 — /your-details
    Fill all personal info fields and click Continue.
    """
    log.info("  [Step 2] Your Details…")

    # Wait for the first personal-info field
    first_el, _ = await _wait(tab, SEL["first_name"], timeout=ELEMENT_WAIT)
    if not first_el:
        url = await _current_url(tab)
        log.error("  [Step 2] Personal info fields not found. URL: %s", url)
        await _screenshot(tab, "step2_fail")
        return False

    dob     = _norm_date(client.get("date_of_birth", ""))
    expiry  = _norm_date(client.get("passport_expiry", ""))
    cc      = _norm_code(client.get("mobile_country_code", ""))

    await _js_fill(tab, SEL["first_name"],         client.get("first_name", ""),    "First Name")
    await _js_fill(tab, SEL["last_name"],           client.get("last_name", ""),     "Last Name")
    await _js_fill(tab, SEL["date_of_birth"],       dob,                             "Date of Birth")
    await _js_fill(tab, SEL["email"],               client.get("email", ""),         "Email")
    await _js_fill(tab, SEL["mobile_country_code"], cc,                              "Country Code")
    await _js_fill(tab, SEL["mobile_number"],       client.get("mobile_number", ""), "Mobile Number")
    await _js_fill(tab, SEL["passport_number"],     client.get("passport_number", ""), "Passport Number")
    await _js_fill(tab, SEL["passport_expiry"],     expiry,                          "Passport Expiry")

    await _mat_select(tab, SEL["gender"],
                      client.get("gender", ""), "Gender")
    await _mat_select(tab, SEL["current_nationality"],
                      client.get("current_nationality", ""), "Nationality")

    await asyncio.sleep(0.5)
    await _screenshot(tab, "step2_filled")

    clicked = await _click(tab, SEL["step2_continue"], "Continue (Step 2)")
    await asyncio.sleep(STEP_WAIT)
    return clicked


async def do_step3_book_appointment(tab) -> bool:
    """Step 3 — /book-appointment: pick the first available slot."""
    log.info("  [Step 3] Book Appointment slot…")
    url = await _current_url(tab)
    log.info("  [Step 3] URL: %s", url)

    # If no slot selector found within timeout, just try to continue
    slot_el, _ = await _wait(tab, SEL["first_slot"], timeout=15)
    if slot_el:
        try:
            await slot_el.click()
            log.info("  [Step 3] Slot selected.")
            await asyncio.sleep(1)
        except Exception as e:
            log.warning("  [Step 3] Could not click slot: %s", e)
    else:
        log.warning("  [Step 3] No available slot element found — form may handle selection differently.")

    await _screenshot(tab, "step3")
    clicked = await _click(tab, SEL["step3_continue"], "Continue (Step 3)")
    await asyncio.sleep(STEP_WAIT)
    return clicked


async def do_step4_services(tab) -> bool:
    """Step 4 — /services: accept defaults and continue."""
    log.info("  [Step 4] Services…")
    await asyncio.sleep(2)
    await _screenshot(tab, "step4")
    clicked = await _click(tab, SEL["step4_continue"], "Continue (Step 4)")
    await asyncio.sleep(STEP_WAIT)
    return clicked


async def do_step5_review_confirm(tab) -> Optional[str]:
    """
    Step 5 — /review: click Confirm and grab reference number.
    Returns the booking reference string, or None.
    """
    log.info("  [Step 5] Review & Confirm…")
    await asyncio.sleep(2)
    await _screenshot(tab, "step5_review")

    await _click(tab, SEL["confirm_button"], "Confirm Booking")
    await asyncio.sleep(4)   # allow network round-trip

    await _screenshot(tab, "step5_confirmed")

    # Scrape reference number
    for sel in SEL["confirm_ref"]:
        try:
            el = await tab.find(sel, timeout=0.5)
            if el:
                text = (await el.get_attribute("innerText") or "").strip()
                if text:
                    log.info("  [Ref] %s", text)
                    return text
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────────────────────
# PER-CLIENT BOOKING
# ─────────────────────────────────────────────────────────────

async def book_client(browser, client: dict, idx: int, total: int) -> dict:
    """
    Open a tab, perform login (if needed), navigate the full 5-step form.
    Returns a result dict.
    """
    name = f"{client.get('first_name','')} {client.get('last_name','')}".strip()
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

    tab = await browser.get(VFS_LOGIN_URL, new_tab=(idx > 1))
    await _wait_cf(tab)

    # ── Login ────────────────────────────────────────────────
    url = await _current_url(tab)
    if "/login" in url or "login" in (await tab.get_title() or "").lower():
        logged_in = await do_login(tab)
        if not logged_in:
            result["error"] = "Login failed"
            return result
    else:
        log.info("  [skip] Already logged in.")

    # ── Navigate to booking form ─────────────────────────────
    # Try to find the "Start New Booking" button on the dashboard
    await asyncio.sleep(1.5)
    start_url = await _current_url(tab)
    log.info("  [nav] Dashboard URL: %s", start_url)

    start_btn, _ = await _wait(tab, SEL["start_booking"], timeout=8)
    if start_btn:
        log.info("  [nav] Clicking 'Start New Booking'…")
        try:
            await start_btn.click()
        except Exception:
            await tab.evaluate(
                "document.querySelector(\"button[class*='new-booking' i], "
                "button[routerlink*='book' i]\").click()"
            )
        await asyncio.sleep(PAGE_NAV_WAIT)
    else:
        # Fall back: navigate directly to booking URL
        log.warning("  [nav] 'Start New Booking' button not found — navigating directly.")
        await tab.get(VFS_BOOKING_URL)
        await _wait_cf(tab)
        await asyncio.sleep(PAGE_NAV_WAIT)

    booking_url = await _current_url(tab)
    log.info("  [nav] Booking URL: %s", booking_url)

    # ── Step 1: Appointment Details ──────────────────────────
    ok = await do_step1_appointment_details(tab, client)
    if not ok:
        result["error"] = "Step 1 (Appointment Details) failed"
        return result

    # ── Step 2: Your Details ─────────────────────────────────
    ok = await do_step2_your_details(tab, client)
    if not ok:
        result["error"] = "Step 2 (Your Details) failed"
        return result

    # ── Step 3: Book Appointment ─────────────────────────────
    await do_step3_book_appointment(tab)

    # ── Step 4: Services ─────────────────────────────────────
    await do_step4_services(tab)

    # ── Step 5: Review & Confirm ─────────────────────────────
    ref = await do_step5_review_confirm(tab)

    if ref:
        result["status"]    = "BOOKED"
        result["reference"] = ref
        log.info("[Client %d] ✓  BOOKED — reference: %s", idx, ref)
    else:
        result["status"] = "SUBMITTED"
        log.info("[Client %d] ✓  Form submitted — no reference text found. Check browser tab.", idx)

    return result


# ─────────────────────────────────────────────────────────────
# BROWSER LAUNCH
# ─────────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    candidates = [
        # Windows standard paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # Linux
        *glob.glob(os.path.expanduser(
            "~/.cache/selenium/chrome/linux64/*/chrome"
        )),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return (shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or shutil.which("chromium-browser"))


async def launch_browser(headless: bool = False, proxy: str = ""):
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    chrome_bin = _find_chrome()

    extra_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--window-size=1366,768",
        f"--user-data-dir={CHROME_PROFILE}",
        "--lang=en-US,en;q=0.9",
    ]
    if proxy:
        extra_args.append(f"--proxy-server={proxy}")
        extra_args.append("--proxy-bypass-list=localhost,127.0.0.1")
        log.info("[Browser] Proxy: %s", proxy)

    kwargs: dict = {
        "headless": headless,
        "user_data_dir": CHROME_PROFILE,
        "browser_args": extra_args,
    }
    if chrome_bin:
        log.info("[Browser] Chrome binary: %s", chrome_bin)
        kwargs["browser_executable_path"] = chrome_bin

    return await uc.start(**kwargs)


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

async def main() -> None:
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
                    help="Book clients one at a time instead of parallel tabs.")
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

    browser = await launch_browser(headless=args.headless, proxy=args.proxy)
    first_tab = await browser.get(VFS_LOGIN_URL)

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
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, input, "  >>> Press ENTER once done: ")
        except EOFError:
            await asyncio.sleep(20)

        # Navigate to booking URL to cache CF cookie
        log.info("[Warmup] Warming booking page…")
        warm_tab = await browser.get(VFS_BOOKING_URL, new_tab=True)
        ok, _ = await _wait(warm_tab, SEL["app_centre"], timeout=60)
        if ok:
            log.info("[Warmup] ✓ Booking form loaded — session warmed.")
        else:
            stuck = await _current_url(warm_tab)
            log.warning("[Warmup] ✗ Booking form did NOT load. Stuck at: %s", stuck)
            log.info("[Warmup]   Cookies may still be partially saved. Try running without --warmup.")
        await asyncio.sleep(3)
        log.info("[Warmup] Profile saved to: %s", CHROME_PROFILE)
        log.info("[Warmup] Run without --warmup when the booking window opens.")
        return

    # ── BOOKING MODE ─────────────────────────────────────────
    # Log in on the first tab to establish a shared session
    logged_in = await do_login(first_tab)
    if not logged_in:
        log.error("[Main] Login failed. Leaving browser open for 60s — solve CF manually then re-run.")
        await asyncio.sleep(60)
        return

    log.info("\n[Main] Starting %d booking(s)  [mode: %s]…",
             len(clients), "sequential" if args.sequential else "parallel")
    t0 = time.monotonic()
    results = []

    if args.sequential:
        for i, client in enumerate(clients, start=1):
            r = await book_client(browser, client, i, len(clients))
            results.append(r)
    else:
        tasks = [
            book_client(browser, client, i, len(clients))
            for i, client in enumerate(clients, start=1)
        ]
        results = list(await asyncio.gather(*tasks, return_exceptions=False))

    elapsed = time.monotonic() - t0
    log.info("\n[Main] All %d client(s) done in %.1fs.", len(clients), elapsed)

    # ── Summary ──────────────────────────────────────────────
    log.info("\n%s  RESULTS  %s", "─"*25, "─"*25)
    for r in results:
        log.info("  %-30s  %-10s  %s", r["name"], r["status"], r.get("reference", r.get("error", "")))
    log.info("─"*61)

    save_results(results)

    log.info("\n[Main] Browser stays open for review — press Ctrl+C to exit.")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        log.info("[Main] Exiting.")


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
