"""
Click / interaction helpers — make the right thing happen no matter
which DOM dialect Salesforce decided to render this widget in.

Salesforce mixes at least four button rendering styles:
  - Plain HTML <button> (rare in Lightning, common in some Aura dialogs)
  - <a role="button"> (Aura picklists, list view headers)
  - <lightning-button> custom element
  - <one-record-action-link> for related-list actions
…and any of them can be wrapped in one or more Shadow Roots.

The helpers in this module walk every shadow root + every styling
variant before giving up. Test files should ALWAYS call them rather
than crafting `.locator(...)` selectors directly, because the moment
SF changes a class name or restructures a component, every test that
hardcoded a selector breaks.

When this doesn't work
----------------------
- ``click_button`` returns False → the visible text doesn't match what
  you passed. Common causes: trailing space, en-dash vs hyphen, case
  variant, button labeled "Save & New" vs "Save and New" (`&` vs
  "and"). Use `re.compile(...)` and pass it directly if you need
  flexibility, or inspect the actual button text via
  ``page.get_by_role("button").all_text_contents()`` from a debug pause.
- The click "succeeds" but nothing happens → the button is disabled or
  covered by a toast. Call ``wait_for_toast(..., settled=True)`` first
  to flush any in-flight toast, then retry.
"""

from __future__ import annotations

import json
import re
from typing import Pattern, Union

from playwright.sync_api import Page


def click_button(page: Page, name: Union[str, Pattern[str]], timeout_ms: int = 10000) -> bool:
    """Click a button / link / Aura action whose visible text matches
    ``name``. Returns True on success, False if nothing matched.

    Strategies tried in order:
      1. Playwright role=button exact match
      2. Playwright role=button case-insensitive partial match
      3. Playwright role=link case-insensitive partial match
      4. Shadow-DOM JS walk: button, a[role=button], [role=button],
         lightning-button, one-record-action-link, a.listItemLink.

    Use a regex for ``name`` when you need flexibility (e.g.
    ``re.compile(r"Save( & New)?", re.I)``).
    """
    name_re = (
        name if hasattr(name, "search")
        else re.compile(re.escape(str(name)), re.I)
    )

    # Strategy 1-3: Playwright role-based locators
    for strategy in (
        lambda: page.get_by_role("button", name=name, exact=True) if isinstance(name, str) else None,
        lambda: page.get_by_role("button", name=name_re),
        lambda: page.get_by_role("link", name=name_re),
    ):
        try:
            loc = strategy()
            if loc is None or loc.count() == 0:
                continue
            if loc.first.is_visible():
                loc.first.click(timeout=timeout_ms)
                return True
        except Exception:
            continue

    # Strategy 4: Shadow DOM JS walk. Recursive scan through every
    # shadowRoot looking for a clickable whose text/title/aria-label
    # matches. SF Lightning encapsulates a lot of buttons this way.
    name_str = name.pattern if hasattr(name, "pattern") else str(name)
    clicked = page.evaluate(
        f"""(() => {{
            function findInShadow(root, depth) {{
                if (depth > 25) return null;
                const candidates = root.querySelectorAll(
                    'button, a[role="button"], [role="button"], '
                    + 'lightning-button, one-record-action-link, a.listItemLink'
                );
                const target = {json.dumps(name_str.lower())};
                for (const c of candidates) {{
                    const txt = (c.textContent || c.getAttribute('title') ||
                                 c.getAttribute('aria-label') || '').trim();
                    if (txt && txt.toLowerCase().includes(target)) {{
                        c.click();
                        return true;
                    }}
                }}
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        if (findInShadow(el.shadowRoot, depth + 1)) return true;
                    }}
                }}
                return false;
            }}
            return findInShadow(document, 0);
        }})()"""
    )
    return bool(clicked)


def click_shadow_button(page: Page, button_text: str) -> None:
    """Stricter variant of ``click_button`` that ONLY looks for plain
    <button> elements via JS shadow-walk with exact text match.

    Use this when ``click_button`` matches too broadly (e.g. when
    there are multiple "Save" buttons and only the inner shadow-DOM
    one is the right target). Throws if not found — call this
    deliberately."""
    page.evaluate(
        f"""() => {{
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
            const btn = findInShadow(document, {json.dumps(button_text)});
            if (btn) btn.click();
            else throw new Error('Button "' + {json.dumps(button_text)} + '" not found in shadow DOM');
        }}"""
    )


def click_shadow_order_link(page: Page) -> str:
    """Click the first order number link in an Orders related list.

    Salesforce renders these as <records-hoverable-link> inside deep
    shadow DOM. Playwright's built-in locators auto-pierce shadow DOM
    so we try three approaches in order of robustness.

    Returns the order number text (so the caller can record it).
    """
    # Approach 1: role=link with 5+ digit number
    order_link = page.get_by_role("link", name=re.compile(r"^\d{5,}$"))
    if order_link.count() > 0:
        order_number = order_link.first.inner_text().strip()
        order_link.first.click()
        return order_number

    # Approach 2: free text matching the same pattern
    order_text = page.get_by_text(re.compile(r"^\d{5,}$"))
    if order_text.count() > 0:
        order_number = order_text.first.inner_text().strip()
        order_text.first.click()
        return order_number

    # Approach 3: any <a> with /Order/ in the href
    order_a = page.locator("a[href*='/Order/']")
    if order_a.count() > 0:
        order_number = order_a.first.inner_text().strip()
        order_a.first.click()
        return order_number

    raise Exception(
        "Order number link not found — tried role, text, and CSS selectors"
    )
