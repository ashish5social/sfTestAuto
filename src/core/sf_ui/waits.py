"""
Wait helpers — Salesforce Lightning is async-heavy and ships with
multiple competing loading indicators. Every test needs to wait for
spinners, toasts, and "Updating..." messages to settle before it can
proceed. These helpers centralize the wait logic so test files don't
each invent their own version.

Common gotchas the helpers cover:

  - Lightning ``networkidle`` almost never fires because the Lightning
    shell keeps polling for notifications + LDS cache refresh. Capping
    the networkidle wait at 5s prevents 10-second dead time on every
    page load.
  - Multiple spinner selectors coexist (`div.slds-spinner_container`,
    `lightning-spinner`, `vlc-slds-spinner`, `[role='progressbar']`).
    All must be checked before declaring the page settled.
  - Vlocity CCI Configure Cart shows a blue "Updating X" toast that
    transitions to a green "Updated X" toast. Both can render *and
    disappear* fast enough that a naive `wait_for_selector(state=hidden)`
    misses them. Poll the visibility every ~500-1000ms instead.

When something doesn't work
---------------------------
- ``wait_page_ready`` returning too early → increase the ``extra_ms``
  buffer; if the page renders dynamic content via LWC, 4-6s is often
  needed instead of 2s.
- ``wait_spinner`` returning too early → add another selector to
  ``SPINNER_SELECTORS`` and we'll check it everywhere.
- A test failing because a toast "blocked" the next click → call
  ``wait_for_toast(page, text, settled=True)`` so the helper waits for
  the toast to *finish* (i.e. blue→green→gone) instead of just for it
  to appear.
"""

from __future__ import annotations

import re
import time
from typing import Iterable, Pattern, Union

from playwright.sync_api import Page, TimeoutError as PWTimeout


SPINNER_SELECTORS: tuple[str, ...] = (
    "div.slds-spinner_container",
    ".slds-spinner",
    "lightning-spinner",
    ".vlc-slds-spinner",          # Vlocity-specific
    "[role='progressbar']",
)


def wait_spinner(page: Page, timeout: int = 30000) -> None:
    """Wait until every known Salesforce/Vlocity spinner is hidden.

    Iterates through SPINNER_SELECTORS and waits for each to vanish.
    Returns once the page has been spinner-free for one polling pass.
    """
    for sel in SPINNER_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.wait_for(state="hidden", timeout=timeout)
        except PWTimeout:
            # A spinner that never disappears within `timeout` is
            # surfaced by the next step's interaction failing — we
            # don't want to fail HERE on a long-running save.
            pass


def wait_page_ready(page: Page, extra_ms: int = 2000) -> None:
    """Wait for a Salesforce page to fully render.

    Three phases:
      1. ``networkidle`` capped at 5s. Lightning rarely actually idles
         (LDS polling, notifications), so a longer wait is just dead
         time. The 5s cap captures the "page loaded quickly" case.
      2. ``domcontentloaded`` capped at 10s.
      3. ``wait_spinner`` to flush all Lightning + Vlocity spinners.
      4. ``extra_ms`` fixed buffer — covers post-render LWC mount.

    Use ``extra_ms=4000+`` after navigating to a Vlocity flow (catalog,
    cart, configure) because those pages do a second wave of rendering
    after the initial spinner clears.
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


def wait_for_toast(
    page: Page,
    text: Union[str, Pattern[str]],
    *,
    settled: bool = False,
    timeout_ms: int = 15000,
) -> bool:
    """Wait for a toast notification matching ``text`` to appear.

    Parameters
    ----------
    text
        Plain string (case-insensitive substring match) or compiled
        regex. SF/Vlocity toasts include "Saved", "Updated X",
        "Updating X", "Error", etc.
    settled
        If True, ALSO wait for the toast to *disappear* (i.e. for the
        operation it represents to finish). Set this when you need the
        UI to be stable before the next click.
    timeout_ms
        How long to wait for the toast to appear (and, if settled=True,
        another timeout_ms for it to disappear).

    Returns
    -------
    bool
        True if the toast was seen. False if it never appeared within
        ``timeout_ms`` (rare but possible — toasts are fast).
    """
    pattern = text if hasattr(text, "search") else re.compile(re.escape(str(text)), re.I)
    deadline = time.time() + (timeout_ms / 1000.0)
    seen = False
    while time.time() < deadline:
        try:
            t = page.get_by_text(pattern)
            if t.count() > 0 and t.first.is_visible(timeout=500):
                seen = True
                break
        except Exception:
            pass
        page.wait_for_timeout(300)
    if not seen:
        return False
    if not settled:
        return True
    # Wait for the toast to vanish (or its blue→green transition to end).
    settle_deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < settle_deadline:
        try:
            t = page.get_by_text(pattern)
            if t.count() == 0 or not t.first.is_visible(timeout=200):
                return True
        except Exception:
            return True
        page.wait_for_timeout(500)
    return True


def wait_for_config_update_complete(page: Page, timeout_ms: int = 45000) -> None:
    """Wait for a Vlocity Configure Cart update to finish.

    Lifted verbatim from the original TC1 helper. Changing any field on
    the Configure Cart screen (Router Provided By, Bandwidth, etc.)
    kicks off a re-render that shows:
      1. A blue "Updating X" toast.
      2. One or more spinners while the cart recalculates.
      3. A short DOM-stabilize window.

    All three phases need to clear before the next click is safe.
    """
    deadline_ms = page.evaluate("Date.now()") + timeout_ms
    updating = re.compile(r"Updating", re.I)

    # Phase 1: blue "Updating..." toast.
    while page.evaluate("Date.now()") < deadline_ms:
        try:
            t = page.get_by_text(updating)
            if t.count() > 0 and t.first.is_visible(timeout=500):
                page.wait_for_timeout(1000)
                continue
        except Exception:
            pass
        break

    # Phase 2: spinners.
    while page.evaluate("Date.now()") < deadline_ms:
        any_visible = False
        for sel in SPINNER_SELECTORS:
            try:
                for sp in page.query_selector_all(sel):
                    if sp.is_visible():
                        any_visible = True
                        break
            except Exception:
                pass
            if any_visible:
                break
        if not any_visible:
            break
        page.wait_for_timeout(1000)

    # Phase 3: 1.5s DOM settle (LWC rerender).
    page.wait_for_timeout(1500)


def wait_until(
    predicate,
    *,
    timeout_ms: int = 30000,
    poll_ms: int = 500,
    description: str = "condition",
) -> None:
    """Generic poll: keep calling ``predicate()`` until it returns
    truthy, or raise TimeoutError. Useful when no built-in wait covers
    the case (e.g. "wait until the Approval Journey section appears").
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(poll_ms / 1000.0)
    raise TimeoutError(f"wait_until: '{description}' did not become true within {timeout_ms}ms")
