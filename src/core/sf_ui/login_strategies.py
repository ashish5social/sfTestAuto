"""Pluggable UI login strategies.

Different orgs authenticate differently — a plain username/password form,
a federated SSO button (Google / Okta / SAML), or an IP-gated org that
needs the frontdoor session bypass. Rather than branching inside every
test, the strategy is chosen per-org in ``profiles/<org>.yml``:

    ui_login: auto            # auto | password | frontdoor | storage_state | google_sso
    ui_login_options:
      storage_state_path: .auth/orgfarm.json

Adding your own is three lines — subclass, set ``name``, decorate:

    @register
    class OktaLogin(LoginStrategy):
        name = "okta"
        def login(self, page, ctx):
            page.click("text=Sign in with Okta")
            ...
            return page.url

Then set ``ui_login: okta`` in the profile. Nothing else changes.

Why storage_state matters
-------------------------
For SSO orgs there is often no password to type — and scripting a real
identity provider's login form is both fragile (bot detection) and
usually against that provider's terms. ``storage_state`` sidesteps it:
a human logs in once via ``sfauto auth capture``, Playwright saves the
cookies, and every subsequent run reuses that session.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from playwright.sync_api import Page

from src.core.sf_ui import auth as _auth
from src.core.sf_ui.waits import wait_page_ready


# ── Context handed to every strategy ──────────────────────────────────

@dataclass
class LoginContext:
    """Everything a strategy might need, resolved once by the caller."""
    sf_url: str
    username: str = ""
    password: str = ""
    security_token: str = ""
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    options: Dict[str, Any] = None          # profile ui_login_options

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.options or {}).get(key, default)


# ── Registry ──────────────────────────────────────────────────────────

REGISTRY: Dict[str, type["LoginStrategy"]] = {}


def register(cls: type["LoginStrategy"]) -> type["LoginStrategy"]:
    REGISTRY[cls.name] = cls
    return cls


class LoginStrategy(ABC):
    """Base class. ``name`` is what goes in the profile's ``ui_login``."""

    name: str = "base"

    @abstractmethod
    def login(self, page: Page, ctx: LoginContext) -> str:
        """Authenticate and return the landed URL. Raise on failure."""

    @classmethod
    def looks_applicable(cls, page: Page, ctx: LoginContext) -> bool:
        """Used only by ``auto`` to pick a strategy from the live page."""
        return False


# ── 1. Standard username / password form ──────────────────────────────

@register
class PasswordLogin(LoginStrategy):
    """Classic Salesforce login page: #username, #password, #Login."""

    name = "password"

    def login(self, page: Page, ctx: LoginContext) -> str:
        _auth.standard_login(page, ctx.sf_url, ctx.username, ctx.password)
        wait_page_ready(page)
        return page.url

    @classmethod
    def looks_applicable(cls, page: Page, ctx: LoginContext) -> bool:
        try:
            return page.locator("#username").count() > 0 and page.locator("#Login").count() > 0
        except Exception:
            return False


# ── 2. Frontdoor session bypass ───────────────────────────────────────

@register
class FrontdoorLogin(LoginStrategy):
    """Obtain a session id out-of-band, then /secur/frontdoor.jsp straight
    into Lightning. Works even when the login page is SSO-gated, provided
    API auth succeeds (OAuth client-credentials or JWT)."""

    name = "frontdoor"

    def login(self, page: Page, ctx: LoginContext) -> str:
        url = _auth.get_frontdoor_url(
            sf_url=ctx.sf_url, username=ctx.username, password=ctx.password,
            security_token=ctx.security_token,
            client_id=ctx.client_id, client_secret=ctx.client_secret,
        )
        page.goto(url, wait_until="domcontentloaded")
        wait_page_ready(page)
        return page.url


# ── 3. Saved browser session (best for SSO / MFA orgs) ────────────────

@register
class StorageStateLogin(LoginStrategy):
    """Reuse cookies captured by ``sfauto auth capture``.

    No credentials are typed by the framework at all — a human authenticated
    once, interactively, and Playwright persisted the resulting session.
    """

    name = "storage_state"
    DEFAULT_PATH = ".auth/storage_state.json"

    def login(self, page: Page, ctx: LoginContext) -> str:
        path = Path(ctx.opt("storage_state_path", self.DEFAULT_PATH))
        if not path.exists():
            raise RuntimeError(
                f"No saved session at {path}.\n"
                f"Create one (opens a browser, you log in yourself):\n"
                f"    sfauto auth capture\n"
                f"The session is reused until it expires."
            )
        # Cookies are applied to the whole context, so every page in this
        # run is already authenticated.
        state = json.loads(path.read_text())
        page.context.add_cookies(state.get("cookies", []))
        page.goto(ctx.sf_url, wait_until="domcontentloaded")
        wait_page_ready(page)
        if "login.salesforce.com" in page.url or "/login" in page.url:
            raise RuntimeError(
                f"Saved session at {path} has expired — re-run: sfauto auth capture"
            )
        return page.url


# ── 4. Google-federated org ───────────────────────────────────────────

@register
class GoogleSSOLogin(LoginStrategy):
    """Org federated to Google.

    Deliberately does NOT type credentials: Google actively blocks
    automated sign-in and scripting it with a real account risks the
    account. This strategy only handles the case where the browser
    profile is ALREADY signed in to Google (persistent context) — it
    clicks through the account chooser and consent.

    For a clean CI runner, use ``storage_state`` instead.
    """

    name = "google_sso"

    def login(self, page: Page, ctx: LoginContext) -> str:
        page.goto(ctx.sf_url, wait_until="domcontentloaded")
        account = ctx.opt("google_account") or ctx.username
        chooser = page.locator(f"text={account}")
        if chooser.count() == 0:
            raise RuntimeError(
                "Google account chooser did not show a pre-authenticated session for "
                f"{account!r}.\n"
                "Automating a Google password form is not supported (and is blocked "
                "by Google). Use `ui_login: storage_state` and run `sfauto auth capture`."
            )
        chooser.first.click()
        wait_page_ready(page)
        return page.url

    @classmethod
    def looks_applicable(cls, page: Page, ctx: LoginContext) -> bool:
        try:
            return "accounts.google.com" in page.url or page.locator(
                "text=Sign in with Google").count() > 0
        except Exception:
            return False


# ── 5. Auto-detect ────────────────────────────────────────────────────

@register
class AutoLogin(LoginStrategy):
    """Load the login URL, look at what actually rendered, then delegate.

    Order: password form -> Google SSO -> frontdoor -> saved session.
    """

    name = "auto"
    PROBE_ORDER = ("password", "google_sso")

    def login(self, page: Page, ctx: LoginContext) -> str:
        page.goto(ctx.sf_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        for name in self.PROBE_ORDER:
            strat = REGISTRY[name]()
            if strat.looks_applicable(page, ctx):
                try:
                    return strat.login(page, ctx)
                except Exception as e:
                    last = f"{name}: {e}"
                    break
        else:
            last = "no login form recognised on the page"

        # Fall back to session-based approaches.
        for name in ("storage_state", "frontdoor"):
            try:
                return REGISTRY[name]().login(page, ctx)
            except Exception as e:
                last = f"{name}: {e}"

        raise RuntimeError(
            f"auto login could not authenticate. Last error — {last}\n"
            f"Set an explicit strategy in your profile: ui_login: "
            f"{' | '.join(k for k in REGISTRY if k != 'auto')}"
        )


# ── Entry point used by the `sf` fixture ──────────────────────────────

def login(page: Page, ctx: LoginContext, strategy: str = "auto") -> str:
    """Dispatch to the configured strategy."""
    key = (strategy or "auto").strip().lower()
    if key not in REGISTRY:
        raise RuntimeError(
            f"Unknown ui_login strategy {strategy!r}. "
            f"Available: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[key]().login(page, ctx)


def available() -> list[str]:
    return sorted(REGISTRY)
