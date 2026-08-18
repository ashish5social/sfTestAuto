#!/usr/bin/env python3
"""
Probe product configuration for a TC* API test.

Given a Salesforce ProductCode (and optionally the target Price List), this
script discovers everything the Working-Cart / CopyToEQ flow needs to add
that product to an Enterprise Quote:

  1. Product2  — Id, Name, Family, IsActive, Type
  2. PricebookEntry on the Price List's Pricebook2 (for CPQ postCartsItems)
  3. Attribute set + default values (from
     Product2.vlocity_cmt__AttributeDefaultValues__c)
  4. Child-item map (for bundles — vlocity_cmt__ProductChildItem__c)
  5. A suggested JSON stanza you can paste into tc{N}_*.json

Why this exists
---------------
TC3 was built by hand-mapping DIA + Router from a TC1 UI capture. The
ih_cci_create_api_test skill uses this script so future TCs (TC4 = UCaaS,
TC5 = SD-WAN, …) skip the capture step: provide a ProductCode, get back a
ready-to-paste JSON config.

Usage
-----
    # From project root, venv active, .env loaded
    python scripts/probe_product_config.py CCI_COMM_DIA_UP_TO_2GBPS
    python scripts/probe_product_config.py CCI_COMM_DIA_UP_TO_2GBPS \\
        --price-list "CCI Commercial Pricing List"
    python scripts/probe_product_config.py CCI_COMM_DIA_UP_TO_2GBPS \\
        --as-json   # just the JSON stanza (pipe into jq, copy-paste, etc.)

Exit codes
----------
  0  Found and discovered cleanly
  1  Auth failure
  2  ProductCode not found / inactive
  3  PricebookEntry missing on target Price List
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.api.sf_api_client import SFApiClient  # noqa: E402
from src.api.api_tracker import APITracker     # noqa: E402


def _resolve_pricebook_id(
    sf_api: SFApiClient, price_list_name: str
) -> tuple[str | None, str | None, str | None]:
    """
    Return ``(price_list_object, price_list_id, pricebook2_id)``.

    Resolves the Price List's namespace-flavored API name, looks up the
    Price List by Name, and follows its ``*__PriceList__c`` → Pricebook2
    reference. Falls back to the standard Pricebook2 (IsStandard=true)
    when the Price List doesn't carry a Pricebook link.
    """
    price_list_object = sf_api.pick_object(
        "vlocity_cmt__PriceList__c",
        "omnistudio__PriceList__c",
        "PriceList__c",
    )
    if not price_list_object:
        return None, None, None

    pl_rows = sf_api.soql(
        f"SELECT Id FROM {price_list_object} "
        f"WHERE Name='{price_list_name}' LIMIT 1",
        name=f"SOQL: resolve Price List '{price_list_name}'",
    )
    if not pl_rows:
        return price_list_object, None, None
    price_list_id = pl_rows[0]["Id"]

    # Find the Pricebook2 linked from the Price List
    pl_fields = sf_api.pick_field(price_list_object, "Pricebook2Id__c")
    pbk_field = pl_fields.get("Pricebook2Id__c")
    pricebook_id: str | None = None
    if pbk_field:
        pl_rows2 = sf_api.soql(
            f"SELECT {pbk_field} FROM {price_list_object} "
            f"WHERE Id='{price_list_id}' LIMIT 1",
            name="SOQL: lookup Pricebook2 on Price List",
        )
        if pl_rows2:
            pricebook_id = pl_rows2[0].get(pbk_field)
    if not pricebook_id:
        # Fallback: the standard Pricebook (IsStandard=true)
        sp = sf_api.soql(
            "SELECT Id FROM Pricebook2 WHERE IsStandard=true LIMIT 1",
            name="SOQL: standard Pricebook2 (fallback)",
        )
        if sp:
            pricebook_id = sp[0]["Id"]
    return price_list_object, price_list_id, pricebook_id


def _describe_product(sf_api: SFApiClient, product_code: str) -> dict | None:
    """Return Product2 + (optional) AttributeDefaultValues + Type/Family."""
    # Detect the AttributeDefaultValues field name once (namespace-safe)
    p2_fields = sf_api.pick_field(
        "Product2", "AttributeDefaultValues__c", "Type__c", "ObjectType__c"
    )
    attr_field = p2_fields.get("AttributeDefaultValues__c")
    type_field = p2_fields.get("Type__c")
    obj_type_field = p2_fields.get("ObjectType__c")

    select_cols = ["Id", "Name", "ProductCode", "IsActive", "Family", "Description"]
    if attr_field:
        select_cols.append(attr_field)
    if type_field:
        select_cols.append(type_field)
    if obj_type_field:
        select_cols.append(obj_type_field)

    rows = sf_api.soql(
        f"SELECT {', '.join(select_cols)} FROM Product2 "
        f"WHERE ProductCode='{product_code}' LIMIT 1",
        name=f"SOQL: Product2 lookup ({product_code})",
    )
    if not rows:
        return None

    row = rows[0]
    out = {
        "Id": row["Id"],
        "Name": row.get("Name"),
        "ProductCode": row.get("ProductCode"),
        "IsActive": row.get("IsActive"),
        "Family": row.get("Family"),
        "Description": row.get("Description"),
        "Type": row.get(type_field) if type_field else None,
        "ObjectType": row.get(obj_type_field) if obj_type_field else None,
        "_attr_field": attr_field,
        "AttributeDefaults": {},
    }
    if attr_field:
        raw = row.get(attr_field)
        if isinstance(raw, str) and raw.strip():
            try:
                out["AttributeDefaults"] = json.loads(raw)
            except json.JSONDecodeError:
                out["AttributeDefaults"] = {"_raw": raw}
    return out


def _find_children(sf_api: SFApiClient, parent_product_id: str) -> list[dict]:
    """
    List bundle children via vlocity_cmt__ProductChildItem__c.

    Empty for non-bundle products (leaf DIA / Router / Seat etc.). For
    bundles, returns a list of ``{ChildProductCode, ChildProductId,
    Quantity, MinQuantity, MaxQuantity}`` dicts.
    """
    pci_object = sf_api.pick_object(
        "vlocity_cmt__ProductChildItem__c",
        "omnistudio__ProductChildItem__c",
        "ProductChildItem__c",
    )
    if not pci_object:
        return []
    pci_fields = sf_api.pick_field(
        pci_object,
        "ParentProductId__c",
        "ChildProductId__c",
        "Quantity__c",
        "MinQuantity__c",
        "MaxQuantity__c",
    )
    parent_f = pci_fields.get("ParentProductId__c")
    child_f = pci_fields.get("ChildProductId__c")
    if not parent_f or not child_f:
        return []
    qty_f = pci_fields.get("Quantity__c")
    min_f = pci_fields.get("MinQuantity__c")
    max_f = pci_fields.get("MaxQuantity__c")
    # Suffix-matcher on pick_field can occasionally resolve two
    # different logical suffixes to the same physical API name (e.g.
    # "Quantity__c" matches "MaxQuantity__c" when no unprefixed
    # "Quantity__c" exists on the object). De-duplicate so the SELECT
    # doesn't 400 with "duplicate field selected".
    cols: list[str] = []
    seen: set[str] = set()
    for f in ("Id", parent_f, child_f, qty_f, min_f, max_f):
        if f and f not in seen:
            cols.append(f)
            seen.add(f)
    # Null out any logical-name slots that share a physical field with
    # another slot — we keep the first slot (qty) and drop the rest
    # from the output dict so we don't read the same column twice under
    # two labels.
    if qty_f and qty_f == min_f:
        min_f = None
    if qty_f and qty_f == max_f:
        max_f = None
    if min_f and min_f == max_f:
        max_f = None
    rows = sf_api.soql(
        f"SELECT {', '.join(cols)} FROM {pci_object} "
        f"WHERE {parent_f}='{parent_product_id}'",
        name=f"SOQL: child items of {parent_product_id}",
    )
    if not rows:
        return []
    # Resolve child ProductCode/Name in a single SOQL
    child_ids = [r.get(child_f) for r in rows if r.get(child_f)]
    name_by_id: dict[str, dict] = {}
    if child_ids:
        in_list = ",".join(f"'{cid}'" for cid in child_ids)
        p_rows = sf_api.soql(
            f"SELECT Id, Name, ProductCode FROM Product2 WHERE Id IN ({in_list})",
            name="SOQL: resolve child Product2 codes",
        )
        for p in p_rows:
            name_by_id[p["Id"]] = p
    out = []
    for r in rows:
        cid = r.get(child_f)
        p = name_by_id.get(cid) or {}
        out.append(
            {
                "ChildProductId": cid,
                "ChildProductCode": p.get("ProductCode"),
                "ChildProductName": p.get("Name"),
                "Quantity": r.get(qty_f) if qty_f else None,
                "MinQuantity": r.get(min_f) if min_f else None,
                "MaxQuantity": r.get(max_f) if max_f else None,
            }
        )
    return out


def _find_pricebook_entry(
    sf_api: SFApiClient, product_id: str, pricebook_id: str
) -> dict | None:
    rows = sf_api.soql(
        f"SELECT Id, UnitPrice, IsActive, Pricebook2.Name FROM PricebookEntry "
        f"WHERE Product2Id='{product_id}' "
        f"AND Pricebook2Id='{pricebook_id}' AND IsActive=true LIMIT 1",
        name="SOQL: active PricebookEntry",
    )
    return rows[0] if rows else None


def _suggest_json(
    product_code: str,
    discovered: dict,
    attr_defaults: dict,
) -> dict:
    """Build a JSON stanza compatible with tc{N}_*.json shape."""
    short = product_code.split("_")[-1].lower() or "product"
    # Keep attribute codes ordered; surface first attribute as "bandwidth"-like
    # hint so the caller sees a suggested attribute to configure
    attr_keys = list(attr_defaults.keys())
    first_attr = attr_keys[0] if attr_keys else None
    suggested = {
        "display_name": discovered.get("Name") or product_code,
        "product_code": product_code,
        "_attribute_defaults": attr_defaults,  # full reference
    }
    if first_attr:
        suggested["attr_code_to_configure"] = first_attr
        suggested["attr_value_to_configure"] = attr_defaults.get(first_attr) or "<EDIT ME>"
    return suggested


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe a Salesforce Product2 for the API test generator."
    )
    parser.add_argument("product_code", help="Salesforce ProductCode (e.g. CCI_COMM_DIA_UP_TO_2GBPS)")
    parser.add_argument(
        "--price-list",
        default=os.environ.get("CCI_PRICE_LIST_NAME", "CCI Commercial Pricing List"),
        help="Price List Name (default: CCI Commercial Pricing List)",
    )
    parser.add_argument(
        "--as-json",
        action="store_true",
        help="Emit only the JSON stanza (for piping into jq / tee / paste)",
    )
    args = parser.parse_args()

    t = APITracker(test_name=f"Probe Product {args.product_code}")
    sf_api = SFApiClient(tracker=t)
    try:
        sf_api.connect()
    except Exception as e:
        print(f"[ERROR] Authentication failed: {e}", file=sys.stderr)
        return 1

    product = _describe_product(sf_api, args.product_code)
    if not product:
        print(
            f"[ERROR] Product2 with ProductCode '{args.product_code}' not found.",
            file=sys.stderr,
        )
        return 2
    if not product.get("IsActive"):
        print(
            f"[WARN] Product2 '{args.product_code}' is INACTIVE — it won't be "
            "addable to a cart. You may need to activate it or pick a different code.",
            file=sys.stderr,
        )

    pl_obj, pl_id, pb_id = _resolve_pricebook_id(sf_api, args.price_list)
    pbe: dict | None = None
    if pb_id:
        pbe = _find_pricebook_entry(sf_api, product["Id"], pb_id)

    children = _find_children(sf_api, product["Id"])

    # Build the suggested JSON stanza (safe even when PBE is missing)
    suggested = _suggest_json(
        args.product_code, product, product.get("AttributeDefaults") or {}
    )

    report = {
        "product_code": args.product_code,
        "product2": {
            "Id": product["Id"],
            "Name": product.get("Name"),
            "IsActive": product.get("IsActive"),
            "Family": product.get("Family"),
            "Type": product.get("Type"),
            "ObjectType": product.get("ObjectType"),
            "attribute_defaults_field": product.get("_attr_field"),
            "attribute_defaults": product.get("AttributeDefaults") or {},
        },
        "price_list": {
            "name": args.price_list,
            "object": pl_obj,
            "id": pl_id,
            "pricebook2_id": pb_id,
        },
        "pricebook_entry": pbe,
        "child_items": children,
        "suggested_json_stanza": suggested,
    }

    if args.as_json:
        print(json.dumps(suggested, indent=2))
    else:
        print(json.dumps(report, indent=2))

    if not pbe:
        print(
            f"\n[ERROR] No active PricebookEntry for "
            f"'{args.product_code}' on Pricebook '{pb_id}' "
            f"(Price List '{args.price_list}'). Add one in Salesforce "
            "Setup → Product → Price Book Entries before generating the test.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
