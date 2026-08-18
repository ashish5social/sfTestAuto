"""
Authentication helpers for Salesforce sandboxes.

The two-step login flow this module implements:

1. Try standard username/password login at the SF login page.
2. If Salesforce intercepts with the email-verification / "verify your
   identity" page (which happens whenever the test runs from an IP not
   on the org's Network Access whitelist — laptops, GitHub Actions
   runners, etc.), bypass it via a "frontdoor" URL.

The frontdoor URL takes a valid session id obtained out-of-band (via
OAuth client_credentials or SOAP) and drops the browser straight into
Lightning, skipping the verification page entirely.

When this doesn't work
----------------------
- "OAuth CC ... 400 invalid_client" → the External Client App you're
  using doesn't have "Enable Client Credentials Flow" turned on, OR the
  policy isn't set to "Relax IP restrictions".
- "OAuth CC ... 400 inactive_user" → SF_USERNAME is locked / deactivated.
- "SOAP login ... INVALID_LOGIN" → password and security token aren't
  matching. If the password contains a colon, this helper splits
  "password:token" automatically; otherwise pass them as separate env
  vars (SF_PASSWORD + SF_SECURITY_TOKEN).
- Page still lands on /verify after the frontdoor redirect → the
  session id wasn't accepted; check the My Domain URL is exactly the
  one the session belongs to.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote as url_quote, urlparse

import requests
from playwright.sync_api import Page

from src.core.sf_ui.waits import wait_page_ready


def standard_login(
    page: Page,
    url: str,
    username: str,
    password: str,
    *,
    wait_after_ms: int = 8000,
) -> None:
    """Standard SF login: fill #username + input[name='pw'], click #Login.

    Strips a trailing ``:token`` from the password if present (some
    .env files concatenate them).
    """
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")
    pw = password.split(":", 1)[0] if ":" in password else password
    page.locator("#username").fill(username)
    page.locator("input[name='pw']").fill(pw)
    page.click("#Login")
    wait_page_ready(page, extra_ms=wait_after_ms)
    page.wait_for_timeout(3000)


def needs_frontdoor_bypass(page: Page) -> bool:
    """Detect whether the post-login page is the email-verification /
    identity-confirmation interstitial.

    Salesforce shows this when the request comes from an IP that isn't
    on the org's Network Access whitelist. Symptoms (any of):
      - URL contains 'verify', 'identity', or 'challenge'
      - A "Verify" button is visible on the page
    """
    current_url = (page.url or "").lower()
    if any(s in current_url for s in ("verify", "identity", "challenge")):
        return True
    try:
        verify_btn = page.locator(
            "//button[@value='Verify' or @title='Verify' or text()='Verify'] | "
            "//input[@value='Verify' or @title='Verify']"
        )
        return verify_btn.count() > 0 and verify_btn.first.is_visible(timeout=2000)
    except Exception:
        return False


def get_frontdoor_url(
    *,
    sf_url: str,
    username: str,
    password: str,
    security_token: str = "",
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> str:
    """Obtain a Salesforce session id (OAuth → SOAP fallback) and build a
    /secur/frontdoor.jsp URL that bypasses the email-verification page.

    Strategy:
      1. OAuth 2.0 Client Credentials Flow (needs client_id + secret +
         the External Client App configured to enable it). No user
         password / security token needed. Preferred because it works
         the same for every test user.
      2. SOAP Partner API login as fallback (needs SF_SECURITY_TOKEN).

    Returns the frontdoor URL the browser should navigate to. Raises
    Exception if both strategies fail (the message lists every attempt
    so you can see exactly why).
    """
    session_id: Optional[str] = None
    instance_url: Optional[str] = None
    errors: list[str] = []

    # ── Strategy 0: JWT bearer ───────────────────────────────────────
    # Tried first because it is the only headless flow that issues a
    # *web*-scoped token. Client-credentials tokens only ever carry the
    # "api" scope, and frontdoor.jsp rejects those — so on an SSO-gated
    # org JWT is what makes the UI login possible at all.
    key_file = os.getenv("SF_JWT_KEY_FILE", "").strip()
    jwt_user = os.getenv("SF_JWT_USERNAME", "").strip() or username
    if key_file and client_id:
        try:
            import time as _time

            import jwt as _jwt  # PyJWT

            aud = ("https://test.salesforce.com"
                   if "sandbox" in sf_url or "test.salesforce" in sf_url
                   else "https://login.salesforce.com")
            assertion = _jwt.encode(
                {"iss": client_id, "sub": jwt_user, "aud": aud,
                 "exp": int(_time.time()) + 300},
                Path(key_file).read_bytes(), algorithm="RS256",
            )
            resp = requests.post(
                f"{sf_url.rstrip('/')}/services/oauth2/token",
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": assertion},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                session_id = data["access_token"]
                instance_url = data["instance_url"]
            else:
                try:
                    body = resp.json()
                    detail = body.get("error_description") or body.get("error", "")
                except Exception:
                    detail = resp.text[:200]
                errors.append(f"JWT bearer: {resp.status_code} — {detail}")
        except ImportError:
            errors.append("JWT bearer: PyJWT not installed")
        except FileNotFoundError:
            errors.append(f"JWT bearer: key file not found: {key_file}")
        except Exception as e:
            errors.append(f"JWT bearer: {type(e).__name__}: {e}")


    # ── Strategy 1: OAuth Client Credentials ─────────────────────────
    if session_id is None and client_id and client_secret:
        token_urls = []
        if (
            "salesforce.com" in sf_url
            and "test.salesforce.com" not in sf_url
            and "login.salesforce.com" not in sf_url
        ):
            token_urls.append(f"{sf_url.rstrip('/')}/services/oauth2/token")
        token_urls.append("https://test.salesforce.com/services/oauth2/token")
        token_urls.append("https://login.salesforce.com/services/oauth2/token")

        for token_url in token_urls:
            try:
                resp = requests.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    session_id = data["access_token"]
                    instance_url = data["instance_url"]
                    break
                errors.append(
                    f"OAuth CC ({token_url}): {resp.status_code} — "
                    f"{resp.json().get('error_description', resp.text)}"
                )
            except Exception as e:
                errors.append(f"OAuth CC ({token_url}): {e}")

    # ── Strategy 2: SOAP Partner API ─────────────────────────────────
    if session_id is None:
        try:
            from simple_salesforce import Salesforce as SFApi

            raw_pw = password
            if security_token:
                pw, tok = raw_pw, security_token
            elif ":" in raw_pw:
                pw, tok = raw_pw.split(":", 1)
            else:
                pw, tok = raw_pw, ""

            domains: list[str] = []
            if (
                "salesforce.com" in sf_url
                and "test.salesforce.com" not in sf_url
                and "login.salesforce.com" not in sf_url
            ):
                host = urlparse(sf_url).hostname or ""
                domains.append(host.replace(".salesforce.com", ""))
            domains.extend(["test", "login"])

            for domain in domains:
                try:
                    sf_conn = SFApi(
                        username=username,
                        password=pw,
                        security_token=tok,
                        domain=domain,
                    )
                    session_id = sf_conn.session_id
                    instance_url = f"https://{sf_conn.sf_instance}"
                    break
                except Exception as e:
                    errors.append(f"SOAP ({domain}): {e}")
        except ImportError:
            errors.append("SOAP: simple-salesforce not installed")

    if session_id is None:
        raise Exception(
            "Frontdoor login failed — all auth methods failed.\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    # Prefer the My Domain URL so cookies match the browser session
    if (
        "salesforce.com" in sf_url
        and "test.salesforce.com" not in sf_url
        and "login.salesforce.com" not in sf_url
    ):
        base_url = sf_url.rstrip("/")
    else:
        base_url = instance_url or "https://test.salesforce.com"

    return (
        f"{base_url}/secur/frontdoor.jsp"
        f"?sid={url_quote(session_id)}"
        f"&retURL={url_quote('/lightning/page/home')}"
    )


def login_with_frontdoor_fallback(
    page: Page,
    *,
    sf_url: str,
    username: str,
    password: str,
    security_token: str = "",
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> str:
    """End-to-end login: standard login first, frontdoor bypass if the
    org gates on identity verification.

    Returns a label describing which path was used: 'standard' or
    'frontdoor'. The label is useful for the tracker assertion text.

    Raises if both paths fail (the frontdoor function provides a
    detailed error chain).
    """
    standard_login(page, sf_url, username, password)
    if needs_frontdoor_bypass(page):
        frontdoor_url = get_frontdoor_url(
            sf_url=sf_url,
            username=username,
            password=password,
            security_token=security_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        page.goto(frontdoor_url)
        page.wait_for_load_state("domcontentloaded")
        wait_page_ready(page, extra_ms=10000)
        page.wait_for_timeout(2000)
        return "frontdoor"
    wait_page_ready(page, extra_ms=5000)
    return "standard"


def env_credentials() -> dict:
    """Pull all SF_* env vars into a single dict — convenient for
    passing into ``login_with_frontdoor_fallback`` without repeating
    every os.getenv call."""
    return {
        "sf_url": os.getenv("SF_LOGIN_URL", "https://test.salesforce.com"),
        "username": os.getenv("SF_USERNAME", ""),
        "password": os.getenv("SF_PASSWORD", ""),
        "security_token": os.getenv("SF_SECURITY_TOKEN", ""),
        "client_id": os.getenv("SF_CLIENT_ID") or None,
        "client_secret": os.getenv("SF_CLIENT_SECRET") or None,
    }
