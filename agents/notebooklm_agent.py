"""
notebooklm_agent.py

Uses Playwright to:
  1. Restore a saved Google session
  2. Create a new NotebookLM notebook
  3. Paste the source document as a text source
  4. Select Cinematic video style with the papercraft prompt
  5. Enter the Steering Prompt in the customization panel
  6. Click Generate and poll for completion (DOM check + Claude vision fallback)

Outputs result to /tmp/notebooklm_result.json

Usage:
  python agents/notebooklm_agent.py "Depression and Daily Functioning"
"""

import os
import sys
import json
import time
import base64
import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

NOTEBOOKLM_BASE = "https://notebooklm.google.com"
POLL_INTERVAL   = 60     # seconds between completion checks
MAX_WAIT        = 7200   # 2-hour ceiling
VISION_EVERY    = 5      # run vision check every N polls

VISUAL_STYLE_PROMPT = "Use colorful papercraft style and modern scenes. The reading level of the words used in the script should be no higher than 6th grade for a layman to clearly understand."


# ─── Session management ───────────────────────────────────────────────────────

def restore_session(playwright_context, page):
    """Load saved Google cookies and localStorage from the NOTEBOOKLM_SESSION secret."""
    raw = os.environ.get("NOTEBOOKLM_SESSION", "").strip()
    if not raw:
        raise EnvironmentError(
            "NOTEBOOKLM_SESSION is not set. "
            "Run scripts/export_session.py locally to generate it."
        )

    session = json.loads(base64.b64decode(raw).decode())

    # Add cookies to the browser context
    cookies = session.get("cookies", [])
    if cookies:
        playwright_context.add_cookies(cookies)

    # Restore localStorage per origin
    for origin_data in session.get("origins", []):
        origin = origin_data.get("origin", "")
        ls_items = origin_data.get("localStorage", [])
        if origin and ls_items:
            page.goto(origin, wait_until="domcontentloaded", timeout=20000)
            for item in ls_items:
                page.evaluate(
                    "([k, v]) => localStorage.setItem(k, v)",
                    [item["name"], item["value"]]
                )


# ─── Notebook creation ────────────────────────────────────────────────────────

def _try_click(page, selectors: list, timeout: int = 5000) -> bool:
    """Try clicking the first matching selector. Returns True if successful."""
    for sel in selectors:
        try:
            page.click(sel, timeout=timeout)
            return True
        except PWTimeout:
            continue
    return False


def _try_fill(page, selectors: list, text: str) -> bool:
    """Try filling the first matching element. Returns True if successful."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                el.fill(text)
                return True
        except Exception:
            continue
    return False


def create_notebook(page, source_document: str, topic: str) -> str:
    """
    Navigate to NotebookLM, create a new notebook, and paste the source document.
    Returns the notebook URL.
    """
    print("Navigating to NotebookLM...")
    page.goto(NOTEBOOKLM_BASE, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # Click New Notebook
    print("Creating new notebook...")
    clicked = _try_click(page, [
        'button:has-text("New notebook")',
        '[aria-label="New notebook"]',
        'button:has-text("Create notebook")',
        '[data-testid="new-notebook"]',
    ], timeout=8000)

    if not clicked:
        # Try clicking the "+" or main CTA
        _try_click(page, ['[aria-label="Create"]', 'button[type="submit"]'], timeout=5000)

    page.wait_for_timeout(3000)
    notebook_url = page.url
    print(f"Notebook URL: {notebook_url}")

    # Add source document via "Add source" → "Copied text"
    print("Adding source document...")
    _try_click(page, [
        'button:has-text("Add source")',
        '[aria-label="Add source"]',
        'button:has-text("Add")',
        '[data-testid="add-source"]',
    ], timeout=10000)

    page.wait_for_timeout(1000)

    # Choose paste/text option
    _try_click(page, [
        'button:has-text("Paste text")',
        'li:has-text("Paste text")',
        'button:has-text("Copied text")',
        'li:has-text("Copied text")',
        '[aria-label="Paste text"]',
    ], timeout=5000)

    page.wait_for_timeout(1000)

    # Fill in the source text
    filled = _try_fill(page, [
        'textarea[placeholder*="Paste"]',
        'textarea[placeholder*="text"]',
        'div[contenteditable="true"]',
        'textarea',
    ], source_document[:50000])

    if not filled:
        # Clipboard fallback
        page.evaluate(f"navigator.clipboard.writeText({json.dumps(source_document[:50000])})")
        page.keyboard.press("Control+a")
        page.keyboard.press("Control+v")

    page.wait_for_timeout(500)

    # Confirm / Insert
    _try_click(page, [
        'button:has-text("Insert")',
        'button:has-text("Add")',
        'button:has-text("Save")',
        'button:has-text("Done")',
    ], timeout=5000)

    page.wait_for_timeout(4000)
    return notebook_url


# ─── Video configuration ──────────────────────────────────────────────────────

class CinematicUnavailableError(Exception):
    """
    Raised when the Cinematic video style cannot be selected in NotebookLM.
    This happens when the option is absent, disabled, or greyed out —
    typically due to a NotebookLM usage limit or regional restriction.
    The pipeline catches this and routes a specific Slack notification
    rather than treating it as a generic failure.
    """
    pass


def _cinematic_is_selectable(page) -> bool:
    """
    Check whether the Cinematic option exists and is interactive.
    Updated selectors match the current NotebookLM UI where Cinematic
    appears as a card inside the Customize Video Overview panel.
    """
    cinematic_selectors = [
        # Current UI: card with "Cinematic" as a heading inside it
        'div:has(> *:has-text("Cinematic"))',
        'div:has-text("Cinematic"):has-text("immersive")',
        'div:has-text("Cinematic"):has-text("storytelling")',
        # Fallback selectors for older UI variants
        'button:has-text("Cinematic")',
        'input[value="Cinematic"]',
        'label:has-text("Cinematic")',
        '[aria-label="Cinematic"]',
    ]

    for sel in cinematic_selectors:
        try:
            el = page.query_selector(sel)
            if el is None:
                continue

            is_disabled = (
                el.get_attribute("disabled") is not None
                or el.get_attribute("aria-disabled") == "true"
            )
            if is_disabled:
                print(f"  Cinematic element found but marked disabled: {sel}")
                return False

            class_attr = (el.get_attribute("class") or "").lower()
            unavailable_indicators = ["disabled", "unavailable", "locked", "inactive", "grayed", "greyed"]
            if any(ind in class_attr for ind in unavailable_indicators):
                print(f"  Cinematic element found but appears unavailable: {sel}")
                return False

            return True

        except Exception:
            continue

    print("  Cinematic option not found in the Video Overview panel.")
    return False


def configure_video(page, steering_prompt: str):
    """
    Open Video Overview, verify Cinematic is selectable, select it,
    enter the visual style prompt and steering prompt, then click Generate.

    Raises CinematicUnavailableError if the Cinematic option is absent,
    disabled, or greyed out so the caller can route a specific Slack alert.
    """
    print("Opening Video Overview panel...")
    _try_click(page, [
        # Current UI label
        'button:has-text("Video Overview")',
        'button:has-text("Video overview")',
        # Fallbacks
        'button:has-text("Video")',
        '[aria-label="Video overview"]',
        '[data-testid="video-tab"]',
    ], timeout=15000)

    page.wait_for_timeout(3000)

    # Check Cinematic availability before attempting selection
    print("Checking Cinematic availability...")
    if not _cinematic_is_selectable(page):
        raise CinematicUnavailableError(
            "Cinematic videos are not available at this time."
        )

    # Select Cinematic style — current UI uses clickable cards
    print("Selecting Cinematic style...")
    _try_click(page, [
        # Current UI: card containing "Cinematic" heading
        'div:has(> *:has-text("Cinematic"))',
        'div:has-text("Cinematic"):has-text("immersive")',
        'div:has-text("Cinematic"):has-text("storytelling")',
        # Fallbacks
        'button:has-text("Cinematic")',
        'input[value="Cinematic"]',
        'label:has-text("Cinematic")',
        '[aria-label="Cinematic"]',
    ], timeout=8000)

    page.wait_for_timeout(1000)

    # Enter customization text — current UI shows one textarea labeled
    # "How would you like the video to be customized?"
    # We combine visual style prompt + steering prompt into this single field.
    print("Entering customization prompt...")
    combined_prompt = VISUAL_STYLE_PROMPT + "\n\n" + steering_prompt[:800]

    custom_filled = _try_fill(page, [
        # Current UI placeholder text
        'textarea[placeholder*="customized"]',
        'textarea[placeholder*="customize"]',
        'textarea[placeholder*="Compare"]',   # placeholder example text
        # Fallbacks
        'textarea[placeholder*="instruct"]',
        'textarea[placeholder*="steering"]',
        'textarea[placeholder*="prompt"]',
        'textarea[placeholder*="topic"]',
        'input[placeholder*="style"]',
        'textarea[placeholder*="style"]',
    ], combined_prompt)

    if not custom_filled:
        print("WARNING: Could not locate customization input. Proceeding without it.")

    page.wait_for_timeout(500)

    # Click Generate
    print("Clicking Generate...")
    _try_click(page, [
        'button:has-text("Generate")',
        'button:has-text("Create video")',
        'button:has-text("Generate video")',
        '[aria-label="Generate video"]',
        'button[type="submit"]:has-text("Generate")',
    ], timeout=8000)

    page.wait_for_timeout(2000)
    print("Video generation initiated.")


# ─── Completion polling ───────────────────────────────────────────────────────

def check_dom_complete(page) -> bool:
    """Fast check: look for DOM elements that appear when a video is ready."""
    selectors = [
        '[aria-label="Download video"]',
        'button:has-text("Download")',
        'video[src]',
        '[data-testid="video-ready"]',
        'button:has-text("View video")',
        '.video-player video',
        'button:has-text("Download video")',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def check_vision_complete(page, client: anthropic.Anthropic) -> bool:
    """Claude vision fallback: screenshot the page and ask if the video is ready."""
    try:
        screenshot_b64 = base64.b64encode(page.screenshot(full_page=False)).decode()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a screenshot of NotebookLM. "
                            "Has the video finished generating? "
                            "Look for: a playable video player, a Download button, "
                            "a video thumbnail, or text saying the video is ready. "
                            "Reply with exactly one word: READY or WAITING."
                        )
                    }
                ]
            }]
        )
        return "READY" in response.content[0].text.upper()
    except Exception as e:
        print(f"Vision check error (non-fatal): {e}")
        return False


def poll_until_complete(page, client: anthropic.Anthropic) -> bool:
    """
    Poll the NotebookLM page until the video is ready or the 2-hour ceiling is hit.
    Refreshes the page every 10 minutes to get the latest state.
    """
    elapsed = 0
    poll_count = 0

    while elapsed < MAX_WAIT:
        mins = elapsed // 60
        print(f"  [{mins}m] Checking for video completion...")

        if check_dom_complete(page):
            print(f"  Completion detected via DOM at {mins}m.")
            return True

        poll_count += 1
        if poll_count % VISION_EVERY == 0:
            print(f"  Running vision check...")
            if check_vision_complete(page, client):
                print(f"  Completion detected via vision at {mins}m.")
                return True

        # Soft page refresh every 10 minutes
        if elapsed > 0 and elapsed % 600 == 0:
            print("  Refreshing page to get latest state...")
            try:
                page.reload(wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"  Refresh failed (non-fatal): {e}")

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    print(f"Polling timed out after {MAX_WAIT // 60} minutes.")
    return False


def get_video_url(page) -> str:
    """Try to extract a direct video URL; fall back to the notebook URL."""
    try:
        el = page.query_selector("video[src]")
        if el:
            src = el.get_attribute("src")
            if src:
                return src
    except Exception:
        pass
    return page.url


# ─── Main entry point ─────────────────────────────────────────────────────────

def run(topic: str, journey_id: str = "", batch_num: str = "") -> dict:
    with open("/tmp/source_document.txt") as f:
        source_document = f.read()

    with open("/tmp/steering_prompt.txt") as f:
        steering_prompt = f.read()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        try:
            print("Restoring Google session...")
            restore_session(ctx, page)

            notebook_url = create_notebook(page, source_document, topic)
            configure_video(page, steering_prompt)

            print(f"Polling for completion (interval: {POLL_INTERVAL}s, max: {MAX_WAIT // 60}m)...")
            completed = poll_until_complete(page, client)

            if completed:
                video_url = get_video_url(page)
                result = {
                    "success": True,
                    "topic": topic,
                    "notebook_url": notebook_url,
                    "video_url": video_url,
                    "journey_id": journey_id,
                    "batch_num": batch_num
                }
            else:
                result = {
                    "success": False,
                    "topic": topic,
                    "notebook_url": notebook_url,
                    "video_url": "",
                    "journey_id": journey_id,
                    "batch_num": batch_num,
                    "error": "Video generation timed out after 2 hours."
                }

        except CinematicUnavailableError as e:
            # Cinematic was absent or disabled in the Video Overview panel.
            # This gets its own error_code so slack_notifier.py can post
            # the specific "not available" message rather than a generic failure.
            print(f"Cinematic unavailable: {e}")
            result = {
                "success": False,
                "error_code": "cinematic_unavailable",
                "topic": topic,
                "notebook_url": page.url if page else "",
                "video_url": "",
                "journey_id": journey_id,
                "batch_num": batch_num,
                "error": str(e)
            }

        except Exception as e:
            result = {
                "success": False,
                "topic": topic,
                "notebook_url": page.url if page else "",
                "video_url": "",
                "journey_id": journey_id,
                "batch_num": batch_num,
                "error": str(e)
            }

        finally:
            browser.close()

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agents/notebooklm_agent.py <topic> [journey_id] [batch_num]")
        sys.exit(1)

    topic      = sys.argv[1]
    journey_id = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("JOURNEY_ID", "")
    batch_num  = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("BATCH_NUM", "")

    result = run(topic, journey_id, batch_num)

    with open("/tmp/notebooklm_result.json", "w") as f:
        json.dump(result, f, indent=2)

    if result["success"]:
        print(f"Success: notebook at {result['notebook_url']}")
    else:
        print(f"Failed: {result.get('error')}")
        sys.exit(1)
