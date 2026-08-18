#!/usr/bin/env python3
"""
Cleanup CCIAUTO Test Data from Salesforce Sandbox
==================================================

Standalone script that connects to the Salesforce org, finds all test records
created by the Salesforce test automation (identified by the "CCIAUTO" keyword in
record names), and deletes them in reverse-creation order — keeping records
from the last N days.

Usage:
    python scripts/cleanup_test_data.py --keep-days 3
    python scripts/cleanup_test_data.py --keep-days 0          # delete ALL test data
    python scripts/cleanup_test_data.py --keep-days 3 --dry-run  # preview only

Environment variables (from .env or shell):
    SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN, SF_LOGIN_URL

This script is NOT inside tests/ so it won't appear in the UI runner or
GitHub Actions test dropdown.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env from project root if present
_project_root = Path(__file__).resolve().parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

try:
    from simple_salesforce import Salesforce
except ImportError:
    print("ERROR: simple-salesforce is required. Install with: pip install simple-salesforce")
    sys.exit(1)


# ── Constants ────────────────────────────────────────────────────────────────

MARKER = "CCIAUTO"

# Salesforce objects to scan, in deletion order (children first, parents last).
# Each entry: (SObject API name, Name field, optional parent-lookup field)
OBJECTS_TO_CLEAN = [
    {
        "sobject": "Quote",
        "name_field": "Name",
        "date_field": "CreatedDate",
        "label": "Quote",
    },
    {
        "sobject": "Opportunity",
        "name_field": "Name",
        "date_field": "CreatedDate",
        "label": "Opportunity",
    },
    {
        "sobject": "Contact",
        "name_field": "Name",
        "date_field": "CreatedDate",
        "label": "Contact",
    },
    {
        "sobject": "Account",
        "name_field": "Name",
        "date_field": "CreatedDate",
        "label": "Account",
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def connect_to_salesforce() -> Salesforce:
    """Authenticate via SOAP with My Domain instance override (CI-safe)."""
    sf_url = os.getenv("SF_LOGIN_URL", "https://test.salesforce.com")
    username = os.getenv("SF_USERNAME", "")
    password = os.getenv("SF_PASSWORD", "")
    token = os.getenv("SF_SECURITY_TOKEN", "")

    if not username or not password:
        print("ERROR: SF_USERNAME and SF_PASSWORD must be set.")
        sys.exit(1)

    # Determine domain for SOAP auth
    from urllib.parse import urlparse
    domain = "test"
    if "salesforce.com" in sf_url \
            and "test.salesforce.com" not in sf_url \
            and "login.salesforce.com" not in sf_url:
        host = urlparse(sf_url).hostname
        domain = host.replace(".salesforce.com", "")

    # Step 1: SOAP login to get session
    sf_tmp = Salesforce(
        username=username,
        password=password,
        security_token=token,
        domain=domain,
    )

    # Step 2: Re-create with My Domain instance to avoid DNS issues in CI
    my_domain = sf_url.rstrip("/").replace("https://", "")
    sf = Salesforce(instance=my_domain, session_id=sf_tmp.session_id)
    print(f"  Connected to {my_domain} as {username}")
    return sf


def find_test_records(sf: Salesforce, obj_config: dict, cutoff_date: str) -> list:
    """Query for CCIAUTO records created before the cutoff date."""
    sobject = obj_config["sobject"]
    name_field = obj_config["name_field"]
    date_field = obj_config["date_field"]

    query = (
        f"SELECT Id, {name_field}, {date_field} "
        f"FROM {sobject} "
        f"WHERE {name_field} LIKE '%{MARKER}%' "
        f"AND {date_field} < {cutoff_date} "
        f"ORDER BY {date_field} DESC"
    )

    try:
        result = sf.query_all(query)
        return result.get("records", [])
    except Exception as e:
        # Object might not exist (e.g., vlocity_cmt__Quote__c in some orgs)
        print(f"  WARNING: Could not query {sobject}: {e}")
        return []


def delete_records(sf: Salesforce, sobject: str, records: list, dry_run: bool) -> tuple:
    """Delete records one by one. Returns (success_count, error_count)."""
    success = 0
    errors = 0
    for rec in records:
        rec_id = rec["Id"]
        rec_name = rec.get("Name", rec_id)
        if dry_run:
            print(f"    [DRY RUN] Would delete {sobject} '{rec_name}' ({rec_id})")
            success += 1
        else:
            try:
                getattr(sf, sobject).delete(rec_id)
                print(f"    DELETED {sobject} '{rec_name}' ({rec_id})")
                success += 1
            except Exception as e:
                # Record may already be cascade-deleted (e.g., Opp deleted when Account was deleted)
                err_msg = str(e)
                if "ENTITY_IS_DELETED" in err_msg or "NOT_FOUND" in err_msg:
                    print(f"    SKIPPED {sobject} '{rec_name}' — already deleted (cascade)")
                    success += 1
                else:
                    print(f"    ERROR deleting {sobject} '{rec_name}': {e}")
                    errors += 1
    return success, errors


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Delete CCIAUTO test records from Salesforce, keeping recent data."
    )
    parser.add_argument(
        "--keep-days", type=int, required=True,
        help="Number of days of data to keep. Records older than this are deleted. Use 0 to delete ALL."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be deleted without actually deleting."
    )
    args = parser.parse_args()

    keep_days = args.keep_days
    dry_run = args.dry_run

    print("=" * 60)
    print("Salesforce Test Data Cleanup")
    print("=" * 60)
    print(f"  Marker:     {MARKER}")
    print(f"  Keep days:  {keep_days}")
    print(f"  Dry run:    {dry_run}")
    print()

    # Calculate cutoff date (UTC)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=keep_days)
    cutoff_soql = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  Cutoff:     {cutoff_soql} (delete records created before this)")
    print()

    # Connect
    print("Connecting to Salesforce...")
    sf = connect_to_salesforce()
    print()

    # Process each object type
    total_deleted = 0
    total_errors = 0
    total_found = 0
    summary = []

    for obj_config in OBJECTS_TO_CLEAN:
        sobject = obj_config["sobject"]
        label = obj_config["label"]
        print(f"--- {label} ({sobject}) ---")

        records = find_test_records(sf, obj_config, cutoff_soql)
        count = len(records)
        total_found += count

        if count == 0:
            print(f"  No {MARKER} records found to delete.")
            summary.append((label, 0, 0, 0))
            print()
            continue

        print(f"  Found {count} record(s) to delete.")
        deleted, errors = delete_records(sf, sobject, records, dry_run)
        total_deleted += deleted
        total_errors += errors
        summary.append((label, count, deleted, errors))
        print()

    # Print summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  {'Object':<25} {'Found':>6} {'Deleted':>8} {'Errors':>7}")
    print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*7}")
    for label, found, deleted, errors in summary:
        print(f"  {label:<25} {found:>6} {deleted:>8} {errors:>7}")
    print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*7}")
    print(f"  {'TOTAL':<25} {total_found:>6} {total_deleted:>8} {total_errors:>7}")
    print()

    if dry_run:
        print("  ** DRY RUN — no records were actually deleted **")
        print()

    if total_errors > 0:
        print(f"  WARNING: {total_errors} error(s) encountered during cleanup.")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
