# Writing tests

A test is **one file + one JSON**. Nothing else. No YAML definitions, no
registration step, no dashboard config — the dashboard discovers tests by
walking `tests/` and parsing them with `ast`.

```
tests/ui/test_create_account.py      ← the test
tests/ui/data/create_account.json    ← the values it types
```

## Start from the template

```bash
sfauto new create_opportunity --ui     # or --api
```

That copies the reference test, renames the class and method, and points it
at a fresh JSON. Edit the steps and run it.

## The rule that matters

**Never re-implement a helper. Use the `sf` fixture.**

The framework ships ~30 Salesforce-aware helpers in `src/core/sf_ui/`. If you
find yourself writing `page.get_by_label(...)`, `page.wait_for_selector(...)`,
or a private `_fill_field_by_label()` method, stop — it already exists.

```python
# ✗ don't
self.page.get_by_label("Account Name").fill(name)

# ✓ do
sf.fill("Account Name", name)
```

This is not style. The old client tests carried **815 lines of private helper
methods duplicated across two files**, every one of which already existed in
`sf_ui`. Those tests were 2,900 lines each. The equivalent written against the
library is under 100.

## Anatomy

```python
class TestCreateAccount:
    """Create an Account via the Lightning UI."""   # ← dashboard display name

    TAGS = ["ui", "smoke", "account"]               # ← dashboard filter chips
    OBJECTIVE = "Create an Account and verify..."   # ← dashboard tooltip

    @pytest.fixture(autouse=True)
    def setup(self, page, tracker, sf):
        self.page, self.tracker, self.sf = page, tracker, sf

    def test_create_account(self):
        with self.sf.step(1, "Log in"):             # ← report + live view step
            self.sf.login()
            self.sf.assert_("On Lightning", "lightning" in self.page.url)
```

`sf.step()` handles all bookkeeping: starts the step, screenshots on success
**and** failure, marks pass/fail, and re-raises so pytest fails correctly.
You never call `tracker.start_step` / `pass_step` / `fail_step` yourself.

API tests are symmetric — `api_tracker` exposes the same `step()` / `assert_()`:

```python
def test_account_crud(self):
    api, sf = self.api, self.tracker
    with sf.step(1, "Create Account"):
        rid = api.create("Account", {"Name": name})
        sf.assert_("id returned", bool(rid))
```

## Helper reference

| Area | Call |
|---|---|
| Auth | `sf.login()` |
| Navigate | `sf.open_list_view("Account")`, `sf.open_record("Account", id)`, `sf.extract_record_id(sobject=...)` |
| Forms | `sf.fill(label, v)`, `sf.fill_date(label, v)`, `sf.fill_lookup(label, v)`, `sf.set_picklist(label, v)`, `sf.set_stage(v)`, `sf.select_record_type(v)`, `sf.wait_form_ready([labels])` |
| Actions | `sf.click(name)`, `sf.click_shadow_button(text)`, `sf.confirm_duplicates()`, `sf.dismiss_error_dialog()` |
| Waits | `sf.wait_spinner()`, `sf.wait_page_ready()`, `sf.wait_for_toast(text)`, `sf.wait_until(pred)` |
| CPQ cart | `sf.search_catalog(term)`, `sf.add_product_to_cart(name)`, `sf.configure_attr(label, v)`, `sf.wait_summary_loaded()` |
| Report | `sf.assert_(desc, cond)`, `sf.screenshot(name)`, `tracker.add_record(label=, name=, record_id=, object_type=)` |
| API | `api.create/update/delete/soql/describe/pick_record_type/pick_field/call_ip` |

## Three things that bite on a real org

**Picklists are not text fields.** `Billing Country` and `Billing
State/Province` render as comboboxes once the org enables State and
Country picklists. Use `sf.set_picklist()`, not `sf.fill()` — and set
Country *first*, because State's options depend on it. Fall back to
`fill()` for orgs without the feature:

```python
for label, value in (("Billing Country", "United States"),
                     ("Billing State/Province", "California")):
    if value and not sf.set_picklist(label, value):
        sf.fill(label, value)
```

**Duplicate rules will fail your second run.** Most orgs ship the
Standard Duplicate Rules enabled, and test data repeats by design — so
run two flags run one's record as "Similar Records Exist". On the API
side `api.create()` already sends the allowSave header. On the UI side,
acknowledge the warning:

```python
sf.assert_("'Save' clicked", sf.click("Save"))
sf.confirm_duplicates()
sf.wait_for_toast("was created")
```

**Register what you create.** `tracker.add_record(...)` does double duty:
it renders the record link in the report *and* enrolls the record for
deletion at teardown. A suite that leaves its data behind eventually
poisons itself through the duplicate rules above. Set
`SFAUTO_KEEP_RECORDS=1` when you need to inspect a failing run's data.

## Targeting a different org

Never hardcode org specifics. They live in `profiles/<org>.yml`:

```yaml
login_url: https://test.salesforce.com
timezone:  America/Los_Angeles   # date fields are typed in the ORG's timezone
namespace: vlocity_cmt           # OmniStudio / Industries managed package
record_prefix: ACME              # every created record is prefixed for cleanup
labels:
  account_name: Client Name      # this org renamed the standard label
```

```bash
sfauto profiles                                  # list them
SFAUTO_PROFILE=acme-uat sfauto test tests/ui     # select one
```

In a test:

```python
PROFILE = load_profile()
STAMP = datetime.now(PROFILE.tz).strftime("%m%d_%H%M%S")   # org-local time
name  = f"{PROFILE.record_prefix}_Acct_{STAMP}"
sf.fill(PROFILE.label("account_name", "Account Name"), name)
api.soql(f"SELECT Id FROM {PROFILE.ns('Product2__c')}")     # namespace-aware
```

Environment variables always override the profile, so CI needs no file edits.

## Parallel-safe naming

The runner executes up to 4 tests concurrently. Two workers starting in the
same second must not generate the same record name:

```python
_slot = os.getenv("UI_TEST_SLOT") or os.getenv("PYTEST_XDIST_WORKER", "").replace("gw", "")
STAMP = datetime.now(PROFILE.tz).strftime("%m%d_%H%M%S") + (f"s{_slot}" if _slot else "")
```

`UI_TEST_SLOT` is set by the dashboard runner, `PYTEST_XDIST_WORKER` by
`pytest -n`. The reference tests already do this — keep it when you copy.

## Running

```bash
sfauto doctor                          # check setup first
sfauto test tests/ui/test_x.py         # one test, headed
sfauto test tests/ --headless          # everything, headless
sfauto server                          # dashboard w/ live 2x2 view :8091
pytest tests/ -n 4                     # CI: 4 workers
```
