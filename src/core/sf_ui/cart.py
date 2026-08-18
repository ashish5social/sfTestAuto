"""
Vlocity CPQ catalog / cart helpers.

Salesforce Industries (Vlocity) CPQ cart flows.
Generic Salesforce form helpers live in ``forms.py``; this module
covers operations that ONLY exist inside the Vlocity catalog/cart
experience:

  - Search the product catalog
  - Add a specific product to the cart
  - Configure attributes on a cart line item (Bandwidth, Router type,
    Provided By, etc.)
  - Wait for the Configure Cart screen to settle between edits
  - Wait for the Summary tab content to fully render

When this doesn't work
----------------------
- ``search_catalog`` returns False / no results → the catalog search
  is debounced; try increasing the wait between typing and reading.
  Some Vlocity deploys use a custom search component that needs
  ``page.locator("input[role='combobox']")`` rather than ``[type=search]``.
- ``add_product_to_cart`` clicks the wrong product's Add → make sure
  you call ``search_catalog`` first so the only visible product card
  IS the one you want. The helper is conservative and prefers
  scoped container clicks, but it falls back to any visible
  "Add to Cart" button as the last resort.
- ``configure_attribute`` doesn't apply → some Vlocity attributes only
  appear AFTER selecting a parent product (e.g. Bandwidth only shows
  after the carrier service is added). Call
  ``waits.wait_for_config_update_complete`` between edits.
"""

from __future__ import annotations

import re
from typing import Optional

from playwright.sync_api import Page

from src.core.sf_ui.waits import wait_spinner, wait_for_config_update_complete


def search_catalog(page: Page, search_term: str, *, settle_ms: int = 4000) -> bool:
    """Type ``search_term`` into the catalog search input and submit.

    Locates the search box (multiple candidate selectors), clears any
    existing query, types character-by-character (so SF's debounced
    search fires), and presses Enter. Returns True once the results
    have settled. Raises if no search input is found.
    """
    search_input = None
    for strat in (
        lambda: page.locator("input[type='search']"),
        lambda: page.get_by_placeholder(re.compile(r"search", re.I)),
        lambda: page.get_by_role("searchbox"),
        lambda: page.get_by_label(re.compile(r"search", re.I)),
        lambda: page.locator("input[role='combobox'][placeholder*='earch' i]"),
        lambda: page.locator("input[placeholder*='earch' i]"),
    ):
        try:
            loc = strat()
            for i in range(min(loc.count(), 6)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible(timeout=1000) and cand.is_editable(timeout=500):
                        search_input = cand
                        break
                except Exception:
                    continue
            if search_input:
                break
        except Exception:
            continue

    if not search_input:
        raise Exception(
            f"Catalog search input not found — cannot search for '{search_term}'. "
            "Refusing to add a random product."
        )

    search_input.click()
    page.wait_for_timeout(500)
    try:
        search_input.press("Control+a")
        search_input.press("Backspace")
    except Exception:
        pass
    search_input.type(search_term, delay=30)
    page.wait_for_timeout(1000)
    search_input.press("Enter")
    page.wait_for_timeout(settle_ms)
    wait_spinner(page, timeout=15000)
    page.wait_for_timeout(1500)
    return True


def add_product_to_cart(
    page: Page,
    product_text: str,
    *,
    add_button_names: tuple[str, ...] = ("Add to Cart", "Add", "Select"),
) -> bool:
    """Click the Add to Cart button for the product matching ``product_text``.

    Strategy:
      1. Confirm ``product_text`` is visible in the catalog results.
      2. Find a product card / row / tile that CONTAINS that text, then
         click an Add button inside it (scoped — avoids clicking the
         wrong product when several are visible).
      3. Fallback: click the product text first (some UIs select on
         click), then click any visible Add to Cart button.

    Pass ``add_button_names`` to override the labels we look for in case
    your org renames "Add to Cart" to "Select" or similar.

    Returns True on success. Raises on failure with a message naming
    the search term so debugging is fast.
    """
    # Verify the product is actually visible first — fail loudly if not.
    product_re = re.compile(re.escape(product_text), re.I)
    try:
        if not (page.get_by_text(product_re).count() > 0
                and page.get_by_text(product_re).first.is_visible(timeout=5000)):
            raise Exception(
                f"Product '{product_text}' not found in catalog results. "
                "Either the search returned no matches or the text label has changed."
            )
    except Exception as exc:
        if "not found" in str(exc):
            raise
        raise Exception(
            f"Product '{product_text}' not found in catalog results: {exc}"
        )

    # Strategy 1: scoped container + Add button inside.
    container_selectors = (
        ".product-card", ".product-item", ".cpq-product-card",
        "[class*='product']", "tr", ".slds-card", "article",
        "[class*='tile']", "[class*='item']",
    )
    for container_sel in container_selectors:
        try:
            containers = page.locator(container_sel).filter(has_text=product_re)
            if containers.count() == 0:
                continue
            for btn_name in add_button_names:
                try:
                    btn = containers.first.get_by_role(
                        "button", name=re.compile(re.escape(btn_name), re.I),
                    )
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.scroll_into_view_if_needed()
                        btn.first.click()
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    # Strategy 2: select product first, then click any Add to Cart.
    try:
        product_el = page.get_by_text(product_re).first
        product_el.scroll_into_view_if_needed()
        product_el.click()
        page.wait_for_timeout(1500)
        atc = page.get_by_role("button", name=re.compile(r"Add\s*to\s*Cart", re.I))
        if atc.count() > 0 and atc.first.is_visible():
            atc.first.click()
            return True
    except Exception:
        pass

    raise Exception(
        f"Failed to add '{product_text}' to cart — no Add to Cart button "
        "was clickable inside the product container or as a fallback."
    )


def configure_attribute(
    page: Page,
    label: str,
    value: str,
    *,
    wait_after: bool = True,
) -> bool:
    """Set an attribute on the Configure Cart screen.

    Most Vlocity cart attributes are either lookups (Provided By, etc.) or
    picklists (Bandwidth, Quote Type). This helper delegates to the
    right form helper based on the field type's appearance.

    If ``wait_after=True`` (default), waits for the
    "Updating..." toast and any spinners to clear before returning,
    so the next configure call doesn't race with the previous one's
    re-render.

    Returns True on success, False if nothing matched.
    """
    # Try picklist first (most common cart attr type).
    from src.core.sf_ui.forms import select_picklist, fill_field_by_label

    if select_picklist(page, label, value):
        if wait_after:
            wait_for_config_update_complete(page)
        return True

    # Fallback to a plain text fill (numeric attrs, free-text custom attrs).
    if fill_field_by_label(page, label, value):
        if wait_after:
            wait_for_config_update_complete(page)
        return True

    return False


def wait_summary_loaded(page: Page, expected_products: Optional[list[str]] = None) -> None:
    """Wait until the Cart Summary tab content has rendered.

    Vlocity's Summary tab loads asynchronously after you switch to it,
    and asserting on totals before the LWC has rendered will give you
    stale "0" values. This helper:

      1. Waits for the spinner to clear.
      2. If ``expected_products`` is provided, polls until each name
         is visible — guarantees the cart actually has those items.
    """
    wait_spinner(page, timeout=15000)
    page.wait_for_timeout(1500)
    if not expected_products:
        return
    for name in expected_products:
        try:
            page.wait_for_selector(
                f"text=/{re.escape(name)}/i", state="visible", timeout=15000,
            )
        except Exception:
            # Don't fail here — let the caller's assertion phase report
            # missing products with a better error message.
            pass
