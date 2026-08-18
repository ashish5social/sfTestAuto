#!/usr/bin/env python3
"""
Probe the Salesforce org to discover which Omnistudio / Vlocity flavor
is installed:

  (a) vlocity_cmt managed package   — objects prefixed vlocity_cmt__
        - Integration Procedures live in vlocity_cmt__OmniScript__c
          with IsProcedure__c = true (older package), or in
          vlocity_cmt__OmniProcess__c (newer versions).
  (b) Core Omnistudio (post-migration) — no prefix
        - IPs live in OmniProcess.
  (c) Unknown / neither

Also reports:
  - Recommended API base URL for Integration Procedures
  - Count of active OmniProcesses / OmniScripts
  - Sample Quote/Order-related IPs
  - Recent CCIAUTO record samples (so we know the custom-field shape)

All Salesforce access goes through sf.query_all() / sf.<obj>.get() so the
correct instance URL (including the double-dash My-Domain host) is used
automatically — no raw requests.
"""

import json
import sys
from pathlib import Path

# Make src.core importable when run standalone
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simple_salesforce import Salesforce  # noqa: E402
from src.core.config import config  # noqa: E402


# Candidate objects where Integration Procedure metadata may live.
# Ordered by preference — the probe stops on first hit per namespace.
VLOCITY_CANDIDATES = [
    "vlocity_cmt__OmniProcess__c",
    "vlocity_cmt__OmniScript__c",
    "vlocity_cmt__InterfaceImplementation__c",
    "vlocity_cmt__VlocityUITemplate__c",
]
OMNISTUDIO_CORE_CANDIDATES = [
    "OmniProcess",
    "omnistudio__OmniProcess__c",
    "OmniProcessVersion",
]


def connect() -> Salesforce:
    """
    SOAP login, then re-create client with the My-Domain host from
    SF_LOGIN_URL. This sidesteps simple-salesforce's habit of returning a
    single-dash `sf_instance` (fidium-test1...) when the real DNS host
    uses double dashes (fidium--test1...). Same pattern used by
    scripts/cleanup_test_data.py.
    """
    from urllib.parse import urlparse

    login_url = config.SF_LOGIN_URL.rstrip("/")

    # Derive the `domain` kwarg for initial SOAP login.
    if "test.salesforce.com" in login_url or "login.salesforce.com" in login_url:
        domain = "test" if "test.salesforce.com" in login_url else "login"
    else:
        host = urlparse(login_url).hostname or ""
        domain = host.replace(".salesforce.com", "") if host else "test"

    # Step 1 — SOAP login (we only use its session_id)
    sf_tmp = Salesforce(
        username=config.SF_USERNAME,
        password=config.SF_PASSWORD,
        security_token=config.SF_SECURITY_TOKEN,
        domain=domain,
    )

    # Step 2 — re-create with the correct My-Domain host (no DNS surprises)
    my_domain_host = login_url.replace("https://", "").replace("http://", "")
    sf = Salesforce(instance=my_domain_host, session_id=sf_tmp.session_id)
    return sf


def safe_count(sf: Salesforce, sobject: str) -> int | None:
    """Return count of records in an sObject, or None if not accessible."""
    try:
        r = sf.query_all(f"SELECT COUNT() FROM {sobject}")
        return r.get("totalSize")
    except Exception:
        return None


def probe_namespace(sf: Salesforce) -> dict:
    """Determine which Omnistudio namespace(s) are present."""
    result = {
        "vlocity_cmt": False,
        "omnistudio_core": False,
        "candidates": [],
        "hit_object_vlocity_cmt": None,
        "hit_object_omnistudio_core": None,
    }

    for name in VLOCITY_CANDIDATES:
        c = safe_count(sf, name)
        if c is not None:
            result["vlocity_cmt"] = True
            if result["hit_object_vlocity_cmt"] is None:
                result["hit_object_vlocity_cmt"] = name
            result["candidates"].append({"sobject": name, "count": c})

    for name in OMNISTUDIO_CORE_CANDIDATES:
        c = safe_count(sf, name)
        if c is not None:
            result["omnistudio_core"] = True
            if result["hit_object_omnistudio_core"] is None:
                result["hit_object_omnistudio_core"] = name
            result["candidates"].append({"sobject": name, "count": c})

    return result


def describe_object(sf: Salesforce, sobject: str) -> list[str] | None:
    """Return sorted field API names on an object, or None if not accessible."""
    try:
        meta = getattr(sf, sobject).describe()
        return sorted(f["name"] for f in meta.get("fields", []))
    except Exception:
        return None


def _pick_fields(all_fields: list[str], wanted_substrings: list[str]) -> list[str]:
    """Pick fields whose API name case-insensitively contains any wanted substring."""
    lower = {f.lower(): f for f in all_fields}
    picked = []
    for sub in wanted_substrings:
        for lk, orig in lower.items():
            if sub in lk and orig not in picked:
                picked.append(orig)
    return picked


def list_ips(sf: Salesforce, obj: str) -> list[dict]:
    """
    List top records from the detected OmniProcess/OmniScript object.

    Queries only the fields that actually exist on the object (different
    Vlocity versions expose different field sets).
    """
    fields = describe_object(sf, obj) or []
    # Always include Id; then try to pick the common identifying fields.
    wanted = [
        "id",
        "name", "uniquename",
        "type", "subtype",
        "isactive",
        "isprocedure", "isintegrationprocedure",
        "language", "omniprocesstype",
    ]
    picked = _pick_fields(fields, wanted)
    if "Id" not in picked:
        picked = ["Id"] + picked

    q = f"SELECT {', '.join(picked)} FROM {obj} LIMIT 500"
    try:
        records = sf.query_all(q)["records"]
    except Exception:
        # Fall back to minimal query
        records = sf.query_all(f"SELECT Id FROM {obj} LIMIT 500")["records"]

    # Filter to records whose fields mention quote/order/enterprise/etc.
    keywords = [
        "quote", "order", "enterprise", "dia", "cart", "cpq", "fbb",
        "submit", "contract", "opportunity", "calculate", "validate",
        "business", "pricing", "mrr",
    ]
    hits = []
    for r in records:
        blob = " ".join(str(v) for v in r.values() if v and not isinstance(v, dict)).lower()
        if any(k in blob for k in keywords):
            # Strip simple-salesforce's attributes key
            clean = {k: v for k, v in r.items() if k != "attributes"}
            hits.append(clean)
    return hits[:100]


def sample_cciauto_records(sf: Salesforce) -> dict:
    """Pull newest CCIAUTO records to understand custom-field structure."""
    out = {}
    for obj in ["Account", "Opportunity", "Quote", "Contact"]:
        try:
            r = sf.query_all(
                f"SELECT Id, Name, CreatedDate FROM {obj} "
                f"WHERE Name LIKE '%CCIAUTO%' ORDER BY CreatedDate DESC LIMIT 1"
            )
            if not r["records"]:
                out[obj] = None
                continue
            rec = r["records"][0]
            rec_id = rec["Id"]
            # sf.<Obj>.get() uses the correct instance URL internally
            full = getattr(sf, obj).get(rec_id)
            # Only keep non-null custom fields (__c / __r suffix)
            custom = {
                k: v for k, v in full.items()
                if ("__c" in k or "__r" in k) and v not in (None, "", [])
            }
            out[obj] = {
                "id": rec_id,
                "name": rec.get("Name"),
                "created": rec.get("CreatedDate"),
                "custom_fields_set": sorted(custom.keys()),
                "custom_fields_sample": {
                    k: (str(v)[:120] if not isinstance(v, (int, float, bool)) else v)
                    for k, v in list(custom.items())[:20]
                },
            }
        except Exception as e:
            out[obj] = {"error": str(e)}
    return out


def probe_ip_endpoint(sf: Salesforce) -> dict:
    """
    Confirm which IP REST endpoint responds. Tries a harmless GET — a 404
    means the path is wrong; any other response means the endpoint exists.

    Uses sf.restful() so it goes through the correct instance URL.
    """
    result = {}
    for ns in ("vlocity_cmt", "omnistudio"):
        path = f"/services/apexrest/{ns}/v1/integrationprocedure"
        try:
            # No such IP — expect 404 with Salesforce's Apex REST "Resource
            # does not exist" message if the path prefix IS recognized, or
            # an "Invalid URL" / no response if the namespace is absent.
            resp = sf.restful(path.replace("/services/apexrest/", "apexrest/", 1), method="GET")
            result[ns] = {"ok": True, "sample_response": str(resp)[:300]}
        except Exception as e:
            msg = str(e)
            # simple_salesforce wraps non-2xx as SalesforceError; the text
            # still tells us whether the URL base was valid.
            recognized = any(
                s in msg.lower() for s in [
                    "integrationprocedure", "resource does not exist",
                    "invalid mapping", "not found",
                ]
            )
            result[ns] = {"ok": False, "recognized_base_path": recognized, "error": msg[:300]}
    return result


def main():
    print("Connecting to Salesforce...")
    sf = connect()
    print(f"  Connected. sf_instance={sf.sf_instance}  API version={sf.sf_version}")

    print("\n— Namespace probe (checking multiple object candidates) —")
    ns_info = probe_namespace(sf)
    print(json.dumps(ns_info, indent=2))

    if ns_info["vlocity_cmt"]:
        ns = "vlocity_cmt"
        ip_base = "/services/apexrest/vlocity_cmt/v1/integrationprocedure"
        ip_object = ns_info["hit_object_vlocity_cmt"]
    elif ns_info["omnistudio_core"]:
        ns = "omnistudio_core"
        ip_base = "/services/apexrest/omnistudio/v1/integrationprocedure"
        ip_object = ns_info["hit_object_omnistudio_core"]
    else:
        ns = "unknown"
        ip_base = None
        ip_object = None

    print(f"\n— Namespace decision: {ns} —")
    print(f"  IP endpoint base: {ip_base}")
    print(f"  IP metadata object: {ip_object}")

    print("\n— Probing IP REST endpoints directly —")
    ip_probe = probe_ip_endpoint(sf)
    print(json.dumps(ip_probe, indent=2))

    ip_hits = []
    if ip_object:
        print(f"\n— Quote/Order-related entries in {ip_object} —")
        ip_hits = list_ips(sf, ip_object)
        print(f"  Found {len(ip_hits)} matches (showing first 30):")
        for h in ip_hits[:30]:
            print(f"    {h}")

    print("\n— Sample CCIAUTO records with custom fields —")
    sample = sample_cciauto_records(sf)
    print(json.dumps(sample, indent=2, default=str))

    # Save full probe to disk
    out_path = Path(__file__).resolve().parent.parent / "tests" / "data" / "_namespace_probe.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "namespace": ns,
                "ip_base": ip_base,
                "ip_object": ip_object,
                "namespace_probe": ns_info,
                "ip_endpoint_probe": ip_probe,
                "quote_related_ips": ip_hits,
                "cciauto_samples": sample,
            },
            indent=2,
            default=str,
        )
    )
    print(f"\nSaved full probe to {out_path}")


if __name__ == "__main__":
    main()
