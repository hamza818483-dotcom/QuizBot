"""
fb_export_session.py — run this LOCALLY (not on the server) to manually log
into Facebook in a real visible browser, solve any 2FA/checkpoint yourself,
then export the logged-in session as JSON.

No PC? Use Kiwi Browser (Android) instead of this script:
    1. Install a cookie-export extension in Kiwi (Menu > Extensions >
       from Chrome Web Store): "Cookie-Editor" or "EditThisCookie".
    2. In Kiwi, go to facebook.com and log in manually (solve any
       2FA/checkpoint).
    3. Tap the extension icon > Export (copies cookie JSON, a list like
       [{"name":...,"value":...,"domain":...}, ...]).
    4. Paste that whole JSON as the FB_SESSION_COOKIE env var on the
       server — the bot auto-converts this format, no extra steps needed.

Usage (PC path):
    pip install playwright
    playwright install chromium
    python fb_export_session.py

Steps:
    1. A browser window opens to facebook.com/login.
    2. Log in manually (enter 2FA/checkpoint code if asked).
    3. Once you see your News Feed, come back to this terminal and press Enter.
    4. It prints the session JSON and saves it to fb_session.json.

Then set the FB_SESSION_COOKIE env var on the server (HuggingFace Space) to
the FULL CONTENTS of fb_session.json (as a single-line string), and restart
the bot. It will reuse this session instead of logging in with a password,
so it will never hit the checkpoint wall again (until the session itself
expires, at which point just redo this).
"""
import asyncio
import json
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.facebook.com/login")

        input("\nLog in manually in the opened browser window (solve any "
              "2FA/checkpoint). When you're on your News Feed, press Enter here...")

        storage = await context.storage_state()
        with open("fb_session.json", "w") as f:
            json.dump(storage, f)

        print("\nSaved to fb_session.json")
        print("Copy its full contents into the FB_SESSION_COOKIE env var on the server.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
