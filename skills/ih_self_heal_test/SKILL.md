---
name: ih_self_heal_test
description: >
  Self-heal a failing Playwright test by running every step in a connected Chrome browser,
  identifying broken locators, discovering correct replacements from the live Salesforce UI,
  and fixing the test script. Use this skill whenever the user says a test is failing, broken,
  needs fixing, needs healing, has locator issues, or mentions "self-heal", "fix test",
  "repair test", "test is broken", "test failed", or provides a test filename to debug.
  Also trigger when the user asks to verify/validate that a test works by running it in the browser.
---

# ih_self_heal_test — Salesforce Playwright Test Self-Healer

## Overview

This skill takes a **failing or untested Playwright test script** and heals it by running every
step in a live Chrome browser connected via Claude in Chrome. For each step, it executes the
locator, checks if it resolves, and if not — discovers the correct element using the accessibility
tree and fixes the test code.

**Input required:** A test filename from `tests/ui/` or `tests/api/` (e.g., `test_cci_tc1_create_enterprise_quote_with_dia.py`).

> **Note for this codebase:** there's a high-level helper library at
> `src/core/sf_ui/` exposed via the `sf` fixture (e.g. `sf.fill(label, value)`,
> `sf.click(name)`, `sf.fill_lookup(label, search)`, `sf.set_picklist(label, value)`,
> `sf.search_catalog(term)`, `sf.add_product_to_cart(text)`, `sf.wait_for_config_update()`).
> **When fixing a broken locator, prefer replacing it with a sf_ui helper
> call rather than another raw `page.locator(...)`** — those helpers
> already handle shadow DOM, Aura vs Lightning picklist variants, lookup
> dialog flow, etc. See `README.md` → Library reference for the full
> catalog, or read `src/core/sf_ui/__init__.py` for the module list.

---

## Workflow Summary (Tell the user this FIRST)

Before starting any work, tell the user:

```
Here's how I'll self-heal your test:

1. READ & PARSE     — I'll read the test script and its JSON data file to
                      understand every step, locator, and assertion.
2. SETUP            — I'll ensure I'm logged into Salesforce in the connected
                      Chrome browser (reuse existing session if available).
3. WALK & VALIDATE  — I'll execute each step in the browser one by one:
                      ✓ If the locator works → record as PASS, move on
                      ✗ If the locator fails → discover the correct element
                        using find/read_page, fix the locator, and continue
4. FIX & UPDATE     — I'll apply all fixes to the .py test script file.
5. VERIFY           — I'll do a second pass on any steps that were fixed
                      to confirm the new locators work.
6. DELIVER          — I'll show you a summary of what was fixed and commit.

Shall I proceed?
```

Wait for confirmation before starting.

---

## Phase 1: Read and Parse the Test

### Step 1: Locate the files

Given a test filename (e.g., `test_cci_tc1_create_enterprise_quote_with_dia.py`), locate:
- `<CCI_ROOT>/tests/ui/<filename>.py` — the test script (UI tests)
  OR `<CCI_ROOT>/tests/api/<filename>.py` — the test script (API tests)
- `<CCI_ROOT>/tests/ui/data/<name>.json` (or `tests/api/data/<name>.json`) — the test data file

where `<name>` is the filename minus the `test_cci_` prefix and `.py` extension.

There are NO YAML definition files in this project — test metadata (display
name, tags, objective) lives inline in the .py as a class docstring +
`TAGS = [...]` + `OBJECTIVE = "..."` class attributes. Step labels are
inline at each `sf.step(N, "label")` / `tracker.start_step(N, "label", ...)`
call site.

### Step 2: Parse the test steps

Read the test script and extract a structured list of steps:

```
Step N:
  Name: "<step name from tracker.start_step>"
  Locators: [list of page.get_by_*, page.locator(), page.evaluate() calls]
  Actions: [click, fill, check, select_option, etc.]
  Assertions: [expect() calls, tracker.add_assertion() calls]
  Wait patterns: [wait_for_timeout, wait_page_ready, etc.]
```

### Step 3: Identify high-risk locators

Flag locators that are most likely to break:
- `page.get_by_label("*Stage").select_option(...)` — Lightning comboboxes are NOT native selects
- `page.evaluate("""...""")` — JS Shadow DOM traversal can be fragile
- `page.get_by_role("checkbox", ...)` — may actually be a radio button
- `page.locator("css=...")` — CSS selectors break with DOM changes
- `page.get_by_placeholder(...)` — placeholder text can change
- Any locator with hardcoded index (`.nth()`, `[i]`)

---

## Phase 2: Browser Setup

### Step 1: Get browser context

Use `tabs_context_mcp` to get the connected Chrome tab. Create a new tab if needed.

### Step 2: Verify Salesforce login

Take a screenshot. If already on a Salesforce Lightning page, skip login.
If not logged in:
1. Navigate to the SF login URL (from the test script's `SF_URL` or `.env`)
2. Enter credentials
3. Handle 2FA if prompted (ask user to complete manually)
4. Verify landing on Lightning page

### Step 3: Prepare test data

Note the test data values that will be used during validation:
- Account name, opportunity name, product name, etc.
- Timestamps should be generated fresh for this validation run
- Record any account/record URLs created during validation for later navigation

---

## Phase 3: Walk and Validate Every Step

This is the core of the skill. For EACH step in the test:

### 3.1: Announce the step

Tell the user:
> "Step N: <step name> — Testing locators..."

### 3.2: Navigate to the correct page state

If the step requires a specific page (e.g., Account detail, Order page), navigate there.
Use URLs stored from previous steps or navigate via the test's pattern.

### 3.3: Test each locator in the step

For each locator in the step:

**Role-based locators** (`get_by_role`, `get_by_label`, `get_by_text`, `get_by_placeholder`):
```
Use find("<element description>") or read_page(filter="interactive")
to check if an element with the expected role and name exists.
```

**JS evaluate calls** (Shadow DOM):
```
Use javascript_tool to run the same JS snippet and check the return value.
If it returns null/0/error → the locator is broken.
```

**CSS selectors** (`page.locator("css=...")`):
```
Use javascript_tool to run document.querySelector("<selector>") and check result.
```

### 3.4: If a locator PASSES

Record it:
```
Step N: ✓ page.get_by_role("button", name="Save") → found as ref_XXX
```
Perform the action (click, fill, etc.) to advance the page to the next state.

### 3.5: If a locator FAILS

1. **Take a screenshot** to see current page state
2. **Use `find` or `read_page(filter="interactive")`** to discover the correct element:
   - Search by the element's purpose: `find("Save button")`, `find("Stage dropdown")`
   - Check the actual role, name, and type of the element found
3. **Determine the correct Playwright locator** based on discovery:
   - Note the actual role (combobox vs select, radio vs checkbox, etc.)
   - Note the actual accessible name
   - Note if Shadow DOM traversal is needed
4. **Record the fix**:
   ```
   Step N: ✗ BROKEN: page.get_by_label("*Stage").select_option(label="Prospecting")
           Reason: Stage is a Lightning combobox, not a native <select>
           Discovery: find("Stage dropdown") → ref_299: combobox "Stage" (button)
           Fix: page.get_by_label("*Stage").click() → page.get_by_role("option", name=STAGE).click()
   ```
5. **Perform the corrected action** to advance the page state

### 3.6: Common Salesforce Locator Failures and Fixes

**Prefer an sf_ui helper over a raw locator.** The library handles every
variant of Shadow DOM, Aura picklist, Lightning combobox, and Vlocity
catalog quirk that we've hit. Replacing a broken raw locator with the
right `sf.*` call is usually a net reduction in code AND more resilient.

| Broken Pattern | Symptom | Correct Pattern |
|---|---|---|
| `page.get_by_label("Account Name").fill(X)` | Field-by-label flaky | `sf.fill("Account Name", X)` — same result, plus `*` fallback |
| `page.get_by_label("Stage").select_option(label=X)` | Stage/picklist fails | `sf.set_picklist("Stage", X)` (handles native/Aura/combobox/button variants) |
| `page.evaluate("findInShadow(...)")` for record type | Returns null | `sf.select_record_type("Business")` |
| Inline lookup typing + dropdown click | Misses search dialog fallback | `sf.fill_lookup("Account", search_value)` |
| `page.get_by_role("checkbox")` | Discount radio fails | `get_by_role("radio")` (or `sf.set_picklist` if applicable) |
| Custom catalog/cart search code | Fragile selector | `sf.search_catalog(term)` + `sf.add_product_to_cart(text)` |
| Cart attribute fill | Doesn't wait for "Updating..." toast | `sf.configure_attr(label, value)` (auto-waits) |
| JS `findInShadow('button[data-row-action-trigger]')` | Returns empty | `get_by_role("button", name="Show Actions")` (Playwright pierces Shadow DOM) |
| `page.locator("a[href*='/Order/']")` | Shadow DOM link | `sf.click_shadow_order_link()` |
| `page.get_by_role("button", name="Save")` | Multiple matches | `sf.click("Save")` (tries multiple strategies); or add `.last` / `exact=True` |
| `page.wait_for_load_state("networkidle")` | Hangs forever in Lightning | `sf.wait_page_ready(4000)` (5s networkidle cap + spinners + buffer) |

### 3.7: Handle page state transitions

After each step's actions are performed:
- Wait for page to settle (`wait 2-3 seconds` or check for URL change)
- Take a screenshot to confirm the expected result
- If the page is in an unexpected state (error, wrong page), stop and tell the user

---

## Phase 4: Fix and Update the Test Script

### Step 1: Apply all fixes

For each broken locator discovered in Phase 3, edit the test script using the Edit tool.
Apply fixes one at a time to avoid conflicts.

### Step 2: Review the complete fix list

Present a summary to the user:
```
Fixes applied to test_create_opportunity.py:

Step 7, Line 199:
  BEFORE: page.get_by_label("*Stage").select_option(label=STAGE)
  AFTER:  page.get_by_label("*Stage").click()
          page.wait_for_timeout(500)
          page.get_by_role("option", name=STAGE).click()
  REASON: Stage is a Lightning combobox, not a native <select>

Total: 1 fix applied, 6 steps verified clean
```

---

## Phase 5: Verify Fixes (Second Pass)

For any step that was fixed, do a quick re-validation:

1. Navigate to the relevant page state
2. Use `find` or `read_page` to confirm the new locator resolves
3. Mark as VERIFIED or flag if still broken

If a fix doesn't work on second pass, try an alternative approach and update again.

---

## Phase 6: Deliver

### Step 1: Show summary

```
✅ Self-heal complete for test_create_opportunity.py

Steps validated: 7
Steps passed:    6 (no changes needed)
Steps fixed:     1
  - Step 7: Stage combobox (select_option → click + option click)

The test should now pass on the next pytest run.
```

### Step 2: Commit (if user confirms)

```bash
git add tests/ui/<filename>.py        # or tests/api/<filename>.py
git commit -m "Self-heal <test_name>: fix <N> broken locator(s)"
git push origin main
```

---

## Edge Cases and Recovery

### Test creates data (account, order, etc.)
- The validation run WILL create real data in Salesforce
- Use timestamped names to avoid conflicts with other test runs
- Warn the user that test data will be created during validation

### Test requires prior state (e.g., amendment needs an existing order)
- For multi-prerequisite tests, you may need to run the full flow from Step 1
- Don't skip prerequisite steps even if they're not the suspected failure point

### Element exists but action fails
- Some elements are visible but not interactable (disabled, overlapped)
- Check for modals, spinners, or overlays blocking the element
- Try scrolling the element into view or waiting longer

### Multiple failures cascade
- If Step 3 fails and all subsequent steps depend on it, fix Step 3 first
- Re-run from Step 3 onward after fixing (don't restart from Step 1)
- Track which steps need re-validation after upstream fixes

### Shadow DOM elements
- Always try `get_by_role` first — Playwright pierces Shadow DOM automatically
- Only fall back to JS `findInShadow` when `get_by_role` can't find the element
- When using `read_page(filter="interactive")`, increase depth if needed

---

## Quick Reference: Discovery Commands

| Goal | Command |
|---|---|
| Find element by description | `find("Save button")` |
| List all interactive elements | `read_page(filter="interactive")` |
| Check a specific area | `read_page(ref_id="ref_XXX")` |
| Run JS in page context | `javascript_tool("document.querySelector('...')")` |
| Check element role/name | `find("<element text>")` → shows role, name, ref |
| Screenshot current state | `screenshot` |
| Get page text content | `get_page_text` |

---

## Pre-Delivery Checklist

Before telling the user the test is healed:

- [ ] Every step in the test was validated in the browser
- [ ] All broken locators were fixed with working alternatives
- [ ] Fixes were verified with a second discovery pass
- [ ] The .py file was updated with all fixes
- [ ] No other files were accidentally modified (JSON, YAML, conftest)
- [ ] The fix summary was shown to the user
