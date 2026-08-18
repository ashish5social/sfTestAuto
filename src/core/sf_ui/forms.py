"""
Form helpers — fill, select, lookup, save, all driven by visible
*label* (not selector).

The library treats labels as the source of truth. Fill a field by
saying "Fill 'Customer's Legal Name' with X", not by hunting for the
right `<input id='abc-12345-...'>` selector. Salesforce changes the
generated ids on every metadata deploy; labels are stable.

This module covers the field types CCI tests touch:

  - Plain text / textarea         → fill_field_by_label
  - Native <select>               → select_native
  - Aura "anchor" picklist        → select_picklist (handles "Stage")
  - Lightning combobox            → select_picklist (same helper)
  - Lookup (autocomplete + dialog)→ fill_lookup
  - Record type radio             → select_record_type
  - Date input                    → fill_date_field

When this doesn't work
----------------------
- "Field not found by label" → the label is rendered differently than
  you typed (e.g. the form uses "Customer Legal Name" not "Customer's
  Legal Name"). Tail the actual `<label>` text from the DOM by hitting
  the field with the browser inspector.
- "Lookup returned 0 results" → the search value isn't an exact match,
  or duplicate records share the prefix. Pass a fuller value (the
  helper does exact-match scanning of the dropdown).
- "Picklist value not found" → SF lazily renders some options on first
  open. The helper already retries the popup open + 700ms settle —
  if you still see this, increase ``timeout_ms``.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from playwright.sync_api import Page


# ── Simple fields ──────────────────────────────────────────────────────


def fill_field_by_label(page: Page, label: str, value: str) -> bool:
    """Fill a text/textarea field by its visible label.

    Tries the label as-is, then prefixed with ``*`` (which is how
    required fields render in some layouts). Returns True if a
    matching field was filled, False otherwise — callers usually
    raise on False to fail the step early.
    """
    for variant in (label, f"*{label}"):
        try:
            field = page.get_by_label(variant, exact=False)
            if field.count() > 0:
                field.first.fill(value)
                return True
        except Exception:
            continue
    return False


def fill_date_field(page: Page, label: str, value: str) -> bool:
    """Fill a date input by its label. Clears any existing value first
    (so reruns on the same record don't append). Presses Tab after to
    commit. Value format is whatever the input accepts (typically
    M/D/YYYY in US locales)."""
    for variant in (label, f"*{label}"):
        try:
            field = page.get_by_label(variant, exact=False)
            if field.count() == 0:
                continue
            try:
                field.first.click()
                field.first.press("Control+a")
            except Exception:
                pass
            field.first.fill(value)
            field.first.press("Tab")
            return True
        except Exception:
            continue
    return False


def wait_for_form_dialog_ready(
    page: Page,
    required_labels: Iterable[str],
    *,
    timeout_ms: int = 30000,
) -> None:
    """Wait until a modal/form is fully rendered and editable.

    Polls each label in ``required_labels`` until it's visible AND
    editable. Prevents the classic race where Playwright fills fields
    before the LWC form has mounted its inputs.

    Raises TimeoutError naming the specific label that never appeared.
    """
    # Give the modal frame a chance to render
    try:
        page.wait_for_selector(
            "div.slds-modal, div.slds-modal__container, "
            "records-record-layout-section, record-layout-edit-form, "
            "lightning-input, lightning-record-edit-form",
            state="visible",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    deadline = page.evaluate("Date.now()") + timeout_ms
    for label in required_labels:
        while True:
            field = page.get_by_label(label, exact=False)
            if field.count() == 0:
                field = page.get_by_label(f"*{label}", exact=False)
            if field.count() > 0:
                try:
                    if field.first.is_visible() and field.first.is_editable():
                        break
                except Exception:
                    pass
            if page.evaluate("Date.now()") > deadline:
                raise TimeoutError(
                    f"Form field '{label}' not ready within {timeout_ms}ms"
                )
            page.wait_for_timeout(500)


# ── Record type radio (shadow-DOM walk) ────────────────────────────────


def select_record_type(page: Page, value: str) -> bool:
    """Select a record type radio by its ``value`` attribute. The
    record type chooser is rendered inside one or more shadow roots in
    most Lightning record-creation flows, which is why this needs a
    JS walk rather than a Playwright locator.

    Returns True if a radio with that value was found and clicked.
    """
    found = page.evaluate(
        f"""(() => {{
            function findInShadow(root, depth) {{
                if (depth > 20) return null;
                const radios = root.querySelectorAll(
                    'input[type="radio"][value=' + {json.dumps(value)} + ']'
                );
                if (radios.length > 0) return radios[0];
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        const f = findInShadow(el.shadowRoot, depth + 1);
                        if (f) return f;
                    }}
                }}
                return null;
            }}
            const radio = findInShadow(document, 0);
            if (radio) {{ radio.click(); return true; }}
            return false;
        }})()"""
    )
    return bool(found)


# ── Picklist / combobox / Aura stage dialog ────────────────────────────


def select_picklist(page: Page, field_label: str, value: str) -> bool:
    """Set a picklist / combobox / Aura-anchor picklist value.

    Handles every picklist variant CCI uses, in order of likelihood:
      A) Native <select> immediately after the field's <label>.
      B) Aura ``<a class="select" role="button">`` trigger.
      C) Lightning ``role="combobox"`` named ``field_label``.
      D) Plain <button> trigger after the label.
      E) Generic "--None--" / "Select an option" trigger inside an
         open dialog.
      F) Keyboard fallback — focus the field, type the value, Enter.

    After clicking a trigger we wait for the popup ``[role=listbox]`` /
    ``[role=menu]`` to render and then click the option matching
    ``value``. Returns True on success.

    The original TC1 helper was specialized to "Stage" — this is the
    generalized version. Use it for any picklist; the only thing
    that changes is ``field_label``.
    """
    # Scope: prefer the open dialog when one exists; this avoids
    # matching a "--None--" trigger from a different open form on the
    # same page (rare but happens during quick succession of dialogs).
    try:
        dialog = page.get_by_role("dialog").last
        if not dialog.is_visible(timeout=1000):
            dialog = page
    except Exception:
        dialog = page

    clicked_trigger = False

    # A) Native <select>
    try:
        native = page.locator(
            f"xpath=//label[contains(normalize-space(.),'{field_label}')]"
            "/following::select[1]"
        )
        if native.count() > 0:
            try:
                native.first.select_option(label=value)
                return True
            except Exception:
                try:
                    native.first.select_option(value=value)
                    return True
                except Exception:
                    pass
    except Exception:
        pass

    # B) Aura picklist anchor trigger
    for strat in (
        lambda: page.locator("a.select[role='button']").filter(
            has_text=re.compile(r"--None--|^None$|Select", re.I)
        ),
        lambda: page.locator("a.select[role='button']"),
        lambda: page.locator(
            f"xpath=//label[contains(normalize-space(.),'{field_label}')]"
            "/following::a[@role='button'][1]"
        ),
    ):
        try:
            loc = strat()
            for i in range(min(loc.count(), 6)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible(timeout=400):
                        cand.scroll_into_view_if_needed()
                        cand.click()
                        clicked_trigger = True
                        break
                except Exception:
                    continue
            if clicked_trigger:
                break
        except Exception:
            continue

    # C) Lightning combobox
    if not clicked_trigger:
        for strat in (
            lambda: dialog.get_by_role(
                "combobox", name=re.compile(rf"^\s*\*?\s*{re.escape(field_label)}\s*$", re.I),
            ),
            lambda: dialog.get_by_role("combobox", name=re.compile(re.escape(field_label), re.I)),
        ):
            try:
                combo = strat()
                if combo.count() > 0 and combo.first.is_visible():
                    combo.first.scroll_into_view_if_needed()
                    combo.first.click()
                    clicked_trigger = True
                    break
            except Exception:
                continue

    # D) Plain <button> trigger after the label
    if not clicked_trigger:
        try:
            trigger = page.locator(
                f"xpath=//label[contains(normalize-space(.),'{field_label}')]"
                "/following::button[1]"
            )
            if trigger.count() > 0 and trigger.first.is_visible():
                trigger.first.scroll_into_view_if_needed()
                trigger.first.click()
                clicked_trigger = True
        except Exception:
            pass

    # E) Generic "--None--" / "Select" button inside the dialog
    if not clicked_trigger:
        for trigger_name in (
            re.compile(r"^\s*--None--\s*$"),
            re.compile(r"^\s*None\s*$"),
            re.compile(r"^\s*Select\s+an?\s*option\s*$", re.I),
            re.compile(r"^\s*Select\s*$", re.I),
        ):
            try:
                btn = dialog.get_by_role("button", name=trigger_name)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.scroll_into_view_if_needed()
                    btn.first.click()
                    clicked_trigger = True
                    break
            except Exception:
                continue

    # F) Keyboard fallback — focus the field via label, then type
    if not clicked_trigger:
        try:
            field = page.get_by_label(
                re.compile(rf"^\s*\*?\s*{re.escape(field_label)}\s*$", re.I),
            )
            if field.count() > 0:
                field.first.click()
                clicked_trigger = True
        except Exception:
            pass

    if clicked_trigger:
        try:
            page.wait_for_selector(
                "[role='listbox'], [role='menu']", state="visible", timeout=3000,
            )
        except Exception:
            page.wait_for_timeout(700)

    # Click the matching option in the popup.
    for opt_locator in (
        lambda: page.locator("[role='listbox'] [role='option']").filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        ),
        lambda: page.locator("[role='menu'] [role='menuitem']").filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        ),
        lambda: page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)),
        lambda: page.get_by_role("menuitem", name=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)),
        lambda: page.locator("a[role='menuitemradio'], a[role='menuitem']").filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        ),
        lambda: page.locator("lightning-base-combobox-item").filter(
            has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        ),
        lambda: page.get_by_text(re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)),
    ):
        try:
            loc = opt_locator()
            for i in range(min(loc.count(), 8)):
                try:
                    cand = loc.nth(i)
                    if cand.is_visible(timeout=400):
                        cand.click()
                        page.wait_for_timeout(300)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    # Last resort — type + Enter
    try:
        page.keyboard.type(value, delay=30)
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


# ``set_stage`` is preserved as a thin alias because it appears verbatim
# in TC1 today and many people will grep for it.
def set_stage(page: Page, value: str) -> bool:
    """Alias for ``select_picklist(page, 'Stage', value)`` — kept so
    existing tests can use the original name."""
    return select_picklist(page, "Stage", value)


# ── Lookup (autocomplete + full search dialog) ─────────────────────────


def fill_lookup(
    page: Page,
    field_label: str,
    search_value: str,
    *,
    timeout_s: int = 30,
) -> bool:
    """Resolve a Salesforce Lightning lookup field reliably.

    Strategy (each step described in line):
      1. Locate the lookup input by label and type ``search_value``.
      2. Wait ~3.5s for the inline dropdown (SF debounces searches).
      3. Scan the inline dropdown for a substring match. If found, click.
      4. Otherwise open the full search dialog (search icon or Enter).
      5. Re-type ``search_value`` in the dialog and click Search.
      6. Expect exactly 1 result row; click it.

    Raises Exception with a descriptive message on any unrecoverable
    case (0 results, multiple ambiguous results, dialog never opened,
    etc.) — the caller's ``sf.step()`` context catches this and turns
    it into the right failure.
    """
    # ── Step A: Find & populate the lookup input ─────────────────
    lookup_input = None
    for strat in (
        lambda: page.get_by_label(re.compile(rf"\*?\s*{re.escape(field_label)}", re.I)),
        lambda: page.get_by_label(field_label, exact=False),
        lambda: page.locator(
            f"input[aria-label*='{field_label}' i]"
            ":not([type='range']):not(.slds-assistive-text)"
        ),
    ):
        try:
            loc = strat()
            for i in range(min(loc.count(), 5)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible(timeout=2000) and cand.is_editable(timeout=1000):
                        lookup_input = cand
                        break
                except Exception:
                    continue
            if lookup_input:
                break
        except Exception:
            continue

    if not lookup_input:
        raise Exception(f"Lookup input for '{field_label}' not found on the page")

    lookup_input.click()
    page.wait_for_timeout(300)
    try:
        lookup_input.press("Control+a")
        lookup_input.press("Backspace")
    except Exception:
        pass
    lookup_input.type(search_value, delay=30)

    # ── Step B: Wait for inline dropdown (SF debounce is ~3-5s) ──
    page.wait_for_timeout(3500)

    # ── Step C: Try inline match ─────────────────────────────────
    for sel in (
        "[role='listbox'] [role='option']",
        "lightning-base-combobox-item",
        ".slds-listbox__item",
        ".lookup__result-item",
    ):
        try:
            options = page.locator(sel)
            for i in range(min(options.count(), 15)):
                opt = options.nth(i)
                try:
                    if not opt.is_visible(timeout=400):
                        continue
                    opt_text = (opt.text_content() or "").strip()
                    if search_value.lower() in opt_text.lower():
                        opt.click()
                        page.wait_for_timeout(1000)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    # ── Step D: Open the full search dialog ──────────────────────
    dialog_opened = False
    for icon_strat in (
        lambda: page.locator(
            "button[aria-label*='Search' i], "
            "button.slds-input__icon, "
            "lightning-icon.slds-input__icon"
        ).filter(has=page.locator("xpath=ancestor::*[contains(@class,'lookup')]")),
        lambda: page.locator(
            "button[aria-label*='Search' i], span.slds-icon-utility-search"
        ),
    ):
        try:
            icons = icon_strat()
            for i in range(min(icons.count(), 5)):
                icon = icons.nth(i)
                try:
                    if icon.is_visible(timeout=1000):
                        icon.click()
                        dialog_opened = True
                        break
                except Exception:
                    continue
            if dialog_opened:
                break
        except Exception:
            continue

    if not dialog_opened:
        try:
            lookup_input.press("Enter")
            dialog_opened = True
        except Exception:
            pass

    if not dialog_opened:
        raise Exception(f"Could not open search dialog for lookup '{field_label}'")

    # ── Step E: Wait for dialog ──────────────────────────────────
    page.wait_for_timeout(3000)
    for modal_sel in (
        "div.modal-container",
        "section[role='dialog']",
        "div[role='dialog']",
        "div.slds-modal__container",
        ".lookup__results",
    ):
        try:
            page.wait_for_selector(modal_sel, state="visible", timeout=10000)
            break
        except Exception:
            continue

    # ── Step F: Search inside the dialog ─────────────────────────
    dialog_search = None
    for strat in (
        lambda: page.locator(
            "[role='dialog'] input[type='search'], "
            "[role='dialog'] input[type='text'], "
            ".modal-container input[type='search'], "
            ".modal-container input[type='text']"
        ),
        lambda: page.get_by_placeholder(re.compile(r"search", re.I)),
    ):
        try:
            loc = strat()
            for i in range(min(loc.count(), 8)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible(timeout=1000) and cand.is_editable(timeout=1000):
                        dialog_search = cand
                        break
                except Exception:
                    continue
            if dialog_search:
                break
        except Exception:
            continue

    if dialog_search:
        dialog_search.click()
        try:
            dialog_search.press("Control+a")
            dialog_search.press("Backspace")
        except Exception:
            pass
        dialog_search.fill(search_value)
        page.wait_for_timeout(500)
        search_clicked = False
        for btn_strat in (
            lambda: page.locator(
                "[role='dialog'] button:has-text('Search'), "
                ".modal-container button:has-text('Search')"
            ),
            lambda: page.get_by_role("button", name=re.compile(r"^\s*Search\s*$", re.I)),
        ):
            try:
                btn = btn_strat()
                if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                    btn.first.click()
                    search_clicked = True
                    break
            except Exception:
                continue
        if not search_clicked:
            try:
                dialog_search.press("Enter")
            except Exception:
                pass
        page.wait_for_timeout(4000)

    # ── Step G: Verify exactly 1 result and click it ─────────────
    result_rows = None
    for row_sel in (
        "[role='dialog'] table tbody tr",
        ".modal-container table tbody tr",
        "[role='dialog'] [role='row']",
        "[role='dialog'] .slds-table tbody tr",
        ".lookup__results .lookup__result-item",
    ):
        try:
            rows = page.locator(row_sel)
            if rows.count() > 0:
                result_rows = rows
                break
        except Exception:
            continue

    if result_rows is None or result_rows.count() == 0:
        raise Exception(
            f"Lookup '{field_label}': search for '{search_value}' "
            f"returned 0 results in the dialog"
        )

    row_count = result_rows.count()
    if row_count > 1:
        # Multiple results — pick the row whose text contains the search
        # value, fail if more than one matches exactly.
        exact_row = None
        for i in range(row_count):
            try:
                txt = (result_rows.nth(i).text_content() or "").strip()
                if search_value.lower() in txt.lower():
                    if exact_row is None:
                        exact_row = result_rows.nth(i)
                    else:
                        raise Exception(
                            f"Lookup '{field_label}': search for "
                            f"'{search_value}' returned {row_count} results "
                            f"— expected exactly 1. Possible duplicate records."
                        )
            except Exception as inner:
                if "returned" in str(inner):
                    raise
                continue
        if not exact_row:
            raise Exception(
                f"Lookup '{field_label}': {row_count} results but none matched exactly"
            )
        exact_row.scroll_into_view_if_needed()
        try:
            link = exact_row.locator("a, [role='link']").first
            if link.is_visible(timeout=1000):
                link.click()
            else:
                exact_row.click()
        except Exception:
            exact_row.click()
    else:
        row = result_rows.first
        row.scroll_into_view_if_needed()
        try:
            link = row.locator("a, [role='link']").first
            if link.is_visible(timeout=1000):
                link.click()
            else:
                row.click()
        except Exception:
            row.click()

    page.wait_for_timeout(2000)
    return True
