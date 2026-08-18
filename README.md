<div align="center">

# sfauto

**Salesforce test automation — UI and API, in Python.**

Write a test once, run it against any Salesforce org, get a single HTML
report with screenshots, video and every API call embedded.

</div>

---

## 1. Install

You need **Python 3.11+** and **git**. Everything else the installer handles.

### macOS / Linux

```bash
git clone https://github.com/ashish5social/sfTestAuto.git
cd sfTestAuto
./install.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/ashish5social/sfTestAuto.git
cd sfTestAuto
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer creates a virtual environment, installs the package, and
downloads the Chromium browser Playwright drives.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium
```

</details>

---

## 2. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your org:

```bash
SF_LOGIN_URL=https://your-org.my.salesforce.com
SF_USERNAME=you@example.com
SF_PASSWORD=your-password
```

If your org uses **Google/Okta/SAML sign-in or MFA**, a password won't work
— use JWT instead, which needs no password and no one-time codes.
[docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) walks through it.

Check everything is wired up:

```bash
source venv/bin/activate          # Windows: .\venv\Scripts\Activate.ps1
sfauto doctor
```

```
✔ Python 3.12.1
✔ Chromium installed
✔ Profile 'default'
✔ Credentials present
✔ Connected to org as user 005f6000008BUmCAAW
All checks passed.
```

Fix anything it flags before moving on — it tells you exactly what's wrong.

---

## 3. Run tests

Activate the venv first (`source venv/bin/activate`), then pick either way.

### A. From the command line

```bash
sfauto test tests/                            # everything
sfauto test tests/ui                          # UI tests only
sfauto test tests/api                         # API tests only
sfauto test tests/ui/test_create_account.py   # one test
sfauto test tests/ --headless                 # no visible browser
```

Plain `pytest` works too, if you prefer it:

```bash
pytest tests/
pytest tests/ui/test_create_account.py -v
pytest tests/ -n 4                            # 4 tests in parallel
```

Reports land in `reports/`. Open the newest `.html` in a browser.

### B. In the browser (dashboard)

```bash
sfauto server start
```

Open **http://localhost:8091**. You get a test picker, live video of each
browser as it runs, up to four at once in a grid, and the report when it
finishes.

```bash
sfauto server status
sfauto server stop
```

> Runs on port 8091. Change it with `DASHBOARD_PORT` in `.env`.

---

## 4. Run from GitHub Actions

Go to the **Actions** tab → **Run Salesforce Tests** → **Run workflow**,
then choose which tests, who gets the report, and how many run in
parallel. You get an email with the pass/fail summary and a link to the
full report.

First time only, add these under
`Settings → Secrets and variables → Actions`:

**Secrets** — `SF_USERNAME`, `SF_CLIENT_ID`, `SF_JWT_USERNAME`,
`SF_JWT_KEY`, `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`

**Variables** — `SF_LOGIN_URL`, `MAIL_FROM`

Step-by-step, including where each value comes from:
**[docs/CI.md](docs/CI.md)**.

---

## 5. Write your own test

```bash
sfauto new my_first_test          # UI test
sfauto new my_api_test --api      # API test
```

That copies a working example you can edit. Tests read like a script:

```python
with sf.step(1, "Log in"):
    sf.login()

with sf.step(2, "Create an Account"):
    sf.open_list_view("Account")
    sf.click("New")
    sf.fill("Account Name", "Acme Corp")
    sf.click("Save")
    sf.assert_("Saved", sf.wait_for_toast("was created"))
```

Every step becomes a card in the report, with a screenshot attached.
[docs/WRITING_TESTS.md](docs/WRITING_TESTS.md) has the full helper list.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `sfauto: command not found` | Activate the venv: `source venv/bin/activate` |
| Login fails / lands on a verification page | Your org is SSO or MFA gated — see [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) |
| Anything else | Run `sfauto doctor` first; it names the actual problem |

---

## Docs

| | |
|---|---|
| [WRITING_TESTS.md](docs/WRITING_TESTS.md) | How to write a test, and every helper available |
| [AUTHENTICATION.md](docs/AUTHENTICATION.md) | Password, SSO and JWT login setup |
| [CI.md](docs/CI.md) | GitHub Actions: secrets, reports, email, cleanup |
| [REFERENCE.md](docs/REFERENCE.md) | Everything else — architecture, dashboard internals, VPS deployment |
