"""Per-org configuration profiles.

Everything that differs between one Salesforce org and the next lives in a
YAML profile under ``profiles/``, never in test code. Adding a new org is:

    cp profiles/example.yml profiles/acme-uat.yml
    # edit the 6 fields that matter
    SFAUTO_PROFILE=acme-uat sfauto test tests/ui

Resolution order (later wins):
    1. profiles/<name>.yml
    2. profiles/<name>.local.yml   (git-ignored — personal overrides)
    3. environment variables       (SF_LOGIN_URL, SF_TIMEZONE, ...)

Env always wins so CI can override any profile value without editing files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = PROJECT_ROOT / "profiles"


@dataclass
class OrgProfile:
    """Resolved settings for one Salesforce org."""

    name: str = "default"

    # ── Connection ────────────────────────────────────────────────────
    login_url: str = "https://test.salesforce.com"
    api_version: str = "v59.0"

    # ── Behaviour ─────────────────────────────────────────────────────
    # Org timezone. Date fields typed into Lightning are interpreted in the
    # *org's* timezone, so "today" must be computed there, not on the runner.
    timezone: str = "UTC"
    # Managed-package namespace for Salesforce Industries / OmniStudio
    # (e.g. "vlocity_cmt", "vlocity_ins", "omnistudio"). Empty = core only.
    namespace: str = ""
    # Some orgs restrict login by IP; frontdoor.jsp bypass uses a SOAP
    # session id. "auto" probes and falls back only when needed.
    login_strategy: str = "auto"          # API auth: auto | standard | frontdoor

    # UI login strategy — see src/core/sf_ui/login_strategies.py
    #   auto | password | frontdoor | storage_state | google_sso | <your own>
    ui_login: str = "auto"
    ui_login_options: Dict[str, Any] = field(default_factory=dict)

    # ── Test data ─────────────────────────────────────────────────────
    # Prefix for every record the suite creates, so cleanup can find them.
    record_prefix: str = "SFAUTO"

    # ── Overrides ─────────────────────────────────────────────────────
    # Field-label overrides where an org has renamed a standard label.
    labels: Dict[str, str] = field(default_factory=dict)
    # Free-form values referenced by tests via profile.get("key").
    extras: Dict[str, Any] = field(default_factory=dict)

    # ── Derived ───────────────────────────────────────────────────────
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def label(self, key: str, default: str) -> str:
        """Org-specific label for a logical field, else the default."""
        return self.labels.get(key, default)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def ns(self, api_name: str) -> str:
        """Prefix an API name with the managed-package namespace if set.

        >>> OrgProfile(namespace="vlocity_cmt").ns("Product2__c")
        'vlocity_cmt__Product2__c'
        """
        if not self.namespace or "__" in api_name.split("__")[0]:
            return api_name
        return f"{self.namespace}__{api_name}"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_ENV_MAP = {
    "login_url": "SF_LOGIN_URL",
    "api_version": "SF_API_VERSION",
    "timezone": "SF_TIMEZONE",
    "namespace": "SF_API_NAMESPACE",
    "login_strategy": "SF_LOGIN_STRATEGY",
    "record_prefix": "SFAUTO_RECORD_PREFIX",
    "ui_login": "SF_UI_LOGIN",
}


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("pyyaml is required to read org profiles")
    return yaml.safe_load(path.read_text()) or {}


def _normalise_url(url: str) -> str:
    """Add a scheme if missing and strip a trailing slash.

    A bare host like ``acme.my.salesforce.com`` makes urlparse().hostname
    return None, which used to silently collapse the SOAP domain to
    "test" and produce a misleading INVALID_LOGIN against the wrong
    endpoint. Normalising here fixes every consumer at once.
    """
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def load_profile(name: str | None = None) -> OrgProfile:
    """Load an org profile by name (default: $SFAUTO_PROFILE or 'default')."""
    name = name or os.getenv("SFAUTO_PROFILE", "default")

    data: Dict[str, Any] = {"name": name}
    data.update(_read_yaml(PROFILES_DIR / f"{name}.yml"))
    data.update(_read_yaml(PROFILES_DIR / f"{name}.local.yml"))

    for attr, env_key in _ENV_MAP.items():
        val = os.getenv(env_key)
        if val:
            data[attr] = val

    known = {f for f in OrgProfile.__dataclass_fields__}
    extras = {k: v for k, v in data.items() if k not in known}
    clean = {k: v for k, v in data.items() if k in known}
    clean.setdefault("extras", {})
    clean["extras"] = {**extras, **(clean.get("extras") or {})}
    clean["name"] = name
    if clean.get("login_url"):
        clean["login_url"] = _normalise_url(clean["login_url"])
    return OrgProfile(**clean)


def available_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(
        p.stem for p in PROFILES_DIR.glob("*.yml") if not p.stem.endswith(".local")
    )
