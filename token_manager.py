"""
token_manager.py

Handles JWT token for SupplyNote automatically.
Does NOT need recipe.json — login URL is hardcoded from Network tab.

Usage from any other script:
    from token_manager import get_valid_token
    token = get_valid_token()   # always returns a fresh, valid token
"""

import base64
import json
import logging
import os
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getenv("token_manager") if False else logging.getLogger("token_manager")

SUPPLYNOTE_USER = os.getenv("SUPPLYNOTE_USER", "")
SUPPLYNOTE_PASS = os.getenv("SUPPLYNOTE_PASS", "")

TOKEN_STORE = Path("config/token_store.json")

# Hardcoded login URL variants — tries each until one works
LOGIN_URLS = [
    "https://www.supplynote.in/api/auth/login",
    "https://www.supplynote.in/api/v1/auth/login",
    "https://www.supplynote.in/api/users/login",
    "https://www.supplynote.in/api/signin",
]

# Refresh 30 min before actual expiry
REFRESH_BUFFER_MINUTES = 30


# ─────────────────────────────────────────────────────────────────────────────
# Token store
# ─────────────────────────────────────────────────────────────────────────────

def _load_store() -> dict:
    if not TOKEN_STORE.exists():
        return {}
    try:
        with open(TOKEN_STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_store(token: str, expires_at: datetime) -> None:
    TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_STORE, "w") as f:
        json.dump({
            "token":      token,
            "expires_at": expires_at.isoformat(),
            "saved_at":   datetime.now().isoformat(),
        }, f, indent=2)
    logger.info(f"Token saved. Expires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')}")


# ─────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_jwt_expiry(token: str) -> datetime | None:
    """Read expiry from JWT payload (base64 encoded JSON in the middle section)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        exp = payload.get("exp")
        if exp:
            expires_at = datetime.fromtimestamp(exp)
            logger.info(f"JWT expires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
            return expires_at
    except Exception as e:
        logger.warning(f"Could not decode JWT expiry: {e}")
    return None


def _find_jwt_in_json(data, path=""):
    """Recursively scan any JSON object for a JWT token string."""
    import re
    JWT_RE = re.compile(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')
    if isinstance(data, dict):
        for key, value in data.items():
            cur = f"{path}.{key}" if path else key
            if isinstance(value, str) and JWT_RE.match(value):
                return value, cur
            elif isinstance(value, (dict, list)):
                result = _find_jwt_in_json(value, cur)
                if result:
                    return result
    elif isinstance(data, list):
        for i, item in enumerate(data):
            result = _find_jwt_in_json(item, f"{path}[{i}]")
            if result:
                return result
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Token validity
# ─────────────────────────────────────────────────────────────────────────────

def _is_token_valid() -> bool:
    store = _load_store()
    if not store.get("token") or not store.get("expires_at"):
        logger.info("No saved token found.")
        return False
    expires_at = datetime.fromisoformat(store["expires_at"])
    now        = datetime.now()
    buffer     = timedelta(minutes=REFRESH_BUFFER_MINUTES)
    if now >= (expires_at - buffer):
        logger.info(f"Token expiring soon or expired.")
        return False
    logger.info(f"Token valid. Remaining: {expires_at - now}")
    return True


def _get_saved_token() -> str | None:
    return _load_store().get("token")


# ─────────────────────────────────────────────────────────────────────────────
# Method 1 — Direct API login
# ─────────────────────────────────────────────────────────────────────────────

def _direct_api_login() -> str | None:
    """
    POST to SupplyNote login endpoint directly.
    Tries multiple URL + body field name combinations automatically.
    No browser needed — completes in ~1 second.
    """
    if not SUPPLYNOTE_USER or not SUPPLYNOTE_PASS:
        raise ValueError(
            "SUPPLYNOTE_USER and SUPPLYNOTE_PASS must be set in .env\n"
            "Example:\n"
            "  SUPPLYNOTE_USER=your@email.com\n"
            "  SUPPLYNOTE_PASS=yourpassword"
        )

    import re
    JWT_RE = re.compile(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')

    # Try different body field names (email vs username vs phone)
    body_variants = [
        {"username": SUPPLYNOTE_USER, "password": SUPPLYNOTE_PASS},
        {"email":    SUPPLYNOTE_USER, "password": SUPPLYNOTE_PASS},
        {"phone":    SUPPLYNOTE_USER, "password": SUPPLYNOTE_PASS},
    ]

    for url in LOGIN_URLS:
        for body in body_variants:
            try:
                logger.info(f"Trying: POST {url} | body keys: {list(body.keys())}")
                res = requests.post(url, json=body, timeout=15)

                if res.status_code == 404:
                    logger.info("  404 — wrong URL, trying next")
                    break   # no point trying other body variants for this URL

                if res.status_code == 401:
                    raise ValueError(
                        "Login failed — wrong credentials.\n"
                        "Check SUPPLYNOTE_USER and SUPPLYNOTE_PASS in .env"
                    )

                if res.status_code not in (200, 201):
                    logger.info(f"  {res.status_code} — unexpected, trying next")
                    continue

                # Status 200 — find token in response
                try:
                    data   = res.json()
                    result = _find_jwt_in_json(data)
                    if result:
                        token, path = result
                        logger.info(f"  Token found at '{path}'")
                        return token
                except Exception:
                    pass

                # Check Authorization response header
                auth = res.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    token = auth.split(" ", 1)[1]
                    logger.info("  Token found in response header")
                    return token

                # Check raw text
                match = JWT_RE.search(res.text)
                if match:
                    logger.info("  Token found in raw response text")
                    return match.group(0)

                logger.warning(f"  200 OK but no token found. Response: {res.text[:200]}")

            except ValueError:
                raise
            except Exception as e:
                logger.info(f"  Error: {e}")
                continue

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Method 2 — Headless browser login (fallback)
# ─────────────────────────────────────────────────────────────────────────────

async def _browser_login() -> str | None:
    """
    Opens an invisible browser, logs in, intercepts the JWT token.
    Fallback when direct API login doesn't work.
    Takes ~5-10 seconds.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    import re
    JWT_RE = re.compile(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')
    captured = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page()

        def on_request(request):
            nonlocal captured
            if captured:
                return
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                t = auth.split(" ", 1)[1]
                if JWT_RE.match(t):
                    captured = t
                    logger.info(f"Browser: token from request header: {t[:30]}...")

        async def on_response(response):
            nonlocal captured
            if captured or "supplynote.in" not in response.url:
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                body   = await response.json()
                result = _find_jwt_in_json(body)
                if result:
                    captured = result[0]
                    logger.info(f"Browser: token from response: {captured[:30]}...")
            except Exception:
                pass

        page.on("request",  on_request)
        page.on("response", on_response)

        await page.goto("https://www.supplynote.in/signin", wait_until="networkidle", timeout=30000)

        # Fill username
        for sel in ['input[type="email"]', 'input[name="username"]',
                    'input[placeholder*="email" i]', 'input[placeholder*="username" i]']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(SUPPLYNOTE_USER)
                    break
            except Exception:
                pass

        await page.fill('input[type="password"]', SUPPLYNOTE_PASS)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(5000)
        await browser.close()

    return captured


# ─────────────────────────────────────────────────────────────────────────────
# Public function — call this from everywhere
# ─────────────────────────────────────────────────────────────────────────────

def get_valid_token() -> str:
    """
    Always returns a valid, non-expired JWT token.

    - Cached and still valid → returned instantly
    - Expired/missing → logs in fresh automatically

    Usage:
        from token_manager import get_valid_token
        token = get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
    """
    if _is_token_valid():
        return _get_saved_token()

    logger.info("Fetching fresh token...")

    # Try direct API first (fast)
    token = _direct_api_login()

    # Fall back to browser if needed
    if not token:
        logger.info("Direct login failed — using browser fallback...")
        token = asyncio.run(_browser_login())

    if not token:
        raise RuntimeError(
            "Could not get auth token. Check:\n"
            "  1. SUPPLYNOTE_USER and SUPPLYNOTE_PASS in .env are correct\n"
            "  2. Internet connection is working\n"
            "  3. You can log in at supplynote.in manually"
        )

    expires_at = _decode_jwt_expiry(token) or (datetime.now() + timedelta(hours=24))
    _save_store(token, expires_at)
    logger.info(f"Token refreshed. Valid until: {expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Background auto-refresh loop
# ─────────────────────────────────────────────────────────────────────────────

def run_auto_refresh_loop(check_interval_minutes: int = 60):
    """
    Run in a background thread to keep token always fresh.

        import threading
        from token_manager import run_auto_refresh_loop
        t = threading.Thread(target=run_auto_refresh_loop, daemon=True)
        t.start()
    """
    import time
    logger.info(f"Auto-refresh loop started ({check_interval_minutes} min interval).")
    while True:
        try:
            get_valid_token()
            logger.info("Token check OK.")
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
        time.sleep(check_interval_minutes * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — run directly to test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("automation.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    if "--loop" in sys.argv:
        run_auto_refresh_loop()
    else:
        print("\n" + "="*50)
        print("🔑 Token Manager — Test")
        print("="*50)
        try:
            token = get_valid_token()
            store = _load_store()
            print(f"Token     : {token[:50]}...")
            print(f"Expires at: {store.get('expires_at', 'unknown')}")
            print(f"Saved at  : {store.get('saved_at', 'unknown')}")
        except Exception as e:
            print(f"FAILED: {e}")
        print("="*50)