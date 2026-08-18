# sfauto — Claude Session Brief

> **If you're a fresh Claude session and don't know this project, read this file
> first.** It's optimized for "get productive in 30 seconds with no prior context".
> The user-facing docs live in `README.md` — link to it from your answers when
> appropriate, but THIS file is what gets you oriented fastest.

---

## 1. What this project is

End-to-end test automation for **Salesforce Revenue Cloud / Communications Cloud
(Vlocity CPQ)**. Two test flavors run side-by-side:

- **UI tests** — Playwright drives real headless Chrome (or Edge/Firefox/WebKit) through Salesforce Lightning + Vlocity catalog/cart screens.
- **API tests** — pure REST + SOAP + Vlocity Integration Procedure calls via `simple-salesforce` + `requests`, no browser.

Both flavors share: the same `sfauto test` CLI, the same web dashboard, the same parallel pool (max 4 workers), the same HTML report format, the same CI workflow. The two flavors are explicitly twinned: TC1+TC2 are UI, TC3+TC4 are their API equivalents.

**Tech stack:** Python 3.11+, Playwright + pytest-playwright, pytest-xdist (CI parallelism), FastAPI (dashboard backend), React via CDN + Babel (dashboard frontend — single file, no build step), simple-salesforce, Pillow (visual regression diff).

---

## 2. The 6 files that explain everything

If you're going to touch test code, read these in order before doing anything:

| # | File | Why |
|--|--|--|
| 1 | `README.md` | The single user-facing doc. Has install, features, writing tests, library reference, troubleshooting, CI walkthrough, VPS deploy. Skim sections 11 (Writing tests) + 12 (Library reference) — that's 80 % of what you'll do. |
| 2 | `tests/conftest.py` | All framework wiring. Fixtures: `page`, `tracker`, `sf`, `api_tracker`, `sf_api`. Screencast bridge, browser-specific launch args, video recording, golden-image visual regression. |
| 3 | `src/core/sf_ui/__init__.py` | What the high-level library exports. From here you can see the module boundaries: auth, navigation, forms, actions, waits, cart, step. |
| 4 | `tests/ui/test_create_account.py` | The canonical UI test (22 steps). Read its first 200 lines + a couple of `tracker.start_step` blocks to learn the pattern. |
| 5 | `tests/api/test_account_api.py` | The canonical API test. Same pattern but uses `sf_api.call_ip(...)` / `sf_api.create(...)` / `sf_api.soql(...)`. |
| 6 | `src/web/parallel_runner.py` | How the dashboard runs N tests in parallel. Spawns 1 pytest subprocess per slot, work-stealing queue, per-slot cancellation. CI uses pytest-xdist with the same model. |

---

## 3. Project layout (annotated)

```
sfauto/
├── README.md                          ← user-facing single source of truth
├── CLAUDE.md                          ← (this file — Claude session brief)
├── install.sh / install.ps1           ← one-step setup (creates venv, installs deps + Chromium)
├── pyproject.toml                     ← Python deps + `sfauto` CLI entry point
├── .env.example                       ← template for Salesforce credentials
│
├── src/
│   ├── cli.py                         ← `sfauto test`, `sfauto server`, `sfauto list` entry points
│   ├── core/
│   │   ├── sf_ui/                     ← **HIGH-LEVEL SALESFORCE UI LIBRARY** (use this in new tests)
│   │   │   ├── __init__.py            ←   re-exports StepRunner, step
│   │   │   ├── auth.py                ←   login + frontdoor bypass (OAuth → SOAP fallback chain)
│   │   │   ├── navigation.py          ←   list views, open record, app switcher, URL-id extraction
│   │   │   ├── forms.py               ←   fill, lookup, picklist, record-type radio, date (LIFTED FROM TC1)
│   │   │   ├── actions.py             ←   click_button (shadow-DOM aware), click_shadow_button, click_shadow_order_link
│   │   │   ├── waits.py               ←   wait_page_ready, wait_spinner, wait_for_toast, wait_for_config_update_complete
│   │   │   ├── cart.py                ←   Vlocity-specific: search_catalog, add_product_to_cart, configure_attribute
│   │   │   └── step.py                ←   StepRunner context manager (with sf.step(N, "label"):)
│   │   ├── step_tracker.py            ← StepTracker — capture step pass/fail + screenshot + assertions (UI tests)
│   │   ├── html_reporter.py           ← per-test HTML with embedded screenshots + base64 .webm video
│   │   ├── playwright_helpers.py      ← screenshot() + compare_screenshot() (visual regression diff)
│   │   ├── step_renderer.py           ← renders {placeholder} tokens against JSON data (used by dashboard parser)
│   │   ├── config.py                  ← SFAUTO_OUTPUT_DIR, REPORTS_DIR, etc.
│   │   └── playwright_generator.py    ← legacy "generate test skeleton from YAML" CLI (no longer used; YAML is gone)
│   ├── api/
│   │   ├── sf_api_client.py           ← jsforce-like wrapper: soql, create, update, delete, call_ip, cpq_v2_* (1100+ lines, auto-logs everything to api_tracker)
│   │   ├── api_tracker.py             ← APITracker — parallel to StepTracker for API tests, holds REQ/RES per step
│   │   └── api_reporter.py            ← HTML with REQ / RES JSON cards instead of screenshots
│   └── web/
│       ├── server.py                  ← FastAPI app; mounts routes + /reports + /screenshots
│       ├── parallel_runner.py         ← Bounded work-stealing pool (max 4 workers), browser_pytest_args() mapping
│       ├── combined_report.py         ← stitches per-test HTMLs into one offline run_<id>.html
│       ├── frontend/runner.html       ← Single-file React dashboard (no build step!). ~2000 lines. Edit in place + refresh.
│       └── routes/
│           ├── generated_tests.py     ← /api/generated/run SSE endpoint + the AST-based metadata parser
│           ├── screencast.py          ← /api/screencast bridge (CDP frame POST → WebSocket fan-out)
│           ├── mode_routes.py         ← (unused stub from legacy)
│           └── sandbox_routes.py      ← (unused stub from legacy)
│
├── tests/
│   ├── ui/
│   │   ├── test_create_account.py    ← 22-step UI test, DIA product
│   │   ├── test_create_account.py    ← 22-step UI test, Fiber Broadband
│   │   ├── data/
│   │   │   ├── tc1_create_enterprise_quote_with_dia.json       ← addresses, products, bandwidth, expected
│   │   │   └── tc2_create_enterprise_quote_with_fbb.json
│   │   └── goldens/                   ← visual-regression baselines (created on demand)
│   ├── api/
│   │   ├── test_create_account.py  ← 13-step API twin of TC1, 5 steps commented out (Phases 4-6)
│   │   ├── test_create_account.py  ← 13-step API twin of TC2, 5 steps commented out
│   │   └── data/                       ← same shape as ui/data
│   ├── draft/                          ← experimental / scratch tests — NOT picked up by CI or dashboard
│   └── conftest.py                     ← all framework wiring (fixtures, screencast, golden diff, browser launch args)
│
├── scripts/
│   ├── ci_report.py                    ← CI: per-test HTMLs → one offline report + email body (CURRENT)
│   ├── gh_pages_index.py               ← rebuilds the gh-pages run index
│   ├── gh_pages_prune.py               ← deletes published runs past the retention window
│   ├── build_combined_run_report.py    ← superseded by ci_report.py; kept, not invoked
│   ├── generate_index.py               ← legacy per-test list view for GH Pages (still callable, not invoked by the workflow anymore)
│   ├── cleanup_test_data.py            ← deletes SFAUTO-marked records from Salesforce (--keep-days N --dry-run)
│   └── sync_test_dropdown.py           ← auto-syncs the test dropdown in run-tests.yml when test files change
│
├── .github/workflows/
│   ├── run-tests.yml                   ← MAIN CI workflow: workflow_dispatch with tests / workers / browser / email inputs
│   ├── cleanup-test-data.yml           ← scheduled cleanup
│   └── sync-test-list.yml              ← auto-syncs CI dropdown when tests/ui or tests/api files change
│
├── skills/
│   ├── ih_create_test/SKILL.md         ← AI skill: generate a new test from high-level steps via live browser exploration
│   └── ih_self_heal_test/SKILL.md      ← AI skill: diagnose + fix a failing test by walking the live browser and re-discovering locators
│
├── reports/                            ← per-run HTML reports (gitignored)
├── screenshots/                        ← per-step screenshots + videos (gitignored)
├── videos_tmp/                         ← Playwright's temp video dir before ffmpeg compression (gitignored)
├── test_run_history.json               ← dashboard's run history, capped at 200 entries
└── venv/                               ← Python virtual env (gitignored)
```

---

## 4. The sf_ui library — use this BEFORE writing raw locators

`src/core/sf_ui/` is the shared toolbox. The `sf` fixture in conftest exposes
everything as instance methods so tests read like recipes:

```python
class TestSomething:
    """TC9 - Friendly Test Name"""
    TAGS = ["smoke", "account"]
    OBJECTIVE = "Create an account at {addresses[0].region} and verify."

    def test_something(self):
        page, sf = self.page, self.sf

        with sf.step(1, "Log into Salesforce"):
            sf.login()
            sf.assert_("on Lightning", "lightning" in page.url)

        with sf.step(2, "Create account"):
            sf.open_list_view("Account")
            sf.click("New")
            sf.select_record_type("Business")
            sf.click("Next")
            sf.wait_form_ready(["Account Name"])
            sf.fill("Account Name", ACCOUNT_NAME)
            sf.click("Save")
            sf.wait_page_ready(4000)
```

| sf.* method (most common) | What it does |
|---|---|
| `sf.step(N, "label")` | Context manager — auto tracker.start_step + pass_step/fail_step + screenshot |
| `sf.assert_(desc, cond)` | Attach assertion to current step |
| `sf.login()` | Standard login + frontdoor bypass if needed. Picks env from .env. |
| `sf.click("Save")` | Role-based + shadow-DOM-walk fallback. Pass a regex for flexibility. |
| `sf.fill(label, value)` | Text/textarea fill by visible label |
| `sf.fill_lookup(label, search)` | Inline-dropdown → search-dialog → exact-1 picker |
| `sf.set_picklist(label, value)` | Handles native select / Aura anchor / Lightning combobox / button trigger |
| `sf.fill_date(label, value)` | Date inputs (clears + Tab-commits) |
| `sf.select_record_type(value)` | Shadow-DOM radio click for record type chooser |
| `sf.open_list_view(sobject)` | Goto /lightning/o/<sobject>/list |
| `sf.open_record(sobject, id)` | Goto /lightning/r/<sobject>/<id>/view |
| `sf.extract_record_id(sobject=…)` | Parse record id from current URL |
| `sf.wait_page_ready(extra_ms=)` | networkidle (5s cap) + spinners + buffer |
| `sf.wait_for_toast(text, settled=)` | Wait for SF/Vlocity toast |
| `sf.wait_for_config_update()` | After every Vlocity Configure Cart edit — flushes "Updating..." toast + spinners |
| `sf.search_catalog(term)` | Type into Vlocity catalog search, wait for results |
| `sf.add_product_to_cart(text)` | Scoped-container Add button |
| `sf.configure_attr(label, value)` | Cart attribute — picklist-first, text-fallback, auto-waits |
| `sf.screenshot(name)` | Take + save |
| `sf.screenshot_with_golden(name)` | Visual-regression diff vs `tests/ui/goldens/<test_stem>/<name>.png` |

**Rule:** never write a raw `page.locator(...)` selector before checking if a helper exists. If you write a helper inline, lift it into the right sf_ui module.

---

## 5. Test metadata convention (single source of truth — NO YAML)

Each test exposes its display info as Python constructs. The dashboard parser
in `src/web/routes/generated_tests.py` walks the AST and pulls out:

```python
class TestCreateEnterpriseQuoteWithDIA:
    """TC1 - Create Enterprise Quote with DIA"""        # ← first line of class docstring = display_name

    # Class attributes — read via ast.literal_eval. Lists + strings only.
    TAGS      = ["enterprise", "dia", "quote", "smoke"]
    OBJECTIVE = "End-to-end flow ... at {product.bandwidth}."  # ← {placeholder} rendered against JSON data
```

**Step labels are the inline strings at each `start_step` / `sf.step` call site.**
The parser also handles the legacy `_step_label(N, "X")` wrapper (peeks
inside, extracts X). Both work — no rewrite needed when refactoring.

There used to be `tests/{ui,api}/definitions/*.yaml` — **they are gone**.
Don't recreate them. The parser does not look at YAML.

---

## 6. Test data convention

```
tests/ui/data/tc<N>_<slug>.json     ← UI test data
tests/api/data/tc<N>_<slug>.json    ← API test data
```

Tests load via:

```python
DATA = json.loads((Path(__file__).parent / "data" / "tc1_create_enterprise_quote_with_dia.json").read_text())
```

JSON values referenced by `{placeholder}` syntax in the YAML are now
resolved at dashboard-display time only (the test doesn't see placeholders —
it has the JSON values directly).

---

## 7. Critical implementation patterns (memorize)

### Slot-aware TIMESTAMP

Every test computes a unique timestamp so parallel runs don't collide on
account names like `SFAUTO_Biz_0521_113900`. Copy this block verbatim to
new tests:

```python
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ  = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(TZ)
_slot = (os.environ.get("UI_TEST_SLOT")
         or os.environ.get("PYTEST_XDIST_WORKER", "").replace("gw", ""))
TIMESTAMP = (NOW.strftime("%m%d_%H%M%S")
             + f"{NOW.microsecond // 1000:03d}"
             + (f"s{_slot}" if _slot else ""))
ACCOUNT_NAME = f"{DATA['account_name_prefix']}{TIMESTAMP}"
```

`UI_TEST_SLOT` is set by the dashboard pool. `PYTEST_XDIST_WORKER` is set by
pytest-xdist in CI. The fallback (no env) is for plain single-process runs.

### SFAUTO marker

Every Salesforce record a test creates MUST have `SFAUTO` somewhere in its
Name field. `scripts/cleanup_test_data.py` finds + deletes records by this
marker. Prefixes used: `SFAUTO_Biz_`, `SFAUTO_Quote_`, `SFAUTO_API_`, etc.

### Per-test report attribution

When 4 subprocesses run in parallel and all write reports concurrently, the
naive snapshot-diff approach mis-attributes. `parallel_runner.py` parses the
`Report: <path>` / `API Report: <path>` line each conftest.py teardown prints,
giving deterministic per-test attribution. Don't break this — if you change
the reporter output format, keep that single line.

### Frontdoor login bypass

Salesforce shows an email-verification page when the request comes from an IP
not on the Network Access whitelist (laptops, GH Actions runners). `sf.login()`
bypasses this by obtaining a session id out of band and navigating to
`/secur/frontdoor.jsp?sid=...`.

**frontdoor requires a `web`-scoped token, and only JWT bearer issues one
headlessly.** Client-credentials tokens carry `api` scope only — adding
`web` to the app does not change the issued scope — and frontdoor rejects
them, bouncing the browser back to the login/SSO page. So SSO-federated or
MFA-gated orgs need `SF_JWT_KEY_FILE` + `SF_CLIENT_ID` + `SF_JWT_USERNAME`,
the certificate uploaded to the app, and the user's profile pre-authorized
("Admin approved users are pre-authorized"). See docs/AUTHENTICATION.md.

### Browser-specific handling

- Chromium-only launch args (`--no-sandbox`, `--disable-dev-shm-usage`,
  `--deny-permission-prompts`) are applied only for Chrome/Edge/Chromium.
  Firefox + WebKit reject those flags.
- CDP screencast is Chromium-only. The conftest fixture short-circuits when
  `UI_TEST_BROWSER_IS_CHROMIUM=false`. Recorded video still works for all
  browsers via Playwright's built-in `record_video_dir`.

---

## 8. Running tests — every path

| Path | Command |
|---|---|
| Dashboard | `sfauto server` → open http://localhost:8091/runner → pick tests + workers + browser → Run |
| Single test (headed) | `sfauto test tests/ui/test_create_account.py` |
| Whole folder | `sfauto test tests/ui` |
| Headless | `sfauto test <path> --headless` |
| Refresh visual-regression baselines | `sfauto test tests/ui --update-goldens` |
| Parallel locally via xdist | `python -m pytest tests/ -n 4 --dist=loadfile --headless` |
| CI | Actions tab → Salesforce UI Test Automation → Run workflow (tests / workers / browser / email inputs) |

---

## 9. Dashboard architecture (in one diagram)

```
+--------------------+       +----------------------+       +-----------------------+
|  React frontend    |  SSE  |  FastAPI backend     |  spawn|  pytest subprocess    |
|  (runner.html)     |<======|  (parallel_runner)   |======>|  (one per slot, max 4)|
|  - Tests + History | <-----|                      |       |                       |
|  - MultiViewer     | WS    |  - /api/generated/run|       |  - tests/conftest.py  |
|  - Live screencast |<======|  - /api/screencast   |<------|  - tests/{ui|api}/    |
+--------------------+ JPEG  +----------------------+ CDP   +-----------------------+
                                       |                              |
                                       v                              v
                            +------------------+         +------------------------+
                            |  test_run_history|         |  Salesforce sandbox    |
                            |  .json (200 cap) |         |  (UI + REST + Vlocity) |
                            +------------------+         +------------------------+
```

- Frontend → backend: HTTP POST `/api/generated/run` with `{tests, parallelism, browser}`. Response is an SSE stream.
- Backend → workers: spawns N pytest subprocesses (`asyncio.create_subprocess_exec`), reads stdout line-by-line, parses for `Report: <path>` to attribute per-test HTML.
- Workers → backend (screencast): each pytest subprocess opens a CDP session on its Playwright `page`, posts JPEG frames to `/api/screencast/{run_id}/frame`.
- Backend → frontend (screencast): per-slot WebSocket fan-out at `/ws/screencast/{run_id}` — base64 JPEG strings.
- After all workers finish: backend calls `combined_report.build_combined_report()` to stitch per-test HTMLs into one `run_<id>.html`. Stores in `reports/`.

---

## 10. The 4 GitHub Actions workflows (and what they do)

| Workflow | Trigger | What it does |
|---|---|---|
| `run-tests.yml` | `workflow_dispatch` (manual) | The main test workflow. Inputs: tests / workers / browser / email. Installs the requested browser only, runs pytest-xdist with the right `--browser` flags, builds combined HTML, deploys to GH Pages at `runs/<id>/`, optionally emails the link. |
| `cleanup-reports.yml` | `workflow_dispatch` | Prunes published runs from gh-pages. `keep_days` counts today as day 1; 0 wipes everything. |
| `sync-test-dropdown.yml` | push to `tests/**` | Regenerates the run-tests dropdown via `scripts/sync_test_dropdown.py`. |
| `sync-test-list.yml` | `push` to main (when `tests/ui/test_*.py` etc. changes) | Auto-refreshes the `tests` dropdown in `run-tests.yml` so it always reflects current test files. Needs `GH_PAT` secret. |

Required secrets (`Settings → Secrets and variables → Actions → Secrets`):
`SF_USERNAME`, `SF_CLIENT_ID`, `SF_JWT_USERNAME`, `SF_JWT_KEY` (the whole
private key), `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`.
Variables: `SF_LOGIN_URL`, `MAIL_FROM` (must be a validated Mailjet
sender). No password, security token or client secret is needed — CI
authenticates with JWT. Full guide: docs/CI.md.

---

## 11. Rules (don't break these)

1. **SFAUTO in every test record name** — cleanup script depends on it.
2. **Slot-aware TIMESTAMP** — copy the block verbatim into new tests; never use plain `datetime.now().strftime("%H%M%S")` because parallel workers WILL collide.
3. **No framework imports in test files** — never `from src.core.step_tracker import StepTracker`. Use the `tracker` + `sf` (or `api_tracker` + `sf_api`) fixtures.
4. **No raw locators without checking sf_ui first** — `sf.fill(label, value)` before `page.locator(...)`.
5. **No YAML** — metadata lives in the .py file. The dashboard parser ignores YAML.
6. **No new dependencies on `tests/generated/`** — that folder is gone, tests live in `tests/ui/` and `tests/api/`.
7. **Don't break the `Report: <path>` print** in conftest's report teardown — `parallel_runner.py` parses it for per-test attribution.

---

## 12. Common things you'll be asked to do (and where the code is)

| Task | Look here |
|---|---|
| "Add a new test for X" | Template is in README.md → "Adding a new test (worked example: TC5)". Copy the skeleton, save to `tests/ui/test_<slug>.py` + `tests/ui/data/<slug>.json`. |
| "Fix this failing test step" | First read the .py file's affected step. If it uses raw `page.locator(...)`, check if `sf.*` has a helper. Use `skills/ih_self_heal_test/SKILL.md` workflow if you have Chrome + Claude in Chrome. |
| "Add a new sf.* helper" | Implement in the right sf_ui module (`auth`/`navigation`/`forms`/`actions`/`waits`/`cart`), then add an SFHelpers method in `tests/conftest.py` that delegates to it. |
| "Tweak the dashboard UI" | `src/web/frontend/runner.html` — single file, no build step. Edit + refresh page. |
| "Change the report layout" | `src/core/html_reporter.py` for UI tests; `src/api/api_reporter.py` for API. |
| "Add a column to the Run History table" | `src/web/frontend/runner.html`, search for `<th style={{width:` in the History section. |
| "Modify the parallel pool" | `src/web/parallel_runner.py`. Don't forget to update CLAUDE.md + README.md if you change SSE event shape. |
| "Add another browser" | Map it in `parallel_runner.py:browser_pytest_args()`, add to the dashboard's `<select>` in `runner.html`, add to the workflow's `options:` list in `.github/workflows/run-tests.yml`, add to the install case statement in the same file. |
| "Send Slack notification on run finish" | New event handler in the `done` SSE branch in `generated_tests.py`, or a webhook call after the combined report builds. |

---

## 13. Recent architecture decisions (2026)

- **YAML definitions removed (May 2026).** Single source of truth = the .py file. Class docstring → name, `TAGS` + `OBJECTIVE` class attrs, inline step labels. Dashboard parses via AST.
- **sf_ui library added (May 2026).** Lifted all the helpers that were duplicated in TC1 + TC2 into `src/core/sf_ui/`. New tests should use it from day one. TC1-4 still use the legacy inline-helper style; they'll be refactored to the new pattern in a future task.
- **Combined report = `index.html` on GH Pages (May 2026).** Old `generate_index.py` list view dropped from CI. Combined report has an in-page Download button that serializes the DOM to a Blob (works locally and on GH Pages).
- **Per-test attribution via printed Report: lines (May 2026).** Replaced fragile snapshot-diff approach. Survives 4-way parallel writes.
- **Browser selector (May 2026).** Dashboard + CI. Chrome / Edge / Firefox / WebKit. Conditional install in CI saves ~60-90s.
- **Worker stagger of 10s (May 2026).** Avoids 4-simultaneous OAuth burst overwhelming the org at run start.

---

## 14. When something feels wrong

Most issues have a known fix in README.md → "Troubleshooting playbook". The
common ones:

- Field not found by label → exact label may include `*` or extra whitespace; right-click + Inspect in real browser
- Lookup returned 0 results → record genuinely missing OR debounce hasn't fired (increase wait)
- Picklist value not found → call `sf.click(label)` first to force-open the popup, wait 1s, then retry
- Configure Cart edit didn't apply → call `sf.wait_for_config_update()` after the edit
- Frontdoor lands back on the login/SSO page → token lacks `web` scope; you are on client-credentials, not JWT
- JWT returns "user hasn't approved this consumer" → app not pre-authorized for the user's profile
- Tests pass locally but fail in CI → IP not whitelisted; frontdoor needs a `web`-scoped token, so confirm `SF_CLIENT_ID` + `SF_JWT_KEY` + `SF_JWT_USERNAME` are set. Client-credentials alone cannot drive a browser login.
- Live screencast tile is dim in Firefox/WebKit → CDP is Chromium-only, fall back to recorded video

When stuck, point the user at the relevant README section rather than guessing.

---

## 15. Final reminders

- **README.md is the user-facing source of truth.** Always link to it (specific section anchors when possible) instead of restating its content in chat.
- **CLAUDE.md (this file) is for YOU.** Update it when you make architectural changes that the next Claude session would otherwise have to rediscover.
- **Run a sanity check before claiming done.** For Python edits: `python3 -c "import ast; ast.parse(open('<file>').read())"`. For YAML: `python3 -c "import yaml; yaml.safe_load(open('<file>').read())"`. For JSX in runner.html: there's no build step, so reload the page and watch the browser console.
- **The user is Ashish at sfauto.** US Pacific timezone. Prefers concise, decisive answers and is comfortable with deep technical discussion. He values explicit trade-offs over "it depends" hedging.
