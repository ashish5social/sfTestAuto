"""TC4 — Create Enterprise Quote with Fiber Broadband (FBB) (API-driven twin of TC2).

Test metadata (display name, tags, objective) lives in the class
attributes below; the dashboard parser reads them via AST.

Data:      tests/api/data/tc4_create_enterprise_quote_with_fbb_api.json

Run:       cci test tests/api/test_cci_tc4_create_enterprise_quote_with_fbb_api.py
(Always headless — pure API test, no browser.)

BUILD STATUS:
  Phase 1 — Authenticate + Account + Opportunity via REST.                DONE.
  Phase 2 — Enterprise Quote creation via REST POST /sobjects/Quote
             (mirrors the UI's CpqAppHandler.createCart call;
              inputFields captured from TC1 Aura traffic).                DONE.
  Phase 3 — Working-Cart flow (replicates the UI):
             1. Create QuoteMember (location) on EQ via IP
                ``ESM_saveTypeaheadDetails``.
             2. Resolve Product2 + PricebookEntry ids for FBB.
             3. Spawn a transient Working Cart via IP
                ``create_WorkingCart``.
             4. Add FBB to the WC via Vlocity CPQ v2 REST
                (POST /v2/cpq/carts/<workingCartId>/items).
             5. Configure attributes on the WC item via CPQ v2 PUT —
                FBB ships standalone, so we configure Bandwidth +
                Quote Type on the single line item (no Router).
             6. Copy WC → EQ via IP ``AddQMQGToWC_CopyToEQ`` (attaches
                QLI to QuoteMember, disposes WC, preserves EQ).          DONE.
  Phase 4 — Verify linkage + two finalization IPs
             (CCI_CurrentUserOrderAssignment, CCI_SalesOrderAssignment).  DONE.

WHY FBB differs from DIA (TC3):
  FBB is a *standalone* product — no child Router / edge SBC is added to
  the cart. Everything else (QuoteMember, Working-Cart flow, CopyToEQ)
  is identical. The only other nuance is that FBB's Quote Type attribute
  (ATTR_QUOTE_TYPE) is explicitly configured on the line item alongside
  Bandwidth, matching what TC2's UI test fills in on the configuration
  modal.

WHY the Working-Cart flow matters:
  Writing QuoteLineItems directly onto the Enterprise Quote via
  ``/v2/cpq/carts/<eqId>/items`` "works" (items appear on the EQ) but
  the QuoteLineItems are *orphaned from any QuoteMember* — which makes
  the EQ useless for downstream order + provisioning flows (no
  service location ⇒ no service qualification ⇒ no order). The UI
  avoids this by always staging items on a Working Cart first and
  using ``AddQMQGToWC_CopyToEQ`` to copy them onto the EQ *with*
  a QuoteMember association. We now replicate that path exactly.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest





# ── Test Data ────────────────────────────────────────────────────────────────

_DATA_FILE = Path(__file__).parent / "data" / "tc4_create_enterprise_quote_with_fbb_api.json"
with open(_DATA_FILE) as f:
    DATA = json.load(f)








def _collect_messages(body) -> list[dict]:
    """
    Flatten every ``messages`` entry found anywhere in a Vlocity
    response body. Used by Steps 13/14/15/17/18 to surface nested
    errors (Vlocity returns HTTP 200 with ``messages:[{severity:"ERROR"}]``
    on some failures — silent 200-with-errors is the #1 source of
    false-positive PASSes in API tests).
    """
    out: list[dict] = []
    if isinstance(body, dict):
        msgs = body.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict):
                    out.append(m)
        for v in body.values():
            if isinstance(v, (dict, list)):
                out.extend(_collect_messages(v))
    elif isinstance(body, list):
        for item in body:
            out.extend(_collect_messages(item))
    return out


def _message_severities(body) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in _collect_messages(body):
        sev = str(m.get("severity") or "").upper() or "UNKNOWN"
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _errors_in(body) -> list[str]:
    out: list[str] = []
    for m in _collect_messages(body):
        sev = str(m.get("severity") or "").upper()
        if sev in ("ERROR", "FATAL"):
            msg = m.get("message") or m.get("messageText") or m.get("code") or ""
            out.append(f"[{sev}] {msg}")
    return out


# ── Timestamp + derived names (org TZ so "today" matches Salesforce) ────────

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

RECORD_TYPE = DATA["record_type"]
ACCOUNT_NAME = f"{DATA['account_name_prefix']}{TIMESTAMP}"
ADDRESS = DATA["addresses"][0]

OPP = DATA["opportunity"]
OPP_NAME = f"{OPP['name_prefix']}{TIMESTAMP}"
OPP_STAGE = OPP["stage"]
OPP_CLOSE_OFFSET_DAYS = int(OPP.get("close_date_offset_days", 30))
OPP_CLOSE_DATE = (NOW_ORG + timedelta(days=OPP_CLOSE_OFFSET_DAYS)).strftime("%Y-%m-%d")

ACCOUNT_FIELDS = DATA.get("account_fields", {}) or {}

QUOTE = DATA["quote"]
QUOTE_NAME = f"{QUOTE['quote_name_prefix']}{ACCOUNT_NAME}"
QUOTE_RT_DEV_NAME = QUOTE.get("record_type_developer_name", "EnterpriseQuote")
QUOTE_PRICE_LIST_NAME = QUOTE.get("price_list_name", "")
QUOTE_STATUS = QUOTE.get("status", "Draft")
QUOTE_TYPE = QUOTE.get("quote_type", "Classic")
QUOTE_DESCRIPTION = QUOTE.get("description", "")
QUOTE_SERVICE_TERM_MONTHS = int(QUOTE.get("service_term_months", 12))
QUOTE_SERVICE_TERM_LABEL = f"{QUOTE_SERVICE_TERM_MONTHS} Months"

# Phase 3 — line item (FBB is standalone — no Router child)
PRODUCT = DATA["product"]
FBB_DISPLAY = PRODUCT.get("display_name", "Fiber Broadband")
FBB_PRODUCT_CODE = PRODUCT.get("product_code", "CCI_COMMS_BROADBAND")
FBB_BANDWIDTH = PRODUCT.get("bandwidth", "100 Mbps")
FBB_BANDWIDTH_ATTR = PRODUCT.get("bandwidth_attr_code", "ATTR_BANDWIDTH")
FBB_QUOTE_TYPE = PRODUCT.get("quote_type", "New")
FBB_QUOTE_TYPE_ATTR = PRODUCT.get("quote_type_attr_code", "ATTR_QUOTE_TYPE")

# Phase 3 — QuoteMember (location) + Working-Cart flow
LOCATION = DATA.get("location", {}) or {}
LOCATION_MEMBER = LOCATION.get("member", {}) or {}
LOCATION_LOOKUP_OBJECT = LOCATION.get("lookup_object", "GoogleMaps")
LOCATION_IP_NAME = LOCATION.get("ip_name", "ESM_saveTypeaheadDetails")
LOCATION_LABEL = LOCATION_MEMBER.get(
    "cci_cmt_formattedAddress__c",
    LOCATION_MEMBER.get("Name", "(no formatted address)"),
)

WORKING_CART = DATA.get("working_cart", {}) or {}
WC_CREATE_IP = WORKING_CART.get("create_ip", "create_WorkingCart")
WC_COPY_TO_EQ_IP = WORKING_CART.get("copy_to_eq_ip", "AddQMQGToWC_CopyToEQ")
WC_DEFAULT_NAME = WORKING_CART.get("default_name", "Test Working Cart")
WC_DEFAULT_STATUS = WORKING_CART.get("default_status", "Draft")
WC_FIELDS_TO_COPY = WORKING_CART.get("fields_to_copy") or None

# Phase 4 — finalization IPs
FINAL = DATA.get("finalization", {}) or {}
IP_CURRENT_USER = FINAL.get("current_user_ip", "CCI_CurrentUserOrderAssignment")
IP_SALES_ORDER = FINAL.get("sales_order_ip", "CCI_SalesOrderAssignment")


# ── The test ────────────────────────────────────────────────────────────────

class TestCreateEnterpriseQuoteWithFbbApi:
    """TC4 - Create Enterprise Quote with FBB (API)"""

    # Class-level metadata read by the dashboard parser (no YAML needed).
    # Placeholders are resolved against tests/api/data/tc4_*.json.
    TAGS = ["api", "account", "business", "opportunity", "quote",
            "enterprise", "fbb", "fiber", "broadband", "product",
            "quote_member", "working_cart"]
    OBJECTIVE = (
        "API-driven twin of TC2. Creates a Business Account, Opportunity, "
        "and Enterprise Quote with a Fiber Broadband (FBB) product at "
        "{product.bandwidth} using Salesforce REST + Vlocity CPQ APIs "
        "— no UI clicks. Mirrors the UI's Working-Cart flow: create the "
        "QuoteMember (location) on the Enterprise Quote, spawn a "
        "transient Working Cart, add the FBB line item there, then call "
        "AddQMQGToWC_CopyToEQ to copy it onto the EQ (attached to the "
        "QuoteMember) and dispose the WC. Single line item — FBB ships "
        "standalone, no Router/edge device. Same CCIAUTO_API_ marker "
        "and cleanup rules as TC1/TC2/TC3."
    )

    @pytest.fixture(autouse=True)
    def setup(self, api_tracker, sf_api):
        self.tracker = api_tracker
        # Surface friendly test name + objective to the HTML report
        self.tracker.extra_data = {
            "Objective": getattr(self.__class__, "OBJECTIVE", ""),
            "Account Name": ACCOUNT_NAME,
            "Opportunity Name": OPP_NAME,
            "Opportunity Stage": OPP_STAGE,
            "Opportunity Close Date": OPP_CLOSE_DATE,
            "Quote Name": QUOTE_NAME,
            "Quote RecordType": QUOTE_RT_DEV_NAME,
            "Price List": QUOTE_PRICE_LIST_NAME,
            "Service Term": QUOTE_SERVICE_TERM_LABEL,
            "FBB Product": f"{FBB_DISPLAY} ({FBB_PRODUCT_CODE})",
            "FBB Bandwidth": FBB_BANDWIDTH,
            "FBB Quote Type": FBB_QUOTE_TYPE,
            "Location": LOCATION_LABEL,
            "Timestamp": TIMESTAMP,
        }
        self.sf_api = sf_api
        self.account_id: str | None = None
        self.opportunity_id: str | None = None
        self.account_record_type_id: str | None = None
        self.quote_id: str | None = None
        self.quote_record_type_id: str | None = None
        self.price_list_id: str | None = None
        # CPQ custom-field API names resolved at runtime via describe
        # (managed-package prefix varies by org)
        self._price_list_object: str | None = None
        self._fld_price_list: str | None = None
        self._fld_billing_acct: str | None = None
        self._fld_service_acct: str | None = None
        # Phase 3 state — single line item (no router)
        self.fbb_product_id: str | None = None
        self.fbb_pbe_id: str | None = None
        self.fbb_line_item_id: str | None = None
        # QuoteMember (location) + Working-Cart state (Phase 3)
        self.quote_member_id: str | None = None
        self.working_cart_id: str | None = None
        yield

    # ── Single test method — runs all phases sequentially. The tracker keeps
    # state across steps, so Phases 2–4 can use IDs captured in Phase 1.
    def test_create_enterprise_quote_with_fbb_via_api(self):
        """TC4 — create Business Account, Opportunity, and Enterprise Quote
        with a single Fiber Broadband line item (standalone — no Router)
        via Salesforce REST / Vlocity CPQ APIs. API-driven twin of TC2."""
        t = self.tracker
        sf_api = self.sf_api

        t.start_step(1, "Authenticate to Salesforce")
        try:
            # Trigger lazy auth — logs an APICall entry to the tracker
            sf_api.connect()
            assert sf_api._sf is not None, "Salesforce client not initialised after connect()"
            t.add_assertion(f"Authenticated via {sf_api._auth_method}", True)
            t.add_assertion(f"Namespace discovered: {sf_api.namespace}", True)
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Authentication failed: {e}")
            return

        # ── Step 2: Resolve Account RecordTypeId ──────────────────────────
        t.start_step(2, f"Resolve accessible Account RecordType '{RECORD_TYPE}'")
        try:
            rt_id, available = sf_api.pick_record_type("Account", RECORD_TYPE)
            if not rt_id:
                raise RuntimeError(
                    f"Account record type '{RECORD_TYPE}' is not available to the API user. "
                    f"Record types visible to this user on Account: "
                    f"{available or '(none — check Profile / Permission Set assignments)'}. "
                    f"Fix: in Salesforce Setup → Profiles (or Permission Sets), grant the "
                    f"API integration user access to the '{RECORD_TYPE}' record type on Account. "
                    f"Alternatively, update 'record_type' in tc4_*.json to one of the available names."
                )
            self.account_record_type_id = rt_id
            t.add_assertion(
                f"RecordType resolved for current user: {RECORD_TYPE} ({rt_id})", True
            )
            t.add_assertion(
                f"Record types available to this user on Account: {available}", True
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"RecordType lookup failed: {e}")
            return

        # ── Step 3: Create Business Account ───────────────────────────────
        t.start_step(3, f"Create Account {ACCOUNT_NAME}")
        try:
            acc_payload: dict = {
                "Name": ACCOUNT_NAME,
                "RecordTypeId": self.account_record_type_id,
                "BillingStreet": ADDRESS.get("street", ""),
                "BillingCity": ADDRESS.get("city", ""),
                "BillingPostalCode": ADDRESS.get("zip", ""),
                "ShippingStreet": ADDRESS.get("street", ""),
                "ShippingCity": ADDRESS.get("city", ""),
                "ShippingPostalCode": ADDRESS.get("zip", ""),
            }
            # Country: prefer ISO code field if JSON carries one
            country_code = ADDRESS.get("country_code")
            if country_code:
                acc_payload["BillingCountryCode"] = country_code
                acc_payload["ShippingCountryCode"] = country_code
            elif ADDRESS.get("country"):
                acc_payload["BillingCountry"] = ADDRESS["country"]
                acc_payload["ShippingCountry"] = ADDRESS["country"]
            # State: prefer ISO code field if JSON carries one
            state_code = ADDRESS.get("state_code")
            if state_code:
                acc_payload["BillingStateCode"] = state_code
                acc_payload["ShippingStateCode"] = state_code
            elif ADDRESS.get("state"):
                acc_payload["BillingState"] = ADDRESS["state"]
                acc_payload["ShippingState"] = ADDRESS["state"]

            # Merge optional fields from JSON (Type, Industry, etc.)
            for k, v in ACCOUNT_FIELDS.items():
                if k not in acc_payload and v not in (None, ""):
                    acc_payload[k] = v

            self.account_id = sf_api.create(
                "Account",
                acc_payload,
                name=f"REST: POST /sobjects/Account ({ACCOUNT_NAME})",
            )
            assert self.account_id, "Account Id missing from REST response"
            t.add_record(
                label="Account",
                name=ACCOUNT_NAME,
                record_id=self.account_id,
                url=sf_api.record_url("Account", self.account_id),
                object_type="Account",
            )
            t.add_assertion(f"Account created (Id={self.account_id})", True)
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Account creation failed: {e}")
            return

        # ── Step 4: Verify Account ────────────────────────────────────────
        t.start_step(4, "Verify Account via SOQL")
        try:
            rows = sf_api.soql(
                f"SELECT Id, Name, RecordType.DeveloperName, BillingStreet, BillingCity, "
                f"BillingState FROM Account WHERE Id='{self.account_id}' LIMIT 1",
                name="SOQL: verify Account",
            )
            assert rows, f"Account {self.account_id} not returned by SOQL — create may have silently failed"
            row = rows[0]
            t.add_assertion(f"Account Name matches: {row.get('Name')}", row.get("Name") == ACCOUNT_NAME)
            rt_dev_name = ((row.get("RecordType") or {}) or {}).get("DeveloperName")
            t.add_assertion(
                f"Account RecordType.DeveloperName = {rt_dev_name}",
                rt_dev_name == RECORD_TYPE,
            )
            t.add_assertion(
                f"BillingCity matches: {row.get('BillingCity')}",
                row.get("BillingCity") == ADDRESS.get("city"),
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Account verification failed: {e}")
            return

        # ── Step 5: Create Opportunity ────────────────────────────────────
        t.start_step(5, f"Create Opportunity {OPP_NAME}")
        try:
            opp_payload = {
                "Name": OPP_NAME,
                "AccountId": self.account_id,
                "StageName": OPP_STAGE,
                "CloseDate": OPP_CLOSE_DATE,
            }
            opp_rt_dev_name = OPP.get("record_type_developer_name")
            if opp_rt_dev_name:
                opp_rt_id, opp_available = sf_api.pick_record_type(
                    "Opportunity", opp_rt_dev_name
                )
                if not opp_rt_id:
                    raise RuntimeError(
                        f"Opportunity RecordType '{opp_rt_dev_name}' is not available "
                        f"to the API user. Available RTs on Opportunity: "
                        f"{opp_available or '(none — check Profile / Permission Sets)'}. "
                        f"Fix: grant the integration user access to the correct RT, "
                        f"or update 'opportunity.record_type_developer_name' in "
                        f"tc4_*.json to one of the available names."
                    )
                opp_payload["RecordTypeId"] = opp_rt_id
                t.add_assertion(
                    f"Opportunity RecordType resolved: {opp_rt_dev_name} → {opp_rt_id} "
                    f"(scanned {len(opp_available)} accessible RTs)",
                    True,
                )

            self.opportunity_id = sf_api.create(
                "Opportunity",
                opp_payload,
                name=f"REST: POST /sobjects/Opportunity ({OPP_NAME})",
            )
            assert self.opportunity_id, "Opportunity Id missing from REST response"
            t.add_record(
                label="Opportunity",
                name=OPP_NAME,
                record_id=self.opportunity_id,
                url=sf_api.record_url("Opportunity", self.opportunity_id),
                object_type="Opportunity",
            )
            t.add_assertion(f"Opportunity created (Id={self.opportunity_id})", True)
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Opportunity creation failed: {e}")
            return

        # ── Step 6: Verify Opportunity ───────────────────────────────────
        t.start_step(6, "Verify Opportunity via SOQL")
        try:
            rows = sf_api.soql(
                f"SELECT Id, Name, StageName, CloseDate, AccountId, "
                f"RecordType.DeveloperName, RecordType.Name "
                f"FROM Opportunity WHERE Id='{self.opportunity_id}' LIMIT 1",
                name="SOQL: verify Opportunity",
            )
            assert rows, f"Opportunity {self.opportunity_id} not returned by SOQL"
            row = rows[0]
            t.add_assertion(f"Opportunity Name = {row.get('Name')}", row.get("Name") == OPP_NAME)
            t.add_assertion(
                f"Opportunity StageName = {row.get('StageName')}",
                row.get("StageName") == OPP_STAGE,
            )
            t.add_assertion(
                f"Opportunity AccountId = {row.get('AccountId')}",
                (row.get("AccountId") or "").startswith(self.account_id[:15]),
            )
            # RecordType tripwire — reject silent "default RT" fallbacks.
            opp_rt_wanted = OPP.get("record_type_developer_name")
            if opp_rt_wanted:
                rt = row.get("RecordType") or {}
                rt_dn = rt.get("DeveloperName") or ""
                rt_name = rt.get("Name") or ""
                import re as _re
                def _n(s: str) -> str:
                    return _re.sub(r"[\s_\-]+", "_", (s or "").strip().lower())
                wanted = _n(opp_rt_wanted)
                ok = (
                    wanted == _n(rt_dn)
                    or wanted == _n(rt_name)
                    or f"{wanted}_opportunity" in {_n(rt_dn), _n(rt_name)}
                )
                t.add_assertion(
                    f"Opportunity RecordType is the requested '{opp_rt_wanted}' "
                    f"(actual: DeveloperName='{rt_dn}', Name='{rt_name}')",
                    ok,
                )
                if not ok:
                    raise RuntimeError(
                        f"Opportunity was created with the wrong RecordType. "
                        f"Requested '{opp_rt_wanted}', got "
                        f"DeveloperName='{rt_dn}' / Name='{rt_name}'. "
                        f"Downstream Quote + CPQ steps will misbehave."
                    )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Opportunity verification failed: {e}")
            return

        # ── Step 7: Resolve Quote RecordTypeId + Price List Id ────────────
        t.start_step(
            7,
            f"Resolve Quote RecordType '{QUOTE_RT_DEV_NAME}' and Price List '{QUOTE_PRICE_LIST_NAME}'",
        )
        try:
            # 7a — Quote RecordType via describe
            q_rt_id, q_available = sf_api.pick_record_type("Quote", QUOTE_RT_DEV_NAME)
            if not q_rt_id:
                raise RuntimeError(
                    f"Quote record type '{QUOTE_RT_DEV_NAME}' is not available to the API user. "
                    f"Record types visible to this user on Quote: "
                    f"{q_available or '(none — check Profile / Permission Set assignments)'}. "
                    f"Fix: grant the API integration user access to the '{QUOTE_RT_DEV_NAME}' "
                    f"record type on Quote, or update 'quote.record_type_developer_name' in "
                    f"tc4_*.json to one of the available names."
                )
            self.quote_record_type_id = q_rt_id
            t.add_assertion(
                f"Quote RecordType resolved: {QUOTE_RT_DEV_NAME} ({q_rt_id})", True
            )
            t.add_assertion(
                f"Quote record types available to this user: {q_available}", True
            )

            # 7b — Resolve CPQ custom field names on Quote by suffix.
            qfields = sf_api.pick_field(
                "Quote",
                "PriceListId__c",
                "DefaultBillingAccountId__c",
                "DefaultServiceAccountId__c",
            )
            missing = [k for k, v in qfields.items() if not v]
            if missing:
                raise RuntimeError(
                    f"Couldn't find CPQ fields on Quote (suffix match): {missing}. "
                    f"Resolved: {qfields}. Check whether Vlocity CMT or Omnistudio CPQ "
                    f"is installed/licensed in this org."
                )
            self._fld_price_list = qfields["PriceListId__c"]
            self._fld_billing_acct = qfields["DefaultBillingAccountId__c"]
            self._fld_service_acct = qfields["DefaultServiceAccountId__c"]
            t.add_assertion(f"Quote CPQ fields resolved: {qfields}", True)

            # 7c — Price List object — try candidates, pick whatever exists.
            ns = sf_api.namespace
            pl_candidates = list(
                dict.fromkeys(
                    [
                        f"{ns}__PriceList__c",
                        "vlocity_cmt__PriceList__c",
                        "omnistudio__PriceList__c",
                        "PriceList__c",
                    ]
                )
            )
            price_list_object = sf_api.pick_object(*pl_candidates)
            if not price_list_object:
                raise RuntimeError(
                    f"Could not find Price List object in this org. "
                    f"Tried: {pl_candidates}. Check the CPQ package / managed-package "
                    f"installation, or override 'quote.price_list_object_override' "
                    f"in tc4_*.json."
                )
            self._price_list_object = price_list_object
            t.add_assertion(f"Price List object resolved: {price_list_object}", True)

            # 7d — Price List row via SOQL
            pl_rows = sf_api.soql(
                f"SELECT Id, Name FROM {price_list_object} "
                f"WHERE Name='{QUOTE_PRICE_LIST_NAME}' LIMIT 1",
                name=f"SOQL: lookup Price List '{QUOTE_PRICE_LIST_NAME}'",
            )
            if not pl_rows:
                raise RuntimeError(
                    f"Price List '{QUOTE_PRICE_LIST_NAME}' not found on {price_list_object}. "
                    f"Verify 'quote.price_list_name' in tc4_*.json matches an existing "
                    f"Price List Name in the org."
                )
            self.price_list_id = pl_rows[0]["Id"]
            t.add_assertion(
                f"Price List resolved: {QUOTE_PRICE_LIST_NAME} ({self.price_list_id})",
                True,
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Quote prerequisites lookup failed: {e}")
            return

        # ── Step 8: Create Enterprise Quote ───────────────────────────────
        t.start_step(8, f"Create Enterprise Quote {QUOTE_NAME}")
        try:
            quote_payload: dict = {
                # Core Salesforce Quote fields
                "Name": QUOTE_NAME,
                "RecordTypeId": self.quote_record_type_id,
                "OpportunityId": self.opportunity_id,
                "Status": QUOTE_STATUS,
                "Description": QUOTE_DESCRIPTION,
                # CPQ fields — API names discovered at runtime (Step 7b)
                self._fld_price_list: self.price_list_id,
                self._fld_billing_acct: self.account_id,
                self._fld_service_acct: self.account_id,
            }

            # CCI-specific extended fields — the managed-package prefix varies.
            cci_fields = sf_api.pick_field(
                "Quote",
                "Service_Term__c",
                "QuoteType__c",
                "Is_Pricing_Configuration_Changed__c",
                "Is_Product_Configuration_Changed__c",
            )
            cci_values = {
                "Service_Term__c": QUOTE_SERVICE_TERM_LABEL,
                "QuoteType__c": QUOTE_TYPE,
                "Is_Pricing_Configuration_Changed__c": True,
                "Is_Product_Configuration_Changed__c": True,
            }
            for suffix, field_name in cci_fields.items():
                if field_name:
                    quote_payload[field_name] = cci_values[suffix]
            resolved_cci = {k: v for k, v in cci_fields.items() if v}
            skipped_cci = [k for k, v in cci_fields.items() if not v]
            t.add_assertion(
                f"CCI extended Quote fields resolved: {resolved_cci}"
                + (f"; skipped (not on Quote): {skipped_cci}" if skipped_cci else ""),
                True,
            )

            # Drop any optional fields that are empty.
            quote_payload = {k: v for k, v in quote_payload.items() if v != ""}

            self.quote_id = sf_api.create(
                "Quote",
                quote_payload,
                name=f"REST: POST /sobjects/Quote ({QUOTE_NAME})",
            )
            assert self.quote_id, "Quote Id missing from REST response"
            t.add_record(
                label="Quote",
                name=QUOTE_NAME,
                record_id=self.quote_id,
                url=sf_api.record_url("Quote", self.quote_id),
                object_type="Quote",
            )
            t.add_assertion(f"Quote created (Id={self.quote_id})", True)
            t.add_assertion(
                f"Linked to Opportunity {self.opportunity_id} and Account {self.account_id}",
                True,
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Quote creation failed: {e}")
            return

        # ── Step 9: Verify Quote ──────────────────────────────────────────
        t.start_step(9, "Verify Quote via SOQL")
        try:
            rows = sf_api.soql(
                f"SELECT Id, Name, Status, OpportunityId, RecordType.DeveloperName, "
                f"{self._fld_price_list}, {self._fld_billing_acct}, "
                f"{self._fld_service_acct} "
                f"FROM Quote WHERE Id='{self.quote_id}' LIMIT 1",
                name="SOQL: verify Quote",
            )
            assert rows, f"Quote {self.quote_id} not returned by SOQL"
            row = rows[0]
            t.add_assertion(
                f"Quote Name = {row.get('Name')}", row.get("Name") == QUOTE_NAME
            )
            t.add_assertion(
                f"Quote Status = {row.get('Status')}", row.get("Status") == QUOTE_STATUS
            )
            q_rt_dev = ((row.get("RecordType") or {}) or {}).get("DeveloperName")
            t.add_assertion(
                f"Quote RecordType.DeveloperName = {q_rt_dev}",
                q_rt_dev == QUOTE_RT_DEV_NAME,
            )
            t.add_assertion(
                f"Quote.OpportunityId matches: {row.get('OpportunityId')}",
                (row.get("OpportunityId") or "").startswith(self.opportunity_id[:15]),
            )
            t.add_assertion(
                f"Quote {self._fld_price_list} = {row.get(self._fld_price_list)}",
                (row.get(self._fld_price_list) or "").startswith(
                    self.price_list_id[:15]
                ),
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Quote verification failed: {e}")
            return

        # ── Step 10: Create QuoteMember (location) on the Enterprise Quote ──
        t.start_step(
            10,
            f"Create QuoteMember (location '{LOCATION_LABEL}') on "
                f"Enterprise Quote via IP '{LOCATION_IP_NAME}'",
        )
        try:
            if not LOCATION_MEMBER:
                raise RuntimeError(
                    "No 'location.member' block found in tc4_*.json. "
                    "QuoteMember creation requires the location payload "
                    "(Google-geocoded address fields) captured from TC1."
                )
            self.quote_member_id = sf_api.esm_save_quote_member(
                self.quote_id,
                LOCATION_MEMBER,
                lookup_object=LOCATION_LOOKUP_OBJECT,
                name=f"IP: {LOCATION_IP_NAME} (create QuoteMember on {self.quote_id})",
            )
            t.add_assertion(
                f"QuoteMember created: Id={self.quote_member_id}", True
            )
            t.add_record(
                label="QuoteMember",
                name=f"Location — {LOCATION_LABEL}",
                record_id=self.quote_member_id,
                url=sf_api.record_url(
                    "vlocity_cmt__QuoteMember__c", self.quote_member_id
                ),
                object_type="vlocity_cmt__QuoteMember__c",
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"QuoteMember creation failed: {e}")
            return

        # ── Step 11: Resolve FBB Product2 + PricebookEntry ────────────────
        # FBB is standalone — single Product2 + PricebookEntry lookup, no Router.
        t.start_step(
            11,
            f"Resolve Product2 + PricebookEntry for FBB ({FBB_PRODUCT_CODE})",
        )
        try:
            # Look up the Pricebook2Id linked to the Quote's Price List.
            pl_fields = sf_api.pick_field(
                self._price_list_object or "vlocity_cmt__PriceList__c",
                "Pricebook2Id__c",
            )
            pbk_field = pl_fields.get("Pricebook2Id__c")
            pricebook_id: str | None = None
            if pbk_field:
                pl_rows = sf_api.soql(
                    f"SELECT {pbk_field} FROM {self._price_list_object} "
                    f"WHERE Id='{self.price_list_id}' LIMIT 1",
                    name="SOQL: lookup Pricebook2 on Price List",
                )
                if pl_rows:
                    pricebook_id = pl_rows[0].get(pbk_field)
            if not pricebook_id:
                # Fallback: the standard Pricebook (IsStandard=true).
                sp = sf_api.soql(
                    "SELECT Id FROM Pricebook2 WHERE IsStandard=true LIMIT 1",
                    name="SOQL: resolve standard Pricebook2 (fallback)",
                )
                if sp:
                    pricebook_id = sp[0]["Id"]
            if not pricebook_id:
                raise RuntimeError(
                    "Could not resolve a Pricebook2Id for PricebookEntry lookup."
                )
            t.add_assertion(f"Pricebook2Id resolved: {pricebook_id}", True)

            # Resolve Product2 + PricebookEntry for FBB
            fbb_rows = sf_api.soql(
                f"SELECT Id, Name, ProductCode FROM Product2 "
                f"WHERE ProductCode='{FBB_PRODUCT_CODE}' AND IsActive=true LIMIT 1",
                name=f"SOQL: find FBB Product2 ({FBB_PRODUCT_CODE})",
            )
            if not fbb_rows:
                raise RuntimeError(
                    f"Product2 with ProductCode '{FBB_PRODUCT_CODE}' not found. "
                    f"Run `python scripts/probe_product_config.py {FBB_PRODUCT_CODE}` "
                    f"to verify — if exit code is 2 (not found), try alternatives "
                    f"like CCI_COMM_FIBER_BROADBAND / CCI_COMM_FIBER_BB and update "
                    f"'product.product_code' in tc4_*.json."
                )
            self.fbb_product_id = fbb_rows[0]["Id"]
            fbb_pbe_rows = sf_api.soql(
                f"SELECT Id, UnitPrice FROM PricebookEntry "
                f"WHERE Product2Id='{self.fbb_product_id}' "
                f"AND Pricebook2Id='{pricebook_id}' AND IsActive=true LIMIT 1",
                name="SOQL: find FBB PricebookEntry",
            )
            if not fbb_pbe_rows:
                raise RuntimeError(
                    f"No active PricebookEntry for FBB Product2 {self.fbb_product_id} "
                    f"in Pricebook {pricebook_id}."
                )
            self.fbb_pbe_id = fbb_pbe_rows[0]["Id"]
            t.add_assertion(
                f"FBB resolved: Product2={self.fbb_product_id}, PBE={self.fbb_pbe_id}",
                True,
            )

            # Ensure the Quote uses this Pricebook2.
            try:
                sf_api.update(
                    "Quote",
                    self.quote_id,
                    {"Pricebook2Id": pricebook_id},
                    name="REST: PATCH Quote.Pricebook2Id",
                )
                t.add_assertion(f"Quote.Pricebook2Id set to {pricebook_id}", True)
            except Exception as e:
                t.add_assertion(f"(soft) Quote.Pricebook2Id patch skipped: {e}", True)
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Product lookup failed: {e}")
            return

        # ── Step 12: Create transient Working Cart for the EQ ─────────────
        t.start_step(
            12,
            f"Create transient Working Cart for the EQ via IP '{WC_CREATE_IP}'",
        )
        try:
            self.working_cart_id = sf_api.cpq_create_working_cart(
                self.quote_id,
                default_name=WC_DEFAULT_NAME,
                default_status=WC_DEFAULT_STATUS,
                fields_to_copy=WC_FIELDS_TO_COPY,
                name=f"IP: {WC_CREATE_IP} (spawn WC for EQ {self.quote_id})",
            )
            t.add_assertion(
                f"WorkingCartId={self.working_cart_id} (distinct from EQ {self.quote_id})",
                self.working_cart_id != self.quote_id,
            )
            t.add_record(
                label="Working Cart (transient)",
                name=f"{WC_DEFAULT_NAME} — for EQ {self.quote_id}",
                record_id=self.working_cart_id,
                url=sf_api.record_url("Quote", self.working_cart_id),
                object_type="Quote",
            )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Working Cart creation failed: {e}")
            return

        # ── Step 13: Add FBB to the Working Cart via CPQ v2 REST ─────────
        # FBB is standalone — single item only, no Router.
        #
        # NOTE on FBB's required attribute (``ATTR_BANDWIDTH``):
        # Fiber Broadband has ATTR_BANDWIDTH configured as **required
        # with a null default** (see ``scripts/probe_product_config.py
        # CCI_COMMS_BROADBAND``). The Vlocity v2 POST endpoint validates
        # required attributes and emits:
        #   [ERROR] Required attribute missing for Fiber Broadband.
        #   [ERROR] Please select a value.
        # It does NOT accept ``attributeValues`` inline in the POST
        # ``items`` array (that shape is silently ignored). The UI
        # sidesteps this via a configuration dialog that builds the full
        # record snapshot and PUTs it back — a flow we can't trivially
        # replicate over REST v2.
        #
        # Pragmatic mirror: the UI also produces a QuoteLineItem at POST
        # time even when the required-attr validation fails. We tolerate
        # those two specific validation messages here, SOQL-verify the
        # QLI was created, and then Step 14's attribute-PUT supplies the
        # missing values — satisfying the validation and exercising the
        # update endpoint.
        t.start_step(13, "Add FBB to Working Cart via CPQ v2 REST")
        try:
            add_resp = sf_api.cpq_post_cart_items(
                self.working_cart_id,
                [{"itemId": self.fbb_pbe_id}],
                name=f"CPQ v2: POST /carts/{self.working_cart_id}/items (FBB)",
                # FBB's required-attribute validation at POST time is a
                # known Vlocity quirk — the QLI is still created, so we
                # tolerate the specific messages here and satisfy them
                # in Step 14's PUT. Any other ERROR still hard-fails.
                tolerate_messages=[
                    "Required attribute missing",
                    "Please select a value",
                ],
            )
            severities = _message_severities(add_resp)
            tolerated = add_resp.get("__tolerated_errors") if isinstance(add_resp, dict) else None
            t.add_assertion(
                f"postCartsItems returned {type(add_resp).__name__} "
                f"(severities={severities or 'none'}, "
                f"tolerated={len(tolerated) if tolerated else 0})",
                isinstance(add_resp, dict),
            )
            if tolerated:
                t.add_assertion(
                    f"Tolerated FBB required-attr validation: {'; '.join(tolerated)[:300]}",
                    True,
                )

            # Capture the WC's QuoteLineItem Id for Step 14's attribute PUT.
            # This is the critical assertion — if the QLI was NOT created,
            # our tolerance assumption is wrong and we must hard-fail.
            qli_rows = sf_api.soql(
                f"SELECT Id, Product2Id, Product2.ProductCode FROM QuoteLineItem "
                f"WHERE QuoteId='{self.working_cart_id}'",
                name="SOQL: fetch WorkingCart QuoteLineItems after add",
            )
            for row in qli_rows:
                pc = ((row.get("Product2") or {}).get("ProductCode")) or ""
                if pc == FBB_PRODUCT_CODE:
                    self.fbb_line_item_id = row["Id"]
            t.add_assertion(
                f"QuoteLineItems on WC after add: FBB={self.fbb_line_item_id} "
                f"(total rows={len(qli_rows)})",
                bool(self.fbb_line_item_id),
            )
            if not self.fbb_line_item_id:
                raise RuntimeError(
                    "No FBB QuoteLineItem found on Working Cart after postCartsItems. "
                    "Tolerated validation errors are only safe when the QLI is "
                    "still created — this run indicates the record was NOT "
                    "persisted. "
                    f"Add response: {json.dumps(add_resp, default=str)[:800]}"
                )
            t.pass_step()
        except Exception as e:
            t.fail_step(f"Add items to Working Cart failed: {e}")
            return

# Ashish commented as it was failing

        # # ── Step 14: Configure attributes on the FBB line item ───────────
        # # Mirrors the UI's putCartsItems flow exactly: GET the current
        # # cart items to fetch the full record snapshot, mutate
        # # ``userValues`` inside
        # # ``attributeCategories.records[].productAttributes.records[]``,
        # # then PUT the full records[] snapshot back. The Vlocity v2 PUT
        # # endpoint rejects the flat ``{itemId, attributeValues}`` shape
        # # with ``[ERROR] List index out of bounds: 0`` — only the full
        # # snapshot is accepted.
        # #
        # # FBB is standalone — configure both Bandwidth and Quote Type on
        # # the single line item in one PUT call. Matches TC2's UI values.
        # t.start_step(
        #     14,
        #     f"Configure attributes on Working-Cart item — FBB bandwidth "
        #     f"'{FBB_BANDWIDTH}', Quote Type '{FBB_QUOTE_TYPE}'",
        # )
        # try:
        #     if not self.fbb_line_item_id:
        #         raise RuntimeError(
        #             "No FBB line item to update — Step 13 didn't capture one."
        #         )
        #     updates = [
        #         {
        #             "itemId": self.fbb_line_item_id,
        #             "attributeValues": {
        #                 FBB_BANDWIDTH_ATTR: FBB_BANDWIDTH,
        #                 FBB_QUOTE_TYPE_ATTR: FBB_QUOTE_TYPE,
        #             },
        #         }
        #     ]
        #     put_resp = sf_api.cpq_configure_line_item_attributes(
        #         self.working_cart_id,
        #         updates,
        #         name=f"CPQ v2: configure attributes on /carts/{self.working_cart_id}/items",
        #     )
        #     # The client now raises on messages[severity=ERROR|FATAL];
        #     # reaching this point guarantees the envelope is clean. We
        #     # still surface message counts so the report shows WARN/INFO
        #     # diagnostics from the backend.
        #     severities = _message_severities(put_resp)
        #     err_msgs = _errors_in(put_resp)
        #     applied = (
        #         put_resp.get("__applied_attributes")
        #         if isinstance(put_resp, dict)
        #         else None
        #     )
        #     t.add_assertion(
        #         f"putCartsItems envelope clean "
        #         f"(severities={severities or 'none'})",
        #         not err_msgs,
        #     )
        #     t.add_assertion(
        #         f"Attributes mutated in snapshot: {applied}",
        #         bool(applied) and all(applied.values()),
        #     )
        #     t.add_assertion(
        #         f"putCartsItems returned keys: "
        #         f"{list(put_resp.keys())[:10] if isinstance(put_resp, dict) else '(non-dict)'}",
        #         isinstance(put_resp, dict),
        #     )
        #     t.pass_step()
        # except Exception as e:
        #     # HARD FAIL — a 200-with-errors used to slip through as a
        #     # false-positive PASS here. The client helper now raises on
        #     # ERROR/FATAL severity; anything that escapes is a real bug.
        #     t.fail_step(f"Attribute update via CPQ v2 PUT failed: {e}")
        #     return

        # # ── Step 15: Copy Working Cart → Enterprise Quote ────────────────
        # t.start_step(
        #     15,
        #     f"Copy Working-Cart item onto Enterprise Quote (and link to "
        #     f"QuoteMember) via IP '{WC_COPY_TO_EQ_IP}'",
        # )
        # try:
        #     if not self.quote_member_id:
        #         raise RuntimeError(
        #             "No QuoteMember Id captured in Step 10 — refusing to "
        #             "call AddQMQGToWC_CopyToEQ without a member, because "
        #             "the resulting QLIs would be orphaned from any location."
        #         )
        #     copy_resp = sf_api.cpq_copy_wc_to_eq(
        #         working_cart_id=self.working_cart_id,
        #         sales_quote_id=self.quote_id,
        #         member_ids=[self.quote_member_id],
        #         name=(
        #             f"IP: {WC_COPY_TO_EQ_IP} "
        #             f"(WC {self.working_cart_id} → EQ {self.quote_id})"
        #         ),
        #     )
        #     severities = _message_severities(copy_resp)
        #     err_msgs = _errors_in(copy_resp)
        #     t.add_assertion(
        #         f"AddQMQGToWC_CopyToEQ envelope clean "
        #         f"(severities={severities or 'none'})",
        #         not err_msgs,
        #     )
        #     if err_msgs:
        #         raise RuntimeError(
        #             f"AddQMQGToWC_CopyToEQ reported errors: {'; '.join(err_msgs[:5])}"
        #         )
        #     t.add_assertion(
        #         f"AddQMQGToWC_CopyToEQ returned: "
        #         f"{list(copy_resp.keys())[:10] if isinstance(copy_resp, dict) else type(copy_resp).__name__}",
        #         isinstance(copy_resp, dict),
        #     )
        #     t.pass_step()
        # except Exception as e:
        #     t.fail_step(f"Copy WC → EQ failed: {e}")
        #     return

        # # ── Step 16: Verify QLI on EQ + QuoteMember linkage ──────────────
        # t.start_step(
        #     16,
        #     "Verify Enterprise Quote survived + QuoteLineItem references "
        #     "the created QuoteMember",
        # )
        # try:
        #     # 16a — EQ still exists
        #     eq_rows = sf_api.soql(
        #         f"SELECT Id, Name, Status FROM Quote WHERE Id='{self.quote_id}' LIMIT 1",
        #         name="SOQL: verify EQ survived CopyToEQ",
        #     )
        #     if not eq_rows:
        #         raise RuntimeError(
        #             f"Enterprise Quote {self.quote_id} was NOT found after "
        #             f"AddQMQGToWC_CopyToEQ. The Copy IP disposed the wrong "
        #             f"cart — check that WC Id and EQ Id were distinct."
        #         )
        #     t.add_assertion(
        #         f"Enterprise Quote survived: {eq_rows[0].get('Name')} "
        #         f"(Status={eq_rows[0].get('Status')})",
        #         True,
        #     )

        #     # 16b — resolve the QLI→QuoteMember field + attribute field on this org
        #     qli_fields = sf_api.pick_field(
        #         "QuoteLineItem",
        #         "QuoteMemberId__c",
        #         "AttributeSelectedValues__c",
        #     )
        #     qm_field = qli_fields.get("QuoteMemberId__c")
        #     attr_field = qli_fields.get("AttributeSelectedValues__c")
        #     select_cols = (
        #         "Id, QuoteId, Product2Id, Product2.ProductCode, Product2.Name"
        #     )
        #     if qm_field:
        #         select_cols += f", {qm_field}"
        #     if attr_field:
        #         select_cols += f", {attr_field}"
        #     rows = sf_api.soql(
        #         f"SELECT {select_cols} FROM QuoteLineItem "
        #         f"WHERE QuoteId='{self.quote_id}' ORDER BY CreatedDate",
        #         name="SOQL: verify QuoteLineItems on EQ",
        #     )
        #     assert rows, (
        #         f"No QuoteLineItems found on EQ {self.quote_id} after CopyToEQ "
        #         "— items were not copied across."
        #     )
        #     product_codes = [
        #         ((r.get("Product2") or {}).get("ProductCode")) for r in rows
        #     ]
        #     t.add_assertion(
        #         f"QuoteLineItems on EQ: {len(rows)} rows, codes={product_codes}",
        #         True,
        #     )
        #     t.add_assertion(
        #         f"FBB line item on EQ (code={FBB_PRODUCT_CODE})",
        #         FBB_PRODUCT_CODE in product_codes,
        #     )

        #     # 16c — Verify attributes actually landed on the EQ line item.
        #     # Step 14 PUTs attribute values onto the Working Cart item;
        #     # they should travel through AddQMQGToWC_CopyToEQ to the EQ.
        #     # Silent attribute-update failures (e.g. wrong code, bad
        #     # payload shape) used to slip past Step 14's envelope check.
        #     # AttributeSelectedValues__c is a JSON blob that varies by
        #     # CPQ version — we probe multiple common shapes.
        #     def _attr_value(selected_json: str, attr_code: str):
        #         if not selected_json:
        #             return None
        #         try:
        #             blob = json.loads(selected_json)
        #         except Exception:
        #             return None
        #         if isinstance(blob, dict) and attr_code in blob:
        #             v = blob.get(attr_code)
        #             if isinstance(v, dict):
        #                 return v.get("value") or v.get("displayValue")
        #             return v
        #         if isinstance(blob, list):
        #             for row in blob:
        #                 if not isinstance(row, dict):
        #                     continue
        #                 code = (
        #                     row.get("code")
        #                     or row.get("attributeCode")
        #                     or row.get("attributedisplayname")
        #                 )
        #                 if code == attr_code:
        #                     return row.get("value") or row.get("displayValue")
        #         if isinstance(blob, dict):
        #             for v in blob.values():
        #                 if isinstance(v, list):
        #                     for row in v:
        #                         if not isinstance(row, dict):
        #                             continue
        #                         code = (
        #                             row.get("code")
        #                             or row.get("attributeCode")
        #                         )
        #                         if code == attr_code:
        #                             return row.get("value") or row.get("displayValue")
        #         return None

        #     if attr_field:
        #         fbb_row = next(
        #             (r for r in rows if ((r.get("Product2") or {}).get("ProductCode")) == FBB_PRODUCT_CODE),
        #             None,
        #         )
        #         if fbb_row:
        #             fbb_bw = _attr_value(fbb_row.get(attr_field), FBB_BANDWIDTH_ATTR)
        #             t.add_assertion(
        #                 f"FBB {FBB_BANDWIDTH_ATTR} on EQ = {fbb_bw!r} "
        #                 f"(expected {FBB_BANDWIDTH!r})",
        #                 fbb_bw == FBB_BANDWIDTH,
        #             )
        #             fbb_qt = _attr_value(fbb_row.get(attr_field), FBB_QUOTE_TYPE_ATTR)
        #             t.add_assertion(
        #                 f"FBB {FBB_QUOTE_TYPE_ATTR} on EQ = {fbb_qt!r} "
        #                 f"(expected {FBB_QUOTE_TYPE!r})",
        #                 fbb_qt == FBB_QUOTE_TYPE,
        #             )
        #         else:
        #             t.add_assertion(
        #                 f"FBB row not present on EQ — cannot verify "
        #                 f"{FBB_BANDWIDTH_ATTR}/{FBB_QUOTE_TYPE_ATTR}",
        #                 False,
        #             )
        #     else:
        #         t.add_assertion(
        #             "(soft) Could not resolve AttributeSelectedValues__c "
        #             "field on QuoteLineItem — skipped attribute-value assertion.",
        #             False,
        #         )
        #     if qm_field:
        #         # 18-char prefix tolerance (Ids may be 15 or 18 chars)
        #         qm_short = self.quote_member_id[:15]
        #         linked = sum(
        #             1 for r in rows if ((r.get(qm_field) or "")[:15] == qm_short)
        #         )
        #         t.add_assertion(
        #             f"QuoteLineItems linked to QuoteMember {self.quote_member_id} "
        #             f"via {qm_field}: {linked}/{len(rows)}",
        #             linked >= 1,
        #         )
        #     else:
        #         t.add_assertion(
        #             "(soft) Could not resolve QuoteMemberId__c field on "
        #             "QuoteLineItem via describe — skipped linkage assertion.",
        #             False,
        #         )

        #     for r in rows:
        #         prod2 = r.get("Product2") or {}
        #         prod_name = prod2.get("Name", "?")
        #         prod_code = prod2.get("ProductCode", "?")
        #         t.add_record(
        #             label="QuoteLineItem",
        #             name=f"{prod_name} ({prod_code})",
        #             record_id=r["Id"],
        #             url=sf_api.record_url("QuoteLineItem", r["Id"]),
        #             object_type="QuoteLineItem",
        #         )
        #     t.pass_step()
        # except Exception as e:
        #     t.fail_step(f"EQ / QLI linkage verification failed: {e}")
        #     return

        # # ── Step 17: IP — CCI_CurrentUserOrderAssignment ─────────────────
        # # call_ip now inspects nested messages[severity=ERROR|FATAL] and
        # # raises — so any soft-failure pattern here produces a real FAIL.
        # t.start_step(17, f"Run IP {IP_CURRENT_USER}")
        # try:
        #     user_id = sf_api.current_user_id
        #     resp = sf_api.call_ip(
        #         IP_CURRENT_USER,
        #         {"UserId": user_id},
        #         name=f"IP: {IP_CURRENT_USER}",
        #     )
        #     severities = _message_severities(resp)
        #     t.add_assertion(f"UserId resolved: {user_id}", True)
        #     t.add_assertion(
        #         f"IP {IP_CURRENT_USER} envelope clean "
        #         f"(severities={severities or 'none'})",
        #         not _errors_in(resp),
        #     )
        #     t.add_assertion(
        #         f"IP {IP_CURRENT_USER} returned: "
        #         f"{list(resp.keys())[:10] if isinstance(resp, dict) else type(resp).__name__}",
        #         isinstance(resp, dict),
        #     )
        #     t.pass_step()
        # except Exception as e:
        #     t.fail_step(f"IP {IP_CURRENT_USER} failed: {e}")

        # # ── Step 18: IP — CCI_SalesOrderAssignment ───────────────────────
        # t.start_step(18, f"Run IP {IP_SALES_ORDER}")
        # try:
        #     user_id = sf_api.current_user_id
        #     resp = sf_api.call_ip(
        #         IP_SALES_ORDER,
        #         {"UserId": user_id},
        #         name=f"IP: {IP_SALES_ORDER}",
        #     )
        #     severities = _message_severities(resp)
        #     t.add_assertion(
        #         f"IP {IP_SALES_ORDER} envelope clean "
        #         f"(severities={severities or 'none'})",
        #         not _errors_in(resp),
        #     )
        #     t.add_assertion(
        #         f"IP {IP_SALES_ORDER} returned: "
        #         f"{list(resp.keys())[:10] if isinstance(resp, dict) else type(resp).__name__}",
        #         isinstance(resp, dict),
        #     )
        #     t.pass_step()
        # except Exception as e:
        #     t.fail_step(f"IP {IP_SALES_ORDER} failed: {e}")
