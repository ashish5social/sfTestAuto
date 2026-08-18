# Salesforce Lightning Locator Reference

> **Before reaching for a raw Playwright locator from this file, check
> the `sf_ui` library first.** The `sf` fixture exposes high-level
> helpers that already handle Shadow DOM, Aura vs Lightning picklist
> variants, lookup-dialog flow, Vlocity catalog quirks, etc.:
>
> | Instead of | Use |
> |---|---|
> | `page.get_by_label("X").fill(v)` | `sf.fill("X", v)` |
> | `page.get_by_role("button", name="Save").click()` | `sf.click("Save")` |
> | Custom shadow-DOM record-type click | `sf.select_record_type("Business")` |
> | Picklist click + option click | `sf.set_picklist("Stage", "Closed Won")` |
> | Inline lookup + search dialog | `sf.fill_lookup("Account", search_value)` |
> | Catalog search + Add to Cart | `sf.search_catalog(term)` + `sf.add_product_to_cart(text)` |
> | Cart attribute fill + "Updating" wait | `sf.configure_attr(label, value)` |
> | `page.wait_for_load_state("networkidle")` | `sf.wait_page_ready(extra_ms)` |
>
> Read `README.md` → Library reference for the full sf.* catalog. The
> raw locators below are useful as **fallback** when no helper covers
> the case, or when you need to understand how a helper works internally.

## Navigation Patterns

### Direct URL Navigation (Most Reliable)
```python
base_url = page.url.split("/lightning")[0]

# Object list views
page.goto(f"{base_url}/lightning/o/Account/list?filterName=__Recent")
page.goto(f"{base_url}/lightning/o/Order/list?filterName=__Recent")
page.goto(f"{base_url}/lightning/o/SBQQ__Quote__c/list?filterName=__Recent")

# Specific record (if ID known)
page.goto(f"{base_url}/lightning/r/Account/{record_id}/view")

# App launcher
page.goto(f"{base_url}/lightning/page/home")
```

### App Navigation Bar
```python
page.get_by_role("link", name="Accounts")
page.locator("one-app-nav-bar-item-root[data-id='Account']")
```

## Login Page
```python
# Username field
page.locator("#username")
page.get_by_label("Username")

# Password field
page.locator("input[name='pw']")
page.get_by_label("Password")

# Login button
page.click("#Login")
page.get_by_role("button", name="Log In")
```

## Buttons
```python
# Standard buttons
page.get_by_role("button", name="New")
page.get_by_role("button", name="Save", exact=True).last  # .last for modal buttons
page.get_by_role("button", name="Next")
page.get_by_role("button", name="Cancel")
page.get_by_role("button", name="Edit")

# Action buttons (page header)
page.locator("lightning-button-menu[data-target-reveals]")  # "Show more actions" dropdown

# Shadow DOM buttons
sf.click_shadow_button("Activate")
sf.click_shadow_button("Submit for Approval")
```

## Form Fields
```python
# Text input
page.get_by_label("*Account Name")          # Required field (star prefix)
page.get_by_label("Description")             # Optional field
page.get_by_placeholder("Search...")

# Lightning input components
page.locator("lightning-input[field-name='Name'] input")
page.locator("lightning-textarea[field-name='Description'] textarea")

# Combobox / Picklist
page.get_by_role("combobox", name="Status").click()
page.get_by_role("option", name="Active").click()

# Lookup field
page.get_by_label("Account Name").fill("Search Term")
page.get_by_role("option", name="Matching Account").click()

# Checkbox
page.get_by_label("Active").check()
page.get_by_label("Active").uncheck()

# Date picker
page.get_by_label("Start Date").fill("04/01/2026")
```

## Record Type Selection Modal
```python
page.get_by_label("Consumer").check()         # Radio button
page.get_by_role("radio", name="Consumer")
page.get_by_text("Consumer").click()
page.get_by_role("button", name="Next").click()
```

## Tables and Related Lists
```python
# Related list tab
page.get_by_role("tab", name="Related")
page.get_by_role("tab", name="Details")

# Related list header link
page.get_by_role("link", name="Orders")
page.get_by_role("link", name="Contacts (1)")

# Table rows
page.locator("table tbody tr").first
page.locator("lightning-datatable tr[data-row-key-value]")

# Order number links
page.get_by_role("link", name=re.compile(r"^\d{5,}$"))
page.locator("a[href*='/Order/']")
sf.click_shadow_order_link()
```

## Toast Messages
```python
# Success toast
toast = page.locator("div.toastMessage")
toast.wait_for(timeout=10000)
assert "was created" in toast.inner_text().lower()

# Alternative
page.get_by_text("was created").wait_for(timeout=10000)
```

## Common Wait Patterns
```python
# After login or heavy page loads
sf.wait_page_ready(5000)

# After form submissions
sf.wait_page_ready(3000)

# After clicking a tab or navigation
sf.wait_page_ready(2000)

# Short pause before form interactions
page.wait_for_timeout(500)

# Wait for specific element
page.get_by_text("Success").wait_for(timeout=10000)

# Wait for URL change
page.wait_for_url("**/lightning/r/Account/**", timeout=15000)
```

## Revenue Cloud / CPQ Specific

### Product Selection
```python
# Product catalog search
page.get_by_placeholder("Search Products...").fill("Mobile Basic")
page.get_by_role("row", name=re.compile("Mobile Basic")).click()
page.get_by_role("button", name="Add to Cart").click()
```

### Pricing Fields
```python
# Price display
page.locator("lightning-formatted-number").filter(has_text="49.99")
page.get_by_text("$49.99")
```

### Order Status
```python
# Status badge
page.locator("lightning-badge:has-text('Draft')")
page.locator("lightning-formatted-text:has-text('Activated')")
```

### Subscription Components
```python
# Asset/Subscription related list
page.get_by_role("tab", name="Assets")
page.get_by_role("tab", name="Subscriptions")
```

## Handling Modals and Popups
```python
# Close unexpected modal
close_btn = page.locator("button[title='Close this window']")
if close_btn.count() > 0:
    close_btn.first.click()
    page.wait_for_timeout(500)

# Handle "Unsaved Changes" dialog
discard = page.get_by_role("button", name="Discard")
if discard.count() > 0:
    discard.click()
```
