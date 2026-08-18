#!/usr/bin/env python3
"""
Analyse tests/data/tc1_ip_capture.json to extract the Vlocity Apex
controllers / methods / Integration Procedure names that the Aura UI
invokes during TC1.

Why it's needed:
  - The TC1 UI runs inside Lightning (Aura), not via direct apexrest
    calls. 99% of traffic is POSTs to /aura?... whose request bodies
    look like:

        message = {"actions":[{
            "id":"123;a",
            "descriptor":"aura://ApexActionController/ACTION$execute",
            "params":{
                "namespace":"vlocity_cmt",
                "classname":"CpqAppHandler",
                "method":"invokeMethod",
                "params":{"sClassName":"CMTVlocityOpenInterface",
                          "sMethodName":"EntBusiness_CreateQuote",
                          "input":"{...}","options":"{...}"},
                ...
            }
        }]}

    So the IP name ends up two layers deep inside the Aura payload.

What this script does:
  1. Load tests/data/tc1_ip_capture.json
  2. For each /aura record: URL-decode + JSON-parse `message`
  3. Pull out each action's (classname, method) and any nested
     (sClassName, sMethodName), (ipMethod), (ProcedureKey), etc.
  4. Group + rank by frequency
  5. Also look at response_body for clues (status, data shape)
  6. Emit tests/data/tc1_aura_analysis.json with the distilled output

Usage:
    python scripts/analyze_tc1_capture.py
    # Or:
    python scripts/analyze_tc1_capture.py --input path/to/capture.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "tests" / "data" / "tc1_ip_capture.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "tests" / "data" / "tc1_aura_analysis.json"


# ── Payload parsing ───────────────────────────────────────────────────────

def parse_aura_message(body: str) -> list[dict]:
    """
    Extract the `actions` array from an Aura POST body.

    Aura POSTs are form-encoded: `message=<json>&aura.token=...&aura.context=...`.
    We strip the form wrapper, URL-decode, JSON-parse, and return
    `message['actions']`.
    """
    if not body:
        return []

    # Form-encoded?
    if body.startswith("message=") or "&message=" in body:
        try:
            form = parse_qs(body, keep_blank_values=True)
            msg_raw = (form.get("message") or [""])[0]
            if not msg_raw:
                return []
            payload = json.loads(msg_raw)
            return payload.get("actions", []) or []
        except Exception:
            return []

    # Sometimes Playwright reports post_data as the already-decoded JSON
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and "actions" in payload:
            return payload["actions"]
    except Exception:
        pass

    # Or just the inner message JSON, URL-encoded
    try:
        payload = json.loads(unquote_plus(body))
        if isinstance(payload, dict) and "actions" in payload:
            return payload["actions"]
    except Exception:
        pass

    return []


def _maybe_json(v):
    """If v is a JSON-encoded string, parse it. Otherwise return v."""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


def _deep_decode(node, depth: int = 0):
    """
    Recursively JSON-decode any string values that look like JSON,
    so sample dumps are readable instead of one-line escape hell.
    Stops at 6 levels deep to avoid runaway recursion.
    """
    if depth > 6:
        return node
    if isinstance(node, str):
        parsed = _maybe_json(node)
        if parsed is not node:
            return _deep_decode(parsed, depth + 1)
        return node
    if isinstance(node, list):
        return [_deep_decode(x, depth + 1) for x in node]
    if isinstance(node, dict):
        return {k: _deep_decode(v, depth + 1) for k, v in node.items()}
    return node


def flatten_action(action: dict) -> dict:
    """
    Pull the relevant identifiers out of one Aura action.

    We're looking at several shapes Vlocity components can take:
      - descriptor: aura://ApexActionController/ACTION$execute
        params: {namespace, classname, method, params: {...}}
      - params.params may itself contain {sClassName, sMethodName, input, options}
      - or {className, methodName, input, options}
      - or {procedureKey: "Business_CalculateMRRs", input:..., options:...}
    """
    out = {
        "descriptor": action.get("descriptor"),
        "outer_namespace": None,
        "outer_classname": None,
        "outer_method": None,
        "inner_classname": None,
        "inner_method": None,
        "ip_procedure_key": None,
        "input_preview": None,
        "options_preview": None,
    }

    params = action.get("params") or {}
    out["outer_namespace"] = params.get("namespace")
    out["outer_classname"] = params.get("classname") or params.get("className")
    out["outer_method"] = params.get("method") or params.get("methodName")

    # Inner params — where Vlocity nests the real target
    inner = params.get("params") or {}
    if isinstance(inner, str):
        inner = _maybe_json(inner) or {}
    if not isinstance(inner, dict):
        inner = {}

    # Vlocity uses several naming conventions — pick first non-empty
    for k in ("sClassName", "className", "classname", "apexClass"):
        if inner.get(k):
            out["inner_classname"] = inner[k]
            break
    for k in ("sMethodName", "methodName", "method", "apexMethod"):
        if inner.get(k):
            out["inner_method"] = inner[k]
            break

    # Integration Procedure: typically `procedureKey` or directly
    # in the payload as `Type_SubType`
    proc = (
        inner.get("procedureKey")
        or inner.get("ipName")
        or inner.get("integrationProcedure")
    )
    if proc:
        out["ip_procedure_key"] = proc

    # Capture a short preview of input/options for sampling later
    for k in ("input", "options"):
        v = inner.get(k)
        if v is None:
            continue
        pv = _maybe_json(v)
        out[f"{k}_preview"] = (
            json.dumps(pv, default=str)[:400]
            if not isinstance(pv, str)
            else pv[:400]
        )

    return out


# ── Deep walk: recursively find every method/class string ────────────────
#
# Vlocity nests params 2–4 levels deep. A single top-level action may
# hide its real intent behind several JSON-encoded strings. This walker
# recursively descends, JSON-parses strings-that-look-like-JSON, and
# collects every (key, value) pair where the key names a method/class.

_METHOD_KEYS = {
    "methodname", "method", "smethodname", "apexmethod",
    "action", "actionname", "procedurekey", "ipname", "ipmethod",
    "integrationprocedure", "invokemethod",
}
_CLASS_KEYS = {
    "classname", "sclassname", "apexclass", "class",
}


def walk_for_method_signals(node, acc: dict):
    """
    Recurse through a nested Aura action looking for method/class names.

    `acc` is accumulated: {"methods": Counter, "classes": Counter,
                           "class_method_pairs": Counter}

    JSON-encoded strings are auto-parsed so we can descend into them.
    """
    if isinstance(node, str):
        # Try to parse as JSON — if it's {..} or [..], recurse into it
        s = node.strip()
        if s.startswith(("{", "[")):
            try:
                parsed = json.loads(s)
                walk_for_method_signals(parsed, acc)
                return
            except Exception:
                return
        return

    if isinstance(node, list):
        for item in node:
            walk_for_method_signals(item, acc)
        return

    if not isinstance(node, dict):
        return

    # Find method + class values at THIS level
    m_val, c_val = None, None
    for k, v in node.items():
        kl = k.lower()
        if kl in _METHOD_KEYS and isinstance(v, str) and v:
            m_val = v
            acc["methods"][v] += 1
        if kl in _CLASS_KEYS and isinstance(v, str) and v:
            c_val = v
            acc["classes"][v] += 1
    if m_val and c_val:
        acc["class_method_pairs"][f"{c_val}.{m_val}"] += 1
    elif m_val and not c_val:
        acc["class_method_pairs"][f"(no-class).{m_val}"] += 1

    # Recurse into children
    for v in node.values():
        walk_for_method_signals(v, acc)


# ── Heuristic: infer IP name from a 'Type_SubType' string in the payload ──

IP_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+_[A-Za-z0-9_]+)\b")


def scan_for_ip_like_names(action: dict) -> list[str]:
    """
    Some IP invocations don't use procedureKey; instead the IP's
    Type_SubType string appears inside `input` JSON. This is a cheap
    keyword scan to surface such strings (high-false-positive — caller
    should inspect manually).
    """
    blob = json.dumps(action, default=str)
    matches = IP_NAME_RE.findall(blob)
    # Filter to plausible IP names (must have underscore, not too short,
    # not look like a class file)
    keep = []
    for m in matches:
        if len(m) < 5 or len(m) > 80:
            continue
        if any(bad in m.lower() for bad in ["controller", "component", "descriptor", "aura_", "lwc_"]):
            continue
        keep.append(m)
    return sorted(set(keep))


# ── Driver ────────────────────────────────────────────────────────────────

def analyze(capture_path: Path, output_path: Path):
    data = json.loads(capture_path.read_text())
    records = data.get("records") or data.get("full_records") or []
    if not records and "summary" in data:
        # Fall back to legacy shape
        records = data.get("records", [])

    print(f"Loaded {len(records)} raw records from {capture_path}")

    aura_records = [r for r in records if "/aura" in (r.get("url") or "").lower()]
    print(f"  {len(aura_records)} are /aura POSTs")

    descriptor_counter = Counter()
    outer_target_counter = Counter()      # "classname.method"
    inner_target_counter = Counter()      # nested Apex class.method
    ip_procedure_counter = Counter()
    ip_like_strings_counter = Counter()

    # Deep-walk signals: every method/class name anywhere in a payload tree
    deep_acc = {
        "methods": Counter(),
        "classes": Counter(),
        "class_method_pairs": Counter(),
    }

    samples_by_inner_target = defaultdict(list)   # first 3 payloads per (class.method)
    samples_by_ip = defaultdict(list)

    # Full-fidelity sample storage for the high-signal targets.
    # We keep up to 3 samples per key with the complete request params
    # (minus aura.token / auth headers — those are already redacted upstream).
    FULL_SAMPLE_TARGETS = [
        # Exact inner (sClassName/sMethodName) matches
        "CpqAppHandler.createCart",
        "B2BCmexAppHandler.getOsStandardRuntimeSetting",
        "IntegrationProcedureService.AddQMQGToWC_CopyToEQ",
        "IntegrationProcedureService.CCI_CurrentUserOrderAssignment",
        "IntegrationProcedureService.CCI_SalesOrderAssignment",
        "DefaultDROmniScriptIntegration.invokeOutboundDR",
    ]
    # Full samples keyed by descriptor+outer target (handleData, GenericInvoke2NoCont)
    FULL_SAMPLE_OUTER = [
        "vlocity_cmt.ComponentController.handleData",
        "omnistudiocore.BusinessProcessDisplayController.GenericInvoke2NoCont",
        "vlocity_cmt.BusinessProcessDisplayController.GenericInvoke2NoCont",
    ]
    full_samples = defaultdict(list)

    parse_failures = 0

    for rec in aura_records:
        body = rec.get("body")
        actions = parse_aura_message(body or "")
        if not actions and body:
            parse_failures += 1
            continue

        for a in actions:
            flat = flatten_action(a)
            if flat["descriptor"]:
                descriptor_counter[flat["descriptor"]] += 1
            outer_key = None
            if flat["outer_classname"]:
                outer_key = f"{flat['outer_namespace'] or '-'}.{flat['outer_classname']}.{flat['outer_method']}"
                outer_target_counter[outer_key] += 1

            inner_key = None
            if flat["inner_classname"]:
                inner_key = f"{flat['inner_classname']}.{flat['inner_method']}"
                inner_target_counter[inner_key] += 1
                if len(samples_by_inner_target[inner_key]) < 3:
                    samples_by_inner_target[inner_key].append({
                        "url_host": (rec.get("url") or "")[:80],
                        "status": rec.get("status"),
                        "input_preview": flat.get("input_preview"),
                        "options_preview": flat.get("options_preview"),
                    })
            if flat["ip_procedure_key"]:
                ip_procedure_counter[flat["ip_procedure_key"]] += 1
                if len(samples_by_ip[flat["ip_procedure_key"]]) < 3:
                    samples_by_ip[flat["ip_procedure_key"]].append({
                        "input_preview": flat.get("input_preview"),
                        "options_preview": flat.get("options_preview"),
                    })

            for m in scan_for_ip_like_names(a):
                ip_like_strings_counter[m] += 1

            # Deep walk for method/class signals anywhere in the tree
            walk_for_method_signals(a.get("params"), deep_acc)

            # Full-fidelity samples for the critical targets
            target_hits = []
            if inner_key and inner_key in FULL_SAMPLE_TARGETS:
                target_hits.append(("inner", inner_key))
            if outer_key and outer_key in FULL_SAMPLE_OUTER:
                target_hits.append(("outer", outer_key))

            for kind, key in target_hits:
                if len(full_samples[f"{kind}::{key}"]) >= 3:
                    continue
                # Rebuild a clean structure: decode embedded JSON strings for readability.
                params_clean = _deep_decode(a.get("params"))
                resp = rec.get("response_body")
                if isinstance(resp, str) and len(resp) > 2000:
                    resp = resp[:2000] + "  …(truncated)"
                full_samples[f"{kind}::{key}"].append({
                    "descriptor": a.get("descriptor"),
                    "status": rec.get("status"),
                    "params": params_clean,
                    "response_preview": resp,
                })

    summary = {
        "total_captured": len(records),
        "aura_records": len(aura_records),
        "aura_parse_failures": parse_failures,
        "descriptors_by_count": descriptor_counter.most_common(),
        "outer_apex_targets_by_count": outer_target_counter.most_common(),
        "inner_apex_targets_by_count": inner_target_counter.most_common(30),
        "integration_procedures_by_count": ip_procedure_counter.most_common(),
        "possible_ip_name_strings": ip_like_strings_counter.most_common(40),
        "deep_methods_by_count": deep_acc["methods"].most_common(60),
        "deep_classes_by_count": deep_acc["classes"].most_common(40),
        "deep_class_method_pairs": deep_acc["class_method_pairs"].most_common(60),
        "samples_by_inner_target": {k: v for k, v in list(samples_by_inner_target.items())[:30]},
        "samples_by_ip": dict(samples_by_ip),
        "full_samples_critical_targets": dict(full_samples),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, default=str))

    # ── Console report ──
    print()
    print("=" * 72)
    print("AURA ANALYSIS SUMMARY")
    print("=" * 72)
    print(f"Aura records parsed:   {len(aura_records) - parse_failures}")
    print(f"Parse failures:        {parse_failures}")
    print()
    print("Top descriptors:")
    for d, c in descriptor_counter.most_common(8):
        print(f"  {c:>5}  {d}")
    print()
    print("Top outer Apex targets (controller.method):")
    for t, c in outer_target_counter.most_common(15):
        print(f"  {c:>5}  {t}")
    print()
    print("Top INNER Apex targets (what Vlocity dispatches to):")
    for t, c in inner_target_counter.most_common(15):
        print(f"  {c:>5}  {t}")
    print()
    print("Explicit Integration Procedure keys:")
    if not ip_procedure_counter:
        print("  (none — IPs may be invoked via sClassName/sMethodName instead)")
    for k, c in ip_procedure_counter.most_common(20):
        print(f"  {c:>5}  {k}")
    print()
    print("Possible IP-shaped strings found in payloads (high-noise, top 20):")
    for s, c in ip_like_strings_counter.most_common(20):
        print(f"  {c:>5}  {s}")
    print()
    print("── DEEP WALK: every (class.method) pair found anywhere in payloads ──")
    print("    (including values nested inside JSON-encoded strings — this is where")
    print("     the real IP names and CPQ methods hide)")
    print()
    print("Top method names (any key that looks like methodName/action/procedureKey):")
    for m, c in deep_acc["methods"].most_common(30):
        print(f"  {c:>5}  {m}")
    print()
    print("Top (class.method) pairs (deep-walked):")
    for p, c in deep_acc["class_method_pairs"].most_common(30):
        print(f"  {c:>5}  {p}")
    print()
    print("Full sample payloads for critical targets captured → see "
          f"{output_path} → 'full_samples_critical_targets'")
    print("    keys captured in full_samples:")
    for key in full_samples:
        print(f"      {key}  ({len(full_samples[key])} sample(s))")
    print()
    print(f"Full analysis saved to {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    capture_path = Path(args.input)
    if not capture_path.exists():
        print(f"ERROR: capture file not found: {capture_path}")
        print("       Run `python scripts/capture_tc1_api_calls.py` first.")
        sys.exit(1)

    analyze(capture_path, Path(args.output))


if __name__ == "__main__":
    main()
