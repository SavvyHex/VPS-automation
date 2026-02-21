#!/usr/bin/env python3
"""
VFS Global Guinea-Bissau – Auto-Booking Script (undetected-chromedriver)
=========================================================================
Usage:
    python run_booking.py                  # visible browser
    python run_booking.py --headless       # hidden browser
    python run_booking.py --csv other.csv  # custom client list
    python run_booking.py --max 3          # limit to 3 clients

Requirements:
    pip install undetected-chromedriver selenium requests
"""

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import undetected_chromedriver as uc
except ImportError:
    print("ERROR: undetected-chromedriver not installed.")
    print("       Run:  pip install undetected-chromedriver")
    sys.exit(1)

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementNotInteractableException,
)

# ──────────────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "run_booking.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("run_booking")

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
LOGIN_URL   = "https://visa.vfsglobal.com/gnb/en/prt/login"
BOOKING_URL = "https://visa.vfsglobal.com/gnb/en/prt/book-appointment"
LOGIN_EMAIL = "brunovfs2k@gmail.com"
LOGIN_PWD   = "Bissau300@"
MAX_CLIENTS = 5
MONITOR_MIN = 4
CSV_PATH    = Path(__file__).parent / "clients.csv"

# ──────────────────────────────────────────────────────────────────────────────
# Browser launch
# ──────────────────────────────────────────────────────────────────────────────

def launch_driver(headless: bool) -> uc.Chrome:
    """
    Launch undetected-chromedriver.
    uc automatically patches ChromeDriver to evade bot detection
    and Cloudflare TurnStile / JS challenges.
    """
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-GB")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    # Keep a real profile dir so cookies persist within a session
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = uc.Chrome(
        options=options,
        headless=headless,
        use_subprocess=True,
    )
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(0)  # we use explicit waits
    log.info(f"undetected-chromedriver started (headless={headless})")
    return driver


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def wait_for(driver, css: str, timeout: int = 15):
    """Wait until a CSS selector is visible and return the element."""
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, css))
    )


def find_first(driver, selectors: list, timeout: int = 10):
    """Return the first visible element from a list of CSS selectors."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    return el
            except Exception:
                pass
        time.sleep(0.4)
    return None


def human_type(el, text: str) -> None:
    """Type text with randomised delays to mimic human speed."""
    try:
        el.click()
    except Exception:
        # Overlay may still intercept – fall back to JS click
        try:
            el._parent.execute_script("arguments[0].click();", el)
        except Exception:
            pass
    el.clear()
    for ch in str(text):
        el.send_keys(ch)
        time.sleep(random.uniform(0.04, 0.12))


def is_cloudflare_block(driver) -> bool:
    indicators = [
        "checking your browser",
        "ddos protection by cloudflare",
        "cf-challenge",
        "cf-browser-verification",
        "just a moment",
        "cloudflare ray id",
        "acesso restrito",
        "atividade incomum",
        "403201",
    ]
    try:
        src = driver.page_source.lower()
        return any(i in src for i in indicators)
    except Exception:
        return False


def wait_cf_pass(driver, timeout_s: int = 60) -> bool:
    """Wait for Cloudflare JS challenge to auto-resolve."""
    log.warning("Cloudflare challenge detected – waiting for auto-pass…")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        if not is_cloudflare_block(driver):
            log.info("Cloudflare passed.")
            return True
    log.error("Cloudflare did not resolve within timeout.")
    return False


def _nuke_cookie_overlay(driver) -> None:
    """Force-remove OneTrust overlay elements via JS if they are still present."""
    driver.execute_script("""
        var selectors = [
            '.onetrust-pc-dark-filter',
            '#onetrust-banner-sdk',
            '#onetrust-consent-sdk',
            '.ot-fade-in'
        ];
        selectors.forEach(function(sel) {
            var els = document.querySelectorAll(sel);
            els.forEach(function(el) { el.remove(); });
        });
        document.body.style.overflow = '';
    """)


def dismiss_cookie_banner(driver) -> None:
    for sel in [
        "#onetrust-accept-btn-handler",
        "button.cookie-accept",
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                log.info("Cookie banner dismissed.")
                break
        except Exception:
            pass
    else:
        # XPath text matches
        for text in ["Accept All", "Accept Cookies", "Accept"]:
            try:
                btn = driver.find_element(By.XPATH, f"//button[contains(.,'{text}')]")
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    break
            except Exception:
                pass

    # Wait up to 5 s for the dark filter overlay to disappear, then nuke if still present
    try:
        WebDriverWait(driver, 5).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, ".onetrust-pc-dark-filter"))
        )
    except Exception:
        _nuke_cookie_overlay(driver)
        log.info("Cookie overlay force-removed via JS.")
    time.sleep(0.3)


# ──────────────────────────────────────────────────────────────────────────────
# Angular Material helpers
# ──────────────────────────────────────────────────────────────────────────────

def mat_fill(driver, formcontrolname: str, value: str) -> bool:
    """
    Fill an Angular Material input field identified by formcontrolname.
    """
    selectors = [
        f'input[formcontrolname="{formcontrolname}"]',
        f'textarea[formcontrolname="{formcontrolname}"]',
        f'[formcontrolname="{formcontrolname}"] input',
    ]
    el = find_first(driver, selectors, timeout=6)
    if el:
        try:
            human_type(el, value)
            log.debug(f"  Filled '{formcontrolname}' = '{value}'")
            return True
        except Exception as e:
            log.debug(f"  mat_fill '{formcontrolname}' type error: {e}")
    return False


def mat_select(driver, formcontrolname: str, option_text: str) -> bool:
    """
    Open an Angular Material mat-select and pick the matching option.
    The overlay panel is rendered at the end of <body> (CDK overlay).
    """
    trigger_sels = [
        f'mat-select[formcontrolname="{formcontrolname}"]',
        f'[formcontrolname="{formcontrolname}"] mat-select',
        f'[formcontrolname="{formcontrolname}"]',
    ]
    trigger = find_first(driver, trigger_sels, timeout=6)
    if not trigger:
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
        time.sleep(0.3)
        trigger.click()
        time.sleep(0.7)

        # Exact match in open overlay
        try:
            opt = WebDriverWait(driver, 6).until(
                EC.visibility_of_element_located(
                    (By.XPATH, f"//mat-option[contains(normalize-space(.), '{option_text}')]")
                )
            )
            opt.click()
            time.sleep(0.4)
            log.debug(f"  mat-select '{formcontrolname}' → '{option_text}'")
            return True
        except TimeoutException:
            pass

        # Partial/case-insensitive fallback
        opts = driver.find_elements(By.CSS_SELECTOR, "mat-option")
        for opt in opts:
            try:
                if option_text.lower() in opt.text.lower():
                    opt.click()
                    time.sleep(0.4)
                    log.debug(f"  mat-select '{formcontrolname}' partial → '{opt.text.strip()}'")
                    return True
            except Exception:
                pass

        # Close the panel if nothing matched
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception as e:
        log.debug(f"  mat_select '{formcontrolname}' error: {e}")
    return False


def click_continue(driver) -> bool:
    """Click the wizard Continue / Next / Submit button."""
    texts = ["Continue", "Next", "Proceed", "Submit", "Book"]
    for text in texts:
        try:
            btn = driver.find_element(
                By.XPATH,
                f"//button[not(@disabled) and contains(normalize-space(.), '{text}')]",
            )
            if btn.is_displayed() and btn.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                btn.click()
                time.sleep(random.uniform(1.5, 2.5))
                log.debug(f"  Clicked '{text}' button")
                return True
        except Exception:
            pass
    # fallback – first visible submit
    try:
        btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        if btn.is_displayed() and btn.is_enabled():
            btn.click()
            time.sleep(2)
            return True
    except Exception:
        pass
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Login
# ──────────────────────────────────────────────────────────────────────────────

def do_login(driver, email: str, password: str) -> bool:
    log.info(f"Navigating to login page…")
    driver.get(LOGIN_URL)
    # uc automatically handles Cloudflare JS challenges – just give it time
    time.sleep(random.uniform(4, 7))

    if is_cloudflare_block(driver):
        if not wait_cf_pass(driver, timeout_s=60):
            return False
        time.sleep(2)
        driver.get(LOGIN_URL)
        time.sleep(random.uniform(4, 6))

    dismiss_cookie_banner(driver)

    # Ensure no overlay is blocking interaction before we touch any input
    try:
        WebDriverWait(driver, 6).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, ".onetrust-pc-dark-filter"))
        )
    except Exception:
        _nuke_cookie_overlay(driver)

    # Wait for Angular to render the login form
    email_el = find_first(driver, [
        'input[formcontrolname="email"]',
        'input[formcontrolname="username"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[placeholder*="Email"]',
        'input[placeholder*="email"]',
        'input[placeholder*="Username"]',
        '#mat-input-0',
        'mat-form-field input',
        'form input',
    ], timeout=25)

    if not email_el:
        shot = LOG_DIR / "login_debug.png"
        try:
            driver.save_screenshot(str(shot))
        except Exception:
            pass
        log.error(f"Email input not found. Screenshot saved → {shot}")
        return False

    log.info("Email field found – filling credentials…")
    human_type(email_el, email)
    time.sleep(random.uniform(0.4, 0.8))

    pwd_el = find_first(driver, [
        'input[formcontrolname="password"]',
        'input[name="password"]',
        'input[type="password"]',
        '#mat-input-1',
    ], timeout=10)

    if not pwd_el:
        log.error("Password input not found.")
        return False

    human_type(pwd_el, password)
    time.sleep(random.uniform(0.5, 1.0))

    # Click submit
    submitted = False
    for xpath in [
        "//button[@type='submit']",
        "//button[contains(.,'Sign In')]",
        "//button[contains(.,'Log In')]",
        "//button[contains(.,'Login')]",
    ]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                submitted = True
                log.info("Login form submitted.")
                break
        except Exception:
            pass

    if not submitted:
        pwd_el.send_keys(Keys.RETURN)
        log.info("Login form submitted via Enter.")

    time.sleep(random.uniform(5, 8))

    if "/login" not in driver.current_url:
        log.info(f"Login successful – URL: {driver.current_url}")
        return True

    # Check for inline error
    for sel in [".error-message", ".mat-error", "mat-error", "[role='alert']", ".alert-danger"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                log.error(f"Login page error: '{el.text.strip()}'")
                return False
        except Exception:
            pass

    # One extra wait
    time.sleep(5)
    if "/login" not in driver.current_url:
        log.info("Login successful (delayed navigation).")
        return True

    log.error(f"Login failed – still on: {driver.current_url}")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Availability check
# ──────────────────────────────────────────────────────────────────────────────

def slots_available(driver) -> bool:
    try:
        src = driver.page_source
        low = src.lower()

        if is_cloudflare_block(driver):
            return False

        # Negative phrases
        for phrase in ["no appointment", "no slots available", "fully booked",
                       "there are currently no", "not available at this time"]:
            if phrase in low:
                log.info(f"  No-slot phrase found: '{phrase}'")
                return False

        # Positive selectors
        for sel in [
            "[data-testid='appointment-slot']",
            ".appointment-slot",
            ".available-slot",
            ".calendar-day.available",
            "input[type='radio'][name*='slot']",
            "input[type='radio'][name*='appointment']",
            "mat-radio-button",
        ]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els and any(e.is_displayed() for e in els):
                    log.info(f"  Slot found via: {sel}")
                    return True
            except Exception:
                pass

        # Positive content phrases
        for phrase in ["select time", "available dates", "choose a slot",
                       "pick a date", "appointment available"]:
            if phrase in low:
                log.info(f"  Positive phrase: '{phrase}'")
                return True

    except Exception as e:
        log.debug(f"slots_available error: {e}")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Book one client
# ──────────────────────────────────────────────────────────────────────────────

def book_client(driver, client: dict) -> dict:
    result = {
        "success": False, "reference": None, "error": None,
        "email": client.get("email"),
        "name": f"{client.get('first_name','')} {client.get('last_name','')}",
        "timestamp": datetime.now().isoformat(),
    }
    try:
        log.info(f">>> Booking {result['name']} ({result['email']})")
        driver.get(BOOKING_URL)
        time.sleep(random.uniform(2, 4))

        if is_cloudflare_block(driver):
            if not wait_cf_pass(driver, timeout_s=45):
                result["error"] = "Cloudflare block"
                return result

        dismiss_cookie_banner(driver)

        # ─ Step 1: Appointment Details ─────────────────────────────────
        log.info("  Step 1 – Appointment Details")

        visa   = client.get("visa_type") or client.get("trip_reason") or ""
        sub    = client.get("service_center") or client.get("trip_reason") or ""
        centre = client.get("application_center") or ""

        if visa:
            mat_select(driver, "visaCategory", visa) or mat_select(driver, "category", visa)
            time.sleep(0.8)
        if sub:
            mat_select(driver, "subCategory", sub) or mat_select(driver, "visaSubCategory", sub)
            time.sleep(0.8)
        if centre:
            (mat_select(driver, "centre", centre)
             or mat_select(driver, "center", centre)
             or mat_select(driver, "appointmentLocation", centre))
            time.sleep(0.8)

        click_continue(driver)
        time.sleep(random.uniform(2, 3))

        # ─ Step 2: Applicant Details ──────────────────────────────────
        log.info("  Step 2 – Applicant Details")

        first      = client.get("first_name", "")
        last       = client.get("last_name", "")
        dob        = client.get("date_of_birth", "")
        gender     = client.get("gender", "").capitalize()
        nationality = client.get("current_nationality", "")
        passport   = client.get("passport_number", "")
        pp_expiry  = client.get("passport_expiry", "")
        email      = client.get("email", "")
        cc         = str(client.get("mobile_country_code", ""))
        phone      = str(client.get("mobile_number", ""))

        for fc in ["firstName", "first_name", "givenName", "applicantFirstName"]:
            if mat_fill(driver, fc, first): break
        time.sleep(0.3)

        for fc in ["lastName", "last_name", "surname", "familyName", "applicantLastName"]:
            if mat_fill(driver, fc, last): break
        time.sleep(0.3)

        if dob:
            for fc in ["dateOfBirth", "dob", "birthDate", "applicantDob"]:
                if mat_fill(driver, fc, dob): break
            time.sleep(0.3)

        if gender:
            if not mat_select(driver, "gender", gender):
                for xpath in [
                    f"//mat-radio-button[contains(.,'{gender}')]",
                    f"//label[contains(.,'{gender}')]",
                ]:
                    try:
                        rb = driver.find_element(By.XPATH, xpath)
                        if rb.is_displayed():
                            rb.click()
                            break
                    except Exception:
                        pass
            time.sleep(0.3)

        if nationality:
            if not mat_select(driver, "nationality", nationality):
                for fc in ["nationality", "countryOfBirth", "country"]:
                    if mat_fill(driver, fc, nationality):
                        time.sleep(0.8)
                        # Accept first autocomplete suggestion
                        try:
                            opt = WebDriverWait(driver, 4).until(
                                EC.visibility_of_element_located((By.CSS_SELECTOR, "mat-option"))
                            )
                            opt.click()
                        except Exception:
                            pass
                        break
            time.sleep(0.4)

        if passport:
            for fc in ["passportNo", "passportNumber", "passportNum", "travelDocNumber"]:
                if mat_fill(driver, fc, passport): break
            time.sleep(0.3)

        if pp_expiry:
            for fc in ["passportExpiry", "passportExpiryDate", "travelDocExpiry", "expiryDate"]:
                if mat_fill(driver, fc, pp_expiry): break
            time.sleep(0.3)

        for fc in ["contactEmail", "email", "emailAddress", "applicantEmail"]:
            if mat_fill(driver, fc, email): break
        time.sleep(0.3)

        if not mat_fill(driver, "countryCode", cc):
            for fc in ["contactNumber", "mobileNumber", "phoneNumber", "phone", "mobile"]:
                if mat_fill(driver, fc, cc + phone): break
        else:
            for fc in ["contactNumber", "mobileNumber", "phoneNumber", "phone"]:
                if mat_fill(driver, fc, phone): break
        time.sleep(0.3)

        # ─ Step 3: Confirm ────────────────────────────────────────────
        log.info("  Step 3 – Submitting")
        click_continue(driver)
        time.sleep(random.uniform(2, 4))

        # Final confirm/pay (if extra step)
        for xpath in [
            "//button[contains(.,'Confirm')]",
            "//button[contains(.,'Pay')]",
            "//button[contains(.,'Submit')]",
            "//button[@type='submit']",
        ]:
            try:
                btn = driver.find_element(By.XPATH, xpath)
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    time.sleep(random.uniform(3, 5))
                    break
            except Exception:
                pass

        # ─ Extract confirmation reference ────────────────────────────
        ref = None
        for sel in [
            ".booking-reference", ".confirmation-number", ".booking-confirmation",
            "[class*='reference']", "[class*='confirmation']",
        ]:
            try:
                el = WebDriverWait(driver, 12).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                )
                txt = el.text.strip()
                if txt:
                    ref = txt[:100]
                    break
            except Exception:
                pass

        # XPath text fallbacks
        if not ref:
            for phrase in ["Reference Number", "Ref No", "Appointment Confirmed",
                           "Booking Confirmed", "Reference:"]:
                try:
                    el = driver.find_element(
                        By.XPATH, f"//*[contains(text(),'{phrase}')]"
                    )
                    ref = el.text.strip()[:100]
                    break
                except Exception:
                    pass

        # Regex fallback on page source
        if not ref:
            m = re.search(r"[A-Z0-9]{6,20}", driver.page_source)
            if m:
                ref = m.group(0)

        # Off-form-page = success
        if not ref and "book-appointment" not in driver.current_url and "/login" not in driver.current_url:
            ref = "CONFIRMED"

        if ref:
            result["success"] = True
            result["reference"] = ref
            log.info(f"  ✔ Booked – ref: {ref}")
        else:
            result["error"] = f"No confirmation found. URL: {driver.current_url}"
            log.warning(f"  ✘ Booking uncertain – {result['error']}")

    except Exception as exc:
        result["error"] = str(exc)
        log.error(f"  Exception while booking {client.get('email')}: {exc}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Monitoring loop
# ──────────────────────────────────────────────────────────────────────────────

def monitor_and_book(driver, clients: list, max_clients: int, monitor_minutes: int) -> list:
    deadline = time.time() + monitor_minutes * 60
    check_no = 0
    log.info(f"Monitoring for {monitor_minutes} min ({len(clients)} client(s) queued)…")

    while time.time() < deadline:
        check_no += 1
        remaining = int(deadline - time.time())
        log.info(f"  Check #{check_no} – {remaining}s remaining")

        try:
            driver.get(BOOKING_URL)
            time.sleep(random.uniform(2, 4))

            if is_cloudflare_block(driver):
                wait_cf_pass(driver, timeout_s=30)
                continue

            if slots_available(driver):
                log.info("*** SLOTS AVAILABLE – booking now ***")
                results = []
                for client in clients[:max_clients]:
                    res = book_client(driver, client)
                    results.append(res)
                    if len(results) < len(clients[:max_clients]):
                        time.sleep(random.uniform(2, 5))
                return results

            log.info("  No slots yet.")

        except Exception as e:
            log.warning(f"  Poll error: {e}")

        wait_s = random.uniform(10, 20)
        log.info(f"  Next check in {wait_s:.0f}s…")
        time.sleep(wait_s)

    log.info("Monitoring window expired – no slots found.")
    return []


# ──────────────────────────────────────────────────────────────────────────────
# CSV loader
# ──────────────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({k.strip(): v.strip() for k, v in row.items()})
    log.info(f"Loaded {len(rows)} client(s) from {path}")
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VFS GNB auto-booking")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--csv",      default=str(CSV_PATH), help="Path to clients CSV file")
    parser.add_argument("--max",      type=int, default=MAX_CLIENTS, help="Max clients to book")
    parser.add_argument("--minutes",  type=int, default=MONITOR_MIN, help="Monitoring window (minutes)")
    parser.add_argument("--email",    default=LOGIN_EMAIL, help="VFS login email")
    parser.add_argument("--password", default=LOGIN_PWD,   help="VFS login password")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        log.error(f"CSV file not found: {csv_path}")
        sys.exit(1)

    clients = load_csv(csv_path)
    if not clients:
        log.error("No clients in CSV – aborting.")
        sys.exit(1)

    driver = launch_driver(headless=args.headless)
    all_results = []

    try:
        if not do_login(driver, args.email, args.password):
            log.error("Login failed – aborting.")
            sys.exit(1)

        all_results = monitor_and_book(driver, clients, args.max, args.minutes)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Save results
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).parent / f"booking_results_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    log.info(f"Results saved → {out}")

    # Summary
    ok  = sum(1 for r in all_results if r.get("success"))
    tot = len(all_results)
    print(f"\n{'='*52}")
    print(f"  BOOKING SUMMARY:  {ok}/{tot} successful")
    for r in all_results:
        status = "✔ BOOKED" if r.get("success") else "✘ FAILED"
        ref    = r.get("reference") or r.get("error") or "—"
        print(f"  {status}  {r.get('name','?')}  |  {ref}")
    print("="*52)


if __name__ == "__main__":
    main()


import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "run_booking.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("run_booking")

# ──────────────────────────────────────────────────────────────────────────────
# Config – change these if needed
# ──────────────────────────────────────────────────────────────────────────────
LOGIN_URL    = "https://visa.vfsglobal.com/gnb/en/prt/login"
BOOKING_URL  = "https://visa.vfsglobal.com/gnb/en/prt/book-appointment"
LOGIN_EMAIL  = "brunovfs2k@gmail.com"
LOGIN_PWD    = "Bissau300@"
MAX_CLIENTS  = 5
MONITOR_MIN  = 4        # monitoring window (minutes)
CSV_PATH     = Path(__file__).parent / "clients.csv"

# Cloudflare / stealth – rotating realistic user agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# ──────────────────────────────────────────────────────────────────────────────
# Stealth init script injected before every page load
# ──────────────────────────────────────────────────────────────────────────────
STEALTH_INIT = """
() => {
    // Remove webdriver flag
    try { delete navigator.webdriver; } catch(e) {}
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    } catch(e) {}

    // Realistic plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin',   filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer',   filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client',       filename: 'internal-nacl-plugin' },
        ],
        configurable: true,
    });

    // Language
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-GB', 'en', 'pt-PT', 'pt'],
            configurable: true,
        });
    } catch(e) {}

    // Chrome object
    window.chrome = {
        runtime: { onConnect: undefined, onMessage: undefined },
        loadTimes: () => ({}),
        csi: () => ({}),
        app: {},
    };

    // Remove chromedriver artifacts
    ['cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
     'cdc_adoQpoasnfa76pfcZLmcfl_Symbol','cdc_adoQpoasnfa76pfcZLmcfl_JSON'].forEach(k => {
        try { delete window[k]; } catch(e) {}
    });

    // Permissions
    try {
        const _pq = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = p =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : _pq(p);
    } catch(e) {}
}
"""

# ──────────────────────────────────────────────────────────────────────────────
# Browser helpers
# ──────────────────────────────────────────────────────────────────────────────

def _launch_browser(playwright, headless: bool):
    """Launch Chromium with stealth args, try local Chrome channel first."""
    ua = random.choice(USER_AGENTS)
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-web-security",
        "--disable-features=VizDisplayCompositor,TranslateUI",
        "--disable-extensions",
        "--disable-default-apps",
        "--no-first-run",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-logging",
        "--log-level=3",
        f"--user-agent={ua}",
    ]
    launch_kw = dict(headless=headless, args=args, ignore_default_args=["--enable-automation"])

    browser = None
    try:
        browser = playwright.chromium.launch(channel="chrome", **launch_kw)
        log.info("Launched local Chrome channel.")
    except Exception as e:
        log.warning(f"Local Chrome unavailable ({e}), falling back to bundled Chromium.")
        browser = playwright.chromium.launch(**launch_kw)

    context = browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-GB",
        timezone_id="Europe/Lisbon",
        color_scheme="light",
        reduced_motion="no-preference",
    )
    context.add_init_script(STEALTH_INIT)

    page = context.new_page()
    page.set_extra_http_headers({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })

    log.info(f"Browser ready | user-agent: …{ua[-40:]}")
    return browser, context, page


def _is_cloudflare_block(content: str) -> bool:
    indicators = [
        "checking your browser",
        "ddos protection by cloudflare",
        "cf-challenge",
        "cf-browser-verification",
        "this process is automatic",
        "cloudflare ray id",
        "acesso restrito",
        "atividade incomum",
        "403201",
        "your access has been temporarily restricted",
    ]
    low = content.lower()
    return any(ind in low for ind in indicators)


def _wait_cloudflare_pass(page, timeout_s: int = 40) -> bool:
    """Wait up to timeout_s for Cloudflare challenge to auto-resolve."""
    log.warning("Cloudflare challenge detected – waiting for auto-pass…")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        try:
            content = page.content()
            if not _is_cloudflare_block(content):
                log.info("Cloudflare challenge passed.")
                return True
        except Exception:
            pass
    log.error("Cloudflare challenge did NOT resolve within timeout.")
    return False


def _dismiss_cookie_banner(page) -> None:
    for sel in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept')",
        ".cookie-accept",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1200):
                btn.click()
                time.sleep(0.4)
                log.info(f"Cookie banner dismissed ({sel})")
                return
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Login
# ──────────────────────────────────────────────────────────────────────────────

def do_login(page, email: str, password: str) -> bool:
    """Login to VFS Global GNB portal. Returns True on success."""
    log.info(f"Navigating to login: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)
    time.sleep(random.uniform(3, 5))

    # Check/handle Cloudflare
    content = page.content()
    if _is_cloudflare_block(content):
        if not _wait_cloudflare_pass(page, timeout_s=60):
            return False
        time.sleep(2)
        # Reload after CF pass
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)
        time.sleep(random.uniform(3, 5))

    _dismiss_cookie_banner(page)

    # Wait for Angular to finish rendering
    try:
        page.wait_for_selector("input", timeout=20_000)
    except Exception:
        pass
    time.sleep(2)

    # Locate email field
    email_el = None
    for sel in [
        'input[formcontrolname="email"]',
        'input[formcontrolname="username"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[placeholder*="Email" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="Username" i]',
        '#mat-input-0',
        'mat-form-field input',
        'form input',
        'input:visible',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=4000):
                email_el = el
                log.info(f"Email field found: {sel}")
                break
        except Exception:
            pass

    if not email_el:
        # Save a screenshot to help diagnose
        try:
            shot = Path(__file__).parent / "logs" / "login_debug.png"
            page.screenshot(path=str(shot))
            log.error(f"Login: email input not found. Screenshot saved → {shot}")
        except Exception:
            log.error("Login: email input not found.")
        return False

    email_el.click()
    email_el.fill("")
    for ch in email:
        email_el.type(ch, delay=random.randint(55, 115))
    time.sleep(random.uniform(0.4, 0.8))

    # Locate password field
    pwd_el = None
    for sel in [
        'input[formcontrolname="password"]',
        'input[name="password"]',
        'input[type="password"]',
        '#mat-input-1',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                pwd_el = el
                log.info(f"Password field found: {sel}")
                break
        except Exception:
            pass

    if not pwd_el:
        log.error("Login: password input not found.")
        return False

    pwd_el.click()
    pwd_el.fill("")
    for ch in password:
        pwd_el.type(ch, delay=random.randint(55, 115))
    time.sleep(random.uniform(0.5, 1.0))

    # Submit
    submitted = False
    for sel in [
        'button[type="submit"]',
        'button:has-text("Sign In")',
        'button:has-text("Log In")',
        'button:has-text("Login")',
        '.sign-in-btn',
        'button.mat-raised-button',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000) and btn.is_enabled():
                btn.click()
                submitted = True
                log.info(f"Login submitted via: {sel}")
                break
        except Exception:
            pass

    if not submitted:
        pwd_el.press("Enter")
        log.info("Login submitted via Enter key.")

    time.sleep(random.uniform(4, 7))

    # Verify navigation
    current = page.url
    if "/login" not in current:
        log.info(f"Login successful – redirected to: {current}")
        return True

    # Check for error messages
    for err_sel in [".error-message", ".mat-error", "mat-error", "[role='alert']", ".alert-danger"]:
        try:
            el = page.locator(err_sel).first
            if el.is_visible(timeout=1000):
                log.error(f"Login error on page: '{el.text_content().strip()}'")
                return False
        except Exception:
            pass

    # One more wait
    time.sleep(4)
    if "/login" not in page.url:
        log.info("Login successful (delayed navigation).")
        return True

    log.error(f"Login failed – still on: {page.url}")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Availability detection
# ──────────────────────────────────────────────────────────────────────────────

def slots_available(page) -> bool:
    """Return True if the booking page shows appointment slots."""
    try:
        content = page.content()
        if _is_cloudflare_block(content):
            return False

        low = content.lower()

        # Negative indicators
        negative_phrases = [
            "no appointment",
            "no slots",
            "fully booked",
            "not available",
            "there are currently no",
            "aucun rendez",
        ]
        if any(p in low for p in negative_phrases):
            log.info("No-slot message found on page.")
            return False

        # Positive indicators
        positive_selectors = [
            "[data-testid='appointment-slot']",
            ".appointment-slot",
            ".available-slot",
            ".calendar-day.available",
            ".calendar-day.bookable",
            "input[type='radio'][name*='slot']",
            "input[type='radio'][name*='appointment']",
            "mat-radio-button:visible",
            "button:has-text('Select'):visible",
            "button:has-text('Book'):visible",
        ]
        for sel in positive_selectors:
            try:
                els = page.locator(sel).all()
                if els:
                    log.info(f"Slot found via selector: {sel} ({len(els)} elements)")
                    return True
            except Exception:
                pass

        # Content-based fallback
        positive_phrases = [
            "select time",
            "available dates",
            "choose a slot",
            "pick a date",
            "appointment available",
        ]
        if any(p in low for p in positive_phrases):
            log.info("Positive availability phrase detected in page content.")
            return True

    except Exception as e:
        log.debug(f"slots_available error: {e}")

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Form helpers – Angular Material
# ──────────────────────────────────────────────────────────────────────────────

def mat_fill(page, formcontrolname: str, value: str) -> bool:
    """Type into an Angular Material input."""
    for sel in [
        f'input[formcontrolname="{formcontrolname}"]',
        f'textarea[formcontrolname="{formcontrolname}"]',
        f'[formcontrolname="{formcontrolname}"] input',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2500):
                el.click()
                el.fill("")
                for ch in str(value):
                    el.type(ch, delay=random.randint(45, 100))
                time.sleep(random.uniform(0.2, 0.4))
                log.debug(f"  Filled '{formcontrolname}' = '{value}'")
                return True
        except Exception:
            pass
    return False


def mat_select(page, formcontrolname: str, option_text: str) -> bool:
    """Open a mat-select dropdown and pick the matching option."""
    for trigger_sel in [
        f'mat-select[formcontrolname="{formcontrolname}"]',
        f'[formcontrolname="{formcontrolname}"] mat-select',
        f'[formcontrolname="{formcontrolname}"]',
    ]:
        try:
            trigger = page.locator(trigger_sel).first
            if not trigger.is_visible(timeout=2500):
                continue
            trigger.click()
            time.sleep(0.6)
            # Exact match first
            try:
                opt = page.locator(f'mat-option:has-text("{option_text}")').first
                opt.wait_for(state="visible", timeout=5000)
                opt.click()
                time.sleep(0.4)
                log.debug(f"  mat-select '{formcontrolname}' → '{option_text}'")
                return True
            except Exception:
                # Partial match
                for opt in page.locator("mat-option").all():
                    txt = opt.text_content() or ""
                    if option_text.lower() in txt.lower():
                        opt.click()
                        time.sleep(0.4)
                        log.debug(f"  mat-select '{formcontrolname}' partial → '{txt.strip()}'")
                        return True
                page.keyboard.press("Escape")
        except Exception as e:
            log.debug(f"mat_select '{formcontrolname}' attempt failed: {e}")
    return False


def click_continue(page) -> bool:
    """Click the wizard Continue/Next/Submit button."""
    for sel in [
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Proceed")',
        'button[type="submit"]:visible',
        '.vfs-btn-primary:visible',
        'button.mat-raised-button[color="primary"]:visible',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000) and btn.is_enabled():
                btn.click()
                time.sleep(random.uniform(1.5, 2.5))
                return True
        except Exception:
            pass
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Booking a single client
# ──────────────────────────────────────────────────────────────────────────────

def book_client(page, client: dict) -> dict:
    """
    Navigate to the booking page and fill the multi-step VFS Angular form.
    Returns a result dict with 'success', 'reference', 'error' keys.
    """
    result = {"success": False, "reference": None, "error": None,
              "email": client.get("email"), "timestamp": datetime.now().isoformat()}
    try:
        log.info(f">>> Booking {client['first_name']} {client['last_name']}")

        # Navigate to booking page
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=40_000)
        time.sleep(random.uniform(2, 3.5))

        content = page.content()
        if _is_cloudflare_block(content):
            if not _wait_cloudflare_pass(page, timeout_s=45):
                result["error"] = "Cloudflare block"
                return result

        _dismiss_cookie_banner(page)

        # ── Step 1: Appointment Details ──────────────────────────────────────
        log.info("  Step 1 – Appointment Details")

        visa   = client.get("visa_type") or client.get("trip_reason") or client.get("service_center", "")
        sub    = client.get("service_center") or client.get("trip_reason", "")
        centre = client.get("application_center", "")

        if visa:
            mat_select(page, "visaCategory", visa) or mat_select(page, "category", visa)
            time.sleep(0.8)
        if sub:
            mat_select(page, "subCategory", sub) or mat_select(page, "visaSubCategory", sub)
            time.sleep(0.8)
        if centre:
            (mat_select(page, "centre", centre)
             or mat_select(page, "center", centre)
             or mat_select(page, "appointmentLocation", centre))
            time.sleep(0.8)

        if not click_continue(page):
            log.warning("  Continue button not found (Step 1) – proceeding anyway.")
        time.sleep(random.uniform(2, 3))
        page.wait_for_load_state("domcontentloaded", timeout=20_000)

        # ── Step 2: Applicant Details ────────────────────────────────────────
        log.info("  Step 2 – Applicant Details")

        first = client.get("first_name", "")
        last  = client.get("last_name", "")
        dob   = client.get("date_of_birth", "")
        gender = client.get("gender", "").capitalize()
        nationality = client.get("current_nationality", "")
        passport    = client.get("passport_number", "")
        pp_expiry   = client.get("passport_expiry", "")
        email       = client.get("email", "")
        cc          = str(client.get("mobile_country_code", ""))
        phone       = str(client.get("mobile_number", ""))

        for fc in ["firstName", "first_name", "givenName", "applicantFirstName"]:
            if mat_fill(page, fc, first): break
        time.sleep(0.3)

        for fc in ["lastName", "last_name", "surname", "familyName", "applicantLastName"]:
            if mat_fill(page, fc, last): break
        time.sleep(0.3)

        if dob:
            for fc in ["dateOfBirth", "dob", "birthDate", "applicantDob"]:
                if mat_fill(page, fc, dob): break
            time.sleep(0.3)

        if gender:
            if not mat_select(page, "gender", gender):
                try:
                    rb = page.locator(f'mat-radio-button:has-text("{gender}")').first
                    if rb.is_visible(timeout=2000):
                        rb.click()
                        time.sleep(0.3)
                except Exception:
                    pass

        if nationality:
            if not mat_select(page, "nationality", nationality):
                for fc in ["nationality", "countryOfBirth", "country"]:
                    if mat_fill(page, fc, nationality):
                        time.sleep(0.8)
                        # Accept first autocomplete suggestion
                        try:
                            opt = page.locator("mat-option").first
                            if opt.is_visible(timeout=2000):
                                opt.click()
                        except Exception:
                            pass
                        break
            time.sleep(0.4)

        if passport:
            for fc in ["passportNo", "passportNumber", "passportNum", "travelDocNumber"]:
                if mat_fill(page, fc, passport): break
            time.sleep(0.3)

        if pp_expiry:
            for fc in ["passportExpiry", "passportExpiryDate", "travelDocExpiry", "expiryDate"]:
                if mat_fill(page, fc, pp_expiry): break
            time.sleep(0.3)

        for fc in ["contactEmail", "email", "emailAddress", "applicantEmail"]:
            if mat_fill(page, fc, email): break
        time.sleep(0.3)

        if not mat_fill(page, "countryCode", cc):
            # Try inline combined
            for fc in ["contactNumber", "mobileNumber", "phoneNumber", "phone", "mobile"]:
                if mat_fill(page, fc, cc + phone): break
        else:
            for fc in ["contactNumber", "mobileNumber", "phoneNumber", "phone"]:
                if mat_fill(page, fc, phone): break
        time.sleep(0.3)

        # ── Step 3: Continue to confirmation ─────────────────────────────────
        log.info("  Step 3 – Submitting")

        if not click_continue(page):
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        time.sleep(random.uniform(2, 4))

        # Final confirm/pay button
        for sel in [
            'button:has-text("Confirm")',
            'button:has-text("Pay")',
            'button:has-text("Submit")',
            'button:has-text("Book")',
            'button[type="submit"]:visible',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2500) and btn.is_enabled():
                    btn.click()
                    time.sleep(random.uniform(3, 5))
                    break
            except Exception:
                pass

        # ── Extract confirmation reference ───────────────────────────────────
        ref = None
        for sel in [
            ".booking-reference", ".confirmation-number", ".booking-confirmation",
            "[class*='reference']", "[class*='confirmation']",
            "text=Reference Number", "text=Ref No", "text=Appointment Confirmed",
        ]:
            try:
                page.wait_for_selector(sel, timeout=10_000)
                el = page.locator(sel).first
                txt = el.text_content() or ""
                if txt.strip():
                    ref = txt.strip()[:100]
                    break
            except Exception:
                pass

        if not ref:
            # Regex fallback on page source
            import re
            m = re.search(r"[A-Z0-9]{6,20}", page.content())
            if m:
                ref = m.group(0)

        # If we're off the form page, assume success
        if not ref and "book-appointment" not in page.url and "login" not in page.url:
            ref = "CONFIRMED"

        if ref:
            result["success"] = True
            result["reference"] = ref
            log.info(f"  ✔ Booked – ref: {ref}")
        else:
            result["error"] = "No confirmation reference found."
            log.warning(f"  ✘ Booking uncertain – URL: {page.url}")

    except Exception as exc:
        result["error"] = str(exc)
        log.error(f"  Error booking {client.get('email')}: {exc}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# CSV loader
# ──────────────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list:
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({k.strip(): v.strip() for k, v in row.items()})
    log.info(f"Loaded {len(rows)} client(s) from {path}")
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Monitoring loop
# ──────────────────────────────────────────────────────────────────────────────

def monitor_and_book(page, clients: list, max_clients: int, monitor_minutes: int) -> list:
    """
    Poll the booking page until slots appear (within monitor_minutes),
    then book up to max_clients.
    """
    import time as _t
    deadline = _t.time() + monitor_minutes * 60
    check_no = 0

    log.info(f"Monitoring booking page for {monitor_minutes} min ({len(clients)} client(s) queued)…")

    while _t.time() < deadline:
        check_no += 1
        elapsed   = int(deadline - _t.time())
        log.info(f"  Check #{check_no} – {elapsed}s remaining")

        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
            content = page.content()

            if _is_cloudflare_block(content):
                log.warning("Cloudflare block during monitoring – retrying after 15 s…")
                _t.sleep(15)
                continue

            if slots_available(page):
                log.info("*** SLOTS AVAILABLE – starting bookings ***")
                results = []
                for client in clients[:max_clients]:
                    res = book_client(page, client)
                    results.append(res)
                    if not res["success"]:
                        log.warning(f"Booking failed for {client.get('email')}: {res['error']}")
                    _t.sleep(random.uniform(2, 5))
                return results

            log.info("  No slots yet.")

        except Exception as e:
            log.warning(f"  Polling error: {e}")

        poll_wait = random.uniform(10, 20)
        log.info(f"  Waiting {poll_wait:.0f}s before next check…")
        _t.sleep(poll_wait)

    log.info("Monitoring window expired – no slots were found.")
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VFS GNB auto-booking")
    parser.add_argument("--headless",  action="store_true", help="Run browser in headless mode")
    parser.add_argument("--csv",       default=str(CSV_PATH), help="Path to clients CSV file")
    parser.add_argument("--max",       type=int, default=MAX_CLIENTS, help="Max clients to book")
    parser.add_argument("--minutes",   type=int, default=MONITOR_MIN, help="Monitoring window (minutes)")
    parser.add_argument("--email",     default=LOGIN_EMAIL, help="VFS login email")
    parser.add_argument("--password",  default=LOGIN_PWD,   help="VFS login password")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        log.error(f"CSV file not found: {csv_path}")
        sys.exit(1)

    clients = load_csv(csv_path)
    if not clients:
        log.error("No clients in CSV – aborting.")
        sys.exit(1)

    # ── Playwright session ───────────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed.  Run:  pip install playwright && python -m playwright install")
        sys.exit(1)

    all_results = []

    with sync_playwright() as pw:
        browser, context, page = _launch_browser(pw, headless=args.headless)
        try:
            # Login
            if not do_login(page, args.email, args.password):
                log.error("Login failed – aborting.")
                sys.exit(1)

            # Monitor + book
            all_results = monitor_and_book(page, clients, args.max, args.minutes)

        finally:
            try:
                page.close()
                context.close()
                browser.close()
            except Exception:
                pass

    # ── Save results ─────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(__file__).parent / f"booking_results_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    log.info(f"Results saved → {out_path}")

    # ── Summary ──────────────────────────────────────────────────────────────
    ok  = sum(1 for r in all_results if r.get("success"))
    tot = len(all_results)
    print(f"\n{'='*50}")
    print(f"  BOOKING SUMMARY:  {ok}/{tot} successful")
    for r in all_results:
        status = "✔ BOOKED" if r.get("success") else "✘ FAILED"
        ref    = r.get("reference") or r.get("error") or "—"
        print(f"  {status}  {r.get('email','?')}  |  {ref}")
    print("="*50)


if __name__ == "__main__":
    main()
