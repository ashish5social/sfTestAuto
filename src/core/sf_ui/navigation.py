"""
Navigation helpers — get the browser to the right place.

Salesforce URLs are predictable, so most "navigate to X" operations are
just well-formed page.goto() calls. This module centralizes those URL
patterns so tests don't hardcode them and so a structural change in
Salesforce (e.g. /lightning/o/ → /lightning/r/) only needs to be fixed
in one place.

When this doesn't work
----------------------
- ``open_list_view`` returning a blank page → the org may not have the
  ``__Recent`` filter for that object. Pass ``filter_name="All"``
  or omit it and SF will fall back to the default list view.
- ``open_record`` URL 404 → the record id is wrong or belongs to a
  different sObject than expected. Salesforce only checks the prefix
  (first 3 chars of the id) — anything else 404s.
"""

from __future__ import annotations

from typing import Optional

from playwright.sync_api import Page

from src.core.sf_ui.waits import wait_page_ready


def _instance_root(page: Page) -> str:
    """Derive the Salesforce instance root (e.g.
    https://fidium--apitest1.sandbox.my.salesforce.com) from the
    browser's current URL. Splits before '/lightning' so we keep the
    org host regardless of which page we're on."""
    return page.url.split("/lightning")[0]


def open_list_view(
    page: Page,
    sobject: str,
    *,
    filter_name: str = "__Recent",
    extra_ms: int = 5000,
) -> None:
    """Navigate to a standard sObject list view.

    e.g. ``open_list_view(page, "Account")`` →
    ``/lightning/o/Account/list?filterName=__Recent``.

    Pass ``filter_name="All"`` for the global all-records list. Pass
    a custom list view name to open that specific view.
    """
    base = _instance_root(page)
    page.goto(f"{base}/lightning/o/{sobject}/list?filterName={filter_name}")
    wait_page_ready(page, extra_ms=extra_ms)


def open_record(
    page: Page,
    sobject: str,
    record_id: str,
    *,
    view: str = "view",
    extra_ms: int = 4000,
) -> None:
    """Navigate to a specific sObject record's view page.

    e.g. ``open_record(page, "Account", "001xx0000012345")`` →
    ``/lightning/r/Account/001xx0000012345/view``.

    ``view`` defaults to "view"; pass "edit" for the edit screen.
    """
    base = _instance_root(page)
    page.goto(f"{base}/lightning/r/{sobject}/{record_id}/{view}")
    wait_page_ready(page, extra_ms=extra_ms)


def open_setup(page: Page, setup_path: str = "SetupOneHome/home", extra_ms: int = 4000) -> None:
    """Navigate to a Setup page. ``setup_path`` is the trailing
    segment after /lightning/setup/ (e.g. "ObjectManager/home",
    "ManageUsers/home")."""
    base = _instance_root(page)
    page.goto(f"{base}/lightning/setup/{setup_path}")
    wait_page_ready(page, extra_ms=extra_ms)


def extract_record_id_from_url(url: str, sobject: Optional[str] = None) -> Optional[str]:
    """Pull a 15- or 18-char Salesforce record id out of a Lightning URL.

    Recognizes patterns like:
      .../lightning/r/Account/001xx0000012345AAA/view
      .../lightning/r/Quote/0Q0xx0000012345/edit?source=...

    Pass ``sobject="Account"`` to be strict — None matches any.
    """
    import re

    pattern = (
        rf"/lightning/r/{re.escape(sobject)}/([a-zA-Z0-9]{{15,18}})"
        if sobject
        else r"/lightning/r/[A-Za-z_]+/([a-zA-Z0-9]{15,18})"
    )
    m = re.search(pattern, url or "")
    return m.group(1) if m else None
