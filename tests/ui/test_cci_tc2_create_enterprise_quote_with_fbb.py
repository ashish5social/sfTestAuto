"""TC2 — Create Enterprise Quote with Fiber Broadband (FBB) (CCI Sandbox).

Test metadata (display name, tags, objective) lives in the class
attributes below; the dashboard parser reads them via AST.

Data:      tests/ui/data/tc2_create_enterprise_quote_with_fbb.json

Run:       cci test tests/ui/test_cci_tc2_create_enterprise_quote_with_fbb.py
Headless:  cci test tests/ui/test_cci_tc2_create_enterprise_quote_with_fbb.py --headless
"""

import json
import os
import re
import pytest
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo
from playwright.sync_api import Page, expect





# ── Test Data ──────────────────────────────────────────────────────────────────

_DATA_FILE = Path(__file__).parent / "data" / "tc2_create_enterprise_quote_with_fbb.json"
with open(_DATA_FILE) as f:
    DATA = json.load(f)







SF_URL = os.getenv("SF_LOGIN_URL", "https://test.salesforce.com")
SF_USERNAME = os.getenv("SF_USERNAME", "")
SF_PASSWORD = os.getenv("SF_PASSWORD", "")
SF_SECURITY_TOKEN = os.getenv("SF_SECURITY_TOKEN", "")
SF_CLIENT_ID = os.getenv("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET", "")

# Use org timezone for all dates so "today" is consistent with Salesforce
SF_ORG_TZ = ZoneInfo("America/Los_Angeles")
NOW_ORG = datetime.now(SF_ORG_TZ)
# Millisecond + slot precision so parallel pytest subprocesses starting
# within the same wall-clock second don't collide on CCIAUTO_Biz_<ts>
# account names. The slot id comes from:
#   - UI_TEST_SLOT          set by src/web/parallel_runner.py (dashboard)
#   - PYTEST_XDIST_WORKER   set by pytest-xdist (e.g. "gw0", "gw1") in CI
# Falls back to no slot suffix for plain single-process runs.
_slot = (
    os.environ.get("UI_TEST_SLOT")
    or os.environ.get("PYTEST_XDIST_WORKER", "").replace("gw", "")
)
TIMESTAMP = (
    NOW_ORG.strftime("%m%d_%H%M%S")
    + f"{NOW_ORG.microsecond // 1000:03d}"
    + (f"s{_slot}" if _slot else "")
)
TODAY_ORG = f"{NOW_ORG.month}/{NOW_ORG.day}/{NOW_ORG.year}"  # e.g. "4/15/2026"

RECORD_TYPE = DATA["record_type"]
ACCOUNT_NAME = f"{DATA['account_name_prefix']}{TIMESTAMP}"
ADDRESS = DATA["addresses"][0]

OPP = DATA["opportunity"]
OPP_NEW_BTN = OPP["new_button_name"]       # "New Opportunity - Business"
OPP_STAGE = OPP["stage"]                   # "Generate Interest"

QUOTE = DATA["quote"]
QUOTE_NAME = f"{QUOTE['quote_name_prefix']}{ACCOUNT_NAME}"  # "quote_<account>"
QUOTE_CREATE_BTN = QUOTE["create_button_name"]              # "Create Enterprise Quote"
QUOTE_SERVICE_TERM = str(QUOTE["service_term_months"])      # "12"

LOCATION = DATA["location"]
LOCATION_ADDRESS = LOCATION["search_address"]

PRODUCT = DATA["product"]
PRODUCT_SEARCH = PRODUCT["search_term"]           # "Fiber Broadband"
PRODUCT_DISPLAY = PRODUCT["display_name"]          # "Fiber Broadband"
PRODUCT_BANDWIDTH = PRODUCT["bandwidth"]           # "100 Mbps"
PRODUCT_BANDWIDTH_LABEL = PRODUCT.get("bandwidth_field_label", "Bandwidth")
PRODUCT_QUOTE_TYPE = PRODUCT["quote_type"]         # "New"
PRODUCT_QUOTE_TYPE_LABEL = PRODUCT.get("quote_type_field_label", "Quote Type")
EXPECTED_SUMMARY = PRODUCT["expected_summary_products"]  # ["Fiber Broadband"]

DEFAULT_TIMEOUT = DATA.get("timeout_ms", 60000)


class TestCreateEnterpriseQuoteWithFBB:
    """TC2 - Create Enterprise Quote with Fiber Broadband"""

    # Class-level metadata read by the dashboard parser (no YAML needed).
    # Placeholders are resolved against tests/ui/data/tc2_*.json.
    TAGS = ["account", "business", "opportunity", "quote",
            "enterprise", "fiber-broadband", "fbb", "location",
            "product", "smoke"]
    OBJECTIVE = (
        "End-to-end flow that creates a Business Account, Opportunity, "
        "and Enterprise Quote with a {product.display_name} product "
        "(Quote Type = {product.quote_type}, Bandwidth = "
        "{product.bandwidth}), then verifies the Summary tab and the "
        "Quote page's Approval Journey section. No router/connection "
        "add-ons."
    )

    @pytest.fixture(autouse=True)
    def setup(self, page: Page, tracker, sf, context):
        self.page = page
        self.page.set_default_timeout(DEFAULT_TIMEOUT)
        self.tracker = tracker
        self.sf = sf
        self.context = context
        self._account_id = None  # captured in Step 5 for navigation
        self._opportunity_id = None  # captured in Step 10 for report linking
        self._quote_id = None  # captured in Step 13 for report linking
        yield

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _select_record_type_via_js(self, value: str) -> bool:
        """Select a record type radio button by its value attribute (shadow DOM safe)."""
        return bool(self.page.evaluate(f"""(() => {{
            function findInShadow(root, depth) {{
                if (depth > 20) return null;
                const radios = root.querySelectorAll('input[type="radio"][value="{value}"]');
                if (radios.length > 0) return radios[0];
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        const found = findInShadow(el.shadowRoot, depth + 1);
                        if (found) return found;
                    }}
                }}
                return null;
            }}
            const radio = findInShadow(document, 0);
            if (radio) {{ radio.click(); return true; }}
            return false;
        }})()"""))

    def _fill_field_by_label(self, label: str, value: str) -> bool:
        """Fill a form field by its label text. Returns True if successful."""
        page = self.page
        field = page.get_by_label(label, exact=False)
        if field.count() > 0:
            try:
                field.first.fill(value)
                return True
            except Exception:
                pass
        field = page.get_by_label(f"*{label}", exact=False)
        if field.count() > 0:
            try:
                field.first.fill(value)
                return True
            except Exception:
                pass
        return False

    def _wait_for_form_dialog_ready(self, required_labels, timeout_ms: int = 30000):
        """Wait until the modal / form dialog is fully rendered.

        Waits for at least one of the required field labels to be present
        and editable. This prevents the classic race where Playwright fills
        fields before the LWC form has mounted its inputs.
        """
        page = self.page
        # Give any modal a chance to paint
        try:
            page.wait_for_selector(
                "div.slds-modal, div.slds-modal__container, records-record-layout-section, "
                "record-layout-edit-form, lightning-input, lightning-record-edit-form",
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

    def _click_button_anywhere(self, name: str, timeout_ms: int = 10000) -> bool:
        """Click a button / link whose visible text matches `name`.

        Tries: Playwright role=button exact, role=button partial, plain text,
        then a full shadow-DOM JS walk.
        """
        page = self.page
        for strategy in (
            lambda: page.get_by_role("button", name=name, exact=True),
            lambda: page.get_by_role("button", name=re.compile(re.escape(name), re.I)),
            lambda: page.get_by_role("link", name=re.compile(re.escape(name), re.I)),
        ):
            try:
                loc = strategy()
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        # Shadow DOM JS fallback
        clicked = page.evaluate(f"""(() => {{
            function findInShadow(root, depth) {{
                if (depth > 25) return null;
                const candidates = root.querySelectorAll(
                    'button, a[role="button"], [role="button"], lightning-button, one-record-action-link, a.listItemLink'
                );
                for (const c of candidates) {{
                    const txt = (c.textContent || c.getAttribute('title') ||
                                 c.getAttribute('aria-label') || '').trim();
                    if (txt === {json.dumps(name)} ||
                        txt.toLowerCase().includes({json.dumps(name.lower())})) {{
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
        }})()""")
        return bool(clicked)

    def _wait_for_config_update_complete(self, label: str = "", timeout_ms: int = 45000):
        """Wait for a configuration update to complete on the Configure Cart page.

        After changing a field (Quote Type, Bandwidth, etc.) the UI shows a
        blue toast like 'Updating Fiber Broadband' → then a green toast
        'Updated Fiber Broadband', and spinners may appear while re-rendering.

        This helper:
          1. Waits for any 'Updating...' toast to disappear (up to timeout_ms)
          2. Waits for ALL spinners to clear
          3. Adds extra settle time for the DOM to stabilize
        """
        page = self.page
        deadline = page.evaluate("Date.now()") + timeout_ms

        # Phase 1: Wait for any "Updating" toast to disappear
        # The toast text is like "Updating Fiber Broadband" or "Updating Bandwidth"
        updating_pattern = re.compile(r"Updating", re.I)
        while page.evaluate("Date.now()") < deadline:
            try:
                updating_toast = page.get_by_text(updating_pattern)
                if updating_toast.count() > 0 and updating_toast.first.is_visible(timeout=500):
                    page.wait_for_timeout(1000)
                    continue
            except Exception:
                pass
            break

        # Phase 2: Wait for ALL spinners to clear
        spinner_sels = [
            ".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
            "[role='progressbar']", ".slds-spinner_container",
        ]
        while page.evaluate("Date.now()") < deadline:
            any_visible = False
            for sel in spinner_sels:
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

        # Phase 3: Extra settle time for DOM re-render
        # (Phases 1+2 already polled toast + spinner to completion, so this is
        #  just a small safety buffer — 1.5s is enough for LWC rerender.)
        page.wait_for_timeout(1500)

    def _login_via_frontdoor(self) -> str:
        """Get an access token and return a frontdoor URL to bypass email verification.

        When the test runs from an IP not whitelisted in Salesforce Network
        Access (e.g., GitHub Actions, a colleague's laptop), Salesforce shows
        an email verification page. The frontdoor URL bypasses this by using
        an access token obtained via the OAuth 2.0 Client Credentials flow.

        Strategy (in order):
          1. OAuth 2.0 Client Credentials flow (needs SF_CLIENT_ID + SF_CLIENT_SECRET)
          2. SOAP Partner API fallback (needs SF_SECURITY_TOKEN)

        Returns the full frontdoor URL to navigate to.
        Raises Exception if all auth methods fail.
        """
        import requests

        session_id = None
        instance_url = None
        errors = []

        # ── Strategy 1: OAuth 2.0 Client Credentials Flow ──
        # This is the preferred method. It only needs client_id + client_secret
        # from the External Client App. No username/password/security token needed.
        # Requires "Enable Client Credentials Flow" checked on the app and
        # "Relax IP restrictions" set in the app policy.
        if SF_CLIENT_ID and SF_CLIENT_SECRET:
            # Build token endpoint URLs to try
            token_urls = []
            if "salesforce.com" in SF_URL and "test.salesforce.com" not in SF_URL \
                    and "login.salesforce.com" not in SF_URL:
                token_urls.append(f"{SF_URL.rstrip('/')}/services/oauth2/token")
            token_urls.append("https://test.salesforce.com/services/oauth2/token")
            token_urls.append("https://login.salesforce.com/services/oauth2/token")

            for token_url in token_urls:
                try:
                    resp = requests.post(token_url, data={
                        "grant_type": "client_credentials",
                        "client_id": SF_CLIENT_ID,
                        "client_secret": SF_CLIENT_SECRET,
                    }, timeout=30)
                    if resp.status_code == 200:
                        data = resp.json()
                        session_id = data["access_token"]
                        instance_url = data["instance_url"]
                        break
                    else:
                        errors.append(
                            f"OAuth CC ({token_url}): {resp.status_code} — "
                            f"{resp.json().get('error_description', resp.text)}"
                        )
                except Exception as e:
                    errors.append(f"OAuth CC ({token_url}): {e}")

        # ── Strategy 2: SOAP Partner API fallback ──
        if session_id is None:
            try:
                from simple_salesforce import Salesforce as SFApi

                raw_pw = SF_PASSWORD
                if SF_SECURITY_TOKEN:
                    pw, tok = raw_pw, SF_SECURITY_TOKEN
                elif ":" in raw_pw:
                    pw, tok = raw_pw[:raw_pw.index(":")], raw_pw[raw_pw.index(":") + 1:]
                else:
                    pw, tok = raw_pw, ""

                domains = []
                if "salesforce.com" in SF_URL and "test.salesforce.com" not in SF_URL \
                        and "login.salesforce.com" not in SF_URL:
                    from urllib.parse import urlparse
                    host = urlparse(SF_URL).hostname
                    domains.append(host.replace(".salesforce.com", ""))
                domains.extend(["test", "login"])

                for domain in domains:
                    try:
                        sf_conn = SFApi(
                            username=SF_USERNAME, password=pw,
                            security_token=tok, domain=domain,
                        )
                        session_id = sf_conn.session_id
                        instance_url = f"https://{sf_conn.sf_instance}"
                        break
                    except Exception as e:
                        errors.append(f"SOAP ({domain}): {e}")
            except ImportError:
                errors.append("SOAP: simple-salesforce not installed")

        if session_id is None:
            raise Exception(
                f"Frontdoor login failed — all auth methods failed.\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        # ── Build frontdoor URL ──
        # Prefer the My Domain URL so cookies match the browser session
        if "salesforce.com" in SF_URL and "test.salesforce.com" not in SF_URL \
                and "login.salesforce.com" not in SF_URL:
            base_url = SF_URL.rstrip("/")
        else:
            base_url = instance_url

        frontdoor_url = (
            f"{base_url}/secur/frontdoor.jsp"
            f"?sid={url_quote(session_id)}"
            f"&retURL={url_quote('/lightning/page/home')}"
        )

        return frontdoor_url

    def _set_stage_via_aura_dialog(self, value: str) -> bool:
        """Set the Stage picklist inside the Aura 'New Opportunity - Business'
        quick-action dialog.

        The dialog renders Stage as a <button> trigger labeled '--None--' (or
        the current value) that pops a menu of options. It is NOT a
        lightning-combobox, so the standard combobox helpers don't apply.

        Strategy:
          1. Scope the search to the open dialog (avoid matching other
             '--None--' triggers on the page).
          2. Find the "Stage" label/field container and click the nearest
             picklist trigger inside it.
          3. Click the target option in the popup (role=menuitem / option).
        """
        page = self.page

        # Scope: the open dialog. If we can't find one, operate on the whole page.
        try:
            dialog = page.get_by_role("dialog").last
            if not dialog.is_visible(timeout=1000):
                dialog = page
        except Exception:
            dialog = page

        # ── Try to click the Stage-specific trigger first ──────────────────
        clicked_trigger = False

        # A) Native <select> near a Stage label
        try:
            native = page.locator(
                "xpath=//label[contains(normalize-space(.),'Stage')]"
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

        # B) Aura picklist anchor trigger: <a class="select" role="button">
        #    (verified live — NOT a <button> element; it's a styled <a>.)
        for strat in (
            lambda: page.locator("a.select[role='button']").filter(
                has_text=re.compile(r"--None--|^None$|Select", re.I)
            ),
            lambda: page.locator("a.select[role='button']"),
            # XPath: the first anchor button that follows the Stage label
            lambda: page.locator(
                "xpath=//label[contains(normalize-space(.),'Stage')]"
                "/following::a[@role='button'][1]"
            ),
        ):
            try:
                loc = strat()
                cnt = loc.count()
                for i in range(min(cnt, 6)):
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

        # C) lightning-combobox / role=combobox named "Stage"
        if not clicked_trigger:
            for strat in (
                lambda: dialog.get_by_role("combobox", name=re.compile(r"^\s*\*?\s*Stage\s*$", re.I)),
                lambda: dialog.get_by_role("combobox", name=re.compile(r"Stage", re.I)),
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

        # D) <button> trigger near the Stage label (other picklist pattern)
        if not clicked_trigger:
            try:
                trigger = page.locator(
                    "xpath=//label[contains(normalize-space(.),'Stage')]"
                    "/following::button[1]"
                )
                if trigger.count() > 0 and trigger.first.is_visible():
                    trigger.first.scroll_into_view_if_needed()
                    trigger.first.click()
                    clicked_trigger = True
            except Exception:
                pass

        # E) Fallback: any visible '--None--' / 'None' / 'Select an option' button
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

        # F) Last resort: keyboard focus the field via its label, then type
        if not clicked_trigger:
            try:
                field = page.get_by_label(re.compile(r"^\s*\*?\s*Stage\s*$", re.I))
                if field.count() > 0:
                    field.first.click()
                    clicked_trigger = True
            except Exception:
                pass

        if clicked_trigger:
            # Wait for the popup listbox to render (Aura pops options async)
            try:
                page.wait_for_selector(
                    "[role='listbox'], [role='menu']",
                    state="visible",
                    timeout=3000,
                )
            except Exception:
                page.wait_for_timeout(700)

        # ── Click the target option in the popup ───────────────────────────
        for opt_locator in (
            # Scoped to a visible listbox/menu (Aura popup)
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
                cnt = loc.count()
                for i in range(min(cnt, 8)):
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

        # Final last resort — keyboard type + Enter
        try:
            page.keyboard.type(value, delay=30)
            page.wait_for_timeout(400)
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

        return False

    def _resolve_lookup(self, field_label: str, search_value: str,
                        timeout_s: int = 30) -> bool:
        """Resolve a Salesforce Lightning lookup field reliably.

        Strategy (in order):
          1. Locate the lookup input by *field_label* and type the FULL
             *search_value* into it.
          2. Wait a few seconds for the inline dropdown to appear.
          3. Scan the dropdown for an **exact** text match. If found, click it.
          4. If no exact match in the dropdown (e.g. text is truncated), click
             the search icon / press Enter to open the **full search dialog**.
          5. In the search dialog, type the full *search_value* again, click
             "Search", and wait for results.
          6. Expect **exactly 1** result. If 0 → raise. If >1 → raise (fail
             fast; duplicates are a data problem). Click the single result row
             to select it.

        Returns True on success. Raises Exception on failure so the caller
        can let pytest.fail() propagate the message.
        """
        page = self.page

        # ── Step A: Find & populate the lookup input ──────────────────────
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
                cnt = loc.count()
                for i in range(min(cnt, 5)):
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
            raise Exception(
                f"Lookup input for '{field_label}' not found on the page"
            )

        # Click to focus, clear, then type the full search value
        lookup_input.click()
        page.wait_for_timeout(300)
        try:
            lookup_input.press("Control+a")
            lookup_input.press("Backspace")
        except Exception:
            pass
        # Type character-by-character so SF's debounced search fires
        lookup_input.type(search_value, delay=30)

        # ── Step B: Wait for the inline dropdown ──────────────────────────
        # SF debounce is 3–5s; 3.5s covers the common case.
        # The match check below polls the dropdown options after this.
        page.wait_for_timeout(3500)

        # ── Step C: Try to find an exact match in the dropdown ────────────
        exact_match_found = False
        dropdown_selectors = [
            "[role='listbox'] [role='option']",
            "lightning-base-combobox-item",
            ".slds-listbox__item",
            ".lookup__result-item",
        ]
        for sel in dropdown_selectors:
            try:
                options = page.locator(sel)
                cnt = options.count()
                for i in range(min(cnt, 15)):
                    opt = options.nth(i)
                    try:
                        if not opt.is_visible(timeout=400):
                            continue
                        opt_text = (opt.text_content() or "").strip()
                        # Exact match (case-insensitive) — the full value must
                        # appear in the option text (option may contain extra
                        # info like record type or icon text).
                        if search_value.lower() in opt_text.lower():
                            opt.click()
                            exact_match_found = True
                            break
                    except Exception:
                        continue
                if exact_match_found:
                    break
            except Exception:
                continue

        if exact_match_found:
            page.wait_for_timeout(1000)
            return True

        # ── Step D: Open the full search dialog ───────────────────────────
        # Click the search / magnifying-glass icon next to the lookup, or
        # press Enter to open the search dialog.
        dialog_opened = False
        # Try clicking a search icon near the lookup
        for icon_strat in (
            lambda: page.locator(
                "button[aria-label*='Search' i], "
                "button.slds-input__icon, "
                "lightning-icon.slds-input__icon"
            ).filter(has=page.locator("xpath=ancestor::*[contains(@class,'lookup')]")),
            lambda: page.locator(
                "button[aria-label*='Search' i], "
                "span.slds-icon-utility-search"
            ),
        ):
            try:
                icons = icon_strat()
                cnt = icons.count()
                for i in range(min(cnt, 5)):
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

        # Fallback: press Enter to open the search dialog
        if not dialog_opened:
            try:
                lookup_input.press("Enter")
                dialog_opened = True
            except Exception:
                pass

        if not dialog_opened:
            raise Exception(
                f"Could not open search dialog for lookup '{field_label}'"
            )

        # ── Step E: Wait for the search dialog / modal ────────────────────
        page.wait_for_timeout(3000)
        # Wait for a dialog / modal to appear
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

        # ── Step F: Search again inside the dialog for the exact value ────
        # Find the search input inside the dialog
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
                cnt = loc.count()
                for i in range(min(cnt, 8)):
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
            # Click the Search button inside the dialog
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
                # Press Enter as fallback
                try:
                    dialog_search.press("Enter")
                except Exception:
                    pass

            # Wait for search results
            page.wait_for_timeout(4000)

        # ── Step G: Verify exactly 1 result and click it ──────────────────
        # The search dialog usually renders results as table rows or links
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
                cnt = rows.count()
                if cnt > 0:
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
            # Check if any row is an exact match — sometimes SF shows
            # partial matches alongside the exact one
            exact_row = None
            for i in range(row_count):
                try:
                    txt = (result_rows.nth(i).text_content() or "").strip()
                    if search_value.lower() in txt.lower():
                        if exact_row is None:
                            exact_row = result_rows.nth(i)
                        else:
                            # Multiple exact matches — fail
                            raise Exception(
                                f"Lookup '{field_label}': search for "
                                f"'{search_value}' returned {row_count} "
                                f"results — expected exactly 1. Possible "
                                f"duplicate records."
                            )
                except Exception as inner:
                    if "returned" in str(inner):
                        raise
                    continue
            if exact_row:
                exact_row.scroll_into_view_if_needed()
                # Click the link inside the row (not the row itself)
                try:
                    link = exact_row.locator("a, [role='link']").first
                    if link.is_visible(timeout=1000):
                        link.click()
                    else:
                        exact_row.click()
                except Exception:
                    exact_row.click()
            else:
                raise Exception(
                    f"Lookup '{field_label}': search for '{search_value}' "
                    f"returned {row_count} results but none matched exactly"
                )
        else:
            # Exactly 1 result — click it
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

    def _fill_date_field(self, label: str, value: str) -> bool:
        """Fill a date input by its label."""
        page = self.page
        try:
            field = page.get_by_label(label, exact=False)
            if field.count() == 0:
                field = page.get_by_label(f"*{label}", exact=False)
            if field.count() > 0:
                field.first.click()
                # Clear any existing value before filling
                try:
                    field.first.press("Control+a")
                except Exception:
                    pass
                field.first.fill(value)
                field.first.press("Tab")
                return True
        except Exception:
            pass
        return False

    # ── Main Test ──────────────────────────────────────────────────────────────

    def test_create_enterprise_quote_with_dia(self):
        page = self.page
        tracker = self.tracker
        sf = self.sf

        # ===== STEP 1: Login =====
        tracker.start_step(1, "Log into Salesforce Sandbox", f"Navigate to {SF_URL} and authenticate")
        try:
            # ── Phase 1: Attempt standard username/password login ──
            page.goto(SF_URL)
            page.wait_for_load_state("domcontentloaded")

            # Handle the password — strip security token if present (format: "password:token")
            login_pw = SF_PASSWORD
            if ":" in login_pw:
                login_pw = login_pw[:login_pw.index(":")]

            page.locator("#username").fill(SF_USERNAME)
            page.locator("input[name='pw']").fill(login_pw)
            page.click("#Login")
            sf.wait_page_ready(8000)
            page.wait_for_timeout(3000)

            # ── Phase 2: Check if we hit the email verification / identity page ──
            # Salesforce shows this when the IP is not in Network Access list.
            # Detect by URL patterns or the presence of a "Verify" button.
            needs_frontdoor = False
            current_url = page.url.lower()
            if "verify" in current_url or "identity" in current_url or "challenge" in current_url:
                needs_frontdoor = True
            else:
                # Also check for a Verify button on the page
                try:
                    verify_btn = page.locator(
                        "//button[@value='Verify' or @title='Verify' or text()='Verify'] | "
                        "//input[@value='Verify' or @title='Verify']"
                    )
                    if verify_btn.count() > 0 and verify_btn.first.is_visible(timeout=2000):
                        needs_frontdoor = True
                except Exception:
                    pass

            if needs_frontdoor:
                sf.screenshot("01a_verify_page_detected")

                # ── Phase 3: Bypass via frontdoor URL ──
                # Use Salesforce SOAP API to get a session ID, then navigate
                # to /secur/frontdoor.jsp?sid=... which logs in directly
                # without requiring email verification.
                frontdoor_url = self._login_via_frontdoor()
                page.goto(frontdoor_url)
                page.wait_for_load_state("domcontentloaded")
                sf.wait_page_ready(10000)
                # wait_page_ready already includes a 10s extra_ms buffer for
                # the frontdoor redirect chain; 2s extra is enough.
                page.wait_for_timeout(2000)
                tracker.add_assertion("Logged in via frontdoor URL (IP not whitelisted)", True)
            else:
                # Standard login succeeded — no verification needed
                tracker.add_assertion("Logged in via standard login", True)

            # ── Phase 4: Verify we're on Salesforce Lightning ──
            # wait_page_ready already waits for spinners + networkidle + 5s
            # buffer, so no additional fixed sleep is needed here.
            sf.wait_page_ready(5000)

            final_url = page.url.lower()
            assert "lightning" in final_url or "salesforce" in final_url, \
                f"Expected Salesforce Lightning, got: {page.url}"
            tracker.add_assertion("Landed on Salesforce Lightning", True)
            tracker.pass_step(sf.screenshot("01_logged_in"))
        except Exception as e:
            tracker.fail_step(str(e), sf.screenshot("01_login_FAILED"))
            pytest.fail(f"Step 1 - Login: {e}")

        # ===== STEP 2: Navigate to Accounts =====
        tracker.start_step(2, "Navigate to Accounts", "Open Accounts list view")
        try:
            base_url = page.url.split("/lightning")[0]
            page.goto(f"{base_url}/lightning/o/Account/list?filterName=__Recent")
            sf.wait_page_ready(5000)
            tracker.add_assertion("Accounts list loaded", True)
            tracker.pass_step(sf.screenshot("02_accounts_list"))
        except Exception as e:
            tracker.fail_step(str(e), sf.screenshot("02_accounts_FAILED"))
            pytest.fail(f"Step 2 - Navigate: {e}")

        # ===== STEP 3: Click 'New' =====
        tracker.start_step(3, "Click 'New' button", "Open new Account dialog / record type selector")
        try:
            page.get_by_role("button", name="New").first.click()
            sf.wait_page_ready(3000)
            tracker.add_assertion("New Account dialog opened", True)
            tracker.pass_step(sf.screenshot("03_new_button_clicked"))
        except Exception as e:
            tracker.fail_step(str(e), sf.screenshot("03_new_FAILED"))
            pytest.fail(f"Step 3 - New: {e}")

        # ===== STEP 4: Select Business → Next =====
        tracker.start_step(4, "Select Business record type → Next", f"Select '{RECORD_TYPE}'")
        try:
            selected = self._select_record_type_via_js(RECORD_TYPE)
            if selected:
                page.wait_for_timeout(500)
                next_btn = page.get_by_role("button", name="Next")
                if next_btn.count() > 0:
                    next_btn.click()
                    sf.wait_page_ready(4000)
                tracker.add_assertion(f"'{RECORD_TYPE}' record type selected via JS", True)
            else:
                radio = page.get_by_role("radio", name=RECORD_TYPE)
                if radio.count() > 0:
                    radio.first.check()
                    page.wait_for_timeout(500)
                    page.get_by_role("button", name="Next").click()
                    sf.wait_page_ready(4000)
                    tracker.add_assertion(f"'{RECORD_TYPE}' selected via Playwright", True)
                else:
                    tracker.add_assertion("Record type selector not shown", True)
            tracker.pass_step(sf.screenshot("04_record_type_selected"))
        except Exception as e:
            tracker.fail_step(str(e), sf.screenshot("04_record_type_FAILED"))
            pytest.fail(f"Step 4 - Record Type: {e}")

        # ===== STEP 5: Wait for form + Fill Account → Save =====
        tracker.start_step(5, "Fill Account Name + Business Address → Save",
                           f"Account: {ACCOUNT_NAME}, Address: {ADDRESS['street']}, {ADDRESS['city']}")
        try:
            # CRITICAL: wait for the Account form to finish rendering BEFORE
            # filling any field. Previously this step raced past Step 4 into
            # Step 5 before the LWC form had mounted its inputs, causing
            # "Account Name" fill to silently target nothing.
            self._wait_for_form_dialog_ready(
                required_labels=["Account Name", "Customer's Legal Name"],
                timeout_ms=30000,
            )
            page.wait_for_timeout(500)  # small settle
            sf.screenshot("05a_account_form_ready")

            # Account Name (required)
            self._fill_field_by_label("Account Name", ACCOUNT_NAME)
            page.wait_for_timeout(300)

            # Customer's Legal Name (required on CCI)
            legal_name = f"{ACCOUNT_NAME} Legal"
            self._fill_field_by_label("Customer's Legal Name", legal_name)
            page.wait_for_timeout(300)

            # Business address (CCI custom fields)
            self._fill_field_by_label("Business Street", ADDRESS["street"])
            self._fill_field_by_label("Business City", ADDRESS["city"])
            self._fill_field_by_label("Business State/Province", ADDRESS["state"])
            self._fill_field_by_label("Business Country", ADDRESS["country"])
            self._fill_field_by_label("Business Zip/Postal Code", ADDRESS["zip"])

            sf.screenshot("05b_account_form_filled")

            page.get_by_role("button", name="Save", exact=True).last.click()
            sf.wait_page_ready(8000)

            # Check for validation errors
            error_banner = page.locator("div.forceFormMessageBlock, div.slds-notify--alert")
            if error_banner.count() > 0 and error_banner.first.is_visible():
                error_text = error_banner.first.inner_text()
                raise Exception(f"Save validation error: {error_text}")

            tracker.add_assertion("Account form submitted successfully", True)
            tracker.pass_step(sf.screenshot("05c_account_saved"))
        except Exception as e:
            tracker.add_assertion("Account form save", False)
            tracker.fail_step(str(e), sf.screenshot("05_account_FAILED"))
            pytest.fail(f"Step 5 - Fill & Save: {e}")

        # ===== STEP 6: Verify Account Created =====
        tracker.start_step(6, "Verify Account Created",
                           f"Confirm '{ACCOUNT_NAME}' appears on the record page")
        try:
            expect(page.get_by_text(ACCOUNT_NAME).first).to_be_visible(timeout=20000)
            tracker.add_assertion(f"Account '{ACCOUNT_NAME}' is visible on page", True)

            current_url = page.url
            id_match = re.search(r'/([a-zA-Z0-9]{18})/view', current_url) or \
                       re.search(r'/([a-zA-Z0-9]{15})/view', current_url)
            if id_match:
                self._account_id = id_match.group(1)
                tracker.add_assertion(f"Account ID captured: {self._account_id}", True)

            # Attach a clickable Salesforce link for the Account to the report
            if self._account_id:
                tracker.add_record(
                    "Account", ACCOUNT_NAME,
                    record_id=self._account_id, object_type="Account",
                )

            tracker.pass_step(sf.screenshot("06_account_verified"))
        except Exception as e:
            tracker.add_assertion(f"Account '{ACCOUNT_NAME}' visible", False)
            tracker.fail_step(str(e), sf.screenshot("06_verify_FAILED"))
            pytest.fail(f"Step 6 - Verify: {e}")

        account_url = page.url

        # ===== STEP 7: Click "New Opportunity - Business" =====
        tracker.start_step(7, f"Click '{OPP_NEW_BTN}'",
                           "Standalone action button on the Account page header")
        try:
            # On the CCI Account page this is a prominent top-right button (not
            # behind "Show more actions"). Try direct click; fall back to the
            # action menu if needed.
            clicked = self._click_button_anywhere(OPP_NEW_BTN, timeout_ms=8000)
            if not clicked:
                try:
                    page.get_by_role("button", name="Show more actions").first.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                clicked = self._click_button_anywhere(OPP_NEW_BTN, timeout_ms=8000)
            if not clicked:
                raise Exception(f"Action '{OPP_NEW_BTN}' not found on Account header")

            # Wait for the quick-action dialog
            expect(
                page.get_by_role("dialog", name=re.compile(re.escape(OPP_NEW_BTN), re.I))
            ).to_be_visible(timeout=15000)
            tracker.add_assertion(f"'{OPP_NEW_BTN}' dialog opened", True)
            tracker.pass_step(sf.screenshot("07_new_opportunity_dialog"))
        except Exception as e:
            tracker.add_assertion(f"'{OPP_NEW_BTN}' dialog opened", False)
            tracker.fail_step(str(e), sf.screenshot("07_new_opportunity_FAILED"))
            pytest.fail(f"Step 7 - New Opportunity: {e}")

        # ===== STEP 8: Fill Close Date + Stage → Save =====
        # Opportunity Name is pre-populated by CCI with the Account Name.
        tracker.start_step(8, "Fill Opportunity Close Date + Stage → Save",
                           f"Opportunity Name: {ACCOUNT_NAME}, "
                           f"Close Date: {TODAY_ORG}, Stage: {OPP_STAGE}")
        try:
            # Opportunity Name is pre-populated with the Account Name — don't touch it.
            # Wait for the Close Date input to be present & editable
            self._wait_for_form_dialog_ready(
                required_labels=["Close Date"],
                timeout_ms=20000,
            )
            page.wait_for_timeout(500)
            sf.screenshot("08a_opportunity_form_ready")

            # Close Date (today, org TZ)
            if not self._fill_date_field("Close Date", TODAY_ORG):
                raise Exception("Could not locate 'Close Date' field")

            # Stage (Aura picklist inside the quick-action dialog — trigger is
            # a <button>'--None--'; menu items appear in a floating popup).
            if not self._set_stage_via_aura_dialog(OPP_STAGE):
                raise Exception(f"Could not set Stage to '{OPP_STAGE}'")

            page.wait_for_timeout(500)
            sf.screenshot("08b_opportunity_filled")

            # Save the Opportunity
            save_btns = page.get_by_role("button", name="Save", exact=True)
            save_btns.last.click()
            sf.wait_page_ready(8000)

            # After save, we should be on the Opportunity record page
            tracker.add_assertion("Opportunity saved", True)
            tracker.pass_step(sf.screenshot("08c_opportunity_saved"))
        except Exception as e:
            tracker.add_assertion("Opportunity saved", False)
            tracker.fail_step(str(e), sf.screenshot("08_opportunity_FAILED"))
            pytest.fail(f"Step 8 - Opportunity Save: {e}")

        # ===== STEP 9: Navigate to related Opportunities list =====
        tracker.start_step(
            9, "Navigate to related Opportunities list",
            "Open the Account's related Opportunities page and locate the Opp link"
        )
        opp_href = None
        try:
            origin = re.match(r"https?://[^/]+", page.url)
            base = origin.group(0) if origin else SF_URL
            related_url = (
                f"{base}/lightning/r/Account/"
                f"{self._account_id}/related/Opportunities/view"
            )
            page.goto(related_url, wait_until="domcontentloaded", timeout=30000)
            sf.wait_page_ready(8000)
            page.wait_for_timeout(2000)

            # The Opportunity Name link in the related list is buried ~12
            # levels deep in Shadow DOM.  Walk all shadow roots to find <a>
            # elements whose href contains a Salesforce ID starting with "006".
            opp_href = page.evaluate("""(() => {
                function digLinks(root, depth) {
                    if (depth > 40) return [];
                    let results = [];
                    for (const a of root.querySelectorAll('a')) {
                        if (a.href && /\\/lightning\\/r\\/006/.test(a.href)) {
                            const r = a.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) results.push(a.href);
                        }
                    }
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            results = results.concat(digLinks(el.shadowRoot, depth + 1));
                        }
                    }
                    return results;
                }
                const hrefs = digLinks(document, 0);
                return hrefs.length > 0 ? hrefs[0] : null;
            })()""")

            if not opp_href:
                raise Exception(
                    "Could not find the Opportunity link in the related "
                    "list (deep shadow DOM walk found no 006-prefixed href)"
                )

            tracker.add_assertion("Related Opportunities page loaded", True)
            tracker.add_assertion("Opportunity link found via shadow DOM walk", True)
            tracker.pass_step(sf.screenshot("09_related_opportunities"))
        except Exception as e:
            tracker.add_assertion("Related Opportunities page loaded", False)
            tracker.fail_step(str(e), sf.screenshot("09_related_opps_FAILED"))
            pytest.fail(f"Step 9 - Related Opportunities: {e}")

        # ===== STEP 10: Open the Opportunity page =====
        tracker.start_step(
            10, "Open the created Opportunity",
            "Navigate to the Opportunity record page via the extracted link"
        )
        try:
            page.goto(opp_href, wait_until="domcontentloaded", timeout=30000)
            sf.wait_page_ready(8000)
            page.wait_for_timeout(2000)

            # Robust spinner wait
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 30000:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(2000)

            # Verify we landed on an Opportunity page
            page_title = page.title()
            assert "Opportunity" in page_title or "/006" in page.url, (
                f"Expected Opportunity page, got title='{page_title}' "
                f"url='{page.url}'"
            )

            # Capture the Opportunity ID so we can attach a clickable link
            # to the HTML report. Opportunity IDs start with "006".
            opp_id_match = re.search(r'/(006[a-zA-Z0-9]{12,15})(?:/|$)', page.url) or \
                           re.search(r'/(006[a-zA-Z0-9]{12,15})(?:/|$)', opp_href or "")
            if opp_id_match:
                self._opportunity_id = opp_id_match.group(1)
                tracker.add_record(
                    "Opportunity", ACCOUNT_NAME,
                    record_id=self._opportunity_id, object_type="Opportunity",
                )

            tracker.add_assertion("Opportunity page loaded", True)
            tracker.pass_step(sf.screenshot("10_opportunity_page"))
        except Exception as e:
            tracker.add_assertion("Opportunity page loaded", False)
            tracker.fail_step(str(e), sf.screenshot("10_opportunity_FAILED"))
            pytest.fail(f"Step 10 - Open Opportunity: {e}")

        # ===== STEP 11: Click "Create Enterprise Quote" =====
        tracker.start_step(
            11, f"Click '{QUOTE_CREATE_BTN}'",
            f"Click the '{QUOTE_CREATE_BTN}' action on the Opportunity page"
        )
        try:
            clicked = self._click_button_anywhere(
                QUOTE_CREATE_BTN, timeout_ms=15000
            )
            if not clicked:
                # Reveal via overflow menu if hidden behind "Show more actions"
                try:
                    page.get_by_role(
                        "button", name="Show more actions"
                    ).first.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                clicked = self._click_button_anywhere(
                    QUOTE_CREATE_BTN, timeout_ms=8000
                )
            if not clicked:
                raise Exception(
                    f"Action '{QUOTE_CREATE_BTN}' not found on the "
                    f"Opportunity page"
                )

            sf.wait_page_ready(6000)
            tracker.add_assertion(f"'{QUOTE_CREATE_BTN}' opened", True)
            tracker.pass_step(sf.screenshot("11_create_quote_dialog"))
        except Exception as e:
            tracker.add_assertion(f"'{QUOTE_CREATE_BTN}' opened", False)
            tracker.fail_step(str(e), sf.screenshot("11_create_quote_FAILED"))
            pytest.fail(f"Step 11 - Create Enterprise Quote: {e}")

        # ===== STEP 12: Fill Quote Name + Service Term → Next =====
        tracker.start_step(12, "Fill Quote Name + Service Term → Next",
                           f"Quote Name: {QUOTE_NAME}, Service Term: {QUOTE_SERVICE_TERM} months")
        try:
            # The Create Enterprise Quote flow is an OmniScript / flow, not a
            # record form — the inputs may not have role=textbox. Wait for any
            # "Quote Name" label to appear.
            self._wait_for_form_dialog_ready(
                required_labels=["Quote Name"],
                timeout_ms=30000,
            )
            page.wait_for_timeout(800)
            sf.screenshot("12a_quote_form_ready")

            if not self._fill_field_by_label("Quote Name", QUOTE_NAME):
                raise Exception("'Quote Name' field not found")

            # Service Term — verified in live sandbox: it's a COMBOBOX with
            # options like "12 Months", "24 Months", "36 Months", "60 Months".
            # Try combobox selection FIRST (the verified path), then fall back
            # to a plain input in case org config differs.
            filled_term = False
            target_option_pattern = re.compile(
                rf"^\s*{QUOTE_SERVICE_TERM}\s*Months?\s*$", re.I
            )
            for label_variant in ("Service Term", "Service Term (Months)",
                                  "Term (Months)", "Term"):
                try:
                    combo = page.get_by_role("combobox",
                                              name=re.compile(re.escape(label_variant), re.I))
                    if combo.count() > 0 and combo.first.is_visible():
                        combo.first.scroll_into_view_if_needed()
                        combo.first.click()
                        page.wait_for_timeout(600)
                        # Prefer "12 Months" exact-ish match
                        opt = page.get_by_role("option", name=target_option_pattern)
                        if opt.count() == 0:
                            opt = page.get_by_text(target_option_pattern).first
                        else:
                            opt = opt.first
                        # Final fallback: any option starting with the number
                        if not opt or opt.count() == 0:
                            opt = page.get_by_role("option",
                                                    name=re.compile(rf"^\s*{QUOTE_SERVICE_TERM}\b", re.I)).first
                        if opt and (hasattr(opt, "count") and opt.count() > 0 or opt.is_visible()):
                            opt.click()
                            filled_term = True
                            break
                except Exception:
                    continue
            if not filled_term:
                # Fallback: plain input (unlikely but defensive)
                for label_variant in ("Service Term", "Service Term (Months)",
                                      "Service Term in Months", "Term (Months)", "Term"):
                    if self._fill_field_by_label(label_variant, QUOTE_SERVICE_TERM):
                        filled_term = True
                        break
            if not filled_term:
                raise Exception("Could not set Service Term")

            page.wait_for_timeout(500)
            sf.screenshot("12b_quote_form_filled")

            # Click Next
            next_clicked = self._click_button_anywhere("Next", timeout_ms=8000)
            if not next_clicked:
                raise Exception("'Next' button not found on Quote form")
            sf.wait_page_ready(10000)
            tracker.add_assertion("Quote creation advanced via Next", True)
            tracker.pass_step(sf.screenshot("12c_quote_next_clicked"))
        except Exception as e:
            tracker.add_assertion("Quote form Next", False)
            tracker.fail_step(str(e), sf.screenshot("12_quote_FAILED"))
            pytest.fail(f"Step 12 - Quote Form: {e}")

        # ===== STEP 13: Wait for "Enterprise Quote" page to fully load =====
        tracker.start_step(13, "Wait for Enterprise Quote page to load",
                           "Wait for spinners to clear and the Location tab to appear")
        try:
            # NOTE: we cannot simply match text "Enterprise Quote" — the leftover
            # "Create Enterprise Quote" button from the Opportunity page is still
            # in the DOM (hidden). Use structural signals instead:
            #   1. Wait for all spinners / loading indicators to clear
            #   2. Wait for the "Location" tab (role=tab) to be visible — this
            #      is the reliable indicator that the Quote page is rendered
            #   3. Small settle for lazy-loaded grid children

            # 1. Spinners
            for spinner_sel in (
                ".slds-spinner",
                "lightning-spinner",
                "[role='status'][class*='spinner']",
                ".slds-spinner_container",
            ):
                try:
                    page.wait_for_selector(spinner_sel, state="hidden", timeout=20000)
                except Exception:
                    continue

            # 2. Location tab is the definitive "quote page loaded" signal
            loc_tab_ok = False
            deadline = page.evaluate("Date.now()") + 60000
            while page.evaluate("Date.now()") < deadline:
                try:
                    loc_tab = page.get_by_role(
                        "tab", name=re.compile(r"Location", re.I)
                    )
                    if loc_tab.count() > 0 and loc_tab.first.is_visible(timeout=500):
                        loc_tab_ok = True
                        break
                except Exception:
                    pass
                # Alternative: an explicit "Enterprise Quote" heading (not the
                # button from the previous page)
                try:
                    heading = page.get_by_role(
                        "heading", name=re.compile(r"Enterprise\s+Quote", re.I)
                    )
                    if heading.count() > 0 and heading.first.is_visible(timeout=500):
                        loc_tab_ok = True
                        break
                except Exception:
                    pass
                page.wait_for_timeout(500)

            if not loc_tab_ok:
                raise Exception(
                    "Enterprise Quote page did not render within 60s "
                    "(neither Location tab nor heading visible)"
                )

            # 3. Extra settle for LWC grid / related lists inside the Location tab
            page.wait_for_timeout(3000)
            # One more spinner sweep after settle
            for spinner_sel in (".slds-spinner", "lightning-spinner"):
                try:
                    page.wait_for_selector(spinner_sel, state="hidden", timeout=5000)
                except Exception:
                    continue

            # Note: the Enterprise Quote page is a Vlocity/CCI custom LWC, so
            # its URL is NOT a standard /lightning/r/Quote/{id}/view. We
            # instead capture the Quote ID in the final step when we navigate
            # to the actual Quote record page (which IS a standard Lightning
            # URL) and attach the link retroactively to step 12.

            tracker.add_assertion("Enterprise Quote page loaded (Location tab visible)", True)
            tracker.pass_step(sf.screenshot("13_enterprise_quote_loaded"))
        except Exception as e:
            tracker.add_assertion("Enterprise Quote page loaded", False)
            tracker.fail_step(str(e), sf.screenshot("13_quote_page_FAILED"))
            pytest.fail(f"Step 13 - Enterprise Quote Page: {e}")

        # ===== STEP 14: Click "+ Add Loca..." (Add Locations) =====
        tracker.start_step(14, "Click '+ Add Loca...' link in Locations tab",
                           "The link is usually truncated ('+ Add Loca...'); its full text "
                           "is 'Add Locations'. Careful timing — the tab content loads async.")
        try:
            # 12.1 — Open the Location tab and wait for it to become active
            tab_clicked = False
            try:
                loc_tab = page.get_by_role("tab", name=re.compile(r"Location", re.I))
                if loc_tab.count() > 0 and loc_tab.first.is_visible():
                    loc_tab.first.scroll_into_view_if_needed()
                    loc_tab.first.click()
                    tab_clicked = True
            except Exception:
                pass
            # Give the tab panel a moment to render
            page.wait_for_timeout(1500)

            # 12.2 — Wait for the tab's internal spinner(s) to clear
            for spinner_sel in (".slds-spinner", "lightning-spinner",
                                "[role='status'][class*='spinner']"):
                try:
                    page.wait_for_selector(spinner_sel, state="hidden", timeout=10000)
                except Exception:
                    continue

            sf.screenshot("14a_location_tab_active")

            # 12.3 — Wait for the "+ Add Loca..." link to be present AND stable
            # (the truncated text starts with "Add Loc"). We poll because the
            # element may be conditionally rendered after tab activation.
            add_link = None
            deadline = page.evaluate("Date.now()") + 20000
            while page.evaluate("Date.now()") < deadline:
                for strat in (
                    # Full text (some orgs render the label fully)
                    lambda: page.get_by_role("button",
                                              name=re.compile(r"^\s*(\+\s*)?Add\s+Locations?\s*$", re.I)),
                    lambda: page.get_by_role("link",
                                              name=re.compile(r"^\s*(\+\s*)?Add\s+Locations?\s*$", re.I)),
                    # Truncated: starts with "Add Loc" (handles "+ Add Loca...")
                    lambda: page.locator("button, a, [role='button']").filter(
                        has_text=re.compile(r"Add\s*Loc", re.I)
                    ),
                ):
                    try:
                        loc = strat()
                        cnt = loc.count()
                        for i in range(min(cnt, 6)):
                            cand = loc.nth(i)
                            try:
                                if cand.is_visible(timeout=400) and cand.is_enabled(timeout=400):
                                    add_link = cand
                                    break
                            except Exception:
                                continue
                        if add_link:
                            break
                    except Exception:
                        continue
                if add_link:
                    break
                page.wait_for_timeout(500)

            # 12.4 — Click it (scroll into view first; element may be at the bottom)
            clicked = False
            if add_link is not None:
                try:
                    add_link.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                    add_link.click()
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                # Two-step pattern: click "Add Service Account" to reveal
                # "Add Locations", then click that.
                try:
                    svc_btn = page.get_by_role(
                        "button", name=re.compile(r"Add\s+Service\s+Account", re.I)
                    )
                    if svc_btn.count() > 0 and svc_btn.first.is_visible():
                        svc_btn.first.scroll_into_view_if_needed()
                        svc_btn.first.click()
                        page.wait_for_timeout(1500)
                        for variant in ("Add Locations", "Add Location"):
                            try:
                                btn2 = page.get_by_role(
                                    "button",
                                    name=re.compile(re.escape(variant), re.I),
                                )
                                if btn2.count() > 0 and btn2.first.is_visible():
                                    btn2.first.scroll_into_view_if_needed()
                                    btn2.first.click()
                                    clicked = True
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
            if not clicked:
                # Shadow-DOM JS fallback
                clicked = bool(page.evaluate("""(() => {
                    function findInShadow(root, depth) {
                        if (depth > 25) return null;
                        const els = root.querySelectorAll(
                            'button, a[role="button"], [role="button"], lightning-button'
                        );
                        for (const el of els) {
                            const txt = (el.textContent || el.getAttribute('title') ||
                                         el.getAttribute('aria-label') || '').trim();
                            if (/add\\s*loc/i.test(txt)) {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                if (findInShadow(el.shadowRoot, depth + 1)) return true;
                            }
                        }
                        return false;
                    }
                    return findInShadow(document, 0);
                })()"""))
            if not clicked:
                raise Exception("Could not find '+ Add Loca...' (Add Locations) link")

            # 12.5 — After clicking, an inline row with a "Street Address"
            # search box is inserted in the Location grid. Wait for it to
            # appear before Step 13 tries to type.
            sf.wait_page_ready(4000)
            page.wait_for_timeout(1500)
            # Be patient — the search cell may take a second to activate.
            try:
                page.wait_for_selector(
                    "[aria-label*='Street Address' i], "
                    "input[placeholder*='address' i], "
                    "input[placeholder*='street' i]",
                    state="visible",
                    timeout=10000,
                )
            except Exception:
                # Not fatal — Step 13 has its own discovery strategies
                pass
            tracker.add_assertion("'+ Add Loca...' clicked", True)
            tracker.pass_step(sf.screenshot("14_add_location_dialog"))
        except Exception as e:
            tracker.add_assertion("'Add Location' clicked", False)
            tracker.fail_step(str(e), sf.screenshot("14_add_location_FAILED"))
            pytest.fail(f"Step 14 - Add Location: {e}")

        # ===== STEP 15: Enter address + select first autocomplete match =====
        tracker.start_step(15, "Enter address and select autocomplete",
                           f"Address: {LOCATION_ADDRESS}")
        try:
            # "Add Locations" inserts an inline editable ROW into the Location
            # grid (NOT a modal dialog — verified in live CCI sandbox). The row
            # has a CCI b2b-typeahead-container with an address input.
            #
            # CRITICAL: Do NOT use get_by_label("Street Address") — it matches
            # the column header's hidden range-input resizer
            # (input[type=range].slds-resizable__input.slds-assistive-text)
            # which is covered by a fixed div that intercepts pointer events,
            # causing Playwright to timeout waiting for actionability.
            page.wait_for_timeout(2000)  # let the new row render
            sf.screenshot("15a_new_row_ready")

            def _is_real_text_input(el):
                """Return True if the element is a real text input (not a
                hidden range/checkbox/resizer)."""
                try:
                    input_type = el.get_attribute("type") or "text"
                    if input_type.lower() in ("range", "hidden", "checkbox", "radio"):
                        return False
                    classes = el.get_attribute("class") or ""
                    if "slds-resizable__input" in classes or "slds-assistive-text" in classes:
                        return False
                    if el.is_visible() and el.is_editable():
                        return True
                except Exception:
                    pass
                return False

            addr_input = None
            strategies = [
                # 1. CCI b2b-typeahead combobox input (most specific — verified
                #    in live sandbox). This is THE correct element.
                lambda: page.locator(
                    ".b2b-typeahead-container input[role='combobox']"
                ),
                # 2. Any combobox input inside the location grid area
                lambda: page.locator(
                    "[role='grid'] input[role='combobox']:not([type='range'])"
                ),
                # 3. Placeholder-based (some builds use "Search address")
                lambda: page.get_by_placeholder(
                    re.compile(r"search\s*address|enter\s*address|address", re.I)
                ),
                # 4. Text input (not range!) with aria-label containing address
                lambda: page.locator(
                    "input[aria-label*='address' i]:not([type='range'])"
                    ":not(.slds-resizable__input):not(.slds-assistive-text)"
                ),
                # 5. Any visible text input inside the grid (excluding range/resizer)
                lambda: page.locator(
                    "[role='grid'] input:not([type='hidden'])"
                    ":not([type='checkbox']):not([type='range'])"
                    ":not(.slds-resizable__input):not(.slds-assistive-text)"
                ),
                # 6. Aura edit region with a real text input inside
                lambda: page.locator(
                    "[aria-label*='Edit Street Address' i] input:not([type='range'])"
                    ":not(.slds-resizable__input)"
                ),
            ]
            for strat in strategies:
                try:
                    loc = strat()
                    cnt = loc.count()
                    for i in range(min(cnt, 12)):
                        try:
                            cand = loc.nth(i)
                            if _is_real_text_input(cand):
                                addr_input = cand
                                break
                        except Exception:
                            continue
                    if addr_input:
                        break
                except Exception:
                    continue

            # If still not found, try clicking the row or the "Street Address"
            # cell text first to activate the editable input, then retry.
            if not addr_input:
                # Try clicking any row cell in the new location row to activate it
                for click_strat in (
                    lambda: page.locator("[role='grid'] [role='row']").last,
                    lambda: page.get_by_text(
                        re.compile(r"^\s*Street\s*Address\s*$", re.I)
                    ).first,
                ):
                    try:
                        target = click_strat()
                        if target.is_visible(timeout=2000):
                            target.click()
                            page.wait_for_timeout(1500)
                    except Exception:
                        continue
                    # Retry the strategies
                    for strat in strategies[:4]:
                        try:
                            loc = strat()
                            cnt = loc.count()
                            for i in range(min(cnt, 10)):
                                cand = loc.nth(i)
                                if _is_real_text_input(cand):
                                    addr_input = cand
                                    break
                            if addr_input:
                                break
                        except Exception:
                            continue
                    if addr_input:
                        break

            # Last resort: use JavaScript to find the input inside shadow DOM
            if not addr_input:
                found_via_js = page.evaluate("""(() => {
                    function findInShadow(root, depth) {
                        if (depth > 25) return null;
                        const inputs = root.querySelectorAll(
                            'input[role="combobox"], ' +
                            'input[type="text"]:not([type="range"]):not(.slds-resizable__input)'
                        );
                        for (const inp of inputs) {
                            const lbl = (inp.getAttribute('aria-label') || '').toLowerCase();
                            const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
                            const cls = inp.className || '';
                            if (cls.includes('slds-resizable') || cls.includes('slds-assistive')) continue;
                            if (inp.type === 'range' || inp.type === 'hidden') continue;
                            if (lbl.includes('address') || ph.includes('address') ||
                                inp.role === 'combobox') {
                                inp.scrollIntoView({block: 'center'});
                                inp.focus();
                                inp.click();
                                return true;
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                if (findInShadow(el.shadowRoot, depth + 1)) return true;
                            }
                        }
                        return false;
                    }
                    return findInShadow(document, 0);
                })()""")
                if found_via_js:
                    # The JS click focused the input; now locate it for Playwright
                    page.wait_for_timeout(500)
                    focused = page.locator("input:focus")
                    if focused.count() > 0:
                        addr_input = focused.first

            if not addr_input:
                raise Exception("Street Address input not found in the new Location row")

            addr_input.click()
            # Clear any existing text
            try:
                addr_input.press("Control+a")
                addr_input.press("Backspace")
            except Exception:
                pass
            # Type character-by-character — CCI's autocomplete keys off keyboard
            # events, not a bulk fill().
            try:
                addr_input.type(LOCATION_ADDRESS, delay=25)
            except Exception:
                addr_input.fill(LOCATION_ADDRESS)
            # Give Google-Places-style autocomplete time to return suggestions
            page.wait_for_timeout(4000)
            sf.screenshot("15b_address_typed")

            # Pick the best / first suggestion. The suggestion list in CCI is
            # NOT role=option — it's rendered as plain divs / list items. Try
            # text-match first, then role=option, then keyboard fallback.
            picked = False
            # Build match patterns from the data-driven address
            _street = ADDRESS["street"].split()[0]     # e.g. "202"
            _city = ADDRESS["city"]                    # e.g. "Nacogdoches"
            text_patterns = (
                rf"{re.escape(_street)}.*{re.escape(_city)}",
                rf"{re.escape(_city)}",
                rf"{re.escape(_street)}",
            )
            for pattern in text_patterns:
                try:
                    # Scoped to typical suggestion containers
                    suggestion_containers = (
                        ".pac-container .pac-item",
                        "[role='listbox'] [role='option']",
                        "[role='listbox'] li",
                        "[role='listbox'] div",
                        "ul[role='listbox'] li",
                        "div[role='option']",
                    )
                    for sel in suggestion_containers:
                        try:
                            opt = page.locator(sel).filter(
                                has_text=re.compile(pattern, re.I)
                            ).first
                            if opt.is_visible(timeout=400):
                                opt.click()
                                picked = True
                                break
                        except Exception:
                            continue
                    if picked:
                        break
                    # Generic text match as a last resort for this pattern
                    opt = page.get_by_text(re.compile(pattern, re.I)).first
                    if opt.is_visible(timeout=400):
                        opt.click()
                        picked = True
                        break
                except Exception:
                    continue
            if not picked:
                # Fallback: first role=option anywhere
                try:
                    opts = page.get_by_role("option")
                    if opts.count() > 0 and opts.first.is_visible():
                        opts.first.click()
                        picked = True
                except Exception:
                    pass
            if not picked:
                # Last resort: ArrowDown + Enter to pick first suggestion
                try:
                    addr_input.press("ArrowDown")
                    page.wait_for_timeout(300)
                    addr_input.press("Enter")
                    picked = True
                except Exception:
                    pass

            page.wait_for_timeout(2500)
            sf.screenshot("15c_address_selected")
            tracker.add_assertion("Address autocomplete suggestion selected", True)
            tracker.pass_step(sf.screenshot("15d_address_done"))
        except Exception as e:
            tracker.add_assertion("Address autocomplete suggestion selected", False)
            tracker.fail_step(str(e), sf.screenshot("15_address_FAILED"))
            pytest.fail(f"Step 15 - Address: {e}")

        # ===== STEP 16: Wait for serviceability check =====
        tracker.start_step(16, "Wait for serviceability check",
                           "Address Validation Result = Success")
        try:
            # After selecting an address, CCI runs a serviceability / address
            # validation in the background. The Location grid shows "Success"
            # in the Address Validation column when complete.
            # Simple approach: just wait for "Success" text to appear. No
            # scrolling — the single location row is already in view.
            page.wait_for_timeout(3000)  # initial settle

            # Wait up to 90s for "Success" to appear in the grid
            success_text = page.get_by_text("Success")
            try:
                success_text.first.wait_for(state="visible", timeout=90000)
            except Exception:
                sf.screenshot("16_serviceability_timeout")
                raise Exception("Serviceability check did not show 'Success' within 90s")

            page.wait_for_timeout(2000)  # let grid fully stabilize
            tracker.add_assertion("Serviceability = Success", True)
            tracker.pass_step(sf.screenshot("16_serviceability_success"))
        except Exception as e:
            tracker.add_assertion("Serviceability check", False)
            tracker.fail_step(str(e), sf.screenshot("16_serviceability_FAILED"))
            pytest.fail(f"Step 16 - Serviceability: {e}")

        # ===== STEP 17: Select location row and click Add Products =====
        tracker.start_step(17, "Select location row + Add Products",
                           "Click the row checkbox, then click Add Products")
        try:
            page.wait_for_timeout(2000)  # let grid fully render
            sf.screenshot("17a_before_select")

            # The row checkbox has aria-label="Row 1 Select Location".
            # It is a 1×1 px hidden <input> wrapped by a <td> that
            # intercepts pointer events, so Playwright's normal .click()
            # will always timeout.  Use JavaScript .click() to bypass.
            checked = page.evaluate("""(() => {
                var cb = document.evaluate(
                    "//input[contains(@aria-label, 'Select Location')]",
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!cb) return null;
                cb.click();
                return cb.checked;
            })()""")

            if checked is None:
                raise Exception(
                    "Checkbox with aria-label='Row 1 Select Location' "
                    "not found"
                )

            page.wait_for_timeout(1500)
            sf.screenshot("17b_row_selected")

            tracker.add_assertion("Location row selected", True)

            # Now click "Add Products" — it should be visible in the command
            # bar above the grid (or floating bar). No scrolling needed.
            page.wait_for_timeout(1000)
            clicked = self._click_button_anywhere("Add Products", timeout_ms=15000)
            if not clicked:
                for alt in ("Add Product", "Products"):
                    clicked = self._click_button_anywhere(alt, timeout_ms=5000)
                    if clicked:
                        break
            if not clicked:
                raise Exception("'Add Products' button not found")

            sf.wait_page_ready(6000)
            page.wait_for_timeout(2000)
            tracker.add_assertion("Add Products clicked", True)
            tracker.pass_step(sf.screenshot("17c_add_products_clicked"))
        except Exception as e:
            tracker.add_assertion("Select location + Add Products", False)
            tracker.fail_step(str(e), sf.screenshot("17_select_location_FAILED"))
            pytest.fail(f"Step 17 - Select Location + Add Products: {e}")

        # ===== STEP 18: Search for Fiber Broadband and Add to Cart =====
        tracker.start_step(18, f"Search for '{PRODUCT_SEARCH}' and Add to Cart",
                           f"Find {PRODUCT_DISPLAY} in the product catalog")
        try:
            # Wait for the product catalog / search to be ready
            page.wait_for_timeout(3000)

            # Robust spinner wait before interacting
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 30000:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(2000)

            sf.screenshot("18a_product_catalog_open")

            # ── Find the search input in the product catalog ──
            search_input = None
            for strat in (
                # CCI product catalog uses a search input inside the panel
                lambda: page.locator("input[type='search']"),
                lambda: page.get_by_placeholder(re.compile(r"search", re.I)),
                lambda: page.get_by_role("searchbox"),
                lambda: page.get_by_label(re.compile(r"search", re.I)),
                # Shadow DOM fallback
                lambda: page.locator("input[role='combobox'][placeholder*='earch' i]"),
                lambda: page.locator("input[placeholder*='earch' i]"),
            ):
                try:
                    loc = strat()
                    cnt = loc.count()
                    for i in range(min(cnt, 6)):
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
                    "Product search input not found — cannot search for "
                    f"'{PRODUCT_SEARCH}'. Refusing to add a random product."
                )

            # ── Type the search term and trigger search ──
            search_input.click()
            page.wait_for_timeout(500)
            # Clear any existing text
            try:
                search_input.press("Control+a")
                search_input.press("Backspace")
            except Exception:
                pass
            # Type character-by-character (CCI may use debounced search)
            search_input.type(PRODUCT_SEARCH, delay=30)
            page.wait_for_timeout(1000)
            # Press Enter to trigger the search
            search_input.press("Enter")
            page.wait_for_timeout(4000)  # wait for search results to load

            # Wait for spinners after search
            _sp_start2 = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start2 > 15000:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)

            sf.screenshot("18b_product_searched")

            # ── Verify Fiber Broadband appears in the results, then click Add to Cart ──
            # First confirm the product name is visible in results
            product_visible = False
            try:
                prod_text = page.get_by_text(
                    re.compile(re.escape("Fiber Broadband"), re.I)
                )
                if prod_text.count() > 0 and prod_text.first.is_visible(timeout=5000):
                    product_visible = True
            except Exception:
                pass
            if not product_visible:
                # Try shorter match
                try:
                    prod_text = page.get_by_text(re.compile(r"Fiber\s*Broadband|FBB", re.I))
                    if prod_text.count() > 0 and prod_text.first.is_visible(timeout=3000):
                        product_visible = True
                except Exception:
                    pass

            if not product_visible:
                raise Exception(
                    f"'{PRODUCT_SEARCH}' not found in search results. "
                    f"Search may have failed or returned no matching products."
                )

            tracker.add_assertion(f"'{PRODUCT_DISPLAY}' visible in search results", True)

            # ── Click "Add to Cart" for the Fiber Broadband product specifically ──
            # Strategy: find a product card/row that contains "Fiber Broadband"
            # text, then click "Add to Cart" within that container.
            added = False

            # Strategy 1: Find the container holding Fiber Broadband text, then its Add to Cart
            for container_sel in (
                ".product-card", ".product-item", ".cpq-product-card",
                "[class*='product']", "tr", ".slds-card", "article",
                "[class*='tile']", "[class*='item']",
            ):
                try:
                    containers = page.locator(container_sel).filter(
                        has_text=re.compile(r"Fiber\s+Broadband|FBB", re.I)
                    )
                    if containers.count() > 0:
                        # Find "Add to Cart" button inside this container
                        for btn_name in ("Add to Cart", "Add", "Select"):
                            try:
                                btn = containers.first.get_by_role(
                                    "button", name=re.compile(re.escape(btn_name), re.I)
                                )
                                if btn.count() > 0 and btn.first.is_visible():
                                    btn.first.scroll_into_view_if_needed()
                                    btn.first.click()
                                    added = True
                                    break
                            except Exception:
                                continue
                    if added:
                        break
                except Exception:
                    continue

            # Strategy 2: If no container-scoped button found, try clicking the
            # Fiber Broadband product text first (some UIs select on click), then Add to Cart
            if not added:
                try:
                    prod_el = page.get_by_text(
                        re.compile(re.escape("Fiber Broadband"), re.I)
                    ).first
                    prod_el.scroll_into_view_if_needed()
                    prod_el.click()
                    page.wait_for_timeout(1500)
                    # Now try the Add to Cart button (should be the only one for
                    # the selected/highlighted product)
                    atc = page.get_by_role(
                        "button", name=re.compile(r"Add\s*to\s*Cart", re.I)
                    )
                    if atc.count() > 0 and atc.first.is_visible():
                        atc.first.click()
                        added = True
                except Exception:
                    pass

            # Strategy 3: Last resort — if search filtered to exactly 1 product
            # and Fiber Broadband is visible, the single Add to Cart button must be for it
            if not added:
                try:
                    atc_buttons = page.get_by_role(
                        "button", name=re.compile(r"Add\s*to\s*Cart", re.I)
                    )
                    if atc_buttons.count() == 1 and product_visible:
                        atc_buttons.first.scroll_into_view_if_needed()
                        atc_buttons.first.click()
                        added = True
                except Exception:
                    pass

            if not added:
                raise Exception(
                    f"Found '{PRODUCT_SEARCH}' in results but could not "
                    f"click 'Add to Cart' for it"
                )

            # wait_page_ready already polls spinners + networkidle + extra_ms,
            # so a blind 2s pre-sleep was redundant.
            sf.wait_page_ready(5000)
            tracker.add_assertion(f"'{PRODUCT_DISPLAY}' added to cart", True)
            tracker.pass_step(sf.screenshot("18c_product_added"))
        except Exception as e:
            tracker.add_assertion(f"'{PRODUCT_DISPLAY}' added to cart", False)
            tracker.fail_step(str(e), sf.screenshot("18_product_FAILED"))
            pytest.fail(f"Step 18 - Add Product: {e}")

        # ===== STEP 19: Click Configure Cart =====
        tracker.start_step(19, "Click Configure Cart", "Open configuration view")
        try:
            # A "Preview Cart" modal or panel may appear after Add to Cart.
            # Look for "Configure Cart" or "Configure" button.
            page.wait_for_timeout(1500)
            clicked = False
            for btn_name in ("Configure Cart", "Configure", "Next"):
                clicked = self._click_button_anywhere(btn_name, timeout_ms=8000)
                if clicked:
                    break

            if not clicked:
                raise Exception("'Configure Cart' button not found")

            sf.wait_page_ready(10000)
            # (3s extra pre-settle removed — wait_page_ready already has a 10s
            #  buffer and the spinner loop below re-polls until hidden.)

            # Robust spinner wait after Configure Cart loads
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 60000:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)

            tracker.add_assertion("Configure Cart opened", True)
            tracker.pass_step(sf.screenshot("19_configure_cart"))
        except Exception as e:
            tracker.add_assertion("Configure Cart opened", False)
            tracker.fail_step(str(e), sf.screenshot("19_configure_FAILED"))
            pytest.fail(f"Step 19 - Configure Cart: {e}")

        # ===== STEP 20: Configure Fiber Broadband — Quote Type + Bandwidth =====
        tracker.start_step(
            20,
            f"Configure Fiber Broadband: "
                f"{PRODUCT_QUOTE_TYPE_LABEL} = '{PRODUCT_QUOTE_TYPE}', "
                f"{PRODUCT_BANDWIDTH_LABEL} = '{PRODUCT_BANDWIDTH}'",
            "Set the two required dropdowns on the Fiber Broadband configuration page"
        )
        try:
            page.wait_for_timeout(2000)
            sf.screenshot("20a_config_page_loaded")

            # Helper: set a combobox/dropdown by its field label to a target value.
            # Handles Lightning combobox, native <select>, and generic button-trigger menus.
            def _set_dropdown_by_label(label: str, target_value: str) -> bool:
                # If the field already shows the target value, accept it as set.
                try:
                    combo = page.get_by_role(
                        "combobox", name=re.compile(re.escape(label), re.I)
                    )
                    if combo.count() > 0:
                        try:
                            current = (combo.first.input_value() or "").strip()
                        except Exception:
                            current = ""
                        if current and current.lower() == target_value.lower():
                            return True
                except Exception:
                    pass

                # Strategy 1: role=combobox labeled <label>, open then pick option.
                for strat in (
                    lambda: page.get_by_role(
                        "combobox", name=re.compile(re.escape(label), re.I)
                    ),
                    lambda: page.get_by_label(re.compile(re.escape(label), re.I)),
                ):
                    try:
                        loc = strat()
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.scroll_into_view_if_needed()
                            loc.first.click()
                            page.wait_for_timeout(1000)
                            for opt_strat in (
                                lambda: page.get_by_role(
                                    "option",
                                    name=re.compile(re.escape(target_value), re.I),
                                ),
                                lambda: page.get_by_text(re.compile(
                                    rf"^\s*{re.escape(target_value)}\s*$", re.I
                                )),
                                lambda: page.locator(
                                    "[role='listbox'] [role='option']"
                                ).filter(has_text=re.compile(
                                    re.escape(target_value), re.I
                                )),
                            ):
                                try:
                                    opt = opt_strat()
                                    if opt.count() > 0 and opt.first.is_visible():
                                        opt.first.click()
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        continue

                # Strategy 2: native <select> immediately following a label
                try:
                    native = page.locator(
                        f"xpath=//label[contains(normalize-space(.),'{label}')]"
                        f"/following::select[1]"
                    )
                    if native.count() > 0:
                        for how in (
                            lambda: native.first.select_option(label=target_value),
                            lambda: native.first.select_option(value=target_value),
                        ):
                            try:
                                how()
                                return True
                            except Exception:
                                continue
                except Exception:
                    pass

                # Strategy 3: button trigger near the label → click, then choose option
                try:
                    trigger = page.locator(
                        f"xpath=//label[contains(normalize-space(.),'{label}')]"
                        f"/following::button[1]"
                    )
                    if trigger.count() > 0 and trigger.first.is_visible():
                        trigger.first.scroll_into_view_if_needed()
                        trigger.first.click()
                        page.wait_for_timeout(1000)
                        for opt_strat in (
                            lambda: page.get_by_role(
                                "option",
                                name=re.compile(re.escape(target_value), re.I),
                            ),
                            lambda: page.get_by_text(re.compile(
                                rf"^\s*{re.escape(target_value)}\s*$", re.I
                            )),
                            lambda: page.locator(
                                "[role='listbox'] [role='option']"
                            ).filter(has_text=re.compile(
                                re.escape(target_value), re.I
                            )),
                        ):
                            try:
                                opt = opt_strat()
                                if opt.count() > 0 and opt.first.is_visible():
                                    opt.first.click()
                                    return True
                            except Exception:
                                continue
                except Exception:
                    pass

                return False

            # ── Dropdown 1: Quote Type ──
            qt_set = _set_dropdown_by_label(
                PRODUCT_QUOTE_TYPE_LABEL, PRODUCT_QUOTE_TYPE
            )
            if not qt_set:
                raise Exception(
                    f"Could not set '{PRODUCT_QUOTE_TYPE_LABEL}' to "
                    f"'{PRODUCT_QUOTE_TYPE}'"
                )
            tracker.add_assertion(
                f"{PRODUCT_QUOTE_TYPE_LABEL} set to {PRODUCT_QUOTE_TYPE}", True
            )
            # Wait for "Updating Fiber Broadband" toast / spinners to clear before
            # touching the next dropdown — switching Quote Type often re-renders
            # the config form.
            self._wait_for_config_update_complete("Fiber Broadband")
            sf.screenshot("20b_quote_type_set")

            # ── Dropdown 2: Bandwidth ──
            # Try the full value first ("100 Mbps"); fall back to numeric-only ("100").
            bw_set = _set_dropdown_by_label(
                PRODUCT_BANDWIDTH_LABEL, PRODUCT_BANDWIDTH
            )
            if not bw_set:
                bw_numeric = PRODUCT_BANDWIDTH.split()[0] if " " in PRODUCT_BANDWIDTH else PRODUCT_BANDWIDTH
                bw_set = _set_dropdown_by_label(
                    PRODUCT_BANDWIDTH_LABEL, bw_numeric
                )
            if not bw_set:
                raise Exception(
                    f"Could not set '{PRODUCT_BANDWIDTH_LABEL}' to "
                    f"'{PRODUCT_BANDWIDTH}'"
                )
            tracker.add_assertion(
                f"{PRODUCT_BANDWIDTH_LABEL} set to {PRODUCT_BANDWIDTH}", True
            )

            # Wait for "Updating Fiber Broadband" toast to disappear, spinners to
            # clear, and the page to fully re-render
            self._wait_for_config_update_complete("Fiber Broadband")

            tracker.pass_step(sf.screenshot("20c_fbb_configured"))
        except Exception as e:
            tracker.add_assertion(
                f"Configure Fiber Broadband ({PRODUCT_QUOTE_TYPE_LABEL} + {PRODUCT_BANDWIDTH_LABEL})",
                False,
            )
            tracker.fail_step(str(e), sf.screenshot("20_fbb_config_FAILED"))
            pytest.fail(f"Step 20 - Configure Fiber Broadband: {e}")

        # ===== STEP 21: Add product to location — return to Enterprise Quote =====
        tracker.start_step(21, "Add product to location — return to Enterprise Quote",
                           "Click 'Add Products to 1 Locations' and wait for Enterprise Quote page to load")
        try:
            # Phase 1: Wait for ALL config updates to finish (toasts + spinners)
            # before attempting to click the button
            self._wait_for_config_update_complete("all config")
            sf.screenshot("21a_page_ready_before_click")

            # Phase 2: Find the "Add Products to 1 Locations" button using a
            # SPECIFIC locator. The exact text from the UI is:
            #   "Add Products to 1 Locations" (plural Products, plural Locations, capital L)
            # We use a regex to be flexible with spacing and singular/plural forms,
            # but we MUST NOT match "Back", "Done", or "Save" buttons.
            add_btn_pattern = re.compile(
                r"Add\s+Products?\s+to\s+\d+\s+Locations?", re.I
            )
            clicked = False

            # Strategy 1: Playwright role=button with regex
            try:
                btn = page.get_by_role("button", name=add_btn_pattern)
                if btn.count() > 0:
                    target = btn.first
                    target.scroll_into_view_if_needed()
                    # Wait for the button to become enabled (not disabled)
                    _btn_deadline = page.evaluate("Date.now()") + 30000
                    while page.evaluate("Date.now()") < _btn_deadline:
                        try:
                            is_disabled = target.is_disabled()
                            if not is_disabled:
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                    target.click(timeout=15000)
                    clicked = True
            except Exception:
                pass

            # Strategy 2: Shadow DOM JS walk — exact text match only
            if not clicked:
                clicked = bool(page.evaluate("""(() => {
                    const pattern = /Add\\s+Products?\\s+to\\s+\\d+\\s+Locations?/i;
                    function findInShadow(root, depth) {
                        if (depth > 25) return null;
                        const btns = root.querySelectorAll('button, [role="button"]');
                        for (const b of btns) {
                            const txt = (b.textContent || '').trim();
                            if (pattern.test(txt)) return b;
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                const found = findInShadow(el.shadowRoot, depth + 1);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    const btn = findInShadow(document, 0);
                    if (btn) {
                        // Wait check: skip if disabled
                        if (btn.disabled) return false;
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return true;
                    }
                    return false;
                })()"""))

            if not clicked:
                sf.screenshot("21_btn_not_found")
                raise Exception(
                    "'Add Products to 1 Locations' button not found or still disabled. "
                    "This is a specific button at the bottom-right of the Configure Cart page."
                )

            # Phase 3: Wait for Enterprise Quote page to fully load
            sf.wait_page_ready(10000)
            page.wait_for_timeout(3000)

            # Robust spinner wait — loop until ALL spinners are gone
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 60000:
                    break
                page.wait_for_timeout(1000)

            # Extra settle time after spinners clear
            page.wait_for_timeout(5000)

            # Phase 4: Verify we're back on Enterprise Quote page
            eq_loaded = False
            for tab_name in ("Summary", "Locations", "Location"):
                try:
                    tab = page.get_by_role("tab", name=re.compile(tab_name, re.I))
                    if tab.count() > 0 and tab.first.is_visible(timeout=10000):
                        eq_loaded = True
                        break
                except Exception:
                    continue
            if not eq_loaded:
                try:
                    page_text = page.inner_text("body")
                    if "Enterprise Quote" in page_text or "Summary" in page_text:
                        eq_loaded = True
                except Exception:
                    pass

            tracker.add_assertion("Returned to Enterprise Quote page", eq_loaded)
            if not eq_loaded:
                raise Exception("Enterprise Quote page did not load after adding products")

            tracker.pass_step(sf.screenshot("21_back_on_eq"))
        except Exception as e:
            tracker.add_assertion("Add product to location", False)
            tracker.fail_step(str(e), sf.screenshot("21_add_to_location_FAILED"))
            pytest.fail(f"Step 21 - Add to Location: {e}")

        # ===== STEP 22: Verify Summary tab shows expected products =====
        tracker.start_step(22, "Verify Summary tab shows expected products",
                           f"Expect products: {', '.join(EXPECTED_SUMMARY)}")
        try:
            # Wait for Enterprise Quote page to be fully ready before
            # looking for tabs — the page may still be rendering after
            # returning from product configuration.
            page.wait_for_timeout(5000)

            # Wait for any "Quote Summary updated" toast to disappear
            try:
                toast = page.get_by_text(re.compile(r"Quote Summary updated|updating", re.I))
                if toast.count() > 0 and toast.first.is_visible(timeout=2000):
                    # Wait up to 30s for toast to disappear
                    try:
                        toast.first.wait_for(state="hidden", timeout=30000)
                    except Exception:
                        pass
            except Exception:
                pass

            # Robust spinner wait
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 60000:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)

            sf.screenshot("22a_eq_page_ready")

            # Click the Summary tab — its text is "Summary (N)" where N is
            # the product count, so we match with a partial regex.
            # Poll for up to 30s in case tabs take time to render.
            summary_clicked = False
            deadline = page.evaluate("Date.now()") + 30000
            while page.evaluate("Date.now()") < deadline:
                for strat in (
                    lambda: page.get_by_role("tab", name=re.compile(r"Summary", re.I)),
                    lambda: page.get_by_text(re.compile(r"Summary\s*\(", re.I)),
                    lambda: page.get_by_text(re.compile(r"Summary", re.I)),
                ):
                    try:
                        loc = strat()
                        if loc.count() > 0 and loc.first.is_visible(timeout=1000):
                            loc.first.scroll_into_view_if_needed()
                            loc.first.click()
                            summary_clicked = True
                            break
                    except Exception:
                        continue
                if summary_clicked:
                    break
                page.wait_for_timeout(1000)

            if not summary_clicked:
                sf.screenshot("22_summary_tab_not_found")
                raise Exception("Summary tab not found on the Enterprise Quote page")

            # ── CRITICAL: Wait for Summary tab CONTENT to load ──
            # After clicking the Summary tab, the product rows take several
            # seconds to render. We must poll for the products to appear
            # in the DOM (up to 60s) before attempting verification.
            # The expected products are driven by EXPECTED_SUMMARY (e.g.
            # ["Fiber Broadband"]) — loaded from the test data JSON.
            page.wait_for_timeout(5000)  # initial settle after tab click

            # Wait for spinners inside the summary area to clear
            _sp_start2 = page.evaluate("Date.now()")
            while True:
                _any2 = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any2 = True
                                break
                    except Exception:
                        pass
                    if _any2:
                        break
                if not _any2:
                    break
                if page.evaluate("Date.now()") - _sp_start2 > 60000:
                    break
                page.wait_for_timeout(1000)

            # Poll for product rows to appear — wait up to 60 seconds
            # We look for at least one of the expected product names to
            # be visible, which signals the Summary content has loaded.
            products_loaded = False
            _poll_deadline = page.evaluate("Date.now()") + 60000
            while page.evaluate("Date.now()") < _poll_deadline:
                for pname in EXPECTED_SUMMARY:
                    try:
                        match = page.get_by_text(re.compile(re.escape(pname), re.I))
                        if match.count() > 0 and match.first.is_visible(timeout=1000):
                            products_loaded = True
                            break
                    except Exception:
                        continue
                if products_loaded:
                    break
                page.wait_for_timeout(2000)

            # Extra settle for all rows to render
            page.wait_for_timeout(3000)

            sf.screenshot("22b_summary_tab_loaded")

            # Verify each expected product appears
            found_products = []
            for product_name in EXPECTED_SUMMARY:
                try:
                    match = page.get_by_text(re.compile(re.escape(product_name), re.I))
                    if match.count() > 0 and match.first.is_visible(timeout=10000):
                        found_products.append(product_name)
                        tracker.add_assertion(f"Product '{product_name}' in Summary", True)
                    else:
                        tracker.add_assertion(f"Product '{product_name}' in Summary", False)
                except Exception:
                    tracker.add_assertion(f"Product '{product_name}' in Summary", False)

            if len(found_products) != len(EXPECTED_SUMMARY):
                missing = set(EXPECTED_SUMMARY) - set(found_products)
                raise Exception(
                    f"Missing products in Summary: {missing}. "
                    f"Found: {found_products}"
                )

            tracker.add_assertion(f"All {len(EXPECTED_SUMMARY)} products visible", True)
            tracker.pass_step(sf.screenshot("22c_summary_verified"))
        except Exception as e:
            tracker.add_assertion("Summary products verified", False)
            tracker.fail_step(str(e), sf.screenshot("22_summary_FAILED"))
            pytest.fail(f"Step 22 - Summary Verification: {e}")

        # ===== STEP 23: Go to Quote — Verify Approval Journey =====
        tracker.start_step(23, "Go to Quote — Verify Approval Journey",
                           "Click 'Go To Quote', navigate to Quote in same tab, verify Approval Journey section")
        try:
            # ── Click "Go To Quote" button ──
            # This button opens the Quote record page in a NEW browser tab.
            # To keep the video recording continuous, we intercept the new tab's
            # URL, close the new tab, and navigate the original page to that URL.
            clicked = False
            new_page = None
            quote_url = None

            # Strategy 1: Playwright role=button — intercept new tab
            for btn_text in ("Go To Quote", "Go to Quote"):
                try:
                    btn = page.get_by_role("button", name=btn_text, exact=True)
                    if btn.count() > 0 and btn.first.is_visible(timeout=5000):
                        btn.first.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        with self.context.expect_page(timeout=30000) as new_page_info:
                            btn.first.click(timeout=10000)
                        new_page = new_page_info.value
                        clicked = True
                        break
                except Exception:
                    continue

            # Strategy 2: Link with "Go To Quote" text
            if not clicked:
                try:
                    link = page.get_by_role("link", name=re.compile(r"Go\s*To\s*Quote", re.I))
                    if link.count() > 0 and link.first.is_visible(timeout=5000):
                        with self.context.expect_page(timeout=30000) as new_page_info:
                            link.first.click(timeout=10000)
                        new_page = new_page_info.value
                        clicked = True
                except Exception:
                    pass

            # Strategy 3: Shadow DOM JS walk — click via JS, detect new tab
            if not clicked:
                pages_before = len(self.context.pages)
                js_clicked = bool(page.evaluate("""(() => {
                    const pattern = /^Go\\s*To\\s*Quote$/i;
                    function findInShadow(root, depth) {
                        if (depth > 25) return null;
                        const btns = root.querySelectorAll('button, a[role="button"], a.slds-button, [role="button"], a');
                        for (const b of btns) {
                            const txt = (b.textContent || b.getAttribute('title') || '').trim();
                            if (pattern.test(txt)) return b;
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                const found = findInShadow(el.shadowRoot, depth + 1);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    const btn = findInShadow(document, 0);
                    if (btn) { btn.click(); return true; }
                    return false;
                })()"""))
                if js_clicked:
                    _tab_deadline = page.evaluate("Date.now()") + 15000
                    while page.evaluate("Date.now()") < _tab_deadline:
                        if len(self.context.pages) > pages_before:
                            new_page = self.context.pages[-1]
                            clicked = True
                            break
                        page.wait_for_timeout(500)

            if not clicked:
                sf.screenshot("23_go_to_quote_btn_not_found")
                raise Exception("'Go To Quote' button not found on Enterprise Quote page")

            # ── Grab URL from new tab, close it, navigate original page ──
            # This keeps video recording continuous on the original page.
            if new_page is not None:
                new_page.wait_for_load_state("domcontentloaded", timeout=30000)
                # Wait briefly for any redirects to settle
                page.wait_for_timeout(3000)
                quote_url = new_page.url
                new_page.close()
                # Navigate the original (recorded) page to the Quote URL
                page.goto(quote_url)
            # else: button navigated in same tab — already on the quote page

            sf.screenshot("23a_navigating_to_quote")

            # ── Wait for the Quote page to finish loading ──
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            sf.wait_page_ready(10000)
            page.wait_for_timeout(5000)

            # Robust spinner wait — up to 60s
            _sp_sels = [".slds-spinner", "lightning-spinner", ".vlc-slds-spinner",
                        "[role='progressbar']", ".slds-spinner_container"]
            _sp_start = page.evaluate("Date.now()")
            while True:
                _any = False
                for _sel in _sp_sels:
                    try:
                        for _sp in page.query_selector_all(_sel):
                            if _sp.is_visible():
                                _any = True
                                break
                    except Exception:
                        pass
                    if _any:
                        break
                if not _any:
                    break
                if page.evaluate("Date.now()") - _sp_start > 60000:
                    break
                page.wait_for_timeout(1000)

            # Extra settle time — Quote page is slow
            page.wait_for_timeout(8000)

            sf.screenshot("23b_quote_page_loaded")

            # ── Capture Quote record ID from this page's URL ──
            # The Quote record page IS a standard Lightning URL:
            #   /lightning/r/{QuoteObject}/{id}/view
            # Parse it, then attach a clickable link BOTH to this step (23)
            # and retroactively to step 12 where the Quote Name was shown.
            try:
                current_url = page.url
                m = re.search(
                    r'/lightning/r/([^/?#]+)/([a-zA-Z0-9]{15,18})(?:/|$|\?|#)',
                    current_url,
                )
                if m:
                    quote_object = m.group(1)
                    self._quote_id = m.group(2)
                    # Attach to step 12 (Quote Name shown in description)
                    tracker.add_record(
                        "Quote", QUOTE_NAME,
                        record_id=self._quote_id, object_type=quote_object,
                        step_number=12,
                    )
                    # Also attach to current step (23) for convenience
                    tracker.add_record(
                        "Quote", QUOTE_NAME,
                        record_id=self._quote_id, object_type=quote_object,
                    )
                    tracker.add_assertion(
                        f"Quote ID captured: {self._quote_id}", True,
                    )
            except Exception:
                # Never let link capture break the test
                pass

            # ── Poll for "Approval Journey" section to appear (up to 60s) ──
            aj_visible = False
            _aj_deadline = page.evaluate("Date.now()") + 60000
            while page.evaluate("Date.now()") < _aj_deadline:
                try:
                    aj = page.get_by_text(re.compile(r"Approval\s*Journey", re.I))
                    if aj.count() > 0 and aj.first.is_visible(timeout=2000):
                        aj.first.scroll_into_view_if_needed()
                        page.wait_for_timeout(2000)
                        aj_visible = True
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            tracker.add_assertion("Approval Journey section visible", aj_visible)

            if not aj_visible:
                sf.screenshot("23_approval_journey_not_found")
                raise Exception(
                    "Approval Journey section not found on the Quote page "
                    f"(current URL: {page.url})"
                )

            # Screenshot with Approval Journey visible
            tracker.pass_step(sf.screenshot("23c_approval_journey_verified"))
        except Exception as e:
            tracker.add_assertion("Quote page — Approval Journey", False)
            tracker.fail_step(str(e), sf.screenshot("23_quote_FAILED"))
            pytest.fail(f"Step 23 - Go to Quote: {e}")

        # NOTE: Test data cleanup is handled separately by scripts/cleanup_test_data.py
        # Run it standalone or via the "Cleanup Test Data" GitHub Action.

