"""Test runner that loads YAML definitions and manages test lifecycle."""

import json
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import config
from src.core.database import Database


class TestDefinition:
    """Represents a test case loaded from YAML."""

    def __init__(self, data: dict, file_path: str = None):
        self.name: str = data.get("name", "Unnamed Test")
        self.description: str = data.get("description", "")
        self.salesforce_org: str = data.get("salesforce_org", "default")
        self.timeout: int = data.get("timeout", 300)
        self.max_steps: int = data.get("max_steps", 50)
        self.steps: list[str] = data.get("steps", [])
        self.expected_results: dict = data.get("expected_results", {})
        self.cleanup: dict = data.get("cleanup", {})
        self.tags: list[str] = data.get("tags", [])
        self.file_path: str = file_path

    @classmethod
    def from_yaml(cls, file_path: str | Path) -> "TestDefinition":
        """Load a test definition from a YAML file."""
        path = Path(file_path)
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(data, str(path))

    @classmethod
    def from_plain_text(cls, text: str, name: str = None) -> "TestDefinition":
        """Create a test definition from plain English text."""
        if name is None:
            name = f"Ad-hoc Test {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        return cls({
            "name": name,
            "description": text,
            "steps": [text],
            "timeout": 300,
        })

    def to_task_string(self) -> str:
        """Convert the test definition into a task string."""
        parts = [f"Test: {self.name}"]
        if self.description:
            parts.append(f"Description: {self.description}")
        if self.steps:
            parts.append("Steps:")
            for i, step in enumerate(self.steps, 1):
                parts.append(f"  {i}. {step}")
        if self.expected_results:
            parts.append("Expected Results:")
            for key, value in self.expected_results.items():
                parts.append(f"  - {key}: {value}")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "name": self.name,
            "description": self.description,
            "salesforce_org": self.salesforce_org,
            "timeout": self.timeout,
            "max_steps": self.max_steps,
            "steps": self.steps,
            "expected_results": self.expected_results,
            "cleanup": self.cleanup,
            "tags": self.tags,
            "file_path": self.file_path,
        }


class TestRunner:
    """Orchestrates test listing and history."""

    def __init__(self, db: Database = None):
        self.db = db or Database()

    def list_tests(self, directory: Path = None) -> list[dict]:
        """List all available test definitions."""
        directory = directory or config.TESTS_DIR
        tests = []

        yaml_files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))

        for yaml_file in yaml_files:
            try:
                test_def = TestDefinition.from_yaml(yaml_file)
                tests.append(test_def.to_dict())
            except Exception as e:
                tests.append({
                    "name": yaml_file.stem,
                    "file_path": str(yaml_file),
                    "error": str(e),
                })

        return tests
