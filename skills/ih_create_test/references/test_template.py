"""TC{{TC_NUM}} — {{Human Readable Test Name}}.

Test flow:
{{STEP_LIST}}

Data file: tests/{{ui|api}}/data/tc{{TC_NUM}}_{{slug}}.json
Selectors captured from LIVE Salesforce org: {{ORG_IDENTIFIER}}
Generated: {{DATE}}

Run:      cci test tests/{{ui|api}}/test_cci_tc{{TC_NUM}}_{{slug}}.py
Headless: cci test tests/{{ui|api}}/test_cci_tc{{TC_NUM}}_{{slug}}.py --headless

Conventions to keep (don't break these):
  - Use the `sf` fixture for every Salesforce action. Helpers like
    sf.fill(label, value), sf.click(name), sf.fill_lookup(label, search),
    sf.set_picklist(label, value), sf.search_catalog(term),
    sf.add_product_to_cart(text), sf.configure_attr(label, value),
    sf.wait_page_ready(), sf.wait_for_config_update() cover almost every
    Salesforce interaction. See README.md → Library reference for the full
    catalog.
  - Use the `with sf.step(N, "label"):` context manager — it auto-handles
    tracker.start_step / pass_step / fail_step + screenshot on entry/exit.
  - Class docstring's first line = the friendly test name shown in the
    dashboard + reports.
  - `TAGS = [...]` and `OBJECTIVE = "..."` class attributes are parsed by
    the dashboard via AST to populate the info-icon popup. Placeholders
    like {addresses[0].region} get rendered against the JSON data file at
    display time.
  - NO YAML — metadata lives entirely in this .py.
  - All record names MUST include "CCIAUTO" so the cleanup script finds
    them (scripts/cleanup_test_data.py).
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page


# ── Test data ─────────────────────────────────────────────────────────
DATA = json.loads(
    (Path(__file__).parent / "data" / "tc{{TC_NUM}}_{{slug}}.json").read_text()
)


# ── Slot-aware timestamp (parallel-safe) ──────────────────────────────
#
# Copy this block verbatim into every new test. UI_TEST_SLOT is set by
# the dashboard's parallel pool (src/web/parallel_runner.py).
# PYTEST_XDIST_WORKER is set by pytest-xdist in CI. With either present
# the timestamp gets an `s0` / `s1` / `s2` / `s3` suffix that prevents
# parallel workers from generating identical CCIAUTO_Biz_… account names.

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(TZ)
_slot = (
    os.environ.get("UI_TEST_SLOT")
    or os.environ.get("PYTEST_XDIST_WORKER", "").replace("gw", "")
)
TIMESTAMP = (
    NOW.strftime("%m%d_%H%M%S")
    + f"{NOW.microsecond // 1000:03d}"
    + (f"s{_slot}" if _slot else "")
)


# ── Derived test values (read from JSON, never hardcode) ──────────────
ACCOUNT_NAME = f"{DATA['account_name_prefix']}{TIMESTAMP}"
# {{ADD_OTHER_DATA_VARS_HERE — e.g. ADDRESS = DATA["addresses"][0], etc.}}

DEFAULT_TIMEOUT = DATA.get("timeout_ms", 60000)


class Test{{PascalCaseName}}:
    """TC{{TC_NUM}} - {{Human Readable Test Name}}"""

    # ── Class-level metadata parsed by the dashboard (AST). ──
    #    Placeholders in OBJECTIVE are rendered against the JSON
    #    data file at display time.
    TAGS = ["{{tag1}}", "{{tag2}}", "smoke"]
    OBJECTIVE = (
        "{{One- or two-sentence summary of what this test validates. "
        "Use {placeholders} like {product.bandwidth} where useful.}}"
    )

    @pytest.fixture(autouse=True)
    def setup(self, page: Page, tracker, sf):
        self.page = page
        self.page.set_default_timeout(DEFAULT_TIMEOUT)
        self.tracker = tracker
        self.sf = sf
        yield

    def test_{{snake_case_method}}(self):
        page, sf = self.page, self.sf

        with sf.step(1, "Log into Salesforce"):
            sf.login()
            sf.assert_("Landed on Lightning", "lightning" in page.url.lower())

        with sf.step(2, "Navigate to Accounts and create new account"):
            sf.open_list_view("Account")
            sf.click("New")
            sf.select_record_type(DATA["record_type"])
            sf.click("Next")
            sf.wait_form_ready(["Account Name"])
            sf.fill("Account Name", ACCOUNT_NAME)
            sf.click("Save")
            sf.wait_page_ready(4000)
            sf.assert_("Account created", ACCOUNT_NAME in page.content())

        # {{ADD MORE STEPS HERE using the same `with sf.step(N, "label"):` pattern}}
        # Common building blocks:
        #
        #   with sf.step(3, "Add product to cart"):
        #       sf.search_catalog(PRODUCT["search_term"])
        #       sf.add_product_to_cart(PRODUCT["display_name"])
        #       sf.wait_for_config_update()
        #
        #   with sf.step(4, "Configure bandwidth"):
        #       sf.configure_attr("Bandwidth", PRODUCT["bandwidth"])
        #
        #   with sf.step(5, "Submit + verify"):
        #       sf.click("Submit")
        #       sf.wait_for_toast("Quote submitted", settled=True)
        #       quote_id = sf.extract_record_id(sobject="Quote")
        #       sf.assert_("Quote id captured", bool(quote_id))
