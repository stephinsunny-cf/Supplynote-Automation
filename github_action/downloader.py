"""
downloader.py
SupplyNote Daily Ingredients Report — GitHub Actions Edition

Runs on a cron schedule via GitHub Actions.
Logs into SupplyNote via real browser (Playwright) → navigates to
today's Ingredients demand plan → downloads All Ingredient Data
→ emails as xlsx attachment.

All credentials come from environment variables / GitHub Secrets.
No .env file needed in production.
"""

import io
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# Load .env file so local credentials work (GitHub Actions uses real env vars)
try:
    from dotenv import load_dotenv
    # override=True ensures .env file values win over system environment variables
    load_dotenv(override=True)
    load_dotenv(Path(__file__).parent / ".env",          override=True)
    load_dotenv(Path(__file__).parent.parent / ".env",   override=True)
except ImportError:
    pass   # dotenv not installed — GitHub Actions uses real env vars, that's fine

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("downloader")

# ── Config — all from GitHub Secrets / env vars ────────────────────────────────
# Accepts both naming conventions:
#   Local .env  → SUPPLYNOTE_USER / SUPPLYNOTE_PASS / SUPPLYNOTE_JWT / SUPPLYNOTE_BUSINESS_ID
#   GitHub Secrets → SN_USERNAME / SN_PASSWORD / SN_TOKEN / SN_BUSINESS_ID
SN_USERNAME    = os.environ.get("SN_USERNAME", "")    or os.environ.get("SUPPLYNOTE_USER", "")
SN_PASSWORD    = os.environ.get("SN_PASSWORD", "")    or os.environ.get("SUPPLYNOTE_PASS", "")
SN_TOKEN       = os.environ.get("SN_TOKEN", "")       or os.environ.get("SUPPLYNOTE_JWT", "")
SN_BUSINESS_ID = os.environ.get("SN_BUSINESS_ID", "") or os.environ.get("SUPPLYNOTE_BUSINESS_ID", "")
GMAIL_USER     = os.environ.get("GMAIL_USER", "")     or os.environ.get("EMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASSWORD", "") or os.environ.get("GMAIL_APP_PASS", "")
RECIPIENT      = os.environ.get("RECIPIENT_EMAIL", "") or os.environ.get("EMAIL_USER", "")
DOWNLOAD_TYPE  = os.environ.get("DOWNLOAD_TYPE", "all")

BASE      = "https://www.supplynote.in/api"
JWT_RE    = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
BIZ_ID_RE = re.compile(r"^[a-f0-9]{24}$")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Date helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_ist_now() -> datetime:
    """Return the current datetime in IST (UTC+5:30)."""
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST)


def date_to_plan_date(dt: datetime) -> str:
    """
    Convert a date in IST to SupplyNote's planDate (UTC ISO string).

    SupplyNote treats plan dates as IST midnight → UTC equivalent.
    e.g. 22-Apr-2026 IST midnight = 21-Apr-2026 18:30:00 UTC

    Args:
        dt: Any datetime in IST timezone.
    Returns:
        planDate string like "2026-04-21T18:30:00.000Z"
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    midnight_ist = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=IST)
    utc = midnight_ist.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Login (token only — used for history lookup)
# ─────────────────────────────────────────────────────────────────────────────

def _find_jwt_in(data) -> str | None:
    """Recursively search any dict/list for a JWT string."""
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and JWT_RE.match(v):
                return v
            found = _find_jwt_in(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_jwt_in(item)
            if found:
                return found
    return None


def _is_jwt_expired(token: str) -> bool:
    """Return True if the JWT's exp claim is in the past."""
    try:
        import base64, json as _json, time
        payload = token.split(".")[1]
        # Add padding if needed
        payload += "=" * (-len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp", 0)
        return exp < time.time()
    except Exception:
        return False   # Can't decode → assume valid


def login() -> str:
    """
    Return a valid JWT token.
    Priority:
      1. SN_TOKEN env var — use directly ONLY if not expired
      2. token_manager.py if available (local dev)
      3. Direct API login with SN_USERNAME + SN_PASSWORD
    """
    # Use pre-existing token only if it's still valid (not expired)
    if SN_TOKEN and JWT_RE.match(SN_TOKEN):
        if _is_jwt_expired(SN_TOKEN):
            log.info("Stored SN_TOKEN is expired — doing fresh login.")
        else:
            log.info("Using pre-existing token from SN_TOKEN env var.")
            return SN_TOKEN

    # Try token_manager.py if available (local machine)
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from token_manager import get_valid_token
        log.info("Using token_manager.py for login...")
        return get_valid_token()
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"token_manager failed: {e} — falling back to direct login")

    if not SN_USERNAME or not SN_PASSWORD:
        raise ValueError(
            "Set either SN_TOKEN (existing JWT) or both SN_USERNAME + SN_PASSWORD."
        )

    login_urls = [
        "https://www.supplynote.in/api/auth/signin",
        "https://www.supplynote.in/api/auth/login",
        "https://www.supplynote.in/api/v1/auth/login",
    ]
    body_variants = [
        {"username": SN_USERNAME, "password": SN_PASSWORD},
        {"email":    SN_USERNAME, "password": SN_PASSWORD},
    ]

    for url in login_urls:
        for body in body_variants:
            try:
                log.info(f"Login attempt: POST {url}")
                res = requests.post(url, json=body, timeout=20)

                if res.status_code == 404:
                    break  # Wrong URL — skip other body variants
                if res.status_code == 401:
                    raise ValueError(
                        "Login failed — wrong credentials.\n"
                        "Check SN_USERNAME and SN_PASSWORD in GitHub Secrets."
                    )
                if res.status_code not in (200, 201):
                    log.warning(f"Unexpected status {res.status_code}")
                    continue

                try:
                    token = _find_jwt_in(res.json())
                    if token:
                        log.info("Login successful — token obtained.")
                        return token
                except Exception:
                    pass

                m = JWT_RE.search(res.text)
                if m:
                    log.info("Login successful — token found in raw response.")
                    return m.group(0)

                log.warning(f"200 OK but no token in response: {res.text[:200]}")

            except ValueError:
                raise
            except Exception as e:
                log.warning(f"Login attempt error: {e}")

    raise RuntimeError(
        "Could not login to SupplyNote after all attempts.\n"
        "Verify your credentials and that supplynote.in is reachable."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Business ID
# ─────────────────────────────────────────────────────────────────────────────

def _extract_biz_id(data) -> str | None:
    """Pull a 24-char hex business/outlet ID from a JSON response."""
    if not isinstance(data, dict):
        return None
    biz = data.get("business")
    out = data.get("outlet")
    candidates = [
        data.get("businessId"),
        data.get("business_id"),
        biz.get("_id") if isinstance(biz, dict) else biz,
        out.get("_id") if isinstance(out, dict) else out,
        (data.get("user") or {}).get("businessId"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and BIZ_ID_RE.match(c):
            return c
    if data.get("data"):
        return _extract_biz_id(data["data"])
    return None


def get_business_id(token: str) -> str:
    """
    Return the SupplyNote business ID.
    Uses SN_BUSINESS_ID secret if set, otherwise auto-discovers from API.
    """
    if SN_BUSINESS_ID and BIZ_ID_RE.match(SN_BUSINESS_ID):
        log.info(f"Using stored business ID: {SN_BUSINESS_ID}")
        return SN_BUSINESS_ID

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for ep in [f"{BASE}/me", f"{BASE}/user/me", f"{BASE}/user/profile", f"{BASE}/auth/profile"]:
        try:
            res = requests.get(ep, headers=headers, timeout=15)
            if res.ok:
                biz_id = _extract_biz_id(res.json())
                if biz_id:
                    log.info(f"Business ID discovered: {biz_id}")
                    return biz_id
        except Exception as e:
            log.debug(f"Endpoint {ep} failed: {e}")

    raise RuntimeError(
        "Could not determine Business ID.\n"
        "Add SN_BUSINESS_ID as a GitHub Secret with your business ID."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. History
# ─────────────────────────────────────────────────────────────────────────────

def fetch_latest_version(token: str, biz_id: str, plan_date: str) -> dict:
    """
    Fetch the semiFinished demand plan history for the given planDate.
    Returns the latest version object (index 0).
    """
    url = (
        f"{BASE}/demandplan/history/semiFinished"
        f"?business={biz_id}&planDate={requests.utils.quote(plan_date)}"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    log.info(f"Fetching history: {url}")
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()

    data = res.json()
    versions = data if isinstance(data, list) else (
        data.get("data") or data.get("history") or data.get("list") or []
    )

    if not versions:
        raise RuntimeError(
            f"No Ingredients demand plan found for date: {plan_date}\n"
            "Make sure demand has been uploaded for today on SupplyNote."
        )

    log.info(f"Found {len(versions)} version(s). Using latest.")
    return versions[0]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Download via Playwright (primary — bypasses 504)
# ─────────────────────────────────────────────────────────────────────────────

def download_via_playwright(biz_id: str, today_ist: datetime, version_key: str = "") -> bytes:
    """
    Use a real Chromium browser (Playwright) to navigate the SupplyNote UI
    and download the All Ingredient Data report.

    Flow: /signin → dismiss modal → demand plan sidebar → set date (md-datepicker)
          → Ingredients tab → All Ingredient Data → Download → capture file.

    Uses the correct AngularJS patterns discovered from playwright_script.py.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "playwright not installed.\n"
            "Run:  pip install playwright && playwright install chromium\n"
            "GitHub Actions: see workflow for install steps."
        )

    # Credentials: local .env uses SUPPLYNOTE_USER/PASS; GitHub Secrets use SN_USERNAME/PASSWORD
    username = os.environ.get("SUPPLYNOTE_USER", "") or SN_USERNAME
    password = os.environ.get("SUPPLYNOTE_PASS", "") or SN_PASSWORD

    if not username or not password:
        raise RuntimeError(
            "No credentials found for Playwright login.\n"
            "Set SUPPLYNOTE_USER + SUPPLYNOTE_PASS in .env  (local)\n"
            "or SN_USERNAME + SN_PASSWORD in GitHub Secrets  (Actions)."
        )

    date_display = today_ist.strftime("%d/%m/%Y")   # DD/MM/YYYY — SupplyNote UI format
    log.info(f"[Browser] Starting Playwright download for {date_display}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── Network interception: capture file bytes if the download comes as XHR ─
        captured = {"bytes": None}

        def on_response(response):
            try:
                if "demandplan/download" in response.url and response.status == 200:
                    body = response.body()
                    if body and len(body) > 1000:
                        captured["bytes"] = body
                        log.info(f"[Browser] XHR intercepted: {len(body):,} bytes")
            except Exception:
                pass

        page.on("response", on_response)

        def _screenshot(label: str) -> None:
            try:
                ws = str(Path(__file__).parent.parent / f"debug_screenshot_{label}.png")
                page.screenshot(path=ws, full_page=True)
                log.info(f"[Browser] Screenshot → {ws}")
            except Exception:
                pass

        try:
            # ── Step 1: Login at /signin ──────────────────────────────────────
            log.info("[Browser] Navigating to https://www.supplynote.in/signin ...")
            page.goto(
                "https://www.supplynote.in/signin",
                wait_until="networkidle",
                timeout=30_000,
            )
            page.wait_for_timeout(1_000)

            log.info("[Browser] Filling credentials...")
            # SupplyNote /signin uses input[name="username"]
            page.fill(
                'input[name="username"], input[name="email"], '
                'input[placeholder*="username" i], input[placeholder*="email" i]',
                username,
            )
            page.fill('input[type="password"]', password)
            page.click('button[type="submit"]')

            # Wait for URL to leave /signin — confirms successful login
            try:
                page.wait_for_url(
                    lambda url: "/signin" not in url and "/login" not in url,
                    timeout=20_000,
                )
            except PWTimeout:
                _screenshot(today_ist.strftime("%Y%m%d_%H%M%S") + "_login_failed")
                raise RuntimeError(
                    "[Browser] Login failed — still on /signin after submit.\n"
                    "Check SUPPLYNOTE_USER / SUPPLYNOTE_PASS in .env"
                )
            log.info(f"[Browser] Logged in → {page.url}")

            # ── Dismiss post-login modal ("Now LoggedIn with ...") ────────────
            try:
                ok = page.locator('button:has-text("OK"), button:has-text("Ok")').first
                ok.wait_for(state="visible", timeout=5_000)
                ok.click()
                log.info("[Browser] Dismissed login modal.")
                page.wait_for_timeout(1_000)
            except Exception:
                log.info("[Browser] No post-login modal.")

            # ── Step 2: Navigate to Recipes (Demand Plan) via sidebar ────────
            # The demand plan section is under "Recipes" in the SupplyNote sidebar.
            log.info("[Browser] Clicking Recipes in sidebar...")

            clicked_nav = False

            # AngularJS sidebar: try ng-click pattern first, then text match
            nav_selectors = [
                'button[ng-click*="goToState"]:has-text("Recipes")',
                'button[ng-click*="goToState"]:has-text("Recipe")',
                'a:has-text("Recipes")',
                'a:has-text("Recipe")',
                'li:has-text("Recipes") a',
                '[class*="sidebar"] :has-text("Recipes")',
            ]
            for sel in nav_selectors:
                try:
                    el = page.locator(sel)
                    if el.count() > 0:
                        el.first.click(force=True)
                        page.wait_for_load_state("networkidle", timeout=15_000)
                        page.wait_for_timeout(2_000)
                        log.info(f"[Browser] Clicked sidebar Recipes via: {sel} → {page.url}")
                        clicked_nav = True
                        break
                except Exception:
                    continue

            if not clicked_nav:
                # JS click as last resort (bypasses any visibility issues)
                clicked_nav = page.evaluate("""() => {
                    const els = Array.from(document.querySelectorAll('a, button, li, span'));
                    const el = els.find(e => /^recipes?$/i.test(e.textContent.trim()));
                    if (el) { el.click(); return true; }
                    return false;
                }""")
                if clicked_nav:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    page.wait_for_timeout(2_000)
                    log.info(f"[Browser] Clicked Recipes via JS → {page.url}")

            if not clicked_nav:
                raise RuntimeError(
                    "[Browser] Could not find Recipes in sidebar. "
                    "Check the debug screenshot for available nav items."
                )

            log.info(f"[Browser] After Recipes click: {page.url}")
            _screenshot(today_ist.strftime("%Y%m%d") + "_recipes_page")

            # ── Click "Demand Upload History" card under Demand Planning ──────
            log.info("[Browser] Clicking 'Demand Upload History' card...")
            try:
                page.get_by_text("Demand Upload History", exact=False).first.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(2_000)
                log.info(f"[Browser] Demand Upload History → {page.url}")
            except Exception:
                # JS fallback
                page.evaluate("""() => {
                    const els = Array.from(document.querySelectorAll('*'));
                    const el = els.find(e => /demand.upload.history/i.test(e.textContent.trim())
                                         && e.children.length <= 2);
                    if (el) el.click();
                }""")
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(2_000)
                log.info(f"[Browser] Demand Upload History (JS) → {page.url}")

            _screenshot(today_ist.strftime("%Y%m%d") + "_demand_plan")

            # ── Step 3: Set date — MM/DD/YYYY format ─────────────────────────
            # The page defaults to today's date, but we set it explicitly.
            # After setting, close the calendar popup before doing anything else.
            date_mmddyyyy = today_ist.strftime("%m/%d/%Y")   # e.g. 04/26/2026
            log.info(f"[Browser] Setting date to {date_mmddyyyy} (MM/DD/YYYY)...")
            page.evaluate(f"""() => {{
                const inputs = Array.from(document.querySelectorAll(
                    'md-datepicker input, input[ng-model*="Date"], input[ng-model*="date"]'
                ));
                const el = inputs[0];
                if (!el) return;
                el.focus();
                el.value = '{date_mmddyyyy}';
                el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Tab', bubbles: true}}));
                el.blur();
            }}""")

            # Close any calendar popup that opened — Escape + click body
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            # Click somewhere safe (left sidebar area) to dismiss calendar
            page.mouse.click(100, 600)
            page.wait_for_timeout(1_500)
            log.info(f"[Browser] Date set: {date_mmddyyyy}")
            _screenshot(today_ist.strftime("%Y%m%d") + "_after_date_set")

            # ── Step 4: Click INGREDIENTS tab ────────────────────────────────
            log.info("[Browser] Clicking INGREDIENTS tab...")
            # Tabs show as "MENU" | "INGREDIENTS" | "PRODUCTION PLANS"
            # Use Playwright locator with exact text (uppercase as shown in UI)
            for tab_text in ["INGREDIENTS", "Ingredients", "INGREDIENT"]:
                try:
                    tab = page.get_by_text(tab_text, exact=True)
                    if tab.count() > 0:
                        tab.first.click(force=True)
                        log.info(f"[Browser] Clicked tab: '{tab_text}'")
                        break
                except Exception:
                    continue

            page.wait_for_timeout(3_000)   # wait for table to reload
            _screenshot(today_ist.strftime("%Y%m%d") + "_ingredients_tab")

            # ── Safety: Disable "Sync with Production" button ─────────────────
            # This button must NEVER be clicked by automation — it overwrites
            # production data. We disable it in the DOM before doing anything else.
            page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('button, md-button, a'));
                els.forEach(el => {
                    if (/sync.*(with.*)?production/i.test(el.textContent)) {
                        el.disabled = true;
                        el.setAttribute('disabled', 'disabled');
                        el.style.pointerEvents = 'none';
                        el.style.opacity = '0.3';
                    }
                });
            }""")
            log.info("[Browser] 'Sync with Production' button disabled for safety.")

            # ── Step 5: Use in-browser fetch() to get the S3 download link ────
            # Instead of clicking the dropdown menu (unreliable in headless mode),
            # we run a fetch() call directly inside the browser page.
            # The browser already has all session cookies set from login, so
            # this call is authenticated automatically — no JWT header needed.
            log.info("[Browser] Using in-browser fetch() to get S3 download link...")

            s3_url = page.evaluate(f"""async () => {{
                const url = '/api/demandplan/download/semiFinished-combined?type=all&versionKey={version_key}';
                try {{
                    const resp = await fetch(url, {{
                        method: 'GET',
                        credentials: 'include',
                        headers: {{ 'Accept': 'application/json' }}
                    }});
                    const body = await resp.json();
                    return body.data || body.url || body.fileUrl || body.download_url || JSON.stringify(body);
                }} catch(e) {{
                    return 'fetch_error: ' + e.message;
                }}
            }}""")

            log.info(f"[Browser] fetch() result: {str(s3_url)[:120]}")

            if not s3_url or not isinstance(s3_url, str) or s3_url.startswith('fetch_error') or s3_url.startswith('{'):
                raise RuntimeError(f"[Browser] fetch() did not return S3 URL: {s3_url}")

            # Download the CSV directly from S3 using requests (no auth needed for S3)
            log.info(f"[Browser] Downloading from S3: {s3_url[:80]}...")
            import requests as _req
            r2 = _req.get(s3_url, timeout=(30, 300), stream=True)
            r2.raise_for_status()
            chunks, total = [], 0
            for chunk in r2.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
                    if total % (20 * 1024 * 1024) < 65536:
                        log.info(f"[Browser] Downloaded {total / 1024 / 1024:.0f} MB...")
            content = b"".join(chunks)
            log.info(f"[Browser] File size: {len(content):,} bytes")
            return content

        except Exception as e:
            _screenshot(today_ist.strftime("%Y%m%d_%H%M%S") + "_error")
            if captured["bytes"]:
                log.info(f"[Browser] Using XHR capture after error: {len(captured['bytes']):,}")
                return captured["bytes"]
            raise RuntimeError(f"Playwright download failed: {e}") from e

        finally:
            browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Download via API (quick attempt — often 504 on large reports)
# ─────────────────────────────────────────────────────────────────────────────

def download_report_api(token: str, biz_id: str, version_key: str) -> bytes:
    """
    Download via the real SupplyNote API endpoint (discovered from HAR capture).

    Flow:
      1. GET /api/demandplan/download/semiFinished-combined?type=all&versionKey=...
         → Returns JSON: {"error": false, "data": "https://s3.amazonaws.com/....csv"}
      2. Download the CSV directly from the S3 URL (fast, no 504 issues).

    The old endpoint (/download/semiFinished) caused 504 Gateway Timeout.
    The correct endpoint (/download/semiFinished-combined) responds in <1s with an S3 link.
    """
    # Step 1: Ask SupplyNote for the S3 download link
    url = (
        f"{BASE}/demandplan/download/semiFinished-combined"
        f"?type={DOWNLOAD_TYPE}&versionKey={requests.utils.quote(version_key)}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.supplynote.in/",
    }

    log.info(f"[API] Requesting download link: {url}")
    try:
        res = requests.get(url, headers=headers, timeout=(15, 120))
        res.raise_for_status()

        body = res.json()
        # Response shape: {"error": false, "message": "...", "data": "<s3-url>"}
        # "data" is a plain string URL (not a nested dict)
        s3_url = body.get("data")
        if not s3_url or not isinstance(s3_url, str):
            # Fallback: check other common fields
            s3_url = (
                body.get("url") or body.get("download_url") or
                body.get("fileUrl") or body.get("link")
            )
        if not s3_url:
            raise RuntimeError(f"No download URL in API response: {str(body)[:300]}")

        log.info(f"[API] Got S3 link: {s3_url[:80]}...")

        # Step 2: Download the actual CSV from S3 (direct, no auth needed)
        log.info(f"[API] Downloading CSV from S3...")
        r2 = requests.get(s3_url, timeout=(30, 300), stream=True)
        r2.raise_for_status()

        chunks, total = [], 0
        for chunk in r2.iter_content(chunk_size=65536):
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if total % (10 * 1024 * 1024) < 65536:   # log every ~10 MB
                    log.info(f"[API] Downloaded {total / 1024 / 1024:.0f} MB so far...")

        if total == 0:
            raise RuntimeError("S3 returned empty file")

        log.info(f"[API] Download complete: {total:,} bytes ({total / 1024 / 1024:.1f} MB)")
        return b"".join(chunks)

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        raise RuntimeError(f"API download HTTP {status}: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# 7. CSV → xlsx conversion
# ─────────────────────────────────────────────────────────────────────────────

def ensure_xlsx(content: bytes, base_filename: str) -> tuple[bytes, str]:
    """
    If the server returned CSV instead of xlsx, convert it.
    Detects xlsx by the PK zip magic bytes at the start.
    Returns (file_bytes, filename).
    """
    if content[:2] == b"PK":
        log.info("File is already xlsx.")
        return content, base_filename

    log.info("File appears to be CSV — converting to xlsx...")
    try:
        import pandas as pd

        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(io.BytesIO(content), encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Could not decode CSV with any known encoding")

        xlsx_name = re.sub(r"\.(csv|txt)$", ".xlsx", base_filename, flags=re.IGNORECASE)
        if not xlsx_name.endswith(".xlsx"):
            xlsx_name = base_filename.rsplit(".", 1)[0] + ".xlsx"

        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        log.info(f"Converted CSV ({len(content):,}B) → xlsx ({buf.tell():,}B)")
        return buf.getvalue(), xlsx_name

    except Exception as e:
        log.warning(f"CSV→xlsx conversion failed: {e}. Will send as original format.")
        return content, base_filename


# ─────────────────────────────────────────────────────────────────────────────
# 8. Email
# ─────────────────────────────────────────────────────────────────────────────

def _compress_to_zip(file_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """Compress file into a ZIP archive. Returns (zip_bytes, zip_filename)."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(filename, file_bytes)
    zip_bytes = buf.getvalue()
    zip_name  = filename.rsplit(".", 1)[0] + ".zip"
    log.info(f"Compressed {len(file_bytes):,}B → {len(zip_bytes):,}B  ({zip_name})")
    return zip_bytes, zip_name


def _upload_to_drive(file_bytes: bytes, filename: str, creds) -> str:
    """
    Upload CSV to Google Drive and return a direct download link.
    File is shared as 'anyone with link can view'.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    drive = build("drive", "v3", credentials=creds)

    mimetype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.endswith(".xlsx") else "text/csv"
    )
    media = MediaInMemoryUpload(
        file_bytes,
        mimetype=mimetype,
        resumable=True,
    )
    meta = {"name": filename}
    uploaded = drive.files().create(
        body=meta, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]

    # Anyone with the link can download
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    info = drive.files().get(
        fileId=file_id, fields="webViewLink,webContentLink"
    ).execute()

    link = info.get("webContentLink") or info.get("webViewLink")
    log.info(f"[Drive] Uploaded '{filename}' → {link}")
    return link


def _build_mime(filename: str, report_date: str,
                sender: str, recipient: str,
                drive_link: str) -> MIMEMultipart:
    """Build the MIME email with a Google Drive download link (no attachment)."""
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = f"SupplyNote Ingredients Report — {report_date}"
    body = (
        f"Hi,\n\n"
        f"The All Ingredient Data report for {report_date} is ready.\n\n"
        f"  File   : {filename}\n"
        f"  Date   : {report_date}\n"
        f"  Type   : {DOWNLOAD_TYPE}\n\n"
        f"Download link (Google Drive):\n{drive_link}\n\n"
        f"This email was sent automatically by the SupplyNote Report Automation.\n\n"
        f"Regards,\nSupplyNote Automation"
    )
    msg.attach(MIMEText(body, "plain"))
    return msg


def _send_via_oauth2(file_bytes: bytes, filename: str, report_date: str) -> bool:
    """
    Send email using Gmail API + OAuth2 (credentials.json / token.json).
    Returns True on success, False if OAuth2 is not configured locally.
    """
    creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", "config/credentials.json"))
    token_path = Path(os.environ.get("GMAIL_TOKEN_PATH",        "config/token.json"))
    sender     = os.environ.get("EMAIL_USER", "") or GMAIL_USER

    # Resolve paths relative to the parent of this script (project root)
    root = Path(__file__).parent.parent
    if not creds_path.is_absolute():
        creds_path = root / creds_path
    if not token_path.is_absolute():
        token_path = root / token_path

    if not token_path.exists():
        log.info("OAuth2 token.json not found — skipping OAuth2 email.")
        return False

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        import base64
    except ImportError:
        log.info("google-api-python-client not installed — skipping OAuth2 email.")
        return False

    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            log.info("OAuth2 token refreshed.")
        else:
            log.warning("OAuth2 token invalid and cannot be refreshed.")
            return False

    recipient = os.environ.get("RECIPIENT_EMAIL", "") or sender

    # File is too large to attach directly (114 MB > Gmail's 25 MB limit).
    # Upload to Google Drive and email the download link instead.
    log.info(f"[Drive] Uploading {filename} ({len(file_bytes):,} bytes) to Google Drive...")
    drive_link = _upload_to_drive(file_bytes, filename, creds)

    msg = _build_mime(filename, report_date, sender, recipient, drive_link)

    gmail = build("gmail", "v1", credentials=creds)
    raw   = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info(f"Email sent to {recipient} with Drive download link.")
    return True


def send_email(file_bytes: bytes, filename: str, report_date: str) -> None:
    """
    Send the report as an email attachment.

    Strategy:
      1. Gmail API + OAuth2  (uses config/credentials.json + config/token.json)
         → works locally where you've already done the OAuth2 flow
      2. Gmail SMTP + App Password  (uses GMAIL_USER + GMAIL_APP_PASSWORD env vars)
         → works on GitHub Actions where secrets are set
    """
    # ── Strategy 1: OAuth2 (local machine) ───────────────────────────────────
    if _send_via_oauth2(file_bytes, filename, report_date):
        return

    # ── Strategy 2: SMTP App Password (GitHub Actions / fallback) ────────────
    sender    = GMAIL_USER or os.environ.get("EMAIL_USER", "")
    recipient = RECIPIENT  or sender

    if not sender:
        raise ValueError(
            "No Gmail sender configured.\n"
            "Local:   ensure config/token.json exists (OAuth2 flow completed)\n"
            "Actions: set GMAIL_USER + GMAIL_APP_PASSWORD in GitHub Secrets"
        )
    if not GMAIL_APP_PASS:
        raise ValueError(
            "GMAIL_APP_PASSWORD not set.\n"
            "Go to myaccount.google.com → Security → App passwords → generate one\n"
            "Then add GMAIL_APP_PASSWORD=<16-char-password> to .env or GitHub Secrets"
        )

    # SMTP can't handle files this large — raise a clear error pointing to the fix
    raise RuntimeError(
        "OAuth2 token.json not found and SMTP cannot send files over 25 MB.\n"
        "On GitHub Actions: add GMAIL_CREDENTIALS_PATH / GMAIL_TOKEN_PATH secrets\n"
        "or use a different delivery method (e.g. Telegram, S3, SharePoint)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 55)
    log.info("  SupplyNote Ingredients Report — Daily Automation")
    log.info("=" * 55)

    # Step 1 — Determine yesterday's date in IST
    # Report runs at 10 AM IST and covers the previous day's demand plan
    today_ist  = get_ist_now() - timedelta(days=1)
    plan_date  = date_to_plan_date(today_ist)
    display    = today_ist.strftime("%d-%m-%Y")
    log.info(f"Report date : {display} (IST — yesterday)")
    log.info(f"Plan date   : {plan_date} (UTC)")

    # Step 2 — Login (get JWT for history lookup)
    log.info("--- Step 1/5: Login ---")
    token = login()

    # Step 3 — Get business ID
    log.info("--- Step 2/5: Business ID ---")
    biz_id = get_business_id(token)

    # Step 4 — Fetch history to get version key
    log.info("--- Step 3/5: Fetch history ---")
    latest = fetch_latest_version(token, biz_id, plan_date)
    version_key = (
        latest.get("versionKey") or latest.get("version_key") or
        latest.get("key")        or latest.get("_id")
    )
    if not version_key:
        raise RuntimeError(f"No versionKey in history response: {latest}")
    log.info(f"Version key : {version_key}")

    # Step 5 — Download via API (semiFinished-combined endpoint → S3 link)
    # HAR analysis confirmed: the correct endpoint returns an S3 URL instantly.
    # No Playwright needed — pure API download works reliably.
    log.info("--- Step 4/5: Download ---")
    content = None

    try:
        content = download_report_api(token, biz_id, version_key)
        log.info("[API] Download succeeded.")
    except Exception as api_err:
        log.warning(f"[API] Download failed: {api_err}")
        log.info("[Browser] Falling back to Playwright browser automation...")
        content = download_via_playwright(biz_id, today_ist, version_key)

    # Convert CSV → Excel (.xlsx)
    csv_filename  = f"All_Ingredient_Data_{today_ist.strftime('%Y%m%d')}.csv"
    content, filename = ensure_xlsx(content, csv_filename)
    log.info(f"File ready: {filename} ({len(content):,} bytes)")

    # Step 6 — Email
    log.info("--- Step 5/5: Send email ---")
    send_email(content, filename, display)

    log.info("=" * 55)
    log.info("  All done! Report downloaded and emailed.")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
