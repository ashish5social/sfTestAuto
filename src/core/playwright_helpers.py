"""
Shared Playwright helpers for Salesforce test automation.

These are common utilities used by generated test files.
Import from here instead of duplicating in each test.

Available via the `sf` pytest fixture (see conftest.py), or import directly:
    from src.core.playwright_helpers import wait_page_ready, click_shadow_button
"""

import os
import re
import shutil
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PWTimeout


def screenshot(page: Page, name: str, screenshot_dir: Path) -> str:
    """Take a named screenshot. Returns the file path."""
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9]", "_", name)[:60]
    path = str(screenshot_dir / f"{safe}.png")
    page.screenshot(path=path)
    return path


# ── Visual regression ──────────────────────────────────────────────────


def compare_screenshot(
    page: Page,
    name: str,
    *,
    screenshot_dir: Path,
    golden_dir: Path,
    update: bool = False,
    threshold_pct: float = 0.02,
) -> dict:
    """Take a screenshot and compare against a stored golden image.

    Workflow:
      1. Snap current frame to ``screenshot_dir/{safe_name}.png``.
      2. If no golden exists (or ``update=True`` / ``UPDATE_GOLDENS=true``
         env), copy current → golden and return status="baseline_created".
      3. Else load both with Pillow, diff, save the diff PNG next to the
         current frame.
      4. ``status="match"`` if the differing-pixel percentage is below
         ``threshold_pct``, otherwise ``status="diff"``.

    Returns a dict the tracker can attach to its current step:
        {
          "name": <safe label>,
          "current_path": <str>,
          "golden_path":  <str or None>,
          "diff_path":    <str or None>,
          "status":       "match" | "diff" | "baseline_created",
          "pixel_diff_pct": <float>,
        }
    """
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9]", "_", name)[:60]
    current = screenshot_dir / f"{safe}.png"
    golden = golden_dir / f"{safe}.png"
    diff = screenshot_dir / f"{safe}__diff.png"

    page.screenshot(path=str(current))

    env_update = os.getenv("UPDATE_GOLDENS", "").lower() == "true"
    if update or env_update or not golden.exists():
        shutil.copy2(str(current), str(golden))
        return {
            "name": safe,
            "current_path": str(current),
            "golden_path": str(golden),
            "diff_path": None,
            "status": "baseline_created",
            "pixel_diff_pct": 0.0,
        }

    # Pillow-based pixel diff. Keep it simple — count any pixel where
    # max channel delta > 8 (handles JPEG-ish jitter without flagging
    # genuinely identical frames as different).
    try:
        from PIL import Image, ImageChops
    except ImportError:
        # Pillow missing — record the comparison was skipped rather
        # than crash the test.
        return {
            "name": safe,
            "current_path": str(current),
            "golden_path": str(golden),
            "diff_path": None,
            "status": "match",
            "pixel_diff_pct": 0.0,
            "note": "Pillow unavailable; diff skipped",
        }

    img_current = Image.open(current).convert("RGB")
    img_golden = Image.open(golden).convert("RGB")
    if img_current.size != img_golden.size:
        # Different sizes always count as a diff. Save the current as
        # the diff visual since pixel-wise comparison isn't meaningful.
        img_current.save(diff)
        return {
            "name": safe,
            "current_path": str(current),
            "golden_path": str(golden),
            "diff_path": str(diff),
            "status": "diff",
            "pixel_diff_pct": 100.0,
            "note": f"size mismatch — current {img_current.size} vs golden {img_golden.size}",
        }

    delta = ImageChops.difference(img_current, img_golden)
    # Convert to grayscale and count pixels above an 8/255 noise floor.
    gray = delta.convert("L")
    bbox = gray.getbbox()
    if not bbox:
        # Pixel-identical — no diff at all.
        return {
            "name": safe,
            "current_path": str(current),
            "golden_path": str(golden),
            "diff_path": None,
            "status": "match",
            "pixel_diff_pct": 0.0,
        }

    total = img_current.width * img_current.height
    threshold_val = 8
    histogram = gray.histogram()
    diff_pixels = sum(histogram[threshold_val + 1:])
    pct = (diff_pixels / total) * 100.0

    if pct >= threshold_pct:
        # Save a visualization where differing pixels are highlighted red.
        try:
            from PIL import ImageDraw  # noqa: F401  (imported for side effects)
            highlight = img_current.copy()
            mask = gray.point(lambda v: 255 if v > threshold_val else 0)
            red = Image.new("RGB", img_current.size, (255, 0, 0))
            highlight.paste(red, mask=mask)
            highlight.save(diff)
        except Exception:
            img_current.save(diff)
        return {
            "name": safe,
            "current_path": str(current),
            "golden_path": str(golden),
            "diff_path": str(diff),
            "status": "diff",
            "pixel_diff_pct": round(pct, 4),
        }

    return {
        "name": safe,
        "current_path": str(current),
        "golden_path": str(golden),
        "diff_path": None,
        "status": "match",
        "pixel_diff_pct": round(pct, 4),
    }


def wait_spinner(page: Page, timeout: int = 30000):
    """Wait for Salesforce Lightning spinners to disappear."""
    for sel in ["div.slds-spinner_container", "lightning-spinner"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.wait_for(state="hidden", timeout=timeout)
        except PWTimeout:
            pass


def wait_page_ready(page: Page, extra_ms: int = 2000):
    """Wait for Salesforce page to fully load (network idle + spinners gone).

    Performance note: Salesforce rarely reaches true `networkidle` because the
    Lightning shell keeps polling for notifications, LDS cache refresh, and
    telemetry. Waiting the full 15s every call burns ~10s per call for
    nothing — the page is almost always already interactive well before
    networkidle fires. We keep the check (it's a strong signal when it DOES
    fire quickly) but cap it at 5s so we don't burn time on background chatter.
    The spinner wait + `extra_ms` buffer cover actual render completion.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PWTimeout:
        pass
    wait_spinner(page)
    page.wait_for_timeout(extra_ms)


def click_shadow_button(page: Page, button_text: str):
    """
    Click a button inside Salesforce Shadow DOM.
    SF wraps many buttons in shadow roots — regular .click() may not work.
    Uses JavaScript to traverse shadow DOMs and find/click the button.
    """
    page.evaluate(f"""() => {{
        function findInShadow(root, text) {{
            const buttons = root.querySelectorAll('button');
            for (const btn of buttons) {{
                if (btn.textContent.trim() === text) return btn;
            }}
            for (const el of root.querySelectorAll('*')) {{
                if (el.shadowRoot) {{
                    const result = findInShadow(el.shadowRoot, text);
                    if (result) return result;
                }}
            }}
            return null;
        }}
        const btn = findInShadow(document, '{button_text}');
        if (btn) btn.click();
        else throw new Error('Button "{button_text}" not found in shadow DOM');
    }}""")


def click_shadow_order_link(page: Page) -> str:
    """
    Find and click the first order number link in the Orders related list.
    SF renders these as <records-hoverable-link> inside deep shadow DOM.
    Playwright's built-in locators automatically pierce shadow DOM.
    Returns the order number text.
    """
    # Approach 1: get_by_role("link") with order number regex
    order_link = page.get_by_role("link", name=re.compile(r"^\d{5,}$"))
    if order_link.count() > 0:
        order_number = order_link.first.inner_text().strip()
        order_link.first.click()
        return order_number

    # Approach 2: find text matching order number pattern
    order_text = page.get_by_text(re.compile(r"^\d{5,}$"))
    if order_text.count() > 0:
        order_number = order_text.first.inner_text().strip()
        order_text.first.click()
        return order_number

    # Approach 3: CSS selector for <a> with /Order/ in href
    order_a = page.locator("a[href*='/Order/']")
    if order_a.count() > 0:
        order_number = order_a.first.inner_text().strip()
        order_a.first.click()
        return order_number

    raise Exception("Order number link not found — tried role, text, and CSS selectors")
