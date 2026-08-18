"""
sf_ui — Salesforce UI test helper library.

This package collects the reusable building blocks for browser-driven
tests against a Salesforce Lightning / Vlocity CCI org. Test files
should import from here rather than reimplementing locator strategies,
wait logic, shadow-DOM traversal, etc.

Modules:
  auth        — login + frontdoor bypass for IP-restricted orgs
  navigation  — list views, opening records, app switcher
  forms       — field/lookup/picklist/date helpers (label-driven)
  actions     — clicks (shadow-DOM aware), related-list interactions
  waits       — page-ready, spinner, toast, Configure-Cart settles
  cart        — Vlocity CPQ catalog / cart / configure operations
  step        — StepRunner context manager that wraps tracker bookkeeping

Quick start (inside a test, with the `sf` fixture from conftest):

    from src.core.sf_ui import StepRunner
    from src.core.sf_ui.auth import login_with_frontdoor_fallback
    from src.core.sf_ui.forms import fill_field_by_label, fill_lookup

    with sf.step(1, "Login"):
        login_with_frontdoor_fallback(page, ...)

    with sf.step(2, "Create account"):
        fill_field_by_label(page, "Account Name", "CCIAUTO_Biz_001")
        sf.click_button("Save")

Heavy docstrings live on each function — read those before reaching for
an inline workaround. If a helper genuinely doesn't fit your case,
extend it (the library is shared) rather than copy-paste.
"""

# Re-export the most-used names so tests can do `from src.core.sf_ui import X`
from src.core.sf_ui.step import StepRunner, step  # noqa: F401

# Each module is also importable as `from src.core.sf_ui.forms import ...`
# — we deliberately don't `from .forms import *` here to keep the public
# surface explicit and discoverable in IDEs.
