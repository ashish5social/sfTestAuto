"""Account CRUD via the Salesforce REST API.

Reference API test. No browser, no Playwright — just the REST client from
the `sf_api` fixture, which records every request/response into the HTML
report automatically.

Works against ANY org: standard Account object, standard fields only.

Run:
    sfauto test tests/api/test_account_api.py
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.core.org_profile import load_profile

DATA = json.loads((Path(__file__).parent / "data" / "account_api.json").read_text())
PROFILE = load_profile()
STAMP = datetime.now(PROFILE.tz).strftime("%m%d_%H%M%S")
ACCOUNT_NAME = f"{PROFILE.record_prefix}_{DATA['account_name_prefix']}{STAMP}"


class TestAccountApi:
    """Account create / read / update / delete via REST."""

    TAGS = ["api", "smoke", "account", "crud"]
    OBJECTIVE = (
        "Exercise the full Account lifecycle over the REST API and confirm "
        "each mutation is visible to a subsequent SOQL read."
    )

    @pytest.fixture(autouse=True)
    def setup(self, sf_api, api_tracker):
        self.api, self.tracker = sf_api, api_tracker

    def test_account_crud(self):
        api, sf = self.api, self.tracker
        record_id = None

        with sf.step(1, "Connect to Salesforce", f"Org: {PROFILE.login_url}"):
            api.connect()
            sf.assert_("Session established", bool(api.current_user_id))

        with sf.step(2, f"Create Account {ACCOUNT_NAME}"):
            record_id = api.create(
                "Account", {"Name": ACCOUNT_NAME, **DATA["create"]},
                name="Create Account",
            )
            sf.assert_("Account id returned", bool(record_id))

        with sf.step(3, "Read it back with SOQL"):
            rows = api.soql(
                f"SELECT Id, Name, Industry FROM Account WHERE Id = '{record_id}'",
                name="Fetch created Account",
            )
            sf.assert_("Exactly one row returned", len(rows) == 1)
            sf.assert_("Name matches", rows and rows[0]["Name"] == ACCOUNT_NAME)

        with sf.step(4, "Update the Account"):
            api.update("Account", record_id, DATA["update"], name="Update Account")
            rows = api.soql(
                f"SELECT Industry, Description FROM Account WHERE Id = '{record_id}'",
                name="Verify update",
            )
            sf.assert_("Industry updated",
                       rows and rows[0]["Industry"] == DATA["update"]["Industry"])

        with sf.step(5, "Clean up"):
            if DATA.get("cleanup") and record_id:
                api.delete("Account", record_id)
                remaining = api.soql(
                    f"SELECT Id FROM Account WHERE Id = '{record_id}'",
                    name="Confirm deletion",
                )
                sf.assert_("Account removed", len(remaining) == 0)
            else:
                sf.assert_("Cleanup skipped by config", True)
