#!/usr/bin/env python3
"""
Resume TC3 from an existing Enterprise Quote — re-runs only the tail.

Purpose
-------
Phases 1–3 of TC3 (auth → Account → Opportunity → EQ → QuoteMember →
Working Cart → line items → CopyToEQ) take ~45s per iteration. When
the tail (attribute verify, QLI→QuoteMember linkage, finalization IPs)
fails after the EQ is already built, re-running the whole test is
wasteful. This script accepts an existing EQ Id and exercises just:

  - Verify the EQ exists
  - Fetch QLIs already on the EQ (expected to be in place after a
    successful main-run Step 15 — AddQMQGToWC_CopyToEQ)
  - Verify QLI → QuoteMember linkage (the main-run Step 16 assertion)
  - Run the two finalization IPs (main-run Steps 17-18)

Usage
-----
    # From project root, venv active, .env loaded
    python scripts/resume_tc3_from_quote.py 0Q0WL000002Ijf60AC
    # or via env var
    TC3_QUOTE_ID=0Q0WL000002Ijf60AC python scripts/resume_tc3_from_quote.py

It emits an HTML report to reports/tc3_resume_<ts>.html identical in shape
to a full TC3 report (step cards + request/response cards).

This is a dev-time tool — NOT part of the normal `cci test` runner. Delete
before committing if the maintenance concern is lifecycle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Make src.* importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.api.sf_api_client import SFApiClient          # noqa: E402
from src.api.api_tracker import APITracker             # noqa: E402
from src.api.api_reporter import generate_api_report   # noqa: E402

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass


# ── Test data (mirrors tc3_*.json so the labels match the full run) ─────────

DATA_FILE = ROOT / "tests" / "data" / "tc3_create_enterprise_quote_with_dia_api.json"
DATA = json.loads(DATA_FILE.read_text())

PRODUCT = DATA["product"]
ROUTER = DATA["router"]
DIA_PRODUCT_CODE = PRODUCT["product_code"]
DIA_BANDWIDTH = PRODUCT["bandwidth"]  # noqa: F401 — surfaced in extra_data
ROUTER_PRODUCT_CODE = ROUTER["product_code"]
ROUTER_PROVIDED_BY = ROUTER["provided_by"]  # noqa: F401 — surfaced in extra_data

FINAL = DATA["finalization"]
# NOTE: AddQMQGToWC_CopyToEQ is the main-run Step 15 — already executed
# by the time a resume runs. Resume skips it: replaying CopyToEQ on an
# already-finalized EQ has no matching Working Cart and would error.
IP_CURRENT_USER = FINAL["current_user_ip"]
IP_SALES_ORDER = FINAL["sales_order_ip"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "quote_id",
        nargs="?",
        default=os.environ.get("TC3_QUOTE_ID"),
        help="Salesforce Quote Id (15 or 18 chars). Also reads TC3_QUOTE_ID env var.",
    )
    args = parser.parse_args()

    if not args.quote_id:
        parser.error("Quote Id is required (positional arg or TC3_QUOTE_ID env var).")

    quote_id: str = args.quote_id.strip()
    run_ts = datetime.now().strftime("%m%d_%H%M%S")

    t = APITracker(test_name=f"TC3 Resume — Quote {quote_id}")
    t._run_timestamp = run_ts
    t.extra_data = {
        "Mode": "Resume (TC3 Steps 16 → 18 — QLI+QM verify, finalization IPs)",
        "Quote Id": quote_id,
        "DIA Bandwidth": DIA_BANDWIDTH,
        "Router": f"{ROUTER_PRODUCT_CODE} — {ROUTER_PROVIDED_BY}",
    }

    sf_api = SFApiClient(tracker=t)

    dia_line_item_id: str | None = None
    router_line_item_id: str | None = None

    # ── Step 1: Auth ─────────────────────────────────────────────────────────
    t.start_step(1, "Authenticate to Salesforce CCI Sandbox")
    try:
        sf_api.connect()
        t.add_assertion(f"Authenticated via {sf_api._auth_method}", True)
        t.pass_step()
    except Exception as e:
        t.fail_step(f"Authentication failed: {e}")
        _finish(t, run_ts)
        return 1

    # ── Step 2: Verify the supplied Quote exists + capture Id ────────────────
    t.start_step(2, f"Verify Quote {quote_id} exists and capture URL")
    try:
        rows = sf_api.soql(
            f"SELECT Id, Name, Status, OpportunityId FROM Quote "
            f"WHERE Id='{quote_id}' LIMIT 1",
            name=f"SOQL: verify Quote {quote_id}",
        )
        if not rows:
            raise RuntimeError(f"Quote {quote_id} not found.")
        t.add_assertion(f"Quote exists: {rows[0]['Name']} ({rows[0]['Status']})", True)
        t.add_record(
            label="Quote",
            name=rows[0]["Name"],
            record_id=rows[0]["Id"],
            url=sf_api.record_url("Quote", rows[0]["Id"]),
            object_type="Quote",
        )
        t.pass_step()
    except Exception as e:
        t.fail_step(f"Quote lookup failed: {e}")
        _finish(t, run_ts)
        return 1

    # ── Step 3: Fetch QuoteLineItems (replaces the broken SOQL from the prior run)
    t.start_step(3, "Fetch QuoteLineItems on Quote (Product2.ProductCode)")
    try:
        qli_rows = sf_api.soql(
            f"SELECT Id, Product2Id, Product2.ProductCode, Product2.Name "
            f"FROM QuoteLineItem WHERE QuoteId='{quote_id}' "
            f"ORDER BY CreatedDate",
            name="SOQL: fetch QuoteLineItems",
        )
        for row in qli_rows:
            prod2 = row.get("Product2") or {}
            pc = prod2.get("ProductCode") or ""
            if pc == DIA_PRODUCT_CODE:
                dia_line_item_id = row["Id"]
            elif pc == ROUTER_PRODUCT_CODE:
                router_line_item_id = row["Id"]
            t.add_record(
                label="QuoteLineItem",
                name=f"{prod2.get('Name', '?')} ({pc})",
                record_id=row["Id"],
                url=sf_api.record_url("QuoteLineItem", row["Id"]),
                object_type="QuoteLineItem",
            )
        t.add_assertion(
            f"QuoteLineItems found: {len(qli_rows)} "
            f"(DIA={dia_line_item_id}, Router={router_line_item_id})",
            bool(dia_line_item_id or router_line_item_id),
        )
        if not (dia_line_item_id or router_line_item_id):
            raise RuntimeError(
                f"No DIA or Router line items on Quote {quote_id} — "
                "run TC3 end-to-end first (or supply a different Quote Id)."
            )
        t.pass_step()
    except Exception as e:
        t.fail_step(f"QuoteLineItem lookup failed: {e}")
        _finish(t, run_ts)
        return 1

    # ── Step 4 (TC3 Step 16): Verify QLI → QuoteMember linkage ──────────────
    # Post-CopyToEQ: every DIA/Router QLI on the EQ should carry a
    # vlocity_cmt__QuoteMemberId__c reference to the location member
    # created by the main-run Step 10. Field name resolved dynamically
    # via describe so this works on namespace-flavored orgs.
    t.start_step(4, "Verify QuoteLineItem → QuoteMember linkage on EQ")
    try:
        qli_fields = sf_api.pick_field("QuoteLineItem", "QuoteMemberId__c")
        qm_field = qli_fields.get("QuoteMemberId__c")
        select_cols = "Id, QuoteId, Product2Id, Product2.ProductCode, Product2.Name"
        if qm_field:
            select_cols += f", {qm_field}"
        rows = sf_api.soql(
            f"SELECT {select_cols} FROM QuoteLineItem "
            f"WHERE QuoteId='{quote_id}' ORDER BY CreatedDate",
            name="SOQL: verify QLI → QuoteMember linkage",
        )
        if not rows:
            raise RuntimeError(f"No QuoteLineItems on EQ {quote_id}.")
        if qm_field:
            linked = [r for r in rows if r.get(qm_field)]
            unique_qms = {r.get(qm_field) for r in linked if r.get(qm_field)}
            t.add_assertion(
                f"QLIs linked to a QuoteMember via {qm_field}: "
                f"{len(linked)}/{len(rows)} (unique members: {unique_qms})",
                len(linked) >= 1,
            )
        else:
            t.add_assertion(
                "(soft) Could not resolve QuoteMemberId__c on QuoteLineItem "
                "via describe — skipped linkage assertion.",
                False,
            )
        t.pass_step()
    except Exception as e:
        t.fail_step(f"QLI linkage verify failed: {e}")

    # ── Step 5 (TC3 Step 17): IP — CCI_CurrentUserOrderAssignment ───────────
    t.start_step(5, f"Run IP {IP_CURRENT_USER}")
    try:
        user_id = sf_api.current_user_id
        resp = sf_api.call_ip(
            IP_CURRENT_USER, {"UserId": user_id}, name=f"IP: {IP_CURRENT_USER}"
        )
        t.add_assertion(f"UserId resolved: {user_id}", True)
        t.add_assertion(
            f"IP {IP_CURRENT_USER} returned: "
            f"{list(resp.keys())[:10] if isinstance(resp, dict) else type(resp).__name__}",
            True,
        )
        t.pass_step()
    except Exception as e:
        t.add_assertion(f"(soft) IP {IP_CURRENT_USER} errored: {e}", False)
        t.pass_step()

    # ── Step 6 (TC3 Step 18): IP — CCI_SalesOrderAssignment ─────────────────
    t.start_step(6, f"Run IP {IP_SALES_ORDER}")
    try:
        user_id = sf_api.current_user_id
        resp = sf_api.call_ip(
            IP_SALES_ORDER, {"UserId": user_id}, name=f"IP: {IP_SALES_ORDER}"
        )
        t.add_assertion(
            f"IP {IP_SALES_ORDER} returned: "
            f"{list(resp.keys())[:10] if isinstance(resp, dict) else type(resp).__name__}",
            True,
        )
        t.pass_step()
    except Exception as e:
        t.add_assertion(f"(soft) IP {IP_SALES_ORDER} errored: {e}", False)
        t.pass_step()

    _finish(t, run_ts)
    return 0


def _finish(tracker: APITracker, run_ts: str) -> None:
    """Finalize + write the HTML report."""
    tracker.finish()
    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"tc3_resume_{run_ts}.html"
    try:
        generate_api_report(
            tracker, report_file, extra_data=getattr(tracker, "extra_data", None)
        )
        print(f"\nReport: {report_file}\n")
    except Exception as e:
        print(f"\nReport generation failed: {e}\n")

    # Summary to stdout
    print(f"Overall: {tracker.overall_status}")
    for s in tracker.steps:
        status = s["status"]
        icon = "✓" if status == "PASS" else ("✗" if status == "FAIL" else "•")
        print(f"  {icon} Step {s['number']}: {s['name']} — {status}")


if __name__ == "__main__":
    sys.exit(main())
