"""
SFApiClient — Salesforce API client used by API-driven tests.

Responsibilities:
  - Authenticate (OAuth client-credentials OR SOAP username+password fallback)
  - Wrap simple-salesforce for SObject CRUD + SOQL
  - Call Vlocity / Omnistudio Integration Procedures via REST
  - Auto-attach every call to the active APITracker (so the HTML report
    shows request / response / duration without the test having to log
    manually)

Authentication strategy (in order):
  1. OAuth 2.0 Client Credentials (SF_CLIENT_ID + SF_CLIENT_SECRET)
     — preferred: no password, no security token, no lockout risk
  2. SOAP Partner API login (SF_USERNAME + SF_PASSWORD + SF_SECURITY_TOKEN)
     — fallback for orgs without External Client App configured

The namespace (vlocity_cmt vs core omnistudio) is discovered at init
time by probing for vlocity_cmt__OmniProcess__c. Override with
SF_API_NAMESPACE=vlocity_cmt|omnistudio.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests
from simple_salesforce import Salesforce

from src.core.config import config
from src.api.api_tracker import APICall, APITracker




def _oauth_err(resp) -> str:
    """Extract Salesforce's OAuth error into one readable line."""
    try:
        j = resp.json()
        return f"HTTP {resp.status_code} {j.get('error','')}: {j.get('error_description','')}".strip()
    except Exception:
        return f"HTTP {resp.status_code} {resp.text[:120]}"


def _auth_hint(attempts) -> str:
    """Turn the collected failures into a concrete next action."""
    blob = " ".join(w.lower() for _, w in attempts)
    if "soap api login() is disabled" in blob:
        return ("Hint: SOAP login is disabled in this org (a default for newer orgs).\n"
                "      If the org uses SSO (Google/Okta/SAML) there is no usable password.\n"
                "      Use JWT bearer: create a private key + self-signed cert, upload the\n"
                "      cert to your Connected App (Use Digital Signatures), pre-authorise\n"
                "      the user, then set SF_JWT_KEY_FILE and SF_JWT_USERNAME.")
    if "no client credentials user enabled" in blob:
        return ("Hint: the Connected App has no 'Run As' user for the client-credentials\n"
                "      flow. Setup -> App Manager -> your app -> Manage -> Edit Policies ->\n"
                "      Client Credentials Flow -> set a Run As user.")
    if "invalid_grant" in blob:
        return ("Hint: invalid_grant usually means the username/password/token combination\n"
                "      is wrong, or the org blocks the username-password flow.")
    return "Hint: run `sfauto doctor` for a full preflight."


class SFApiClient:
    """Salesforce API client with IP support and automatic tracking."""

    def __init__(self, tracker: APITracker | None = None, api_version: str = "59.0"):
        self.tracker = tracker
        self.api_version = api_version
        self._sf: Salesforce | None = None
        self._session_id: str | None = None
        self._instance_url: str | None = None
        self._namespace: str | None = None  # "vlocity_cmt" or "omnistudio"
        self._auth_method: str | None = None

    # ── Authentication ────────────────────────────────────────

    def connect(self) -> None:
        """Authenticate, trying each configured method in order.

        Order: JWT bearer -> OAuth client-credentials -> OAuth password
        -> SOAP username/password.

        Every failure is collected and, if all methods fail, reported
        together. Previously OAuth errors were swallowed by a bare
        `except: pass`, so a misconfigured Connected App surfaced as a
        confusing SOAP INVALID_LOGIN instead of the real cause.
        """
        if self._sf is not None:
            return

        login_url = config.SF_LOGIN_URL.rstrip("/")
        attempts: list[tuple[str, str]] = []   # (method, why it failed)

        # ── Method 1: JWT bearer ──────────────────────────────────────
        # The right flow for SSO-federated orgs (Google/Okta/SAML), where
        # there is no usable Salesforce password to send.
        key_file = os.getenv("SF_JWT_KEY_FILE", "").strip()
        jwt_user = os.getenv("SF_JWT_USERNAME", "").strip() or config.SF_USERNAME
        client_id = os.getenv("SF_CLIENT_ID", "").strip()
        if key_file and client_id:
            try:
                import jwt as _jwt  # PyJWT
                import time as _time
                with open(key_file, "rb") as fh:
                    private_key = fh.read()
                aud = ("https://test.salesforce.com"
                       if "sandbox" in login_url or "test.salesforce" in login_url
                       else "https://login.salesforce.com")
                assertion = _jwt.encode(
                    {"iss": client_id, "sub": jwt_user, "aud": aud,
                     "exp": int(_time.time()) + 300},
                    private_key, algorithm="RS256",
                )
                resp = requests.post(
                    f"{login_url}/services/oauth2/token",
                    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                          "assertion": assertion},
                    timeout=30,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    self._session_id = body["access_token"]
                    self._instance_url = body["instance_url"].rstrip("/")
                    self._sf = Salesforce(session_id=self._session_id,
                                          instance_url=self._instance_url,
                                          version=self.api_version)
                    self._auth_method = "oauth_jwt_bearer"
                    self._log_auth("jwt", f"{login_url}/services/oauth2/token", 200, "jwt-bearer")
                    self._discover_namespace()
                    return
                attempts.append(("JWT bearer", _oauth_err(resp)))
            except ImportError:
                attempts.append(("JWT bearer", "PyJWT not installed (pip install pyjwt cryptography)"))
            except FileNotFoundError:
                attempts.append(("JWT bearer", f"key file not found: {key_file}"))
            except Exception as e:
                attempts.append(("JWT bearer", f"{type(e).__name__}: {e}"))
        elif client_id and not key_file:
            attempts.append(("JWT bearer", "skipped (SF_JWT_KEY_FILE not set)"))

        # ── Method 2: OAuth client credentials ────────────────────────
        client_secret = os.getenv("SF_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            try:
                token_url = f"{login_url}/services/oauth2/token"
                resp = requests.post(
                    token_url,
                    data={"grant_type": "client_credentials",
                          "client_id": client_id, "client_secret": client_secret},
                    timeout=30,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    self._session_id = body["access_token"]
                    self._instance_url = body["instance_url"].rstrip("/")
                    self._sf = Salesforce(session_id=self._session_id,
                                          instance_url=self._instance_url,
                                          version=self.api_version)
                    self._auth_method = "oauth_client_credentials"
                    self._log_auth("oauth", token_url, 200, "client_credentials")
                    self._discover_namespace()
                    return
                attempts.append(("OAuth client-credentials", _oauth_err(resp)))
            except Exception as e:
                attempts.append(("OAuth client-credentials", f"{type(e).__name__}: {e}"))
        else:
            attempts.append(("OAuth client-credentials", "skipped (SF_CLIENT_ID/SECRET not set)"))

        # ── Method 3: OAuth password grant ────────────────────────────
        if client_id and client_secret and config.SF_USERNAME and config.SF_PASSWORD:
            try:
                resp = requests.post(
                    f"{login_url}/services/oauth2/token",
                    data={"grant_type": "password", "client_id": client_id,
                          "client_secret": client_secret,
                          "username": config.SF_USERNAME,
                          "password": config.SF_PASSWORD + (config.SF_SECURITY_TOKEN or "")},
                    timeout=30,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    self._session_id = body["access_token"]
                    self._instance_url = body["instance_url"].rstrip("/")
                    self._sf = Salesforce(session_id=self._session_id,
                                          instance_url=self._instance_url,
                                          version=self.api_version)
                    self._auth_method = "oauth_password"
                    self._log_auth("oauth", f"{login_url}/services/oauth2/token", 200, "password")
                    self._discover_namespace()
                    return
                attempts.append(("OAuth password grant", _oauth_err(resp)))
            except Exception as e:
                attempts.append(("OAuth password grant", f"{type(e).__name__}: {e}"))

        # ── Method 4: SOAP username + password + token ────────────────
        try:
            from urllib.parse import urlparse

            if "test.salesforce.com" in login_url or "login.salesforce.com" in login_url:
                domain = "test" if "test.salesforce.com" in login_url else "login"
            else:
                host = urlparse(login_url).hostname or ""
                domain = host.replace(".salesforce.com", "") if host else "test"

            sf_tmp = Salesforce(username=config.SF_USERNAME,
                                password=config.SF_PASSWORD,
                                security_token=config.SF_SECURITY_TOKEN,
                                domain=domain)
            session_id = sf_tmp.session_id
            my_domain_host = login_url.replace("https://", "").replace("http://", "")
            self._sf = Salesforce(instance=my_domain_host, session_id=session_id,
                                  version=self.api_version)
            self._session_id = session_id
            self._instance_url = f"https://{my_domain_host}".rstrip("/")
            self._auth_method = "soap_password"
            self._log_auth("soap", f"{login_url}/services/Soap", 200, "username+password")
            self._discover_namespace()
            return
        except Exception as e:
            attempts.append(("SOAP username/password", str(e).split("\n")[0][:160]))

        # ── All methods failed — report every attempt ─────────────────
        self._log_auth("all", login_url, 0, "; ".join(f"{m}: {w}" for m, w in attempts))
        lines = [f"Could not authenticate to {login_url}", ""]
        for m, w in attempts:
            lines.append(f"  - {m}: {w}")
        lines += ["", _auth_hint(attempts)]
        raise RuntimeError("\n".join(lines))

    def _log_auth(self, method: str, url: str, status: int, detail: str):
        """Record auth attempt as an APICall so it shows in the report."""
        if self.tracker is None:
            return
        call = APICall(
            name=f"Auth: {method}",
            method="POST",
            url=url,
            request_body={"method": method, "grant_type": detail[:80]},
            response_body={"status": status, "info": detail[:300]},
            status_code=status,
            duration_ms=0,
        )
        self.tracker.log_api_call(call)

    def _discover_namespace(self):
        """Set self._namespace by probing for vlocity_cmt vs omnistudio."""
        override = os.getenv("SF_API_NAMESPACE", "").strip()
        if override in ("vlocity_cmt", "omnistudio"):
            self._namespace = override
            return
        # Probe for vlocity_cmt__OmniProcess__c first
        try:
            self._sf.query("SELECT COUNT() FROM vlocity_cmt__OmniProcess__c")
            self._namespace = "vlocity_cmt"
            return
        except Exception:
            pass
        try:
            self._sf.query("SELECT COUNT() FROM OmniProcess")
            self._namespace = "omnistudio"
            return
        except Exception:
            pass
        # Last resort — default to vlocity_cmt since that's most common in Salesforce
        self._namespace = "vlocity_cmt"

    # ── Accessors ────────────────────────────────────────────

    @property
    def sf(self) -> Salesforce:
        if self._sf is None:
            self.connect()
        return self._sf

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            self.connect()
        return self._namespace

    @property
    def apex_namespace(self) -> str:
        """
        Namespace under which the Vlocity/Omnistudio apexrest services are
        actually reachable (CPQ v2, Integration Procedures, etc.).

        On a *hybrid* org (where core Omnistudio lives unprefixed for
        OmniProcess records but the Vlocity managed package still owns
        the apexrest services), the generic ``namespace`` probe may
        correctly return ``omnistudio`` while the apexrest endpoints only
        respond under ``vlocity_cmt``. We probe once by calling the
        lightweight CPQ catalogs GET on each candidate namespace and
        cache the winner. Both ``ip_base`` and ``cpq_v2_base`` then use
        this single source of truth.

        Individual probe attempts are silent — only a single summary entry
        ("Resolved apexrest namespace: vlocity_cmt") is logged to the tracker.
        """
        if getattr(self, "_apex_namespace", None):
            return self._apex_namespace
        candidates: list[str] = []
        detected = self.namespace
        if detected:
            candidates.append(detected)
        for c in ("vlocity_cmt", "omnistudio"):
            if c not in candidates:
                candidates.append(c)
        for ns in candidates:
            # /v2/cpq/catalogs returns 200 when the apexrest services are
            # exposed under `ns`, 404 otherwise. Silent so failed probes
            # don't clutter the report.
            status, _ = self._request(
                "GET",
                f"/services/apexrest/{ns}/v2/cpq/catalogs",
                name=f"Probe apexrest base @ {ns}",
                silent=True,
            )
            if status < 400:
                self._apex_namespace = ns
                self._log_summary(
                    name=f"Resolved apexrest namespace: {ns}",
                    detail={"resolved": ns, "tried": candidates},
                )
                return ns
        raise RuntimeError(
            f"No apexrest namespace responded. Tried: {candidates}. "
            f"Verify the Vlocity CPQ / Omnistudio package is installed "
            f"and the running user has API access to its apex REST services."
        )

    def _log_summary(self, *, name: str, detail: Any) -> None:
        """
        Emit a synthetic APICall entry that summarizes a discovery result.

        Used by apex_namespace / pick_object / connect() auth to replace
        noisy per-probe log entries with a single informative row (no URL,
        0ms, status 200) so the HTML report stays readable.
        """
        if self.tracker is None:
            return
        self.tracker.log_api_call(
            APICall(
                name=name,
                method="INFO",
                url="",
                request_body=None,
                response_body=detail,
                status_code=200,
                duration_ms=0,
            )
        )

    @property
    def ip_base(self) -> str:
        """Integration Procedure endpoint base (shares apex_namespace probe)."""
        return f"/services/apexrest/{self.apex_namespace}/v1/integrationprocedure"

    @property
    def cpq_v2_base(self) -> str:
        """Vlocity CPQ v2 REST API base (shares apex_namespace probe)."""
        return f"/services/apexrest/{self.apex_namespace}/v2/cpq"

    @property
    def current_user_id(self) -> str:
        """Return the User Id of the authenticated user (cached after first call)."""
        if getattr(self, "_current_user_id", None):
            return self._current_user_id
        # UserInfo endpoint is the lightest way to get the user id
        status, body = self._request(
            "GET",
            "/services/oauth2/userinfo",
            name="REST: GET /services/oauth2/userinfo",
        )
        if status >= 400 or not isinstance(body, dict) or not body.get("user_id"):
            # Fallback: SOQL against Auth.UserInfo / User
            rows = self.soql(
                "SELECT Id FROM User WHERE Username = '"
                + (config.SF_USERNAME or "") + "' LIMIT 1",
                name="SOQL: resolve current User Id",
            )
            if rows:
                self._current_user_id = rows[0]["Id"]
                return self._current_user_id
            raise RuntimeError(f"Could not resolve current user id: {body}")
        self._current_user_id = body["user_id"]
        return self._current_user_id

    # ── Low-level request wrapper ────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        name: str,
        body: Any = None,
        params: dict | None = None,
        silent: bool = False,
        extra_headers: dict | None = None,
    ) -> tuple[int, Any]:
        """
        Make an authenticated REST call and log it to the tracker.

        path: either "/services/apexrest/..." or "/services/data/vXX.X/..."
              (absolute path, no host).

        silent: when True, the call is NOT logged to the tracker. Used
                for discovery probes (namespace resolution, object-
                existence checks, auth-method fallback) where failed
                attempts are expected and would only create noise in
                the HTML report. The caller is responsible for logging
                a meaningful summary entry after the probe chain runs.
        """
        if self._sf is None:
            self.connect()
        url = self._instance_url + path
        headers = {
            "Authorization": f"Bearer {self._session_id}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        t0 = time.time()
        call = APICall(
            name=name,
            method=method.upper(),
            url=url,
            request_body=body,
        )
        try:
            resp = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                data=json.dumps(body) if body is not None and method.upper() != "GET" else None,
                timeout=60,
            )
            # Parse response body
            resp_body: Any
            text = resp.text or ""
            if "application/json" in resp.headers.get("Content-Type", "") or text.startswith(("{", "[")):
                try:
                    resp_body = resp.json()
                except Exception:
                    resp_body = text
            else:
                resp_body = text
            call.status_code = resp.status_code
            call.response_body = resp_body
            call.duration_ms = int((time.time() - t0) * 1000)
            if self.tracker and not silent:
                self.tracker.log_api_call(call)
            return resp.status_code, resp_body
        except Exception as e:
            call.error = str(e)
            call.duration_ms = int((time.time() - t0) * 1000)
            if self.tracker and not silent:
                self.tracker.log_api_call(call)
            raise

    # ── High-level helpers ───────────────────────────────────

    def soql(self, query: str, *, name: str | None = None) -> list[dict]:
        """Run SOQL and log it. Returns records list (no metadata)."""
        call_name = name or f"SOQL: {query[:60]}..."
        status, body = self._request(
            "GET",
            f"/services/data/v{self.api_version}/query",
            name=call_name,
            params={"q": query},
        )
        if status >= 400:
            raise RuntimeError(f"SOQL failed [{status}]: {body}")
        return (body or {}).get("records", [])

    def create(
        self,
        sobject: str,
        data: dict,
        *,
        name: str | None = None,
        allow_duplicates: bool = True,
    ) -> str:
        """Create an SObject. Returns the new record Id.

        ``allow_duplicates`` sends ``Sforce-Duplicate-Rule-Header:
        allowSave=true``, which lets the record through an *alert*-level
        duplicate rule (``allowSave: true`` in the error payload). Most
        orgs ship the Standard Account Duplicate Rule enabled, and test
        data is repetitive by nature — without this, the second run of a
        suite fails with DUPLICATES_DETECTED against the record the
        first run left behind. Block-level rules still reject the
        create, as they should. Pass ``allow_duplicates=False`` when the
        duplicate rule is the thing under test.
        """
        call_name = name or f"REST: POST /sobjects/{sobject}"
        status, body = self._request(
            "POST",
            f"/services/data/v{self.api_version}/sobjects/{sobject}",
            name=call_name,
            body=data,
            extra_headers=(
                {"Sforce-Duplicate-Rule-Header": "allowSave=true"}
                if allow_duplicates else None
            ),
        )
        if status >= 400:
            raise RuntimeError(f"Create {sobject} failed [{status}]: {body}")
        return body["id"]

    def update(
        self,
        sobject: str,
        record_id: str,
        data: dict,
        *,
        name: str | None = None,
        allow_duplicates: bool = True,
    ):
        """PATCH an SObject. See ``create`` for ``allow_duplicates``."""
        call_name = name or f"REST: PATCH /sobjects/{sobject}/{record_id}"
        status, body = self._request(
            "PATCH",
            f"/services/data/v{self.api_version}/sobjects/{sobject}/{record_id}",
            name=call_name,
            body=data,
            extra_headers=(
                {"Sforce-Duplicate-Rule-Header": "allowSave=true"}
                if allow_duplicates else None
            ),
        )
        if status >= 400:
            raise RuntimeError(f"Update {sobject} failed [{status}]: {body}")

    def delete(self, sobject: str, record_id: str, *, name: str | None = None) -> None:
        """DELETE an SObject. A record that is already gone is not an error."""
        call_name = name or f"REST: DELETE /sobjects/{sobject}/{record_id}"
        status, body = self._request(
            "DELETE",
            f"/services/data/v{self.api_version}/sobjects/{sobject}/{record_id}",
            name=call_name,
        )
        if status == 404:
            return
        if status >= 400:
            raise RuntimeError(f"Delete {sobject} failed [{status}]: {body}")

    def describe(
        self, sobject: str, *, name: str | None = None, silent: bool = False
    ) -> dict:
        """Return the SObject describe payload (fields + recordTypeInfos + …).

        The result is cached per-client so repeat describe calls on the same
        sobject (common when multiple helpers inspect the same object — e.g.
        ``pick_record_type("Quote")`` then ``pick_field("Quote", ...)``) hit
        the network exactly once. Pass ``silent=True`` for probe-style calls
        that shouldn't create a visible entry in the HTML report.
        """
        cache = getattr(self, "_describe_cache", None)
        if cache is None:
            cache = {}
            self._describe_cache = cache
        if sobject in cache:
            return cache[sobject]
        call_name = name or f"REST: GET /sobjects/{sobject}/describe"
        status, body = self._request(
            "GET",
            f"/services/data/v{self.api_version}/sobjects/{sobject}/describe",
            name=call_name,
            silent=silent,
        )
        if status >= 400:
            raise RuntimeError(f"Describe {sobject} failed [{status}]: {body}")
        result = body or {}
        cache[sobject] = result
        return result

    def pick_record_type(
        self, sobject: str, developer_name: str, *, name: str | None = None
    ) -> tuple[str | None, list[str]]:
        """
        Return ``(recordTypeId, available_developer_names)`` for ``sobject``.

        Uses describe so the result respects the *running user's* profile /
        permission-set access — a plain SOQL on RecordType would return
        record types the API user can't actually assign on a DML call
        (error: ``INVALID_CROSS_REFERENCE_KEY``).

        Matching is tolerant: we accept a match against either the RT's
        ``developerName`` (API identifier, e.g. ``Business_Opportunity``)
        OR its user-facing ``name`` (e.g. ``Business Opportunity``),
        case-insensitively and treating spaces and underscores as
        interchangeable. This lets a single JSON value (``"Business"``)
        succeed on Account (where the RT is literally ``Business``) *and*
        on Opportunity (where the same business concept is exposed as
        ``Business_Opportunity`` / ``Business Opportunity``).

        - recordTypeId: first active + available non-master RT that
          matches, or ``None``.
        - available_developer_names: sorted list of all active + available
          non-master RT developer-names for this sobject (used to build
          actionable error messages).
        """
        def _norm(s: str) -> str:
            # Case-insensitive, spaces/underscores/dashes collapsed to "_".
            import re as _re
            return _re.sub(r"[\s_\-]+", "_", (s or "").strip().lower())

        wanted = _norm(developer_name)

        desc = self.describe(
            sobject,
            name=name or f"REST: describe {sobject} (find accessible RecordTypes)",
        )
        match_id: str | None = None
        available: list[str] = []
        for rt in desc.get("recordTypeInfos") or []:
            if rt.get("master"):
                continue
            if not rt.get("active") or not rt.get("available"):
                continue
            dn = rt.get("developerName") or ""
            user_name = rt.get("name") or ""
            available.append(dn)
            if match_id is None:
                # Try exact dev-name match first (fastest, preferred),
                # then the normalized form, then the user-facing name —
                # so a JSON value of "Business" matches either
                # "Business" or "Business_Opportunity" / "Business Opportunity".
                candidates = {
                    _norm(dn),
                    _norm(user_name),
                }
                # Also accept suffix match so "Business" resolves an RT
                # whose dev-name is "Business_Opportunity" (but still a
                # tight match — "Business" won't match "Business_XX_YY").
                suffix_variants = {
                    f"{wanted}_opportunity",
                    f"{wanted}_account",
                    f"{wanted}_quote",
                    f"{wanted}_contact",
                }
                if (
                    wanted in candidates
                    or wanted == _norm(dn)
                    or wanted == _norm(user_name)
                    or any(v in candidates for v in suffix_variants)
                ):
                    match_id = rt.get("recordTypeId")
        return match_id, sorted(set(available))

    def pick_object(self, *candidates: str, name: str | None = None) -> str | None:
        """
        Return the first object name in ``candidates`` that exists in the org.

        Useful when an object's name varies by managed-package namespace
        (e.g. ``vlocity_cmt__PriceList__c`` vs ``PriceList__c`` vs
        ``omnistudio__PriceList__c``). Uses describe under the hood so the
        result is authoritative and respects the user's access.

        Individual candidate probes are silent so the HTML report isn't
        polluted with 404 rows for namespaces that don't apply. A single
        summary entry is logged once the winner is known.

        Returns ``None`` if none of the candidates resolve.
        """
        tried: list[str] = []
        for cand in candidates:
            if not cand:
                continue
            tried.append(cand)
            try:
                self.describe(
                    cand,
                    name=name or f"REST: describe {cand} (probe existence)",
                    silent=True,
                )
                # Log a single clean summary entry instead of N probe rows.
                self._log_summary(
                    name=name or f"Resolved object: {cand}",
                    detail={"resolved": cand, "tried": tried},
                )
                return cand
            except Exception:
                continue
        return None

    def pick_field(
        self, sobject: str, *suffixes: str, name: str | None = None
    ) -> dict[str, str | None]:
        """
        Resolve actual field API names on ``sobject`` by matching against
        one or more trailing suffixes.

        Returns a dict mapping each ``suffix`` → the first matching field
        API name on the object, or ``None`` if no field matches. Case-
        insensitive suffix match against ``field["name"]``.

        Example:
            pick_field("Quote", "PriceListId__c", "DefaultBillingAccountId__c")
            → {"PriceListId__c": "vlocity_cmt__PriceListId__c",
               "DefaultBillingAccountId__c": "vlocity_cmt__DefaultBillingAccountId__c"}

        Lets tests write managed-package-agnostic payloads without hard-
        coding a namespace prefix.
        """
        desc = self.describe(
            sobject,
            name=name or f"REST: describe {sobject} (resolve custom field names)",
        )
        result: dict[str, str | None] = {s: None for s in suffixes}
        suffix_lowers = {s: s.lower() for s in suffixes}
        for f in desc.get("fields") or []:
            fname = f.get("name") or ""
            flower = fname.lower()
            for s, s_low in suffix_lowers.items():
                if result[s] is None and flower.endswith(s_low):
                    result[s] = fname
        return result

    # ── Vlocity envelope inspection ──────────────────────────────────
    #
    # Vlocity CPQ v2 returns HTTP 200 even when the underlying operation
    # failed — the failure is surfaced as entries in a top-level
    # ``messages`` array with ``severity`` in {"WARN","ERROR","FATAL"}.
    # Silently treating 200 as success causes false-positive PASSes
    # (the test sees no exception, asserts ``True``, and moves on while
    # the attribute update, cart add, etc. never happened).
    #
    # Every CPQ helper below MUST funnel its response body through
    # ``_inspect_cpq_envelope`` so a 200-with-ERROR is converted into a
    # RuntimeError that the test framework records as a FAIL.

    @staticmethod
    def _collect_cpq_messages(body: Any) -> list[dict]:
        """Return every message entry nested anywhere in a CPQ response."""
        out: list[dict] = []
        if isinstance(body, dict):
            msgs = body.get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict):
                        out.append(m)
            # Vlocity sometimes nests response payloads under "result",
            # "IPResult", per-item keys, etc. — scan one level deep.
            for v in body.values():
                if isinstance(v, (dict, list)):
                    out.extend(SFApiClient._collect_cpq_messages(v))
        elif isinstance(body, list):
            for item in body:
                out.extend(SFApiClient._collect_cpq_messages(item))
        return out

    @staticmethod
    def _inspect_cpq_envelope(
        body: Any,
        *,
        op: str,
        tolerate_messages: list[str] | None = None,
    ) -> list[str]:
        """Raise if the Vlocity response body contains ERROR/FATAL messages.

        ``tolerate_messages`` is an optional list of case-insensitive
        substrings. Any error whose message contains one of these
        substrings is downgraded — it is still collected and returned to
        the caller (so it can be logged / asserted on), but it does not
        raise. Returns the list of tolerated error strings (may be empty).
        """
        errors: list[str] = []
        tolerated: list[str] = []
        seen: set[str] = set()
        seen_tol: set[str] = set()
        tolerated_lowered = [t.lower() for t in (tolerate_messages or [])]
        for m in SFApiClient._collect_cpq_messages(body):
            severity = str(m.get("severity") or "").upper()
            if severity in ("ERROR", "FATAL"):
                msg = (
                    m.get("message")
                    or m.get("messageText")
                    or m.get("code")
                    or json.dumps(m, default=str)
                )
                key = f"{severity}:{msg}"
                msg_lower = str(msg).lower()
                if any(tok in msg_lower for tok in tolerated_lowered):
                    if key in seen_tol:
                        continue
                    seen_tol.add(key)
                    tolerated.append(f"[{severity}] {msg}")
                    continue
                if key in seen:
                    continue
                seen.add(key)
                errors.append(f"[{severity}] {msg}")
        if errors:
            joined = "; ".join(errors[:5])
            extra = "" if len(errors) <= 5 else f" (+{len(errors) - 5} more)"
            raise RuntimeError(f"{op} reported Vlocity errors: {joined}{extra}")
        return tolerated

    def cpq_post_cart_items(
        self,
        cart_id: str,
        items: list[dict],
        *,
        name: str | None = None,
        tolerate_messages: list[str] | None = None,
    ) -> dict:
        """
        POST to Vlocity CPQ v2: add line items to a cart.

        Endpoint: POST /services/apexrest/{ns}/v2/cpq/carts/{cart_id}/items

        ``items`` is a list of entries like ``{"itemId": "<pricebookEntryId>"}``
        (itemId may also be a Product2 Id depending on the CPQ config).

        ``tolerate_messages`` — list of case-insensitive error-message
        substrings to downgrade from FAIL to soft-tolerated. Used for the
        "required attribute missing" class of validation errors that the
        Vlocity UI emits on initial add (the UI follow-up configuration
        dialog supplies the values via a subsequent PUT). Any tolerated
        errors are stashed under ``response["__tolerated_errors"]`` so
        the caller can log / assert on them.

        Returns the parsed response dict. Raises if the Vlocity envelope
        carries a non-tolerated ERROR/FATAL message (200-with-errors pattern).
        """
        call_name = name or f"CPQ v2: POST /carts/{cart_id}/items"
        path = f"{self.cpq_v2_base}/carts/{cart_id}/items"
        status, body = self._request(
            "POST", path, name=call_name, body={"items": items}
        )
        if status >= 400:
            raise RuntimeError(f"CPQ postCartsItems HTTP {status}: {body}")
        tolerated = self._inspect_cpq_envelope(
            body, op="postCartsItems", tolerate_messages=tolerate_messages
        )
        result = body if isinstance(body, dict) else {"raw": body}
        if tolerated and isinstance(result, dict):
            result["__tolerated_errors"] = tolerated
        return result

    def cpq_put_cart_items(
        self,
        cart_id: str,
        items: list[dict] | dict,
        *,
        name: str | None = None,
    ) -> dict:
        """
        PUT to Vlocity CPQ v2: update line items on a cart (attributes, qty).

        Endpoint: PUT /services/apexrest/{ns}/v2/cpq/carts/{cart_id}/items

        The CPQ v2 PUT contract expects ``items`` as a **MAP keyed by
        itemId**, not a list (sending a list produces the Apex error
        "Invalid conversion from runtime type List<ANY> to
        Map<String,ANY>"). Callers may pass either shape — a list of
        ``{"itemId": "<id>", ...attr_overrides}`` dicts is normalised
        into the required map here.

        Raises if the Vlocity envelope carries an ERROR/FATAL message.
        """
        call_name = name or f"CPQ v2: PUT /carts/{cart_id}/items"
        path = f"{self.cpq_v2_base}/carts/{cart_id}/items"

        # Normalise list → map keyed by itemId.
        if isinstance(items, list):
            items_map: dict[str, dict] = {}
            for entry in items:
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"cpq_put_cart_items: list entries must be dicts, got {type(entry)}"
                    )
                item_id = entry.get("itemId") or entry.get("Id")
                if not item_id:
                    raise ValueError(
                        "cpq_put_cart_items: each list entry needs an 'itemId' key"
                    )
                payload = {k: v for k, v in entry.items() if k != "itemId"}
                items_map[item_id] = payload
            payload_items: dict = items_map
        elif isinstance(items, dict):
            payload_items = items
        else:
            raise ValueError(
                f"cpq_put_cart_items: items must be list or dict, got {type(items)}"
            )

        status, body = self._request(
            "PUT", path, name=call_name, body={"items": payload_items}
        )
        if status >= 400:
            raise RuntimeError(f"CPQ putCartsItems HTTP {status}: {body}")
        self._inspect_cpq_envelope(body, op="putCartsItems")
        return body if isinstance(body, dict) else {"raw": body}

    def cpq_get_cart_items(
        self,
        cart_id: str,
        *,
        name: str | None = None,
        tolerate_messages: list[str] | None = None,
    ) -> dict:
        """GET /services/apexrest/{ns}/v2/cpq/carts/{cart_id}/items.

        ``tolerate_messages`` — same semantics as
        ``cpq_post_cart_items``: a list of case-insensitive substrings
        to downgrade from FAIL to soft-tolerated. This is useful when
        the cart is in a known-invalid state (e.g. required attribute
        missing, about to be configured) and the GET is an intermediate
        step toward fixing it.
        """
        call_name = name or f"CPQ v2: GET /carts/{cart_id}/items"
        path = f"{self.cpq_v2_base}/carts/{cart_id}/items"
        status, body = self._request("GET", path, name=call_name)
        if status >= 400:
            raise RuntimeError(f"CPQ getCartsItems HTTP {status}: {body}")
        tolerated = self._inspect_cpq_envelope(
            body, op="getCartsItems", tolerate_messages=tolerate_messages
        )
        result = body if isinstance(body, dict) else {"raw": body}
        if tolerated and isinstance(result, dict):
            result["__tolerated_errors"] = tolerated
        return result

    def cpq_configure_line_item_attributes(
        self,
        cart_id: str,
        updates: list[dict],
        *,
        name: str | None = None,
    ) -> dict:
        """
        GET-modify-PUT: configure attribute values on cart line items.

        Mirrors the UI's ``putCartsItems`` flow (captured from TC1 Aura
        traffic): the UI fetches the full item records, mutates
        ``userValues`` inside
        ``attributeCategories.records[].productAttributes.records[]``,
        then PUTs the full record snapshot back. The Vlocity v2 PUT
        endpoint REJECTS the flat ``{itemId, attributeValues}`` shape
        with ``[ERROR] List index out of bounds: 0`` — it only accepts
        the full snapshot.

        ``updates`` is a list of entries like::

            [
                {
                    "itemId": "<QuoteLineItem Id>",
                    "attributeValues": {
                        "ATTR_BANDWIDTH": "100 Mbps",
                        "ATTR_QUOTE_TYPE": "New",
                    },
                },
                ...
            ]

        This helper:
          1. GETs the current cart items,
          2. Finds the matching records by Id,
          3. Mutates ``userValues`` on each matching ``productAttributes``
             record whose ``code`` is in the update's ``attributeValues``,
          4. PUTs the full ``{"items": {"records": [...]}}`` payload back.

        Raises if any target attribute code cannot be located, if any
        requested itemId is not present in the cart, or if the PUT
        envelope carries ERROR/FATAL messages.

        Returns the PUT response body.
        """
        call_name = name or f"CPQ v2: configure attrs /carts/{cart_id}/items"

        # 1. Fetch current cart items (full snapshot).
        #
        # NB: when a line item has a required attribute still set to
        # null (i.e. the POST that added the item was tolerated by
        # ``cpq_post_cart_items(tolerate_messages=...)``), the GET
        # response echoes those same validation messages. Since our
        # very next action is to PUT the missing value, the messages
        # are expected here too — tolerate them so the GET half of
        # this GET-modify-PUT flow doesn't raise.
        get_resp = self.cpq_get_cart_items(
            cart_id,
            name=f"{call_name} — GET current items",
            tolerate_messages=[
                "Required attribute missing",
                "Please select a value",
            ],
        )

        # Locate the records[] list — top level or nested under "items".
        records = None
        if isinstance(get_resp, dict):
            if isinstance(get_resp.get("records"), list):
                records = get_resp["records"]
            elif isinstance(get_resp.get("items"), dict) and isinstance(
                get_resp["items"].get("records"), list
            ):
                records = get_resp["items"]["records"]
        if not isinstance(records, list) or not records:
            raise RuntimeError(
                f"cpq_configure_line_item_attributes: could not find records[] "
                f"in GET response. Top-level keys: "
                f"{list(get_resp.keys()) if isinstance(get_resp, dict) else type(get_resp)}"
            )

        # 2. Build update-map: {qli_id: {attr_code: value}}
        update_map: dict[str, dict[str, Any]] = {}
        for entry in updates:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"cpq_configure_line_item_attributes: entries must be dicts, got {type(entry)}"
                )
            item_id = entry.get("itemId") or entry.get("Id")
            if not item_id:
                raise ValueError(
                    "cpq_configure_line_item_attributes: each entry needs 'itemId'"
                )
            attr_vals = entry.get("attributeValues") or {}
            if not isinstance(attr_vals, dict) or not attr_vals:
                raise ValueError(
                    f"cpq_configure_line_item_attributes: entry {item_id} has "
                    f"empty/invalid attributeValues={attr_vals!r}"
                )
            update_map[item_id] = dict(attr_vals)

        # 3. Walk records, mutate userValues where code matches.
        # Defensive: Vlocity's cart GET can include auxiliary / bundle /
        # header records whose ``Id`` or ``itemId`` is missing or is a
        # structured value (dict/list) rather than a plain string.
        # Skip anything that isn't a plain string — same for attribute
        # ``code`` — so we never blow up with "unhashable type: 'dict'"
        # when doing the ``in update_map`` lookup.
        applied: dict[str, list[str]] = {k: [] for k in update_map}
        not_found_attrs: dict[str, list[str]] = {k: [] for k in update_map}
        matched_records: set[str] = set()
        for rec in records:
            if not isinstance(rec, dict):
                continue
            raw_id = rec.get("Id") or rec.get("itemId")
            if not isinstance(raw_id, str):
                continue
            if raw_id not in update_map:
                continue
            rec_id = raw_id
            matched_records.add(rec_id)
            target_attrs = dict(update_map[rec_id])  # attr_code → desired value
            # attributeCategories can be either a {"records": [...]} wrapper
            # or a bare list, depending on the CPQ version; handle both.
            acs = rec.get("attributeCategories")
            if isinstance(acs, dict):
                ac_recs = acs.get("records") or []
            elif isinstance(acs, list):
                ac_recs = acs
            else:
                ac_recs = []
            for ac in ac_recs:
                if not isinstance(ac, dict):
                    continue
                pas = ac.get("productAttributes")
                if isinstance(pas, dict):
                    pa_recs = pas.get("records") or []
                elif isinstance(pas, list):
                    pa_recs = pas
                else:
                    pa_recs = []
                for pa in pa_recs:
                    if not isinstance(pa, dict):
                        continue
                    code = pa.get("code")
                    if not isinstance(code, str):
                        continue
                    if code in target_attrs:
                        pa["userValues"] = target_attrs[code]
                        applied[rec_id].append(code)
                        target_attrs.pop(code, None)
            not_found_attrs[rec_id] = list(target_attrs.keys())

        # 4. Validate: every requested itemId + attr code must have landed.
        missing_items = [k for k in update_map if k not in matched_records]
        if missing_items:
            raise RuntimeError(
                f"cpq_configure_line_item_attributes: itemIds not present in cart: {missing_items}"
            )
        unresolved = {k: v for k, v in not_found_attrs.items() if v}
        if unresolved:
            raise RuntimeError(
                f"cpq_configure_line_item_attributes: could not locate attribute "
                f"codes on their line items: {unresolved}"
            )

        # 5. PUT the full snapshot back.
        path = f"{self.cpq_v2_base}/carts/{cart_id}/items"
        put_body = {"items": {"records": records}}
        status, body = self._request(
            "PUT",
            path,
            name=f"{call_name} — PUT snapshot",
            body=put_body,
        )
        if status >= 400:
            raise RuntimeError(f"CPQ putCartsItems HTTP {status}: {body}")
        self._inspect_cpq_envelope(body, op="putCartsItems (snapshot)")
        result = body if isinstance(body, dict) else {"raw": body}
        if isinstance(result, dict):
            result["__applied_attributes"] = applied
        return result

    # ── Vlocity Enterprise-Sales-Module (ESM) flow helpers ──────────
    #
    # The three helpers below replicate the UI's "add products to a
    # quote" workflow so QuoteLineItems on the Enterprise Quote are
    # properly attached to a QuoteMember (location). Raw REST
    # /sobjects + CPQ v2 alone do NOT establish that linkage — it only
    # happens as a side effect of AddQMQGToWC_CopyToEQ. See the
    # rationale in tests/generated/test_cci_tc3_... (Phase-3 header
    # comment) and the captured Aura traffic in
    # tests/data/tc1_ip_capture.json.

    def esm_save_quote_member(
        self,
        quote_id: str,
        member: dict,
        *,
        lookup_object: str = "GoogleMaps",
        name: str | None = None,
    ) -> str:
        """
        Create a QuoteMember (location) on ``quote_id`` via IP
        ``ESM_saveTypeaheadDetails``.

        ``member`` is a dict of QuoteMember fields (Name, MemberType__c,
        vlocity_cmt__StreetAddress__c, etc.) — pass it verbatim from
        the JSON test data so the record matches what the UI's
        typeahead autocomplete produces.

        Returns the new QuoteMember Id (``a5j...``).
        """
        payload = {
            "members": [member],
            "lookupObject": lookup_object,
            "QuoteId__c": quote_id,
        }
        call_name = name or f"IP: ESM_saveTypeaheadDetails (QuoteMember on {quote_id})"
        resp = self.call_ip("ESM_saveTypeaheadDetails", payload, name=call_name)

        # The IP wraps the result one level deep: {"IPResult": {"members": [...]}}.
        # Older / newer namespaces occasionally flatten it. Handle both.
        ip_result = (resp or {}).get("IPResult", resp) or {}
        members = ip_result.get("members") or []
        if not members or not members[0].get("Id"):
            raise RuntimeError(
                f"ESM_saveTypeaheadDetails didn't return a QuoteMember Id. "
                f"Response: {json.dumps(resp)[:600]}"
            )
        return members[0]["Id"]

    def cpq_create_working_cart(
        self,
        sales_quote_id: str,
        *,
        default_name: str = "Test Working Cart",
        default_status: str = "Draft",
        fields_to_copy: list[str] | None = None,
        name: str | None = None,
    ) -> str:
        """
        Spawn a transient Working Cart linked to ``sales_quote_id`` via IP
        ``create_WorkingCart``. Items should be added to the returned
        Working Cart Id, not directly to the Enterprise Quote, so
        AddQMQGToWC_CopyToEQ can safely dispose the WC afterwards.

        Returns the Working Cart Id (``0Q0...``, distinct from
        ``sales_quote_id``).
        """
        payload = {
            "DefaultFieldValues": {
                "Name": default_name,
                "Status": default_status,
            },
            "FieldsToCopy": fields_to_copy
            or [
                "Id",
                "AccountId",
                "OpportunityId",
                "vlocity_cmt__OriginatingChannel__c",
                "vlocity_cmt__PriceListId__c",
                "vlocity_cmt__PriceListId__r.Name",
                "cci_cmt_Service_Term__c",
            ],
            "SalesQuoteId": sales_quote_id,
        }
        call_name = name or f"IP: create_WorkingCart (on EQ {sales_quote_id})"
        resp = self.call_ip("create_WorkingCart", payload, name=call_name)
        ip_result = (resp or {}).get("IPResult", resp) or {}
        wc_id = ip_result.get("WorkingCartId")
        if not wc_id:
            raise RuntimeError(
                f"create_WorkingCart didn't return a WorkingCartId. "
                f"Response: {json.dumps(resp)[:600]}"
            )
        if wc_id == sales_quote_id:
            # Defensive guard — if the IP ever returns the same Id as the
            # EQ, refuse to continue. The whole point of the WC flow is
            # to keep them distinct so AddQMQGToWC_CopyToEQ doesn't
            # dispose of the EQ itself.
            raise RuntimeError(
                f"create_WorkingCart returned WorkingCartId == SalesQuoteId "
                f"({wc_id}). Aborting — AddQMQGToWC_CopyToEQ would dispose "
                f"the Enterprise Quote."
            )
        return wc_id

    def cpq_copy_wc_to_eq(
        self,
        working_cart_id: str,
        sales_quote_id: str,
        member_ids: list[str],
        *,
        quote_group_ids: list[str] | None = None,
        qmb_data: dict | None = None,
        execute_last_chunk_steps: bool = True,
        name: str | None = None,
    ) -> dict:
        """
        Copy Working-Cart items onto the Enterprise Quote (attaching them
        to the given QuoteMember(s)) and dispose of the Working Cart via
        IP ``AddQMQGToWC_CopyToEQ``.

        CRITICAL: ``working_cart_id`` MUST differ from ``sales_quote_id``.
        The IP uses ``working_cart_id`` as the disposal target — passing
        the same Id for both would delete the EQ.
        """
        if working_cart_id == sales_quote_id:
            raise RuntimeError(
                "AddQMQGToWC_CopyToEQ called with WorkingCartId == SalesQuoteId "
                f"({working_cart_id}). Refusing to run — this would delete "
                "the Enterprise Quote. Spawn a proper Working Cart via "
                "cpq_create_working_cart() first."
            )
        payload = {
            "WorkingCartId": working_cart_id,
            "MemberIds": [{"Id": mid} for mid in (member_ids or [])],
            "QMBData": qmb_data or {},
            "QuoteGroupIds": quote_group_ids or [],
            "SalesQuoteId": sales_quote_id,
            "executeLastChunkSteps": execute_last_chunk_steps,
        }
        call_name = (
            name
            or f"IP: AddQMQGToWC_CopyToEQ (WC {working_cart_id} → EQ {sales_quote_id})"
        )
        return self.call_ip("AddQMQGToWC_CopyToEQ", payload, name=call_name)

    def call_ip(self, ip_key: str, payload: dict, *, name: str | None = None) -> dict:
        """
        Call a Vlocity / Omnistudio Integration Procedure.

        ip_key: 'Type_SubType', e.g. 'Business_CalculateMRRs'
        payload: options/input merged into one dict (ContextId, QuoteId, etc.)

        Returns the parsed response dict. Raises on HTTP error or
        if the response contains status='Error' or error field.
        """
        call_name = name or f"IP: {ip_key}"
        path = f"{self.ip_base}/{ip_key}/"
        status, body = self._request(
            "POST",
            path,
            name=call_name,
            body=payload,
        )
        if status >= 400:
            raise RuntimeError(f"IP {ip_key} HTTP {status}: {body}")
        # IPs sometimes return a JSON-encoded string — normalize.
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                pass
        if isinstance(body, dict):
            # Vlocity IPs emit {"error": "OK", "errorCode": "INVOKE-200"} on
            # success — so a bare truthy `error` isn't enough. Treat the
            # "OK" sentinel (and any INVOKE-2xx code) as success.
            raw_err = body.get("error") or body.get("errorMessage")
            err_code = str(body.get("errorCode") or "")
            is_ok_sentinel = (
                isinstance(raw_err, str) and raw_err.strip().upper() == "OK"
            )
            is_2xx_code = err_code.startswith("INVOKE-2")
            err = None
            if not is_ok_sentinel and not is_2xx_code:
                err = raw_err or (
                    body.get("message") if body.get("status") == "Error" else None
                )
            if err:
                raise RuntimeError(f"IP {ip_key} returned error: {err}")
            # Inspect nested payload for Vlocity-style {"messages":[{"severity":"ERROR"}]}
            # — some IPs return INVOKE-200 at the envelope while reporting
            # per-item failures deeper in the tree (e.g. postCartsItems,
            # AddQMQGToWC_CopyToEQ). Silent 200-with-errors is the #1
            # source of false-positive PASSes in API tests.
            self._inspect_cpq_envelope(body, op=f"IP {ip_key}")
        return body

    # ── Convenience: build Lightning record URL ─────────────

    def record_url(self, sobject: str, record_id: str) -> str:
        base = config.SF_LOGIN_URL.rstrip("/")
        return f"{base}/lightning/r/{sobject}/{record_id}/view"
