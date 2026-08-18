"""
Playwright Test Generator

Converts plain-text test definitions into runnable Playwright Python test scripts.
These generated scripts have ZERO AI dependency — they run independently via pytest.

Two modes of operation:
  1. TEMPLATE MODE: Generate a test skeleton from YAML definition + known page patterns
  2. COWORK MODE: Claude Code/Cowork browses the actual Salesforce pages first,
     learns the selectors/flows, then generates precise tests

Usage:
  generator = PlaywrightGenerator()
  script = generator.generate_from_yaml("tests/definitions/my_test.yaml")
  # or
  script = generator.generate_from_text("Log in, create order, verify pricing")
"""

import yaml
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import config


# Common Salesforce UI patterns and selectors
SF_PATTERNS = {
    "login": {
        "username_field": "#username",
        "password_field": "#password",
        "login_button": "#Login",
    },
    "navigation": {
        "app_launcher": "button.slds-icon-waffle",
        "search_box": "input[placeholder*='Search']",
        "global_search": "button[aria-label='Search']",
    },
    "lightning": {
        "tab_item": "one-app-nav-bar-item-root",
        "record_action": "runtime_platform_actions-action-renderer",
        "modal": "div.slds-modal__container",
        "toast": "div.slds-notify__content",
        "spinner": "div.slds-spinner_container",
    },
}


class PlaywrightGenerator:
    """Generates Playwright Python test scripts from test definitions."""

    def __init__(self, sf_url: str = None, sf_username: str = None, sf_password: str = None):
        self.sf_url = sf_url or config.SF_LOGIN_URL
        self.sf_username = sf_username or config.SF_USERNAME
        self.sf_password = sf_password or config.SF_PASSWORD

    def generate_from_yaml(self, yaml_path: str | Path) -> str:
        """Generate a Playwright test script from a YAML test definition."""
        path = Path(yaml_path)
        with open(path) as f:
            data = yaml.safe_load(f)

        return self._generate_script(
            name=data.get("name", "Unnamed Test"),
            description=data.get("description", ""),
            steps=data.get("steps", []),
            expected_results=data.get("expected_results", {}),
            timeout=data.get("timeout", 300),
            cleanup=data.get("cleanup", {}),
        )

    def generate_from_text(self, text: str, name: str = None) -> str:
        """Generate a Playwright test script from plain text instructions."""
        if name is None:
            name = f"Adhoc Test {datetime.now().strftime('%Y%m%d_%H%M')}"
        steps = [s.strip() for s in text.strip().split("\n") if s.strip()]
        if len(steps) == 1:
            # Single block of text — treat as one instruction
            steps = [text.strip()]
        return self._generate_script(name=name, description=text, steps=steps)

    def generate_from_page_knowledge(
        self,
        name: str,
        steps: list[str],
        page_selectors: dict[str, str],
        expected_results: dict = None,
    ) -> str:
        """
        Generate a precise test using real selectors learned from browsing the page.

        This is the COWORK MODE: Claude Code/Cowork browses Salesforce,
        extracts real selectors, and passes them here for script generation.

        Args:
            name: Test name
            steps: List of step descriptions
            page_selectors: Dict mapping logical names to CSS/XPath selectors
                           e.g. {"new_order_btn": "button[title='New Order']",
                                 "plan_dropdown": "select.plan-select"}
            expected_results: Expected outcomes
        """
        return self._generate_script(
            name=name,
            steps=steps,
            expected_results=expected_results or {},
            page_selectors=page_selectors,
        )

    def _generate_script(
        self,
        name: str,
        description: str = "",
        steps: list[str] = None,
        expected_results: dict = None,
        timeout: int = 300,
        cleanup: dict = None,
        page_selectors: dict = None,
    ) -> str:
        """Generate the full Playwright Python test script."""
        test_func_name = self._to_func_name(name)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        timeout_ms = timeout * 1000

        # Build step comments and placeholder code
        step_code = self._generate_step_code(steps or [], page_selectors)
        assertion_code = self._generate_assertions(expected_results or {})
        cleanup_code = self._generate_cleanup(cleanup or {})

        script = f'''"""
{name}
{description}

Generated: {timestamp}
Mode: {"Cowork (real selectors)" if page_selectors else "Template (customize selectors as needed)"}

Run: pytest {test_func_name}.py -v --headed
Run headless: pytest {test_func_name}.py -v
"""

import re
import pytest
from playwright.sync_api import Page, expect, TimeoutError as PWTimeout


# --- Configuration ---
SF_URL = "{self.sf_url}"
SF_USERNAME = "{self.sf_username}"
SF_PASSWORD = "{self.sf_password}"
DEFAULT_TIMEOUT = {timeout_ms}


# --- Helpers ---

def sf_login(page: Page):
    """Log into Salesforce."""
    page.goto(SF_URL)
    page.fill("{SF_PATTERNS['login']['username_field']}", SF_USERNAME)
    page.fill("{SF_PATTERNS['login']['password_field']}", SF_PASSWORD)
    page.click("{SF_PATTERNS['login']['login_button']}")
    # Wait for Lightning to load
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)  # Extra wait for SF Lightning


def wait_for_sf_spinner(page: Page, timeout: int = 30000):
    """Wait for Salesforce loading spinners to disappear."""
    try:
        spinner = page.locator("{SF_PATTERNS['lightning']['spinner']}")
        if spinner.count() > 0:
            spinner.first.wait_for(state="hidden", timeout=timeout)
    except PWTimeout:
        pass


def sf_navigate_to_tab(page: Page, tab_name: str):
    """Navigate to a Salesforce tab/app."""
    # Try nav bar first
    nav_item = page.locator(f"one-app-nav-bar-item-root a[title='{{tab_name}}']")
    if nav_item.count() > 0:
        nav_item.first.click()
    else:
        # Use App Launcher
        page.click("{SF_PATTERNS['navigation']['app_launcher']}")
        page.fill("input[placeholder*='Search']", tab_name)
        page.wait_for_timeout(1000)
        page.click(f"one-app-launcher-menu-item a:has-text('{{tab_name}}')")
    page.wait_for_load_state("networkidle")
    wait_for_sf_spinner(page)


def take_step_screenshot(page: Page, step_name: str, run_id: str):
    """Capture a screenshot for this step."""
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", step_name)[:50]
    path = f"screenshots/{{run_id}}/{{safe_name}}.png"
    try:
        import os
        os.makedirs(f"screenshots/{{run_id}}", exist_ok=True)
        page.screenshot(path=path)
    except Exception:
        pass


# --- Test ---

class Test{test_func_name.replace("test_", "").title().replace("_", "")}:
    """Test: {name}"""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Setup: login to Salesforce."""
        self.page = page
        self.page.set_default_timeout(DEFAULT_TIMEOUT)
        self.run_id = "Test_UI_{datetime.now().strftime('%m%d_%H%M')}"
        sf_login(self.page)
        yield
        # Teardown
{cleanup_code}

{step_code}

{assertion_code}
'''
        return script

    def _generate_step_code(self, steps: list[str], selectors: dict = None) -> str:
        """Generate code for each test step."""
        if not steps:
            return "    def test_placeholder(self):\n        pass\n"

        lines = []
        lines.append(f"    def test_execute(self):")
        lines.append(f'        """Execute all test steps."""')
        lines.append(f"        page = self.page")
        lines.append(f"")

        for i, step in enumerate(steps, 1):
            lines.append(f"        # Step {i}: {step}")
            lines.append(f'        take_step_screenshot(page, "step_{i}", self.run_id)')

            # Try to generate intelligent code based on step content
            step_lower = step.lower()

            if any(w in step_lower for w in ["log in", "login", "sign in"]):
                lines.append(f"        # Login handled in setup fixture")

            elif any(w in step_lower for w in ["navigate to", "go to"]):
                tab = self._extract_tab_name(step)
                if tab:
                    lines.append(f'        sf_navigate_to_tab(page, "{tab}")')
                else:
                    lines.append(f"        # TODO: Navigate to the correct page")
                    lines.append(f"        # sf_navigate_to_tab(page, 'TabName')")

            elif any(w in step_lower for w in ["click", "press", "select"]):
                if selectors:
                    sel = self._find_matching_selector(step, selectors)
                    if sel:
                        lines.append(f'        page.click("{sel}")')
                    else:
                        lines.append(f"        # TODO: Add correct selector")
                        lines.append(f'        # page.click("selector")')
                else:
                    lines.append(f"        # TODO: Add correct selector for this action")
                    lines.append(f'        # page.click("button:has-text(\'Button Text\')")')

            elif any(w in step_lower for w in ["fill", "enter", "type", "input"]):
                lines.append(f"        # TODO: Fill in the field")
                lines.append(f'        # page.fill("input[name=\'field\']", "value")')

            elif any(w in step_lower for w in ["verify", "check", "confirm", "assert", "expect"]):
                lines.append(f"        # TODO: Add assertion")
                lines.append(f'        # expect(page.locator("selector")).to_be_visible()')

            elif any(w in step_lower for w in ["wait", "pause"]):
                lines.append(f"        page.wait_for_load_state('networkidle')")
                lines.append(f"        wait_for_sf_spinner(page)")

            elif any(w in step_lower for w in ["search", "find", "open"]):
                lines.append(f"        # TODO: Search/find the record")
                lines.append(f'        # page.fill("input[placeholder*=\'Search\']", "search term")')

            elif any(w in step_lower for w in ["submit", "save"]):
                lines.append(f"        # TODO: Click submit/save button")
                lines.append(f'        # page.click("button:has-text(\'Submit\')")')
                lines.append(f"        page.wait_for_load_state('networkidle')")
                lines.append(f"        wait_for_sf_spinner(page)")

            else:
                lines.append(f"        # TODO: Implement this step")
                lines.append(f"        pass")

            lines.append(f"")

        lines.append(f'        take_step_screenshot(page, "final", self.run_id)')
        return "\n".join(lines)

    def _generate_assertions(self, expected: dict) -> str:
        """Generate assertion code from expected results."""
        if not expected:
            return ""

        lines = ["    def test_verify_results(self):"]
        lines.append('        """Verify expected results."""')
        lines.append("        page = self.page")
        lines.append("")

        for key, value in expected.items():
            lines.append(f"        # Verify: {key} = {value}")
            if isinstance(value, bool):
                lines.append(f"        # TODO: Assert {key} is {value}")
                lines.append(f'        # expect(page.locator("selector")).to_be_visible()')
            else:
                lines.append(f"        # TODO: Assert {key} equals '{value}'")
                lines.append(f'        # expect(page.locator("selector")).to_have_text("{value}")')
            lines.append("")

        return "\n".join(lines)

    def _generate_cleanup(self, cleanup: dict) -> str:
        """Generate cleanup/teardown code."""
        records = cleanup.get("delete_records", [])
        if not records:
            return "        pass  # No cleanup needed"

        lines = ["        # Cleanup: delete test records"]
        lines.append("        # Uncomment and configure when ready:")
        for record in records:
            lines.append(f"        # Delete {record} records created during test")
        lines.append("        pass")
        return "\n".join(lines)

    def _to_func_name(self, name: str) -> str:
        """Convert test name to a valid Python function name."""
        import re
        name = re.sub(r"[^a-zA-Z0-9\s]", "", name)
        name = re.sub(r"\s+", "_", name.strip()).lower()
        if not name.startswith("test_"):
            name = "test_" + name
        return name

    def _extract_tab_name(self, step: str) -> Optional[str]:
        """Try to extract a Salesforce tab name from a step description."""
        keywords = ["accounts", "contacts", "orders", "opportunities", "cases",
                     "leads", "products", "quotes", "contracts", "assets"]
        step_lower = step.lower()
        for kw in keywords:
            if kw in step_lower:
                return kw.title()
        return None

    def _find_matching_selector(self, step: str, selectors: dict) -> Optional[str]:
        """Find a matching selector from the provided selectors dict."""
        step_lower = step.lower()
        for key, selector in selectors.items():
            if key.lower().replace("_", " ") in step_lower:
                return selector
        return None


def generate_test(yaml_path: str, output_dir: str = None) -> Path:
    """
    Generate a Playwright test script from a YAML definition.

    Args:
        yaml_path: Path to YAML test definition
        output_dir: Directory to save the generated script (default: tests/)

    Returns:
        Path to the generated test file
    """
    generator = PlaywrightGenerator()
    script = generator.generate_from_yaml(yaml_path)

    yaml_name = Path(yaml_path).stem
    output_dir = Path(output_dir) if output_dir else config.PROJECT_ROOT / "tests"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"test_{yaml_name}.py"
    output_path.write_text(script)
    return output_path


def generate_all_tests(output_dir: str = None) -> list[Path]:
    """Generate Playwright scripts for all YAML definitions."""
    yaml_files = sorted(config.TESTS_DIR.glob("*.yaml")) + sorted(config.TESTS_DIR.glob("*.yml"))
    paths = []
    for yaml_file in yaml_files:
        path = generate_test(str(yaml_file), output_dir)
        paths.append(path)
    return paths
