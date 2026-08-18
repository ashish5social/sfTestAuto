"""Salesforce API connector for data validation and cleanup."""

import json
from typing import Optional
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed

from src.core.config import config


class SFConnector:
    """Connects to Salesforce for API-level operations."""

    def __init__(self):
        self._sf: Optional[Salesforce] = None

    def connect(self) -> Salesforce:
        """Establish connection to Salesforce."""
        if self._sf is None:
            try:
                self._sf = Salesforce(
                    username=config.SF_USERNAME,
                    password=config.SF_PASSWORD,
                    security_token=config.SF_SECURITY_TOKEN,
                    domain="login" if "login.salesforce" in config.SF_LOGIN_URL else "test",
                )
            except SalesforceAuthenticationFailed as e:
                raise ConnectionError(f"Failed to authenticate with Salesforce: {e}")
        return self._sf

    @property
    def sf(self) -> Salesforce:
        """Get the Salesforce connection (lazy init)."""
        return self.connect()

    def query(self, soql: str) -> list[dict]:
        """Execute a SOQL query and return records."""
        result = self.sf.query_all(soql)
        return result.get("records", [])

    def get_record(self, sobject: str, record_id: str) -> dict:
        """Get a single record by ID."""
        obj = getattr(self.sf, sobject)
        return obj.get(record_id)

    def create_record(self, sobject: str, data: dict) -> str:
        """Create a record and return its ID."""
        obj = getattr(self.sf, sobject)
        result = obj.create(data)
        return result["id"]

    def delete_record(self, sobject: str, record_id: str):
        """Delete a record by ID."""
        obj = getattr(self.sf, sobject)
        obj.delete(record_id)

    def cleanup_test_data(self, records: list[dict]):
        """
        Delete test records created during a test run.

        Args:
            records: List of dicts with 'sobject' and 'id' keys
        """
        errors = []
        for record in reversed(records):  # Delete in reverse order (children first)
            try:
                self.delete_record(record["sobject"], record["id"])
            except Exception as e:
                errors.append(f"Failed to delete {record['sobject']} {record['id']}: {e}")
        return errors

    def verify_record_exists(self, sobject: str, conditions: dict) -> Optional[dict]:
        """
        Check if a record exists matching the given conditions.

        Args:
            sobject: Salesforce object name
            conditions: Dict of field=value conditions

        Returns:
            The record dict if found, None otherwise
        """
        where_clauses = " AND ".join(
            f"{k} = '{v}'" for k, v in conditions.items()
        )
        soql = f"SELECT Id, Name FROM {sobject} WHERE {where_clauses} LIMIT 1"
        records = self.query(soql)
        return records[0] if records else None

    def get_org_info(self) -> dict:
        """Get basic org information."""
        try:
            org = self.sf.query(
                "SELECT Id, Name, OrganizationType, IsSandbox FROM Organization LIMIT 1"
            )
            return org["records"][0] if org["records"] else {}
        except Exception as e:
            return {"error": str(e)}

    def test_connection(self) -> dict:
        """Test the Salesforce connection and return status."""
        try:
            self.connect()
            org_info = self.get_org_info()
            return {
                "connected": True,
                "org_id": org_info.get("Id", ""),
                "org_name": org_info.get("Name", ""),
                "is_sandbox": org_info.get("IsSandbox", False),
            }
        except Exception as e:
            return {
                "connected": False,
                "error": str(e),
            }
