"""Configuration management for sfauto."""

import os
from pathlib import Path
from dotenv import load_dotenv

from src.core.org_profile import load_profile

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    """Application configuration loaded from environment variables."""

    # Salesforce
    # Normalised via the org profile (adds https:// if the user omitted it).
    SF_LOGIN_URL: str = ""  # set below from PROFILE
    SF_USERNAME: str = os.getenv("SF_USERNAME", "")
    SF_PASSWORD: str = os.getenv("SF_PASSWORD", "")
    SF_SECURITY_TOKEN: str = os.getenv("SF_SECURITY_TOKEN", "")
    SF_ORG_ID: str = os.getenv("SF_ORG_ID", "")

    # Active org profile (profiles/<name>.yml). Everything that differs
    # between orgs — timezone, namespace, record prefix, label overrides —
    # lives here rather than in test code. See src/core/org_profile.py.
    PROFILE = load_profile()
    SF_LOGIN_URL = PROFILE.login_url

    # Browser (default: headed mode, set to true for headless)
    BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"

    # Dashboard
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8091"))
    DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")

    # Paths
    # The project lays tests out as tests/{ui,api}/, each containing its
    # own definitions/ and data/ subfolders. There is no top-level
    # definitions/ or data/ anymore.
    TESTS_DIR: Path = PROJECT_ROOT / "tests"
    UI_TESTS_DIR: Path = PROJECT_ROOT / "tests" / "ui"
    API_TESTS_DIR: Path = PROJECT_ROOT / "tests" / "api"
    REPORTS_DIR: Path = PROJECT_ROOT / "reports"
    SCREENSHOTS_DIR: Path = PROJECT_ROOT / "screenshots"
    DB_PATH: Path = PROJECT_ROOT / "sfauto_runs.db"

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing required config values.

        A password is only required when there is no better credential.
        JWT bearer authenticates with a private key and needs no password
        at all — demanding one would force CI to store a secret it never
        uses.
        """
        errors = []
        if not cls.SF_USERNAME:
            errors.append("SF_USERNAME is not set")

        has_jwt = bool(os.getenv("SF_JWT_KEY_FILE", "").strip()
                       and os.getenv("SF_CLIENT_ID", "").strip())
        has_client_creds = bool(os.getenv("SF_CLIENT_ID", "").strip()
                                and os.getenv("SF_CLIENT_SECRET", "").strip())
        if not (cls.SF_PASSWORD or has_jwt or has_client_creds):
            errors.append(
                "no usable credential — set SF_PASSWORD, or SF_JWT_KEY_FILE "
                "+ SF_CLIENT_ID (see docs/AUTHENTICATION.md)"
            )
        return errors

    @classmethod
    def ensure_dirs(cls):
        """Create required directories if they don't exist."""
        for d in [
            cls.UI_TESTS_DIR, cls.UI_TESTS_DIR / "definitions", cls.UI_TESTS_DIR / "data",
            cls.API_TESTS_DIR, cls.API_TESTS_DIR / "definitions", cls.API_TESTS_DIR / "data",
            cls.REPORTS_DIR, cls.SCREENSHOTS_DIR,
        ]:
            d.mkdir(parents=True, exist_ok=True)


config = Config()
