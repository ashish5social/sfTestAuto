"""Test data cleanup utilities."""

from src.core.sf_connector import SFConnector


def cleanup_test_records(connector: SFConnector, sobjects: list[str], prefix: str = "Test_CCI"):
    """
    Delete test records matching a naming prefix.

    Args:
        connector: SFConnector instance
        sobjects: List of Salesforce object names to clean up
        prefix: Name prefix to match for deletion
    """
    errors = []
    for sobject in sobjects:
        try:
            records = connector.query(
                f"SELECT Id, Name FROM {sobject} WHERE Name LIKE '{prefix}%' LIMIT 100"
            )
            for record in records:
                try:
                    connector.delete_record(sobject, record["Id"])
                except Exception as e:
                    errors.append(f"Failed to delete {sobject} {record['Id']}: {e}")
        except Exception as e:
            errors.append(f"Failed to query {sobject}: {e}")

    return errors
