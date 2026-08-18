# Authentication

Two separate problems, often confused:

| | What it needs | Used by |
|---|---|---|
| **API auth** | a session id | `sf_api` fixture, REST calls, cleanup |
| **UI auth** | a logged-in *browser* | `sf.login()`, every Playwright test |

The framework solves both from one credential set. Pick a strategy per org
in `profiles/<org>.yml`; credentials always live in `.env`, never in a
profile.

---

## Which strategy do I need?

```
Can you log in with a Salesforce username + password,
with no SSO redirect and no MFA prompt?
├─ yes → ui_login: password        (simplest; nothing to set up)
└─ no  → the org is SSO-federated (Google / Okta / SAML) or MFA-gated
         └─ use JWT bearer + ui_login: frontdoor   ← the robust answer
```

There is also `ui_login: storage_state` — a human logs in once via
`sfauto auth capture` and Playwright replays the cookies. It works, but
the session expires and someone has to repeat it, so it is a stopgap
rather than a CI answer.

---

## Why SSO orgs need JWT specifically

Worth stating plainly, because two obvious-looking shortcuts are dead
ends and you will otherwise rediscover them the hard way:

- **Automating the SSO login form does not work.** Identity providers
  detect the automation and refuse: Google answers "Couldn't sign you in
  — this browser or app may not be secure." This is not a bug to route
  around; it is the provider working as intended. The framework's
  `google_sso` strategy deliberately refuses to type credentials.

- **OAuth client-credentials is not enough for the *UI*.** It
  authenticates REST calls fine, but the token it issues only ever
  carries the `api` scope — adding `web` to the app changes nothing.
  `/secur/frontdoor.jsp` rejects an `api`-only token, so the browser
  lands back on the SSO page.

**JWT bearer is the one headless flow that issues a `web`-scoped token.**
That token works for REST *and* can be handed to `frontdoor.jsp` to drop
a real browser straight into Lightning — no password typed, no MFA
prompt, no OTP to intercept, and nothing that expires next week.

---

## Setting up JWT bearer (once per org)

### 1. Generate a keypair

```bash
openssl req -x509 -sha256 -nodes -days 3650 -newkey rsa:2048 -keyout .certs/sfauto.key -out .certs/sfauto.crt -subj "/CN=sfauto-test-automation"
```

`.certs/` is git-ignored. The private key never leaves your machine (in
CI, inject it as a secret file).

### 2. Create an External Client App

Setup → **External Client App Manager** → **New External Client App**.

- Contact email: yours
- Distribution State: **Local**
- Enable OAuth, callback URL `http://localhost:1717/callback` (unused by
  JWT, but the form requires one)
- Scopes: `api`, `web`, `refresh_token`
- **Enable Client Credentials Flow** (optional, for API-only runs) and
  set *Run As* to your integration user
- **Enable JWT Bearer Flow** → upload `.certs/sfauto.crt` → Save
- IP Relaxation: **Relax IP restrictions**

> A **Connected (Managed)** app cannot be used — its OAuth definition is
> read-only, so you cannot enable these flows on it. Create a new app.

### 3. Pre-authorize the user

Policies tab → Edit → OAuth Policies → **Permitted Users** =
*Admin approved users are pre-authorized* → add the user's **Profile**
(e.g. System Administrator) or a permission set → Save.

Skip this and the token request fails with:

```
{"error":"invalid_grant","error_description":"user hasn't approved this consumer"}
```

### 4. Point `.env` at the key

```bash
SF_CLIENT_ID=3MVG9...            # the app's consumer key
SF_JWT_KEY_FILE=.certs/sfauto.key
SF_JWT_USERNAME=you@example.com  # the user to impersonate
```

### 5. Switch the profile to frontdoor

```yaml
# profiles/<org>.yml
ui_login: frontdoor
```

### 6. Verify

```bash
sfauto doctor
```

---

## Auth order

Both the API client and `get_frontdoor_url()` try, in order:

1. **JWT bearer** — if `SF_JWT_KEY_FILE` and `SF_CLIENT_ID` are set
2. **OAuth client-credentials** — if `SF_CLIENT_SECRET` is set
3. **OAuth password grant**
4. **SOAP login** — needs `SF_SECURITY_TOKEN`

Every attempt is recorded, and the failure message lists each one with
its reason, so you never have to guess which link in the chain broke.

---

## Troubleshooting

| Message | Cause |
|---|---|
| `user hasn't approved this consumer` | Step 3 not done — profile not pre-authorized |
| `invalid_client_id` | `SF_CLIENT_ID` doesn't match the app, or the app was recreated |
| `invalid_grant: invalid assertion` | Cert uploaded doesn't match `SF_JWT_KEY_FILE` |
| `inactive_user` | `SF_JWT_USERNAME` is deactivated or frozen |
| Browser lands on the SSO page after frontdoor | Token lacks `web` scope — you're on client-credentials, not JWT |
| `INVALID_LOGIN` on SOAP | `SF_LOGIN_URL` is missing its `https://` scheme, or the security token is stale |

The audience (`aud`) claim is chosen automatically:
`https://test.salesforce.com` for sandboxes, `https://login.salesforce.com`
otherwise — including My Domain URLs, which is what trial and production
orgs need.
