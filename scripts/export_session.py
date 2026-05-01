"""
export_session.py

Opens a fresh Chromium browser window so you can log in to Google
manually (email + password + 2FA), then captures the session for use
as the NOTEBOOKLM_SESSION GitHub Secret.

No existing Chrome profile is needed. Chrome does not need to be closed.

Requirements:
  python3 -m pip install playwright
  python3 -m playwright install chromium

Usage:
  python3 scripts/export_session.py
"""

import json
import base64
import os
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright not found.")
    print("Run: python3 -m pip install playwright && python3 -m playwright install chromium")
    sys.exit(1)

NOTEBOOKLM_URL = "https://notebooklm.google.com"
OUTPUT_FILE    = Path(__file__).parent.parent / "notebooklm_session.txt"


def main():
    print("=" * 60)
    print("NotebookLM Session Export")
    print("=" * 60)
    print()
    print("A browser window will open.")
    print("Log in with your Google email and password.")
    print("Passkeys will not be available — use your password instead.")
    print()
    print("If prompted for 2FA, complete it in the browser window.")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()

        print("Opening Google sign-in...")
        page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded")

        print()
        print("-" * 60)
        print("BROWSER IS OPEN.")
        print()
        print("1. Enter your email: matthew@neuroflow.com")
        print("2. Enter your password")
        print("3. Complete 2FA if prompted")
        print("-" * 60)
        print()
        print("Once you are fully logged in to Google,")
        input("press ENTER here to continue...")

        print()
        print("Navigating to NotebookLM...")
        page.goto(NOTEBOOKLM_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        current = page.url
        print(f"Current URL: {current}")

        if "notebooklm.google.com" not in current:
            print()
            print("WARNING: Did not land on NotebookLM.")
            print("You may need to complete the login in the browser.")
            input("Once NotebookLM is visible in the browser, press ENTER...")

        print()
        print("Capturing session cookies and storage...")

        cookies  = context.cookies()
        ls_raw   = page.evaluate("() => Object.entries(localStorage)")
        ls_items = [{"name": k, "value": v} for k, v in ls_raw]

        session_data = {
            "cookies": cookies,
            "origins": [{
                "origin":       NOTEBOOKLM_URL,
                "localStorage": ls_items
            }]
        }

        encoded = base64.b64encode(json.dumps(session_data).encode()).decode()
        OUTPUT_FILE.write_text(encoded)

        browser.close()

    print()
    print("=" * 60)
    print(f"Session saved to:")
    print(f"  {OUTPUT_FILE}")
    print()
    print("Next steps:")
    print("  1. Open the file in TextEdit and copy ALL the text.")
    print("  2. Go to GitHub Secrets:")
    print("     https://github.com/mmiclette/content-automation/settings/secrets/actions")
    print("  3. Add secret named: NOTEBOOKLM_SESSION")
    print("  4. Paste as the value and save.")
    print("  5. Delete the local file immediately after:")
    print(f"     rm '{OUTPUT_FILE}'")
    print()
    print("WARNING: Delete the file right after saving to GitHub.")
    print("It contains your Google session token.")
    print("=" * 60)

    # Open Finder to the file location
    os.system(f"open -R '{OUTPUT_FILE}'")


if __name__ == "__main__":
    main()
