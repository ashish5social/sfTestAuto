"""Query Salesforce for accounts containing 'CCI' in the name."""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from simple_salesforce import Salesforce

load_dotenv(Path(__file__).parent.parent / ".env")

# Authenticate via test.salesforce.com (SOAP login)
sf = Salesforce(
    username=os.getenv("SF_USERNAME"),
    password=os.getenv("SF_PASSWORD"),
    security_token=os.getenv("SF_SECURITY_TOKEN"),
    domain="test",
)

# Override instance URL — simple_salesforce resolves to fidium-test1 which
# doesn't exist in DNS. The correct My Domain is fidium--apitest1.
sf.sf_instance = "fidium--apitest1.sandbox.my.salesforce.com"
sf.base_url = f"https://{sf.sf_instance}/services/data/v{sf.sf_version}/"

result = sf.query_all(
    "SELECT Id, Name, CreatedDate, RecordType.Name "
    "FROM Account WHERE Name LIKE '%CCI%' ORDER BY CreatedDate DESC"
)

records = result.get("records", [])
print(f"\nTotal accounts found with 'CCI': {len(records)}\n")
print(f"{'Name':<60} {'Record Type':<20} {'Created Date':<12} {'Id'}")
print("-" * 120)
for r in records:
    name = r.get("Name", "")
    rt = r.get("RecordType") or {}
    rt_name = rt.get("Name", "N/A")
    created = r.get("CreatedDate", "")[:10]
    rid = r.get("Id", "")
    print(f"{name:<60} {rt_name:<20} {created:<12} {rid}")
