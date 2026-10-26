"""
fb_scraper.py — Facebook post image extraction + PDF compilation.

Logs into Facebook via Playwright using stored credentials (session cookie
cached to avoid repeated logins), opens a given post URL, scrolls through
and collects full-resolution image URLs, downloads them, and compiles into
a single PDF.

Env vars required:
  FB_EMAIL, FB_PASSWORD  — Facebook login credentials
Optional:
  FB_COOKIE_PATH — path to persist the logged-in session (default: fb_session.json)
"""
import os
import re
import io
import json
import time
import random
import asyncio
import logging
import httpx
import img2pdf
from PIL import Image

logger = logging.getLogger(__name__)

FB_EMAIL = os.environ.get("FB_EMAIL", "")
FB_PASSWORD = os.environ.get("FB_PASSWORD", "")
FB_COOKIE_PATH = os.environ.get("FB_COOKIE_PATH", "/tmp/fb_session.json")

# --- Safety limits (tunable via env) ---
FB_MIN_COOLDOWN_SEC = int(os.environ.get("FB_MIN_COOLDOWN_SEC", "45"))   # min gap between any two scrapes
FB_DAILY_CAP = int(os.environ.get("FB_DAILY_CAP", "25"))                 # max scrapes per rolling 24h

_LOGIN_LOCK = asyncio.Lock()
_RATE_LOCK = asyncio.Lock()
_last_scrape_ts = 0.0
_daily_timestamps = []  # epoch seconds of past scrapes, pruned to last 24h


class FBScrapeError(Exception):
    pass


async def _enforce_rate_limit():
    """Global cooldown + rolling 24h cap, shared across all users of the bot."""
    global _last_scrape_ts
    async with _RATE_LOCK:
        now = time.time()
        # prune timestamps older than 24h
        _daily_timestamps[:] = [t for t in _daily_timestamps if now - t < 86400]

        if len(_daily_timestamps) >= FB_DAILY_CAP:
            raise FBScrapeError(
                f"⚠️ আজকের জন্য সর্বোচ্চ {FB_DAILY_CAP} বার scrape limit শেষ — অ্যাকাউন্ট সুরক্ষার জন্য। "
                f"কিছুক্ষণ পর আবার চেষ্টা করুন।"
            )

        elapsed = now - _last_scrape_ts
        if elapsed < FB_MIN_COOLDOWN_SEC:
            wait_left = round(FB_MIN_COOLDOWN_SEC - elapsed)
            raise FBScrapeError(
                f"⏳ একটু ধীরে — পরবর্তী request-এর জন্য আরও {wait_left}s অপেক্ষা করুন (অ্যাকাউন্ট সুরক্ষার জন্য cooldown)।"
            )

        _last_scrape_ts = now
        _daily_timestamps.append(now)


async def _human_delay(min_ms: int, max_ms: int):
    """Randomized jitter instead of fixed waits, to avoid predictable bot timing."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def _ensure_logged_in(context, page):
    """Load cached session if valid; otherwise log in fresh and cache it."""
    if os.path.exists(FB_COOKIE_PATH):
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            if "login" not in page.url:
                return
        except Exception:
            pass

    if not FB_EMAIL or not FB_PASSWORD:
        raise FBScrapeError("FB_EMAIL/FB_PASSWORD env vars not set — login credentials missing.")

    async with _LOGIN_LOCK:
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=30000)
        await page.fill("input[name='email']", FB_EMAIL)
        await page.fill("input[name='pass']", FB_PASSWORD)

        login_selectors = [
            "button[name='login']",
            "button[data-testid='royal_login_button']",
            "#loginbutton",
            "button[type='submit']",
        ]
        clicked = False
        for sel in login_selectors:
            try:
                await page.locator(sel).click(timeout=8000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            # Last resort: submit the password field directly
            await page.press("input[name='pass']", "Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await _human_delay(4000, 6500)

        if "checkpoint" in page.url or "login" in page.url:
            raise FBScrapeError(
                "Login failed or 2FA/checkpoint triggered — manual verification needed on this account."
            )

        storage = await context.storage_state()
        with open(FB_COOKIE_PATH, "w") as f:
            json.dump(storage, f)


def _normalize_fb_url(url: str) -> str:
    url = url.strip()
    if "m.facebook.com" in url:
        url = url.replace("m.facebook.com", "www.facebook.com")
    return url


async def extract_image_urls(post_url: str, max_images: int = 60) -> list:
    """Open the FB post and collect full-resolution image URLs."""
    from playwright.async_api import async_playwright

    post_url = _normalize_fb_url(post_url)
    urls = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        storage_state = FB_COOKIE_PATH if os.path.exists(FB_COOKIE_PATH) else None
        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await _ensure_logged_in(context, page)

            await page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            await _human_delay(2500, 4500)

            # Reject anything that isn't a plain public post/photo view (checkpoint,
            # login wall, or a restricted/private post all redirect or show a wall).
            current_url = page.url
            if "checkpoint" in current_url or "login" in current_url:
                raise FBScrapeError(
                    "❌ Facebook login/checkpoint wall এসেছে — সম্ভবত অ্যাকাউন্ট flagged, বা post টি private/restricted। "
                    "শুধু public post ব্যবহার করুন।"
                )

            # Try opening the first photo to enter the photo-viewer (carousel) —
            # this exposes high-res "scontent" URLs, more reliable than feed thumbnails.
            try:
                photo_link = page.locator("a[href*='/photo/'], a[href*='/photos/']").first
                if await photo_link.count() > 0:
                    await photo_link.click(timeout=5000)
                    await _human_delay(1800, 3000)

                    seen = set()
                    for _ in range(max_images):
                        imgs = await page.locator("img[src*='scontent']").all()
                        for im in imgs:
                            src = await im.get_attribute("src")
                            if src and src not in seen and "scontent" in src:
                                seen.add(src)
                                urls.append(src)
                        # advance carousel
                        next_btn = page.locator("div[aria-label='Next photo']").first
                        if await next_btn.count() == 0:
                            break
                        try:
                            await next_btn.click(timeout=3000)
                            await _human_delay(900, 1800)
                        except Exception:
                            break
                    if urls:
                        await browser.close()
                        return _dedupe_preserve_order(urls)
            except Exception as e:
                logger.info(f"Photo-viewer carousel path failed, falling back to feed scroll: {e}")

            # Fallback: scroll the feed post and collect scontent images directly.
            seen = set()
            for _ in range(8):
                imgs = await page.locator("img[src*='scontent']").all()
                for im in imgs:
                    src = await im.get_attribute("src")
                    if src and src not in seen and "scontent" in src:
                        seen.add(src)
                        urls.append(src)
                await page.mouse.wheel(0, 1500)
                await _human_delay(800, 1600)
                if len(urls) >= max_images:
                    break

        finally:
            await browser.close()

    return _dedupe_preserve_order(urls)[:max_images]


def _dedupe_preserve_order(urls: list) -> list:
    seen = set()
    out = []
    for u in urls:
        key = re.sub(r"[?&](oh|oe)=[^&]+", "", u)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


async def download_images(urls: list) -> list:
    """Download images concurrently; return list of valid JPEG bytes."""
    results = [None] * len(urls)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        async def _fetch(i, u):
            try:
                r = await client.get(u)
                if r.status_code == 200 and r.content:
                    img = Image.open(io.BytesIO(r.content))
                    img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=92)
                    results[i] = buf.getvalue()
            except Exception as e:
                logger.warning(f"Image download failed for {u}: {e}")

        await asyncio.gather(*[_fetch(i, u) for i, u in enumerate(urls)])

    return [b for b in results if b]


def images_to_pdf_bytes(image_bytes_list: list) -> bytes:
    if not image_bytes_list:
        raise FBScrapeError("No images to compile into PDF.")
    return img2pdf.convert(image_bytes_list)


async def fb_post_to_pdf(post_url: str, max_images: int = 60) -> tuple:
    """
    Full pipeline: post URL -> PDF bytes.
    Returns (pdf_bytes, image_count).
    """
    await _enforce_rate_limit()
    urls = await extract_image_urls(post_url, max_images=max_images)
    if not urls:
        raise FBScrapeError("কোনো ছবি পাওয়া যায়নি — post টা public কিনা বা link সঠিক কিনা চেক করুন।")

    img_bytes_list = await download_images(urls)
    if not img_bytes_list:
        raise FBScrapeError("ছবি download করা যায়নি।")

    pdf_bytes = images_to_pdf_bytes(img_bytes_list)
    return pdf_bytes, len(img_bytes_list)
