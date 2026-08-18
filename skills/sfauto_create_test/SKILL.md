---
name: sfauto_create_test
description: >
  Generate a production-ready Playwright test script (.py) and JSON test data file
  for the sfauto tool by actually navigating a live Salesforce Revenue Cloud org in a
  browser. Use this skill whenever the user wants to create a new test, convert manual test steps to
  automation, generate a Playwright script for Salesforce, add a test case, or mentions test
  creation, test generation, or "new test" in any form. Also trigger when the user provides a list
  of test steps (numbered or plain text) that describe a Salesforce workflow. This skill covers
  Revenue Cloud / CPQ objects including Accounts, Quotes, Orders, Subscriptions, Product Rules,
  Price Books, Assets, and any Lightning UI workflow.
---

# sfauto_create_test — Salesforce Playwright Test Generator

## Overview

This skill generates a complete, production-ready test for the sfauto tool.
It doesn't just write code — it **validates every locator in a live browser** and then runs the test,
self-correcting failures up to 3 times.

**Output is two files** (no YAML — metadata lives inline in the .py):
- `tests/<ui|api>/test_cci_tc<N>_<slug>.py` — Playwright test code + class-level metadata
- `tests/<ui|api>/data/tc<N>_<slug>.json` — test data values (account names, addresses, products, etc.)

**Critical convention: use the `sf_ui` library before raw locators.** The
project ships a high-level helper library at `src/core/sf_ui/` exposed via
the `sf` fixture (`sf.fill`, `sf.click`, `sf.fill_lookup`, `sf.set_picklist`,
`sf.search_catalog`, `sf.add_product_to_cart`, `sf.wait_for_config_update`,
etc.). Always prefer `sf.fill("Account Name", X)` over
`page.get_by_label("Account Name").fill(X)`. Always prefer
`with sf.step(N, "label"):` over the legacy
`tracker.start_step(N, ...) / try / pass_step / fail_step` boilerplate.
Read `README.md` → Library reference for the full catalog before
generating anything.

---

## Workflow Summary (Tell the user this FIRST)

Before starting any work, tell the user:

```
Here's what I'll do to create your test:

1. GATHER INFO        — I'll ask you for the sfauto project folder location, whether
                        this is a UI or API test, Salesforce credentials, and
                        clarify any vague test steps.
2. PRESENT PLAN       — I'll show you the numbered test steps I'll automate and get
                        your confirmation.
3. DISCOVER           — I'll open a real Chrome browser, log into your Salesforce org,
                        and walk through every step to discover the actual UI locators
                        (buttons, fields, dropdowns, shadow DOM elements). For each
                        action I'll pick the right sf_ui helper (sf.fill, sf.click,
                        sf.fill_lookup, etc.) instead of raw locators.
4. GENERATE FILES     — I'll create 2 files (.py + data .json) directly in the
                        sfauto project so the web dashboard picks them up immediately.
                        NO YAML — metadata (name, tags, objective) lives as class
                        attributes in the .py.
5. BROWSER VALIDATE   — I'll re-run every locator from the generated test script in
                        the connected Chrome browser to confirm they actually resolve.
                        Any mismatches get fixed before the pytest run.
6. RUN & VALIDATE     — I'll run the test via pytest in headed mode. If it fails, I'll
                        diagnose the issue, fix the code, and re-run up to 3 times.
7. DELIVER            — Once the test passes, I'll show you the results. If it doesn't
                        pass after 3 attempts, I'll explain what's wrong and ask for help.

Shall I proceed?
```

Wait for confirmation before starting.

---

## Phase 1: Gather Requirements

### Step 1: Get the sfauto project folder location

Ask the user:
> Where is your sfauto project folder? (e.g., `/Users/yourname/ih_cci_test_automation`)

This is CRITICAL because the 2 output files must go directly into:
- `<cci_folder>/tests/ui/test_cci_tc<N>_<slug>.py` — UI test (with the `page` + `tracker` + `sf` fixtures)
  OR `<cci_folder>/tests/api/test_cci_tc<N>_<slug>_api.py` — API test (with `api_tracker` + `sf_api`)
- `<cci_folder>/tests/ui/data/tc<N>_<slug>.json` (or `tests/api/data/<…>.json`) — the test data file

**No YAML.** Test name, tags, objective, and step labels all live inline in
the .py — class docstring (= name), `TAGS = [...]` + `OBJECTIVE = "..."`
class attributes, and per-step labels at each `sf.step(N, "label")` call
site. The dashboard parses everything via AST.

Store the path as `CCI_ROOT` for all subsequent operations.

### Step 1b: Determine UI vs API

Ask the user (or infer from their description) whether this is a UI test or
an API test:

- **UI test** → `tests/ui/…`. Drives the browser via Playwright. Use this
  for end-to-end "click here, fill there" workflows that mirror what a
  user does in Lightning.
- **API test** → `tests/api/…`. Uses `SFApiClient` (REST + SOAP + Vlocity
  IPs) directly. Use this for data-path regressions, fast smoke tests of
  Integration Procedures, or anything where you don't need to verify the
  UI actually renders.

If the same flow exists in both, prefer authoring both — they're twinned
in this codebase (TC1↔TC3 = DIA; TC2↔TC4 = FBB).

### Step 2: Get the Test Steps

The user provides test steps — either as:
- A numbered list of manual steps ("1. Login, 2. Go to Accounts, 3. Click New...")
- A reference to an existing test file in `tests/ui/` or `tests/api/` (just borrow its structure)
- A plain English description of the workflow

If the steps are vague or incomplete, ask for clarification. Common things to clarify:
- Which Salesforce object are we testing? (Account, Order, Quote, Subscription, Asset, etc.)
- What record type should be selected (if applicable)?
- What fields need to be filled in and with what values?
- What is the expected end state? (record created, status changed, price calculated, etc.)
- Should test data be cleaned up after the run?

### Step 3: Get Salesforce Credentials

Check if `<CCI_ROOT>/.env` exists and has credentials. If it does, confirm with the user:
> I found credentials in your .env file (username: xxx). Should I use these?

If no .env or user wants different credentials, ask for:
1. **Salesforce org URL** — e.g., `https://login.salesforce.com`
2. **Username**
3. **Password**
4. **2FA / MFA** — if yes, warn that you will pause for manual verification

### Step 4: Confirm the Test Plan

Present a clear numbered test plan back to the user:

```
Test Plan: <Test Name>
  Step 1: Log into Salesforce at <URL>
  Step 2: Navigate to <Object> tab
  Step 3: Click "New" button
  Step 4: Select "<Record Type>" record type, click Next
  Step 5: Fill <fields> with auto-generated values
  Step 6: Click Save
  Expected: <what should happen>

Files I'll create:
  tests/ui/test_cci_tc<N>_<slug>.py    (or tests/api/… for API tests)
  tests/ui/data/tc<N>_<slug>.json      (or tests/api/data/…)
```

(No YAML — metadata is class attributes in the .py.)

Get confirmation before proceeding.

---

## Phase 2: Browser Navigation and Locator Discovery

This is the critical phase. You will use Claude in Chrome to navigate the actual Salesforce org
and discover locators for every action.

### Setting Up the Browser

1. Use `tabs_context_mcp` to get the current tab context (create if needed)
2. Navigate to the Salesforce login URL
3. Take a screenshot to confirm the page loaded

### Login Flow

1. Navigate to the Salesforce login URL
2. Find the username field and enter credentials
3. Find the password field and enter credentials
4. Click the Login button
5. **CRITICAL — 2FA Handling**: After clicking login, take a screenshot. If you see a
   verification code page, MFA prompt, or any authentication challenge:
   - STOP immediately
   - Tell the user: "I see a 2FA/MFA prompt. Please complete the verification in your browser, then tell me when you're done."
   - Wait for the user's confirmation before continuing
6. After login, verify you're on a Lightning page (URL contains "lightning")
7. Take a screenshot of the landed page

### Navigating Each Test Step

For EACH step in the test plan, TELL THE USER what you are doing:
> "Step 3: I'm now looking for the 'New' button on the Accounts page..."

Then:

1. **Take a screenshot BEFORE the action** — to understand the current page state
2. **Use `read_page` or `find` to locate the target element** — this gives you the element's role, name, ref, and position
3. **Record ALL viable locators** for the action. For every clickable/fillable element, capture:

   ```
   Priority 1 (Role-based):     page.get_by_role("button", name="Save")
   Priority 2 (Label-based):    page.get_by_label("*Account Name")
   Priority 3 (Text-based):     page.get_by_text("Consumer")
   Priority 4 (CSS selector):   page.locator("lightning-button[data-id='save']")
   Priority 5 (Shadow DOM JS):  sf.click_shadow_button("Activate")
   ```

4. **Perform the action** using the best locator
5. **Wait for page to settle** — Salesforce Lightning is heavy on async loading
6. **Take a screenshot AFTER the action** — to verify it worked
7. **If the action fails**, try alternative locators. If all fail, ask the user for help.

### Salesforce Lightning Locator Strategies

Salesforce Lightning uses Web Components with Shadow DOM extensively. Here are the most reliable strategies:

**For buttons:**
```
1. page.get_by_role("button", name="Save")                    # Best — resilient to DOM changes
2. page.get_by_role("button", name="Save", exact=True).last   # When multiple matches, .last is usually the modal
3. page.locator("button:has-text('Save')")                     # CSS fallback
4. sf.click_shadow_button("Save")                              # Shadow DOM fallback
```

**For input fields:**
```
1. page.get_by_label("*Account Name")                  # Label-based (the * prefix matches required fields)
2. page.get_by_placeholder("Search...")                 # Placeholder-based
3. page.locator("input[name='Name']")                   # Attribute-based
4. page.locator("lightning-input[field-name='Name'] input")  # Lightning component + native input
```

**For navigation:**
```
1. page.goto(f"{base_url}/lightning/o/Account/list")    # Direct URL navigation (most reliable)
2. page.get_by_role("link", name="Accounts")            # App navigation bar
3. page.locator("one-app-nav-bar-item-root[data-id='Account']")  # CSS on nav bar
```

**For dropdowns/picklists (Lightning combobox):**
```
1. page.get_by_role("combobox", name="Record Type").click()
   page.get_by_role("option", name="Consumer").click()
2. page.locator("lightning-combobox[label='Status'] button").click()
   page.get_by_text("Active", exact=True).click()
```

**For record type selection modals:**
```
1. page.get_by_label("Consumer").check()                # Radio button by label
2. page.get_by_text("Consumer").click()                 # Text click
3. page.get_by_role("radio", name="Consumer").check()   # Role-based radio
```

**For tables/related lists:**
```
1. page.get_by_role("link", name=re.compile(r"^\d{5,}$"))  # Order number links
2. page.locator("a[href*='/Order/']")                        # Href-based
3. sf.click_shadow_order_link()                               # Shadow DOM helper
```

**Common Salesforce wait patterns:**
```
sf.wait_page_ready(3000)          # Standard: networkidle + spinner wait + 3s buffer
sf.wait_page_ready(5000)          # After login or heavy page loads
page.wait_for_timeout(500)        # Short pause before form interactions
page.get_by_text("Success").wait_for(timeout=10000)  # Wait for success toast
```

### Handling Problems

If a locator doesn't work or a step fails:

1. Take a screenshot and examine the current page state
2. Use `read_page` with `filter: "interactive"` to see all available interactive elements
3. Try alternative locator strategies from the priority list above
4. If the page is different from what you expected (e.g., an error message, unexpected modal),
   take a screenshot and ask the user what to do
5. If a step is unclear or you need specific data (like a product name or price), ask the user

### Data to Record Per Step

For each step, record this information for code generation:

```
Step N:
  Name: "Click Save button"
  Description: "Save the new account record"
  Locator (primary):   page.get_by_role("button", name="Save", exact=True).last
  Locator (fallback1): page.locator("button.slds-button[title='Save']")
  Locator (fallback2): sf.click_shadow_button("Save")
  Pre-wait: None
  Post-wait: sf.wait_page_ready(5000)
  Screenshot name: "05_account_saved"
  Assertion: "Account detail page loaded"
  Assertion check: '"lightning/r/Account/" in page.url'
```

---

## Phase 3: Generate Output Files (Directly in Project Folders)

After successfully navigating all steps, generate three files **directly in the sfauto project**.

### IMPORTANT: File Locations

Files go DIRECTLY into the project — NOT a temp folder. **Only 2 files**:

- `.py` → `<CCI_ROOT>/tests/ui/test_cci_tc<N>_<snake_case_name>.py` (UI)
  OR `<CCI_ROOT>/tests/api/test_cci_tc<N>_<snake_case_name>_api.py` (API)
- `.json` → `<CCI_ROOT>/tests/ui/data/tc<N>_<snake_case_name>.json` (UI)
  OR `<CCI_ROOT>/tests/api/data/tc<N>_<snake_case_name>_api.json` (API)

No YAML. The dashboard parses metadata (name, tags, objective, step labels)
directly from the .py file via AST. See the .py template below — the
`TAGS = [...]` and `OBJECTIVE = "..."` class attributes are what the parser
picks up.

The web dashboard picks up the new test on refresh — no restart needed.

### Output File 1: Playwright Test Script (.py) — UI test template

Follow this template — it uses the `sf_ui` library + `StepRunner` pattern.
Replace `<TC_NUM>`, `<SLUG>`, `<PascalCaseName>` and the per-step content.

```python
"""TC<TC_NUM> — <Human Readable Test Name>."""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page

# ── Test data ─────────────────────────────────────────────────────────
DATA = json.loads(
    (Path(__file__).parent / "data" / "tc<TC_NUM>_<SLUG>.json").read_text()
)

# ── Slot-aware timestamp — parallel runs never collide on account names.
#    Copy this block verbatim into every new test.
TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(TZ)
_slot = (
    os.environ.get("UI_TEST_SLOT")
    or os.environ.get("PYTEST_XDIST_WORKER", "").replace("gw", "")
)
TIMESTAMP = (
    NOW.strftime("%m%d_%H%M%S")
    + f"{NOW.microsecond // 1000:03d}"
    + (f"s{_slot}" if _slot else "")
)

# ── Derived test values from JSON + TIMESTAMP ─────────────────────────
ACCOUNT_NAME = f"{DATA['account_name_prefix']}{TIMESTAMP}"
ADDRESS = DATA["addresses"][0]
PRODUCT = DATA["product"]

DEFAULT_TIMEOUT = DATA.get("timeout_ms", 60000)


class Test<PascalCaseName>:
    """TC<TC_NUM> - <Human Readable Test Name>"""

    # ── Class-level metadata read by the dashboard parser (AST).
    #    Placeholders like {addresses[0].region} are rendered against
    #    the JSON data file at display time.
    TAGS      = ["<tag1>", "<tag2>", "smoke"]
    OBJECTIVE = (
        "<One- or two-sentence summary. Use {data.placeholders} where "
        "useful — they render in the dashboard's info popup.>"
    )

    @pytest.fixture(autouse=True)
    def setup(self, page: Page, tracker, sf):
        self.page = page
        self.page.set_default_timeout(DEFAULT_TIMEOUT)
        self.tracker = tracker
        self.sf = sf
        yield

    def test_<snake_case_method>(self):
        page, sf = self.page, self.sf

        with sf.step(1, "Log into Salesforce"):
            sf.login()
            sf.assert_("Landed on Lightning", "lightning" in page.url.lower())

        with sf.step(2, "Navigate to Accounts and create new account"):
            sf.open_list_view("Account")
            sf.click("New")
            sf.select_record_type(DATA["record_type"])
            sf.click("Next")
            sf.wait_form_ready(["Account Name"])
            sf.fill("Account Name", ACCOUNT_NAME)
            sf.click("Save")
            sf.wait_page_ready(4000)
            sf.assert_("Account created", ACCOUNT_NAME in page.content())

        with sf.step(3, "<Next step>"):
            # ... use sf.fill_lookup, sf.set_picklist, sf.click,
            # sf.search_catalog, sf.add_product_to_cart, sf.configure_attr,
            # sf.wait_for_config_update, etc.
            pass

        # ... more steps
```

**Critical rules for the generated .py file:**
- NEVER import `StepTracker`, `html_reporter`, or any `src.core.*` module
- NEVER generate reports or handle video — conftest.py does this
- ALWAYS use the `tracker` + `sf` (UI) or `api_tracker` + `sf_api` (API) fixtures from conftest
- ALWAYS load data from `tests/{ui,api}/data/<stem>.json` (relative path `Path(__file__).parent / "data" / …`)
- ALWAYS use the **slot-aware TIMESTAMP block** verbatim — required for parallel-safe account names
- ALWAYS use `sf.*` library calls before raw `page.locator(...)` — see README.md Library reference
- ALWAYS use `with sf.step(N, "label"):` pattern — auto-handles tracker bookkeeping + screenshot
- ALWAYS `sf.assert_(desc, cond)` to attach assertions to the current step
- ALWAYS `sf.wait_page_ready()` (or `sf.wait_for_config_update()` on Configure Cart edits) after navigations
- Class docstring's first line = display name in dashboard + report
- `TAGS = [...]` and `OBJECTIVE = "..."` class attrs surface in the dashboard's info-icon popup
- Test record names MUST include "SFAUTO" (e.g. `SFAUTO_Biz_{TIMESTAMP}`)

### API test template

For API tests, swap `page` / `tracker` / `sf` for `api_tracker` / `sf_api`:

```python
class Test<PascalCaseName>Api:
    """TC<TC_NUM> - <Name> (API)"""

    TAGS      = ["api", "<other-tag>"]
    OBJECTIVE = "<API-flow summary>"

    @pytest.fixture(autouse=True)
    def setup(self, api_tracker, sf_api):
        self.tracker = api_tracker
        self.sf_api = sf_api
        yield

    def test_<snake_case_method>_via_api(self):
        t, sf_api = self.tracker, self.sf_api

        t.start_step(1, "Authenticate to Salesforce")
        sf_api.authenticate(name="POST Auth: soap")
        t.pass_step()

        t.start_step(2, f"Create Account {ACCOUNT_NAME}")
        account_id = sf_api.create(
            "Account",
            {"Name": ACCOUNT_NAME, "RecordTypeId": RT_ID},
            name="REST: create Account",
        )
        t.add_assertion("Account Id returned", bool(account_id))
        t.pass_step()

        # ... more steps with sf_api.soql(), sf_api.create(),
        # sf_api.update(), sf_api.call_ip(), sf_api.cpq_v2_*(), etc.
```

`SFApiClient` auto-logs every call (REQ + RES + status + duration) to the
`api_tracker` so you don't need manual logging. See
`src/api/sf_api_client.py` for the full method catalog.

### Output File 2: JSON Test Data (.json)

```json
{
  "_comment": "Test data for: <Test Name>",
  "_safe_to_change": "<fields that can be freely changed>",
  "_flow_defining": "<fields that may need a new test script if changed>",

  "record_type": "<value>",
  "account_name_prefix": "Test_Auto_",

  "<other_data_fields>": "<values>",

  "timeout_ms": 60000,

  "expected": {
    "<assertion_key>": "<expected_value>"
  },

  "cleanup": {
    "sobjects": ["<SObject1>", "<SObject2>"],
    "name_pattern": "Test_Auto_%"
  }
}
```

---

## Phase 4: Browser-Based Locator Validation (MANDATORY)

Before running pytest, **validate every locator from the generated test script in the connected Chrome browser**.
This catches locator issues that would otherwise require multiple pytest retry cycles.

### Why This Phase Matters

During Phase 2 (Discovery), you used `find`, `read_page`, and `screenshot` to identify locators.
But the generated Python code uses Playwright locators (`get_by_role`, `get_by_label`, `evaluate(JS)`) —
which may behave differently from the Chrome-based discovery tools. This phase bridges that gap by
running each Playwright-style locator against the live page to confirm it resolves correctly.

### How to Validate

1. **Navigate the test flow again from the start** in the connected Chrome browser (the same one used in Phase 2).
   Use the account/order already created during discovery — no need to create new data.

2. **For each step in the test script**, validate the primary locator:

   - **Role/label/text locators** → Use `find` or `read_page(filter="interactive")` in the browser to confirm
     the element exists with the expected role and accessible name. Example:
     ```
     Test script: page.get_by_role("button", name="Show more actions")
     Browser check: find("Show more actions button") → ref_XXX found ✓
     ```

   - **JS evaluate() calls (Shadow DOM)** → Run the same JS snippet via `javascript_tool` in the browser.
     If it returns an error or no results, the locator needs fixing. Example:
     ```
     Test script: page.evaluate("findInShadow(document, 'input[type=checkbox]')")
     Browser check: javascript_tool("findInShadow(document, 'input[type=checkbox]').length") → 12 ✓
     ```

   - **Assertions (expect/get_by_text)** → Use `find` or `get_page_text` to confirm expected text is present.

3. **When a locator fails validation**, fix it immediately:
   - Use `read_page(filter="interactive")` to discover the actual element
   - Check the element's role, name, and accessible properties
   - Update the test script with the corrected locator
   - Common fixes discovered through browser validation:
     - `checkbox` role → actually `radio` role (discount selections)
     - `button[data-row-action-trigger]` JS → `get_by_role("button", name="Show Actions")` (row actions)
     - Element not found by JS `findInShadow` → Playwright `get_by_role` pierces Shadow DOM automatically

4. **Record validation results** per step:
   ```
   Step 13 (Assetize): get_by_role("button", name="Show more actions") → ✓ found via find()
   Step 18 (Row action): JS findInShadow for data-row-action-trigger → ✗ NOT FOUND
     Fixed: get_by_role("button", name="Show Actions") → ✓ found as ref_1088
   Step 20 (FLO discount): get_by_role("checkbox", name=FLO) → ✗ WRONG ROLE
     Fixed: get_by_role("radio", name=FLO) → ✓ found as ref_1324
   ```

5. **After all locators are validated and fixed**, proceed to Phase 5 (pytest run).
   Tests validated this way typically pass on the first pytest attempt.

### Validation Shortcuts

Not every locator needs deep validation. Focus validation effort on:
- **Shadow DOM JS evaluate calls** — most likely to fail
- **Role-based locators for SF custom components** — role may differ from assumption
- **Complex selectors with regex patterns** — easy to get wrong
- **New/unfamiliar UI patterns** — anything you haven't seen before

Standard Playwright locators that are very likely to work without validation:
- `page.goto(url)` — URL navigation
- `page.fill("#username", ...)` — standard HTML ID selectors
- `page.get_by_role("button", name="Save")` — common buttons
- `sf.wait_page_ready()` — framework helper

---

## Phase 5: Run, Validate, and Self-Correct

This is what makes this skill different — **the test must actually pass**.

### Step 1: Run the test in headed mode

Tell the user:
> "Browser validation passed — all locators confirmed. Now running the full test via pytest..."

Run the test using:
```bash
cd <CCI_ROOT> && python -m pytest tests/ui/test_cci_tc<N>_<slug>.py -v -s --tb=short --no-header
# (or tests/api/test_cci_tc<N>_<slug>_api.py for API tests)
```

**IMPORTANT**: Do NOT pass `--headless`. Run in headed mode so the browser is visible and
you can debug failures if needed.

### Step 2: Check the result

- If the test **PASSES** → Go to Phase 6 (Deliver)
- If the test **FAILS** → Go to Step 3

### Step 3: Diagnose and fix (repeat up to 3 times)

When a test fails:

1. **Read the error output carefully** — identify which step failed and why
2. **Common failure patterns and fixes:**
   - `TimeoutError: locator.click` → The locator didn't match. Try a fallback locator strategy.
   - `Element not visible` → Add a longer wait before the action, or scroll the element into view.
   - `strict mode violation` → Multiple elements matched. Use `.first`, `.last`, or `exact=True`.
   - `page.url doesn't contain expected` → Navigation didn't complete. Add `sf.wait_page_ready()`.
   - `Shadow DOM element not found` → Use `sf.click_shadow_button()` or `sf.click_shadow_order_link()`.
   - `Modal blocking interaction` → Close unexpected modals first.
   - `Asset checkbox not clicking` → Shadow DOM checkbox — use JS traversal: `findInShadow(document, 'input[type="checkbox"]')`, iterate to find unchecked visible checkbox.
   - `Row action dropdown unreachable` → Deeply nested shadow DOM — use `page.get_by_role("button", name="Show Actions")` (pierces Shadow DOM). JS `findInShadow` for `button[data-row-action-trigger]` does NOT work reliably.
   - `Configure/Amend menu item not clicking` → Use JS shadow DOM: find `<a>` or `[role="menuitem"]` with matching text and call `.click()`.
   - `Confirmation dialog OK not dismissing` → Find button by ref using `find("OK button")` instead of coordinate clicks.
3. **After the FIRST failure or if anything is unclear**: Ask the user for details that might help.
   For example:
   > "The test failed at Step 6 — it couldn't find the 'Create Fixed Order' button on the Account page.
   > Can you tell me: Does this button appear for Consumer record type accounts?
   > Or is it only available for certain account types?"
4. **Edit the .py file** to fix the issue
5. **Re-run the test** from the beginning
6. **Track attempt count** — after 3 failed attempts, stop and tell the user:
   > "I've tried 3 times but the test is still failing at Step X. Here's the error: <error>.
   > This might need manual investigation. Here's what I've tried so far: <list of fixes>."

### Self-correction narration

While running, keep the user informed:
```
Attempt 1/3: Running test...
  ❌ Failed at Step 6: TimeoutError — 'Create Fixed Order' button not found
  Fix: Switching to text-based locator instead of role-based

Attempt 2/3: Running test...
  ❌ Failed at Step 8: Shadow DOM order link not clickable
  Fix: Adding 3-second wait after order creation, using sf.click_shadow_order_link()

Attempt 3/3: Running test...
  ✅ All steps passed! Test is green.
```

---

## Phase 6: Deliver

Once the test passes, tell the user:

```
✅ Test created and validated!

Files created:
  tests/<ui|api>/test_cci_tc<N>_<slug>.py    (test script + inline metadata)
  tests/<ui|api>/data/tc<N>_<slug>.json      (test data — edit this to change values)

Test result: PASSED on attempt <N>/3
  Steps: <X> steps executed
  Duration: <Y> seconds

You can:
  - Refresh the web dashboard to see the new test
  - Run it again: sfauto test tests/<ui|api>/test_cci_tc<N>_<slug>.py
  - Run headless: sfauto test tests/<ui|api>/test_cci_tc<N>_<slug>.py --headless
  - Edit tests/<ui|api>/data/tc<N>_<slug>.json to change test data values
```

---

## Revenue Cloud / CPQ Specific Patterns

### Common Revenue Cloud Objects and Their Typical UI Flows

**Account > New Order flow:**
```
Accounts list > Open account > Related tab > Orders > New Order
  or: Account detail > "New Order" button (if custom action exists)
```

**Quote to Order flow:**
```
Account > New Quote > Add Products > Configure > Price > Approve > Create Order
```

**Subscription Management:**
```
Account > Subscriptions tab > New Subscription > Select Product > Configure > Activate
```

**Order Amendment / Discount Change:**
```
Account > Assets tab > Select asset checkbox (Shadow DOM JS) > Amend button >
  Set Amendment Date dialog > Submit > Opens new supplemental order >
  Row action dropdown (▾) > Configure > Discount tab > Select new discount >
  Confirm removal dialog (OK) > Save & Exit > Assetize supplemental order >
  Verify on Account Assets tab (old discount Cancelled, new discount added)
```
Key discovery patterns for amendment flow:
- Asset checkboxes are in deep Shadow DOM — use JS: `findInShadow(document, 'input[type="checkbox"]')`
- Row action dropdown (▾) for Configure: use `read_page(filter="interactive")` to find button in last gridcell, or JS: `findInShadow(document, 'button[data-row-action-trigger]')`
- Configure menu item: JS shadow DOM find `<a>` with text "Configure" and `.click()`
- Discount tabs, radio buttons, confirmation dialogs: standard Playwright `get_by_role`/`get_by_text`
- Assetize: `page.get_by_role("menuitem", name="Assetize")` after opening "Show more actions" dropdown
- Assetize dialog has Status=Activated and Assetize checkbox pre-checked — just click Save

**Pricing and Discounts:**
```
Quote/Order > Line items > Apply discount rule > Recalculate > Verify final price
```

### Revenue Cloud Locator Tips

- **Product selection modals** often use `lightning-datatable` — look for `role="row"` and `role="gridcell"`
- **Price fields** are in `lightning-formatted-number` components — use `get_by_text` with the price value
- **Status badges** are often `lightning-badge` — `page.locator("lightning-badge:has-text('Active')")`
- **Related lists** render inside `lightning-tab` — navigate tabs first, then find content
- **CPQ configuration pages** may have custom LWC components — use `read_page` to discover structure
- **Process buttons** (Submit, Activate, Amend) are sometimes in the page header actions — look in `lightning-page-header` or `records-lwc-highlights-panel`

### Shadow DOM Deep-Dive Patterns (Learned from Real Discovery)

These patterns were validated against a live Salesforce org and solve common Shadow DOM challenges:

**Asset/related list checkboxes (deeply nested in Shadow DOM):**
```python
# Standard Playwright locators CANNOT reach these. Use JS shadow DOM traversal:
page.evaluate("""() => {
    function findInShadow(root, selector) {
        let results = Array.from(root.querySelectorAll(selector));
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) results = results.concat(findInShadow(el.shadowRoot, selector));
        }
        return results;
    }
    const cbs = findInShadow(document, 'input[type="checkbox"]');
    // Skip header checkboxes (index 0-7 typically), find first unchecked visible one
    for (let i = 7; i < cbs.length; i++) {
        if (!cbs[i].checked && cbs[i].offsetParent !== null) { cbs[i].click(); break; }
    }
}""")
```

**Row action dropdown buttons (▾) for Configure/Edit/Delete on product rows:**
```python
# These buttons are inside <lightning-primitive-cell-actions> in deep Shadow DOM.
# PREFERRED: Playwright's get_by_role pierces Shadow DOM automatically.
# The button has accessible name "Show Actions" in product rows.
show_actions_btn = page.get_by_role("button", name="Show Actions")
if show_actions_btn.count() > 0:
    show_actions_btn.first.click()
else:
    # Fallback: JS shadow DOM traversal for primitive-cell-actions
    page.evaluate("""() => {
        function findInShadow(root, selector) {
            let results = Array.from(root.querySelectorAll(selector));
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) results = results.concat(findInShadow(el.shadowRoot, selector));
            }
            return results;
        }
        const cells = findInShadow(document, 'lightning-primitive-cell-actions');
        if (cells.length > 0) {
            const btn = cells[0].shadowRoot ?
                cells[0].shadowRoot.querySelector('button') :
                cells[0].querySelector('button');
            if (btn) btn.click();
        }
    }""")
# NOTE: JS findInShadow for 'button[data-row-action-trigger]' does NOT work reliably.
# Always prefer get_by_role("button", name="Show Actions") first.
```

**Menu items inside Shadow DOM dropdowns (Configure, Edit, etc.):**
```python
# After opening the row action dropdown, menu items are <a> elements in Shadow DOM:
page.evaluate("""() => {
    function findInShadow(root, selector) {
        let results = Array.from(root.querySelectorAll(selector));
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) results = results.concat(findInShadow(el.shadowRoot, selector));
        }
        return results;
    }
    const links = findInShadow(document, 'a');
    for (const link of links) {
        if (link.textContent.trim() === 'Configure') { link.click(); return; }
    }
}""")
```

**"Show more actions" dropdown on Order page:**
```python
# The dropdown button next to the last visible action button
page.get_by_role("button", name="Show more actions").click()
page.wait_for_timeout(1000)
page.get_by_role("menuitem", name="Assetize").click()
```

**Assetize dialog pattern:**
```python
# Dialog has Status=Activated (pre-selected) and Assetize checkbox (pre-checked)
# Just click Save — use .last to target the footer Save button
page.get_by_role("button", name="Save").last.click()
sf.wait_page_ready(8000)
```

**Configure dialog discount tabs and radio buttons:**
```python
# Tabs inside Configure dialog are standard role="tab"
page.get_by_role("tab", name="Youth Direct Discounts").click()
# Discount selection uses RADIO buttons (NOT checkboxes — validated in browser)
flo_radio = page.get_by_role("radio", name=re.compile("FLO"))
if flo_radio.count() > 0:
    flo_radio.first.check()
else:
    # Fallback: try checkbox role or text click
    flo_cb = page.get_by_role("checkbox", name=re.compile("FLO"))
    if flo_cb.count() > 0:
        flo_cb.first.check()
    else:
        page.get_by_text("FLO & Data Discount").first.click()
# Confirmation dialog has standard OK/Cancel buttons
page.get_by_role("button", name="OK").click()
# Save & Exit to close Configure
page.get_by_role("button", name="Save & Exit").click()
```

**Amendment flow — Amend button and Submit dialog:**
```python
# On Assets tab, after selecting checkbox, click Amend
page.get_by_role("button", name="Amend").click()
sf.wait_page_ready(5000)
# Amendment Date dialog has pre-filled date and Submit button
page.get_by_role("button", name="Submit").click()
sf.wait_page_ready(8000)
```

---

## Error Recovery and Edge Cases

### Page Load Timeouts
If `sf.wait_page_ready()` hangs, try:
```python
try:
    sf.wait_page_ready(5000)
except:
    page.wait_for_timeout(3000)  # fallback: just wait 3s
```

### Modal Dialogs
Salesforce modals can block interactions. Always check for and handle:
```python
# Close any unexpected modals
close_btn = page.locator("button[title='Close this window']")
if close_btn.count() > 0:
    close_btn.first.click()
    page.wait_for_timeout(500)
```

### Toast Messages
Success/error toasts appear briefly. Capture them:
```python
# Wait for success toast
toast = page.locator("div.toastMessage")
toast.wait_for(timeout=10000)
assert "created" in toast.inner_text().lower() or "saved" in toast.inner_text().lower()
```

### Dynamic IDs
Salesforce generates random IDs for many elements. Never use them as locators:
```python
# BAD:  page.locator("#input-123-456")
# GOOD: page.get_by_label("Account Name")
```

---

## Pre-Generation Checklist

Before generating files, verify:

- [ ] You have the sfauto project folder path (CCI_ROOT)
- [ ] You have confirmed Salesforce credentials
- [ ] You have walked through EVERY test step in the browser
- [ ] You have recorded primary + fallback locators for each step
- [ ] You know what assertions to make at each step

## Pre-Delivery Checklist

Before telling the user the test is done, verify:

- [ ] The .py file has NO imports from `src.core.*`
- [ ] The .py file uses `tracker` and `sf` from pytest fixtures only
- [ ] Credentials come from `os.getenv()`, not hardcoded
- [ ] All configurable data is loaded from the JSON file
- [ ] Test data includes timestamps for uniqueness
- [ ] Every step has a try/except with tracker.start_step/pass_step/fail_step
- [ ] Every step has `sf.screenshot()` on both success and failure paths
- [ ] At least 2 locator strategies are documented per step (primary + fallback comment)
- [ ] `sf.wait_page_ready()` is called after page navigations and form submissions
- [ ] The class has a meaningful docstring (becomes the report title)
- [ ] The YAML has appropriate tags and cleanup instructions
- [ ] The JSON has `_safe_to_change` and `_flow_defining` documentation
- [ ] **The test has actually been RUN and PASSED**

---

## Reference: Existing Test Examples

Before generating, READ these files from the Salesforce project to see working examples:
- `tests/ui/test_cci_tc1_create_enterprise_quote_with_dia.py` — canonical 22-step UI test (account → opportunity → enterprise quote → DIA product → submit). BEST REFERENCE for UI tests.
- `tests/ui/test_cci_tc2_create_enterprise_quote_with_fbb.py` — same flow with Fiber Broadband (standalone product, no router child). BEST REFERENCE for product variations.
- `tests/api/test_cci_tc3_create_enterprise_quote_with_dia_api.py` — API twin of TC1: REST + Vlocity IP calls + Working-Cart flow. BEST REFERENCE for API tests.
- `tests/api/test_cci_tc4_create_enterprise_quote_with_fbb_api.py` — API twin of TC2.
- `tests/ui/data/tc1_create_enterprise_quote_with_dia.json` — UI test data shape (addresses, products, bandwidth, expected summary, etc.)
- `tests/api/data/tc3_create_enterprise_quote_with_dia_api.json` — API test data shape (record types, IPs, Vlocity attribute names).
- `tests/conftest.py` — the framework that wraps test scripts (DO NOT modify)
- `src/core/sf_ui/` — the high-level helper library exposed via the `sf` fixture
- `src/api/sf_api_client.py` — the API helper exposed via the `sf_api` fixture

Also read `README.md` end-to-end — the "Writing tests" + "Library reference"
sections are the canonical guide, and you should link to specific README
sections in your final delivery message rather than restating their content.
