#!/usr/bin/env python3
"""
Capture all Salesforce XHR / API calls triggered during a TC1 UI run.

Wraps Playwright with page.on('request') / page.on('response') hooks that
filter for Vlocity Integration Procedure / OmniScript / Apex REST / Aura
traffic. Saves a single JSON bundle to tests/data/tc1_ip_capture.json
so we can see which IPs the UI actually fires, with what payloads.

Usage (run locally — from project root, with .env set):
    python scripts/capture_tc1_api_calls.py                # headed by default
    python scripts/capture_tc1_api_calls.py --headless     # no visible browser

What it does:
    1. Launches Chromium with network interception
    2. Runs tests/generated/test_cci_tc1_create_enterprise_quote_with_dia.py
       as a subprocess with CCI_CAPTURE=1 env var, which tells the test
       to register request/response listeners via the 'sf' fixture's page
    3. Dumps captured traffic to tests/data/tc1_ip_capture.json

Note: This is a one-off discovery tool — not a repeatable test.
      After capture, delete/ignore tc1_ip_capture.json in source control.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_PATH = PROJECT_ROOT / "tests" / "data" / "tc1_ip_capture.json"

# URL patterns we care about (substring match is enough)
INTERESTING_PATTERNS = [
    "/integrationprocedure/",
    "/apexrest/",
    "/actions/custom/",          # Invocable Apex via Lightning / SFDX
    "/aura",                     # Aura RPC used by Vlocity CPQ UI
    "/services/data/",           # standard REST
    "/cpq/",                     # CPQ-specific endpoints
    "/vlocity_cmt",              # any namespace-prefixed endpoint
    "/omnistudio",               # core Omnistudio endpoints
]


def install_capture_hook():
    """
    Capture is now wired directly into tests/conftest.py (gated by
    CCI_CAPTURE=1). This function only cleans up the orphan
    tests/conftest_capture.py file from the previous approach, which
    pytest never auto-loaded anyway.
    """
    stale = PROJECT_ROOT / "tests" / "conftest_capture.py"
    if stale.exists():
        stale.unlink()
        print(f"  Removed stale {stale} (hook now lives in tests/conftest.py).")
    else:
        print("  Capture hook is built into tests/conftest.py (no external file needed).")
    return None


def run_tc1(headless: bool, test_file: str):
    """Run TC1 with the capture hook active."""
    env = os.environ.copy()
    env["CCI_CAPTURE"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        test_file,
        "-s",
        "--tb=short",
    ]
    if headless:
        cmd.append("--headless")
    print(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    return proc.returncode


def load_capture() -> list[dict]:
    p = Path("/tmp/cci_capture.jsonl")
    if not p.exists():
        return []
    records = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def summarize(records: list[dict]) -> dict:
    """Produce a summary grouped by IP/endpoint for quick analysis."""
    from collections import Counter, defaultdict

    endpoint_counts = Counter()
    by_endpoint = defaultdict(list)

    for r in records:
        url = r.get("url", "")
        # For IPs, extract the Type_SubType from the URL tail
        if "/integrationprocedure/" in url:
            key = "IP: " + url.split("/integrationprocedure/", 1)[1].strip("/")
        elif "/apexrest/" in url:
            tail = url.split("/apexrest/", 1)[1].split("?")[0]
            key = "ApexREST: " + tail
        elif "/aura" in url:
            key = "Aura"
        elif "/actions/custom/" in url:
            key = "Invocable: " + url.split("/actions/custom/", 1)[1].split("?")[0]
        elif "/services/data/" in url:
            # Group by the /vXX.X/{resource}/ piece
            parts = url.split("/services/data/", 1)[1].split("/", 2)
            res = parts[1] if len(parts) > 1 else "?"
            key = f"REST /services/data/.../{res}"
        else:
            key = url.split("?")[0]

        endpoint_counts[key] += 1
        by_endpoint[key].append(r)

    # For each IP, keep a SAMPLE of the first request body
    samples = {}
    for key, recs in by_endpoint.items():
        sample = next((r for r in recs if r.get("body")), recs[0])
        samples[key] = {
            "count": len(recs),
            "method": sample.get("method"),
            "sample_url": sample.get("url"),
            "sample_request_body": sample.get("body"),
            "sample_response_body_start": (
                (sample.get("response_body") or "")[:500]
            ),
            "status_codes_seen": sorted(set(r.get("status") for r in recs if r.get("status"))),
        }

    return {
        "total_captured": len(records),
        "endpoint_counts": dict(endpoint_counts.most_common()),
        "samples": samples,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run TC1 without visible browser (not recommended for capture — see requests in real time)",
    )
    parser.add_argument(
        "--test",
        default="tests/generated/test_cci_tc1_create_enterprise_quote_with_dia.py",
        help="Test file to run (default: TC1)",
    )
    args = parser.parse_args()

    print("Step 1/3: Preparing capture hook (built into tests/conftest.py)...")
    install_capture_hook()

    print("Step 2/3: Running TC1 with CCI_CAPTURE=1 ...")
    Path("/tmp/cci_capture.jsonl").unlink(missing_ok=True)
    rc = run_tc1(args.headless, args.test)
    print(f"  TC1 exit code: {rc}")

    print("Step 3/3: Parsing captured traffic ...")
    records = load_capture()
    summary = summarize(records)

    CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_PATH.write_text(json.dumps({"summary": summary, "records": records}, indent=2, default=str))

    print()
    print(f"Captured {len(records)} API calls.")
    print(f"Full capture: {CAPTURE_PATH}")
    print()
    print("Top endpoints by call count:")
    for k, c in list(summary["endpoint_counts"].items())[:20]:
        print(f"  {c:>4}  {k}")


if __name__ == "__main__":
    main()
