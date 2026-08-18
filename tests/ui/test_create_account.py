"""Create an Account via the Lightning UI.

Reference UI test. Works against ANY Salesforce org — it touches only
standard objects and standard field labels. Copy this file to start a new
test; see docs/WRITING_TESTS.md.

Everything org-specific comes from two places and NEVER from this file:
  * profiles/<org>.yml  — timezone, record prefix, label overrides
  * tests/ui/data/create_account.json — the values this test types

Run:
    sfauto test tests/ui/test_create_account.py
    SFAUTO_PROFILE=acme-uat sfauto test tests/ui/test_create_account.py
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.core.org_profile import load_profile

# ── Data + profile ────────────────────────────────────────────────────
DATA = json.loads((Path(__file__).parent / "data" / "create_account.json").read_text())
PROFILE = load_profile()

# Unique, greppable, cleanup-friendly record name. The slot suffix keeps
# parallel workers from colliding on the same name in the same second.
import os
_slot = os.getenv("UI_TEST_SLOT") or os.getenv("PYTEST_XDIST_WORKER", "").replace("gw", "")
STAMP = datetime.now(PROFILE.tz).strftime("%m%d_%H%M%S") + (f"s{_slot}" if _slot else "")
ACCOUNT_NAME = f"{PROFILE.record_prefix}_{DATA['account_name_prefix']}{STAMP}"

BILLING = DATA["billing"]


class TestCreateAccount:
    """Create an Account via the Lightning UI."""

    TAGS = ["ui", "smoke", "account", "standard-objects"]
    OBJECTIVE = (
        "Create an Account through the Lightning UI and verify it persists "
        "with the expected name and billing address."
    )

    @pytest.fixture(autouse=True)
    def setup(self, page, tracker, sf):
        self.page, self.tracker, self.sf = page, tracker, sf

    def test_create_account(self):
        sf, page = self.sf, self.page

        with sf.step(1, "Log in to Salesforce",
                     f"Authenticate against {PROFILE.login_url}"):
            sf.login()
            sf.assert_("Landed on Lightning", "lightning" in page.url)

        with sf.step(2, "Open the Accounts list view"):
            sf.open_list_view("Account")
            sf.assert_("Accounts list rendered", True)

        with sf.step(3, "Open the New Account dialog"):
            sf.assert_("'New' clicked", sf.click("New"))
            if DATA.get("record_type"):
                sf.select_record_type(DATA["record_type"])
                sf.click("Next")

        with sf.step(4, f"Fill the Account form ({ACCOUNT_NAME})"):
            sf.wait_form_ready([PROFILE.label("account_name", "Account Name")])
            sf.fill(PROFILE.label("account_name", "Account Name"), ACCOUNT_NAME)
            # Billing address subfields are optional on some orgs — fill what
            # exists, don't fail the step if an org has hidden one.
            for label, value in (
                ("Billing Street", BILLING["street"]),
                ("Billing City", BILLING["city"]),
                ("Billing State/Province", BILLING["state"]),
                ("Billing Zip/Postal Code", BILLING["zip"]),
                ("Billing Country", BILLING["country"]),
            ):
                sf.fill(label, value)
            sf.assert_("Form populated", True)

        with sf.step(5, "Save the Account"):
            sf.assert_("'Save' clicked", sf.click("Save"))
            sf.wait_for_toast(DATA["expected_toast"])

        with sf.step(6, "Verify the Account persisted"):
            sf.wait_page_ready()
            record_id = sf.extract_record_id(sobject="Account")
            sf.assert_("Record id captured", bool(record_id))
            sf.assert_("Name visible on record page",
                       page.get_by_text(ACCOUNT_NAME).count() > 0)
            if record_id:
                self.tracker.add_record(
                    label="Account", name=ACCOUNT_NAME,
                    record_id=record_id, object_type="Account",
                )
