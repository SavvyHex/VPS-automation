#!/usr/bin/env python3
"""
VFS Global Guinea-Bissau → Portugal Appointment Booking Bot
=============================================================
Standalone local script.  No OTP.  Supports up to 5 clients.
Cloudflare bypass via nodriver + persistent Chrome profile.

USAGE
-----
# 1 — Run warmup BEFORE the booking window opens (solves CF once, saves session)
    python book_vfs.py --warmup

# 2 — Run when the booking window opens (CF already cleared, all clients in parallel)
    python book_vfs.py

# Optional flags
    python book_vfs.py --max-clients 3
    python book_vfs.py --proxy socks5://user:pass@host:port
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import shutil
import time
from pathlib import Path

import nodriver as uc
import pandas as pd

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────
VFS_LOGIN_URL = "https://visa.vfsglobal.com/gnb/en/prt/login"
VFS_APP_URL   = "https://visa.vfsglobal.com/gnb/en/prt/book-an-appointment"

VFS_PROXY_DEFAULT = "http://176.61.151.123:80"   # Portugal residential HTTP proxy

VFS_USERNAME = os.getenv("VFS_USERNAME", "brunovfs2k@gmail.com")
VFS_PASSWORD = os.getenv("VFS_PASSWORD", "Bissau300@")

CLIENTS_CSV    = Path(__file__).resolve().parent / "clients.csv"
MAX_CLIENTS    = 5
CHROME_PROFILE = os.path.expanduser("~/.vfs_chrome_profile")

CF_POLL_MAX   = 120  # seconds to wait for Cloudflare JS challenge to clear
ELEMENT_WAIT  = 20   # seconds to wait for a DOM element
DROPDOWN_WAIT = 3    # seconds to wait for mat-option list

# ──────────────────────────────────────────────────────────────
# CSS Selectors
# ──────────────────────────────────────────────────────────────
SELECTORS = {
    "login_email":    "input[id='mat-input-0']",
    "login_password": "input[id='mat-input-1']",
    "login_button":   "button[type='submit']",

    # Personal info fields
    "first_name":           "input[formcontrolname='firstName']",
    "last_name":            "input[formcontrolname='lastName']",
    "date_of_birth":        "input[formcontrolname='dateOfBirth']",
    "email":                "input[formcontrolname='email']",
    "mobile_country_code":  "input[formcontrolname='mobileCountryCode']",
    "mobile_number":        "input[formcontrolname='mobileNumber']",
    "passport_number":      "input[formcontrolname='passportNumber']",
    "passport_expiry":      "input[formcontrolname='passportExpiryDate']",

    # Step-1 dropdowns (appear BEFORE personal info fields on VFS form)
    "visa_type":            "mat-select[formcontrolname='visaType']",
    "application_center":   "mat-select[formcontrolname='applicationCenter']",
    "service_center":       "mat-select[formcontrolname='serviceType']",
    "trip_reason":          "mat-select[formcontrolname='purposeOfTravel']",

    # Step-2 dropdowns (personal info section)
    "gender":               "mat-select[formcontrolname='gender']",
    "current_nationality":  "mat-select[formcontrolname='nationality']",

    "submit": "button[type='submit']",

    # Confirmation selectors — tried after submit
    "confirm_ref": [
        ".booking-reference",
        "[class*='reference']",
        "[class*='confirmation']",
        "[class*='booking-id']",
        "strong",
    ],
}


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _normalise_date(raw: str) -> str:
    """Return date in DD/MM/YYYY regardless of input format."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if len(raw) == 10 and raw[2] == "/" and raw[5] == "/":
        day, month, year = raw[:2], raw[3:5], raw[6:]
        if int(day) <= 31 and int(month) <= 12:
            return raw
        return f"{month}/{day}/{year}"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        year, month, day = raw[:4], raw[5:7], raw[8:]
        return f"{day}/{month}/{year}"
    return raw


def _normalise_country_code(code: str) -> str:
    code = str(code or "").strip()
    if code and not code.startswith("+"):
        code = "+" + code
    return code


async def wait_for(tab, selector: str, timeout: float = ELEMENT_WAIT):
    """Wait for a CSS selector to appear; return element or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            el = await tab.find(selector, timeout=0.5)
            if el:
                return el
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return None


async def wait_for_any(tab, selectors: list, timeout: float = ELEMENT_WAIT):
    """Try each selector; return (element, selector) for the first hit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                el = await tab.find(sel, timeout=0.3)
                if el:
                    return el, sel
            except Exception:
                pass
        await asyncio.sleep(0.1)
    return None, None


async def js_fill(tab, selector: str, value: str, field_name: str = "") -> bool:
    """
    Fill an Angular input via JavaScript.
    Fires input/change/blur so Angular reactive-form validators accept the value.
    """
    if not value:
        return False
    el = await wait_for(tab, selector, timeout=ELEMENT_WAIT)
    if not el:
        print(f"  [warn] field not found: {field_name} ({selector})")
        return False
    try:
        result = await tab.evaluate(
            f"""(function(){{
                var el = document.querySelector({repr(selector)});
                if (!el) return false;
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, {repr(str(value))});
                el.dispatchEvent(new Event('input',  {{bubbles:true}}));
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                el.dispatchEvent(new Event('blur',   {{bubbles:true}}));
                return true;
            }})()"""
        )
        if result:
            print(f"  [ok]   {field_name} = {value}")
            return True
        await el.send_keys(str(value))
        print(f"  [ok-sk] {field_name} = {value}")
        return True
    except Exception as e:
        print(f"  [err]  {field_name}: {e}")
        return False


async def mat_select(tab, selector: str, visible_text: str, field_name: str = "") -> bool:
    """Open a mat-select dropdown and click the matching option."""
    if not visible_text:
        return False
    el = await wait_for(tab, selector)
    if not el:
        print(f"  [warn] mat-select not found: {field_name}")
        return False
    try:
        await el.click()
        deadline = time.monotonic() + DROPDOWN_WAIT
        options = []
        while time.monotonic() < deadline:
            options = await tab.find_all("mat-option")
            if options:
                break
            await asyncio.sleep(0.05)

        target = visible_text.strip().lower()
        for strict in (True, False):
            for opt in options:
                try:
                    text = (await opt.get_attribute("innerText") or "").strip()
                    match = (text.lower() == target) if strict else (target in text.lower())
                    if match:
                        await opt.click()
                        label = "=" if strict else "~"
                        print(f"  [ok]  {field_name} {label} {text}")
                        return True
                except Exception:
                    continue

        print(f"  [warn] no option matched '{visible_text}' for {field_name}")
        return False
    except Exception as e:
        print(f"  [err]  {field_name}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# Cloudflare bypass
# ──────────────────────────────────────────────────────────────

async def wait_for_cloudflare(tab) -> bool:
    """
    Poll until the Cloudflare JS challenge is gone.
    With a persistent profile the cf-clearance cookie is already present
    and this returns almost immediately.
    """
    print(f"  [CF] Checking for Cloudflare challenge (max {CF_POLL_MAX}s)...")
    deadline = time.monotonic() + CF_POLL_MAX
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        try:
            title = (await tab.get_title() or "").lower()
            if "just a moment" not in title and "checking your browser" not in title:
                print("  [CF] Clear.")
                return True
        except Exception:
            pass
    print("  [CF] Timed out waiting for Cloudflare — continuing anyway.")
    return False


# ──────────────────────────────────────────────────────────────
# Login
# ──────────────────────────────────────────────────────────────

async def login(tab) -> bool:
    print("\n[Login] Navigating...")
    await tab.get(VFS_LOGIN_URL)
    await wait_for_cloudflare(tab)

    email_el, _ = await wait_for_any(tab, [
        "input[id='mat-input-0']",
        "input[type='email']",
        "input[formcontrolname='email']",
        "input[formcontrolname='username']",
        "input[placeholder*='mail' i]",
    ])
    password_el, _ = await wait_for_any(tab, [
        "input[id='mat-input-1']",
        "input[type='password']",
        "input[formcontrolname='password']",
        "input[placeholder*='assword' i]",
    ])

    try:
        title = await tab.get_title()
        print(f"  [debug] Page title: {title}")
    except Exception:
        pass

    if not email_el:
        print("[Login] Email field not found — page may still be blocked.")
        return False
    if not password_el:
        print("[Login] Password field not found.")
        return False

    try:
        await email_el.clear_input()
    except Exception:
        pass
    await email_el.send_keys(VFS_USERNAME)
    await asyncio.sleep(0.2)

    try:
        await password_el.clear_input()
    except Exception:
        pass
    await password_el.send_keys(VFS_PASSWORD)
    await asyncio.sleep(0.2)

    btn, _ = await wait_for_any(tab, [
        "button[type='submit']",
        "button[id*='login' i]",
        "button[class*='login' i]",
    ], timeout=3)
    if btn:
        await btn.click()
        print("[Login] Clicked submit.")
    else:
        await password_el.send_keys("\n")
        print("[Login] Pressed Enter to submit.")

    post_login, _ = await wait_for_any(tab, [
        "app-dashboard",
        "app-home",
        "app-new-appointment",
        "[class*='dashboard']",
        "[routerlink*='book-an-appointment']",
        "a[href*='book-an-appointment']",
        "[routerlink*='dashboard']",
    ], timeout=10)
    if post_login:
        print("[Login] Post-login page detected.")
    else:
        print("[Login] Could not confirm post-login page — continuing.")

    print("[Login] Done.")
    return True


# ──────────────────────────────────────────────────────────────
# Form fill (one client, one tab)
# ──────────────────────────────────────────────────────────────

async def fill_client(browser, client: dict, client_idx: int, total: int) -> None:
    name = f"{client.get('first_name','')} {client.get('last_name','')}".strip()
    print(f"\n[Client {client_idx}/{total}] Opening tab for {name}...")

    tab = await browser.get(VFS_APP_URL, new_tab=True)
    await wait_for_cloudflare(tab)

    # Diagnose: print actual URL and title so we can see if redirected to /login
    try:
        current_url   = await tab.evaluate("window.location.href")
        current_title = await tab.get_title()
        print(f"  [nav]  URL:   {current_url}")
        print(f"  [nav]  Title: {current_title}")
    except Exception:
        current_url = ""

    # If redirected back to login (session not established), attempt login again
    if current_url and "/login" in current_url:
        print(f"  [!]   Redirected to login page — session not established. Re-logging in...")
        logged = await login(tab)
        if not logged:
            print(f"  [!]   Re-login failed for {name} — skipping.")
            return
        # Navigate to booking form in this same tab after re-login
        await tab.get(VFS_APP_URL)
        await wait_for_cloudflare(tab)
        try:
            current_url = await tab.evaluate("window.location.href")
            print(f"  [nav]  URL after re-login: {current_url}")
        except Exception:
            pass

    visa_present = await wait_for(tab, SELECTORS["visa_type"], timeout=30)
    if not visa_present:
        try:
            current_url = await tab.evaluate("window.location.href")
            print(f"  [!] Booking form did not load for {name} — current URL: {current_url}")
        except Exception:
            print(f"  [!] Booking form did not load for {name} — skipping.")
        return

    # Step 1: dropdowns
    await mat_select(tab, SELECTORS["visa_type"],          client.get("visa_type"),           "visa_type")
    await asyncio.sleep(0.4)
    await mat_select(tab, SELECTORS["application_center"], client.get("application_center"),  "application_center")
    await asyncio.sleep(0.4)
    await mat_select(tab, SELECTORS["service_center"],     client.get("service_center"),      "service_center")
    await asyncio.sleep(0.4)
    await mat_select(tab, SELECTORS["trip_reason"],        client.get("trip_reason"),         "trip_reason")
    await asyncio.sleep(0.8)

    # Step 2: personal info
    first_field = await wait_for(tab, SELECTORS["first_name"], timeout=ELEMENT_WAIT)
    if not first_field:
        print(f"  [!] Personal info section did not appear for {name} — skipping.")
        return

    raw_dob      = client.get("date_of_birth", "")
    raw_expiry   = client.get("passport_expiry", "")
    country_code = _normalise_country_code(client.get("mobile_country_code", ""))

    await js_fill(tab, SELECTORS["first_name"],          client.get("first_name"),       "first_name")
    await js_fill(tab, SELECTORS["last_name"],           client.get("last_name"),        "last_name")
    await js_fill(tab, SELECTORS["date_of_birth"],       _normalise_date(raw_dob),       "date_of_birth")
    await js_fill(tab, SELECTORS["email"],               client.get("email"),            "email")
    await js_fill(tab, SELECTORS["mobile_country_code"], country_code,                  "mobile_country_code")
    await js_fill(tab, SELECTORS["mobile_number"],       client.get("mobile_number"),    "mobile_number")
    await js_fill(tab, SELECTORS["passport_number"],     client.get("passport_number"),  "passport_number")
    await js_fill(tab, SELECTORS["passport_expiry"],     _normalise_date(raw_expiry),    "passport_expiry")

    await mat_select(tab, SELECTORS["gender"],              client.get("gender"),               "gender")
    await mat_select(tab, SELECTORS["current_nationality"], client.get("current_nationality"),  "current_nationality")

    btn = await wait_for(tab, SELECTORS["submit"], timeout=3)
    if btn:
        await btn.click()
        print(f"  [ok]   submit clicked for {name}")
    else:
        print(f"  [warn] submit button not found for {name}")
        return

    await asyncio.sleep(2.5)

    ref = None
    for sel in SELECTORS["confirm_ref"]:
        try:
            el = await tab.find(sel, timeout=0.5)
            if el:
                text = (await el.get_attribute("innerText") or "").strip()
                if text:
                    ref = text
                    break
        except Exception:
            continue

    if ref:
        print(f"  [CONFIRMED] Booking reference for {name}: {ref}")
    else:
        print(f"  [?] Submit done — no reference found yet for {name}. Check the browser tab.")


# ──────────────────────────────────────────────────────────────
# Browser launch
# ──────────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    return (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or next(iter(sorted(glob.glob(
            os.path.expanduser("~/.cache/selenium/chrome/linux64/*/chrome")
        ), reverse=True)), None)
    )


async def launch_browser(proxy: str = ""):
    chrome_bin = _find_chrome()
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    browser_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1280,900",
        f"--user-data-dir={CHROME_PROFILE}",
    ]
    if proxy:
        browser_args.append(f"--proxy-server={proxy}")
        browser_args.append("--proxy-bypass-list=localhost,127.0.0.1")
        print(f"[Browser] Routing through proxy: {proxy}")

    launch_kwargs: dict = dict(
        headless=False,
        user_data_dir=CHROME_PROFILE,
        browser_args=browser_args,
    )
    if chrome_bin:
        print(f"[Browser] Using: {chrome_bin}")
        launch_kwargs["browser_executable_path"] = chrome_bin

    return await uc.start(**launch_kwargs)


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="VFS booking automation — run --warmup first, then run without flags during the booking window."
    )
    parser.add_argument(
        "--warmup", action="store_true",
        help="Log in and warm the session/Cloudflare cookies. Run BEFORE the booking window opens.",
    )
    parser.add_argument(
        "--max-clients", type=int, default=MAX_CLIENTS, metavar="N",
        help="Maximum number of clients to book per session.",
    )
    parser.add_argument(
        "--proxy", default=os.getenv("VFS_PROXY", VFS_PROXY_DEFAULT),
        help="Proxy URL e.g. socks5://user:pass@host:port. Can also be set via VFS_PROXY env variable.",
    )
    parser.add_argument(
        "--clients-csv", default=str(CLIENTS_CSV), metavar="PATH",
        help="Path to clients.csv",
    )
    args = parser.parse_args()

    if not args.proxy:
        print("[Warn] No proxy set. If geo-blocked the site may return 403.")
        print("       Set one with --proxy socks5://user:pass@host:port or export VFS_PROXY=...")
    else:
        print(f"[Proxy] Using: {args.proxy}")

    df = pd.read_csv(args.clients_csv, dtype=str).fillna("")
    clients = df.to_dict(orient="records")[: args.max_clients]
    print(f"[Main] Loaded {len(clients)} client(s) from {args.clients_csv}.")

    browser = await launch_browser(proxy=args.proxy)
    tab = await browser.get(VFS_LOGIN_URL)

    if args.warmup:
        print("\n" + "="*60)
        print("[Warmup] MANUAL SESSION SETUP")
        print("="*60)
        print("  The browser is open at the VFS login page.")
        print("  1. Solve any Cloudflare challenge in the browser window.")
        print("  2. Log in with your VFS credentials if not already done.")
        print("  3. Come back here and press ENTER when you are logged in.")
        print("="*60)
        try:
            await asyncio.get_event_loop().run_in_executor(None, input, "     >>> Press ENTER once logged in: ")
        except EOFError:
            await asyncio.sleep(10)

        print("\n[Warmup] Navigating to booking page to warm CF cookies...")
        warmup_tab = await browser.get(VFS_APP_URL, new_tab=True)

        print("[Warmup] Waiting up to 120s for booking form to appear...")
        print("         If a Cloudflare challenge appears, solve it in the browser.")
        form_el = await wait_for(warmup_tab, SELECTORS["visa_type"], timeout=120)
        if form_el:
            print("[Warmup] ✓ Booking form loaded successfully!")
        else:
            try:
                stuck_url = await warmup_tab.evaluate("window.location.href")
                stuck_title = await warmup_tab.get_title()
            except Exception:
                stuck_url, stuck_title = "unknown", "unknown"
            print(f"[Warmup] ✗ Booking form did NOT load.")
            print(f"         Stuck at: {stuck_url}")
            print(f"         Title:    {stuck_title}")
            print("         Cookies may still be partially saved. Try running without --warmup.")

        print(f"[Warmup] Session cookies saved to profile: {CHROME_PROFILE}")
        print("[Warmup] Run without --warmup when the booking window opens.")
        await asyncio.sleep(3)
        return

    # Normal run: automated login
    logged_in = await login(tab)
    if not logged_in:
        print("\n[!] Login failed. Check credentials or solve Cloudflare manually in the browser window.")
        print("    The browser will stay open for 60s — solve any challenge then re-run.")
        await asyncio.sleep(60)
        return

    print(f"\n[Booking] Launching {len(clients)} client tab(s) in parallel...")
    t0 = time.monotonic()

    tasks = [
        fill_client(browser, client, idx, len(clients))
        for idx, client in enumerate(clients, start=1)
    ]
    await asyncio.gather(*tasks)

    elapsed = time.monotonic() - t0
    print(f"\n[Done] All {len(clients)} client(s) processed in {elapsed:.1f}s.")
    print("       Browser stays open for review — press Ctrl+C to exit.")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
