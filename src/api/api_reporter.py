"""
HTML Report Generator for API-driven tests (TC3 family).

Produces the same layout + branding as the UI report (src/core/html_reporter.py)
so the two live side-by-side in the runner UI, but each step renders a list
of API-call cards instead of screenshots/video.

Each API call card shows:
  - IP / endpoint label + method + status
  - Duration
  - Request body (collapsed by default, click to expand, pretty-printed JSON)
  - Response body (collapsed by default, click to expand)

Report is self-contained (no external assets except logo URLs).
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime

from src.core.branding import (
    BRAND_CSS, BRAND_TAGLINE, brand_block,
)
from pathlib import Path

from src.api.api_tracker import APICall, APITracker


def _resolve_org_name() -> str:
    override = os.getenv("SF_ORG_NAME", "").strip()
    if override:
        return override
    url = os.getenv("SF_LOGIN_URL", "").strip()
    if not url:
        return "unknown"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if ".my.salesforce.com" in host or ".sandbox.my.salesforce.com" in host:
        return host.split(".", 1)[0]
    return host


def _format_duration(seconds) -> str:
    try:
        total = float(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        if abs(total - round(total)) < 0.05:
            return f"{int(round(total))}s"
        return f"{total:g}s"
    minutes = int(total // 60)
    remainder = total - (minutes * 60)
    if abs(remainder - round(remainder)) < 0.05:
        return f"{minutes}m {int(round(remainder))}s"
    return f"{minutes}m {remainder:g}s"


def _pretty_json(value) -> str:
    """Render any value as pretty JSON (or string if not JSON-able)."""
    if value is None:
        return ""
    if isinstance(value, str):
        # Try to parse it as JSON first (many IPs return JSON-as-string)
        try:
            parsed = json.loads(value)
            return json.dumps(parsed, indent=2, default=str)
        except Exception:
            return value
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)


# Max characters of request / response body embedded in the report.
# describe() payloads for Account, QuoteLineItem, Product2 can easily
# run to ~300 KB each — embedding them whole bloats reports to
# multi-MB files. Truncate with a clear marker; the underlying raw
# body is still available in the APITracker JSON if needed.
_MAX_BODY_CHARS = 20_000


def _pretty_json_truncated(value, *, limit: int = _MAX_BODY_CHARS) -> tuple[str, int, bool]:
    """
    Pretty-print ``value`` and truncate to ``limit`` characters.

    Returns ``(rendered_text, original_length, truncated)``. A trailing
    "…truncated (<kept>/<original> chars)" marker is appended when
    content is cut so the report makes it obvious the body is capped.
    """
    rendered = _pretty_json(value)
    original_len = len(rendered)
    if original_len <= limit:
        return rendered, original_len, False
    # Cut at the limit but try to end on a line boundary for readability.
    cut = rendered[:limit]
    last_nl = cut.rfind("\n")
    if last_nl > limit * 0.5:
        cut = cut[:last_nl]
    marker = (
        f"\n\n… truncated ({len(cut):,} of {original_len:,} chars shown) — "
        f"raw body preserved in the APITracker JSON."
    )
    return cut + marker, original_len, True


def _status_class(call: APICall) -> str:
    if call.error:
        return "fail"
    if call.status_code and 200 <= call.status_code < 300:
        return "pass"
    if call.status_code and call.status_code >= 400:
        return "fail"
    return "neutral"


def _render_api_call(call: APICall, idx: int) -> str:
    """One collapsible card per API call."""
    cls = _status_class(call)
    badge = (
        f"HTTP {call.status_code}" if call.status_code else ("ERROR" if call.error else "—")
    )

    req_text, req_orig_len, req_truncated = _pretty_json_truncated(call.request_body)
    resp_text, resp_orig_len, resp_truncated = _pretty_json_truncated(call.response_body)
    req_pretty = html.escape(req_text)
    resp_pretty = html.escape(resp_text)

    req_summary = (
        f"Request body ({req_orig_len:,} chars"
        + (", truncated" if req_truncated else "")
        + ")"
        if call.request_body
        else "Request body — empty"
    )
    resp_summary = (
        f"Response body ({resp_orig_len:,} chars"
        + (", truncated" if resp_truncated else "")
        + ")"
        if call.response_body
        else "Response body — empty"
    )

    err_html = (
        f'<div class="api-error"><strong>Error:</strong> {html.escape(call.error)}</div>'
        if call.error
        else ""
    )
    url_html = (
        f'<div class="api-url" title="{html.escape(call.url)}">{html.escape(call.url)}</div>'
        if call.url
        else ""
    )
    return f"""
    <div class="api-call {cls}">
      <div class="api-call-header">
        <span class="api-method">{html.escape(call.method)}</span>
        <span class="api-name">{html.escape(call.name)}</span>
        <span class="api-badge {cls}">{badge}</span>
        <span class="api-timing">{call.duration_ms}ms</span>
      </div>
      {url_html}
      {err_html}
      <details class="api-body">
        <summary>{html.escape(req_summary)}</summary>
        <pre>{req_pretty}</pre>
      </details>
      <details class="api-body">
        <summary>{html.escape(resp_summary)}</summary>
        <pre>{resp_pretty}</pre>
      </details>
    </div>"""


def _render_records(records: list[dict]) -> str:
    if not records:
        return ""
    items = []
    for rec in records:
        label = html.escape(rec.get("label") or "Record")
        name = html.escape(rec.get("name") or "")
        url = rec.get("url") or ""
        if url:
            items.append(
                f'<a class="record-link" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="record-label">{label}:</span> '
                f'<span class="record-name">{name}</span>'
                f'<span class="record-ext">↗</span></a>'
            )
        else:
            items.append(
                f'<span class="record-link record-link-plain">'
                f'<span class="record-label">{label}:</span> '
                f'<span class="record-name">{name}</span></span>'
            )
    return f'<div class="records">{"".join(items)}</div>'


def _render_assertions(assertions: list[dict]) -> str:
    if not assertions:
        return ""
    items = "".join(
        f'<li class="{"assertion-pass" if a["passed"] else "assertion-fail"}">'
        f'{"OK" if a["passed"] else "FAIL"} {html.escape(a["description"])}</li>'
        for a in assertions
    )
    return f'<ul class="assertions">{items}</ul>'


def generate_api_report(
    tracker: APITracker,
    report_path: Path,
    extra_data: dict | None = None,
):
    """Write an API-test HTML report. Self-contained; no screenshots/video."""
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Step cards ──
    step_rows = ""
    for step in tracker.steps:
        status_class = "pass" if step["status"] == "PASS" else ("fail" if step["status"] == "FAIL" else "neutral")
        status_badge = (
            '<span class="badge pass">PASS</span>'
            if step["status"] == "PASS"
            else (
                '<span class="badge fail">FAIL</span>'
                if step["status"] == "FAIL"
                else '<span class="badge">RUNNING</span>'
            )
        )

        calls_html = "".join(
            _render_api_call(c, i + 1) for i, c in enumerate(step.get("api_calls", []))
        )
        records_html = _render_records(step.get("records", []))
        assertions_html = _render_assertions(step.get("assertions", []))

        error_html = ""
        if step.get("error"):
            raw = step["error"]
            first_line = raw.split("\n")[0].strip()
            if len(first_line) > 300:
                first_line = first_line[:300] + "..."
            error_html = (
                f'<div class="error-box"><strong>Error:</strong> '
                f'<span style="font-size:13px">{html.escape(first_line)}</span></div>'
            )

        step_rows += f"""
        <div class="step-card {status_class}">
          <div class="step-header">
            <div class="step-number">Step {step["number"]}</div>
            <div class="step-name">{html.escape(step["name"])}</div>
            {status_badge}
            <div class="step-duration">{_format_duration(step["duration_sec"])}</div>
            <div class="step-callcount">{len(step.get("api_calls", []))} call(s)</div>
          </div>
          {f'<div class="step-desc">{html.escape(step["description"])}</div>' if step.get("description") else ""}
          {records_html}
          {assertions_html}
          {error_html}
          <div class="api-calls">{calls_html}</div>
        </div>"""

    overall_class = "pass" if tracker.overall_status == "PASS" else "fail"
    overall_icon = "PASS" if tracker.overall_status == "PASS" else "FAIL"

    # Build lookups of records so that any Test-Data value matching a
    # created record's Name becomes a clickable link straight into
    # Salesforce. Two overlapping lookups handle the common ambiguity
    # where Account.Name == Opportunity.Name (both prefixed "SFAUTO_API_"
    # with the same timestamp):
    #   - ``scoped_urls[(label, name)]`` — exact match when we can infer
    #     the target object from the Test-Data field name (e.g.
    #     "Opportunity Name" → look up records with label "Opportunity")
    #   - ``name_to_url[name]`` — first-seen match, used as a fallback.
    scoped_urls: dict[tuple[str, str], str] = {}
    name_to_url: dict[str, str] = {}
    for s in tracker.steps:
        for rec in s.get("records", []) or []:
            nm = (rec.get("name") or "").strip()
            url = rec.get("url") or ""
            label = (rec.get("label") or rec.get("object_type") or "").strip()
            if nm and url:
                key = (label.lower(), nm)
                if key not in scoped_urls:
                    scoped_urls[key] = url
                if nm not in name_to_url:
                    name_to_url[nm] = url

    # Map common Test-Data field names to the record label they point to.
    # "Opportunity Name" has historically linked to the Account URL because
    # both records share the same Name string and Account was added first.
    _FIELD_LABEL_HINTS: dict[str, tuple[str, ...]] = {
        "account name": ("Account",),
        "opportunity name": ("Opportunity",),
        "quote name": ("Quote",),
        "location": ("QuoteMember",),
        "quote member": ("QuoteMember",),
    }

    def _linkify(value: str, field_name: str | None = None) -> str:
        """Escape value; if it matches a known record Name, wrap in an <a>.

        Prefers a label-scoped match derived from ``field_name`` (so
        "Opportunity Name" resolves to the Opportunity URL even when
        the Account shares the same Name).
        """
        esc = html.escape(value)
        target = value.strip()
        if not target:
            return esc
        url: str | None = None
        if field_name:
            hints = _FIELD_LABEL_HINTS.get(field_name.strip().lower()) or ()
            for hint in hints:
                url = scoped_urls.get((hint.lower(), target))
                if url:
                    break
        if not url:
            url = name_to_url.get(target)
        if url:
            return (
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#1e40af;text-decoration:none;border-bottom:1px dashed #93c5fd">'
                f'{esc} <span style="font-size:11px;opacity:.6">↗</span></a>'
            )
        return esc

    extra_table = ""
    if extra_data:
        rows = "".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{_linkify(str(v), field_name=str(k))}</td></tr>"
            for k, v in extra_data.items()
        )
        extra_table = f'<div class="test-data"><h3>Test Data</h3><table>{rows}</table></div>'

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Test Report - {html.escape(tracker.test_name)}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f7fa; color:#333; padding:24px; }}

{BRAND_CSS}
.brand-strip {{ display:flex; align-items:center; gap:14px; padding:12px 20px; background:white; border-radius:12px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.brand-strip .brand-divider {{ width:1px; height:24px; background:#e5e7eb; align-self:center; }}
.brand-strip .brand-label {{ font-size:13px; font-weight:600; color:#6b7280; letter-spacing:.3px; line-height:1; }}

.report-header {{ background:linear-gradient(135deg,#2E75B6 0%,#1B4F72 100%); color:white; padding:32px; border-radius:12px; margin-bottom:24px; }}
.report-header h1 {{ font-size:24px; margin-bottom:8px; }}
.report-header .subtitle {{ opacity:.85; font-size:14px; }}
.report-header .api-pill {{ display:inline-block; background:rgba(255,255,255,.18); padding:3px 10px; border-radius:4px; font-size:11px; font-weight:700; letter-spacing:.8px; margin-right:8px; }}

.summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }}
.summary-card {{ background:white; border-radius:10px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,.06); text-align:center; }}
.summary-card .label {{ font-size:12px; text-transform:uppercase; color:#888; letter-spacing:.5px; }}
.summary-card .value {{ font-size:28px; font-weight:700; }}
.summary-card .value.pass {{ color:#22c55e; }}
.summary-card .value.fail {{ color:#ef4444; }}
.summary-card .value.neutral {{ color:#2E75B6; }}

.test-data {{ background:white; border-radius:10px; padding:20px; margin-bottom:24px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.test-data h3 {{ margin-bottom:10px; color:#1a1a2e; }}
.test-data table {{ width:100%; border-collapse:collapse; }}
.test-data td {{ padding:6px 12px; font-size:13px; border-bottom:1px solid #f0f0f0; }}
.test-data td:first-child {{ font-weight:600; color:#555; width:200px; }}

.steps-title {{ font-size:20px; font-weight:600; margin-bottom:16px; color:#1a1a2e; }}

.step-card {{ background:white; border-radius:10px; padding:20px; margin-bottom:12px; box-shadow:0 2px 8px rgba(0,0,0,.06); border-left:4px solid #ccc; }}
.step-card.pass {{ border-left-color:#22c55e; }}
.step-card.fail {{ border-left-color:#ef4444; }}
.step-card.neutral {{ border-left-color:#2E75B6; }}

.step-header {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.step-number {{ background:#e5e7eb; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; color:#555; }}
.step-name {{ font-weight:600; font-size:15px; flex:1; }}
.step-duration {{ font-size:13px; color:#888; }}
.step-callcount {{ font-size:12px; color:#2E75B6; background:#eff6ff; padding:3px 9px; border-radius:10px; font-weight:600; }}
.step-desc {{ margin-top:8px; font-size:13px; color:#666; }}

.badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }}
.badge.pass {{ background:#dcfce7; color:#166534; }}
.badge.fail {{ background:#fef2f2; color:#dc2626; }}

.assertions {{ list-style:none; margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px; }}
.assertions li {{ padding:3px 0; font-size:13px; }}
.assertion-pass {{ color:#166534; }}
.assertion-fail {{ color:#dc2626; font-weight:600; }}

.records {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:8px; }}
.record-link {{ display:inline-flex; align-items:center; gap:6px; padding:6px 12px; background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px; font-size:13px; color:#1e40af; text-decoration:none; }}
.record-link:hover {{ background:#dbeafe; border-color:#60a5fa; }}
.record-link .record-label {{ font-weight:600; color:#1e3a8a; }}
.record-link .record-name {{ font-family:'SF Mono',Menlo,Consolas,monospace; font-size:12px; }}
.record-link .record-ext {{ font-size:11px; opacity:.6; }}
.record-link-plain {{ background:#f3f4f6; border-color:#e5e7eb; color:#374151; cursor:default; }}

.error-box {{ margin-top:10px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px; padding:12px; }}

/* ── API call cards ─────────────────────────── */
.api-calls {{ margin-top:14px; display:flex; flex-direction:column; gap:10px; }}
.api-call {{ background:#fafafc; border:1px solid #e5e7eb; border-radius:8px; padding:12px 14px; }}
.api-call.pass {{ border-left:3px solid #22c55e; }}
.api-call.fail {{ border-left:3px solid #ef4444; background:#fff8f8; }}
.api-call.neutral {{ border-left:3px solid #94a3b8; }}
.api-call-header {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.api-method {{ font-family:'SF Mono',Menlo,Consolas,monospace; font-size:11px; font-weight:700; background:#1e293b; color:white; padding:2px 8px; border-radius:4px; letter-spacing:.5px; }}
.api-name {{ font-weight:600; font-size:14px; color:#1a1a2e; flex:1; min-width:200px; }}
.api-badge {{ font-family:'SF Mono',Menlo,Consolas,monospace; font-size:11px; font-weight:600; padding:3px 9px; border-radius:4px; }}
.api-badge.pass {{ background:#dcfce7; color:#166534; }}
.api-badge.fail {{ background:#fef2f2; color:#dc2626; }}
.api-badge.neutral {{ background:#f1f5f9; color:#475569; }}
.api-timing {{ font-size:12px; color:#888; font-family:'SF Mono',Menlo,Consolas,monospace; }}
.api-url {{ font-family:'SF Mono',Menlo,Consolas,monospace; font-size:11px; color:#64748b; margin-top:4px; word-break:break-all; }}
.api-body {{ margin-top:10px; }}
.api-body summary {{ cursor:pointer; font-size:12px; color:#2E75B6; font-weight:600; padding:4px 8px; background:#eff6ff; border-radius:4px; display:inline-block; user-select:none; }}
.api-body summary:hover {{ background:#dbeafe; }}
.api-body pre {{ margin-top:8px; background:#0f172a; color:#e2e8f0; padding:12px; border-radius:6px; overflow-x:auto; font-size:12px; font-family:'SF Mono',Menlo,Consolas,monospace; max-height:400px; line-height:1.5; }}
.api-error {{ margin-top:8px; padding:8px 12px; background:#fef2f2; border:1px solid #fecaca; border-radius:4px; color:#991b1b; font-size:12px; }}

.footer {{ text-align:center; margin-top:32px; padding:16px; font-size:12px; color:#aaa; }}
</style>
</head>
<body>

<div class="brand-strip">
    {brand_block("a")}
  <div class="brand-divider"></div>
  <span class="brand-label">API Test Report</span>
</div>

<div class="report-header">
  <h1><span class="api-pill">API</span>{html.escape(tracker.test_name)}</h1>
  <div class="subtitle">
    API Test Report &nbsp;|&nbsp;
    {tracker.start_time.strftime('%B %d, %Y at %I:%M:%S %p')} &nbsp;|&nbsp;
    Org: {_resolve_org_name()}
  </div>
</div>

<div class="summary-grid">
  <div class="summary-card"><div class="label">Result</div><div class="value {overall_class}">{overall_icon}</div></div>
  <div class="summary-card"><div class="label">Steps Passed</div><div class="value pass">{tracker.passed_steps}</div></div>
  <div class="summary-card"><div class="label">Steps Failed</div><div class="value {'fail' if tracker.failed_steps > 0 else 'neutral'}">{tracker.failed_steps}</div></div>
  <div class="summary-card"><div class="label">Total Steps</div><div class="value neutral">{len(tracker.steps)}</div></div>
  <div class="summary-card"><div class="label">API Calls</div><div class="value neutral">{tracker.total_api_calls}</div></div>
  <div class="summary-card"><div class="label">Duration</div><div class="value neutral">{_format_duration(tracker.total_duration)}</div></div>
</div>

{extra_table}

<h2 class="steps-title">Test Steps — API Calls</h2>
{step_rows}

<div class="footer">sfauto — API Layer | sfauto | Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>

</body>
</html>"""

    report_path.write_text(html_doc, encoding="utf-8")
