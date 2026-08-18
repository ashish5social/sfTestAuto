"""
HTML Report Generator for generated Playwright tests.

Produces a single-file HTML report with:
- Summary stats (pass/fail, duration, step counts)
- Embedded video player (if video was recorded)
- Step-by-step detail cards with embedded screenshots
- Failure details and assertion tracking

Reports are fully self-contained (base64-encoded media) so they can
be opened from anywhere without a server.
"""

import os
import base64
from datetime import datetime
from pathlib import Path

from src.core.step_tracker import StepTracker


def _resolve_org_name() -> str:
    """Derive the SF org (My Domain) name from the SF_LOGIN_URL env var.

    Examples:
      https://fidium--apitest1.sandbox.my.salesforce.com  ->  fidium--apitest1
      https://acme.my.salesforce.com                      ->  acme
      https://test.salesforce.com                         ->  test.salesforce.com

    Falls back to SF_ORG_NAME env var if set, or 'unknown' otherwise.
    """
    override = os.getenv("SF_ORG_NAME", "").strip()
    if override:
        return override
    url = os.getenv("SF_LOGIN_URL", "").strip()
    if not url:
        return "unknown"
    # Strip protocol
    host = url.split("://", 1)[-1].split("/", 1)[0]
    # For my.salesforce.com / sandbox.my.salesforce.com subdomains, take the leftmost label
    if ".my.salesforce.com" in host or ".sandbox.my.salesforce.com" in host:
        return host.split(".", 1)[0]
    return host


def _format_duration(seconds) -> str:
    """Format a duration in seconds as "Ys" or "Xm Ys".

    Examples:
      58    -> "58s"
      58.5  -> "58.5s"
      60    -> "1m 0s"
      68    -> "1m 8s"
      674.7 -> "11m 14.7s"
    """
    try:
        total = float(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        # Preserve the original granularity for sub-minute values
        if abs(total - round(total)) < 0.05:
            return f"{int(round(total))}s"
        return f"{total:g}s"
    minutes = int(total // 60)
    remainder = total - (minutes * 60)
    if abs(remainder - round(remainder)) < 0.05:
        return f"{minutes}m {int(round(remainder))}s"
    return f"{minutes}m {remainder:g}s"


def _img_to_base64(path: str) -> str:
    """Convert image file to base64 data URI for embedding in HTML."""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return f"data:image/png;base64,{data}"
    except Exception:
        return ""


def _video_to_base64(path: str) -> str:
    """Convert video file to base64 data URI for embedding in HTML."""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return f"data:video/webm;base64,{data}"
    except Exception:
        return ""


def generate_html_report(
    tracker: StepTracker,
    report_path: Path,
    video_path: str = None,
    extra_data: dict = None,
):
    """
    Generate a single-page HTML report with embedded screenshots and video.

    Args:
        tracker: StepTracker instance with recorded step data.
        report_path: Where to write the HTML file.
        video_path: Optional path to a .webm video recording.
        extra_data: Optional dict of key-value pairs shown in a "Test Data" table.
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Video player ──
    video_html = ""
    if video_path and os.path.exists(video_path):
        b64_video = _video_to_base64(video_path)
        if b64_video:
            video_html = f'''
    <div class="video-section">
        <h3 class="video-title">Test Recording</h3>
        <video controls preload="metadata" class="test-video">
            <source src="{b64_video}" type="video/webm">
            Your browser does not support the video tag.
        </video>
    </div>'''

    # ── Step cards ──
    step_rows = ""
    for step in tracker.steps:
        status_class = "pass" if step["status"] == "PASS" else "fail"
        status_badge = (
            '<span class="badge pass">PASS</span>'
            if step["status"] == "PASS"
            else '<span class="badge fail">FAIL</span>'
        )

        screenshot_html = ""
        if step["screenshot"] and os.path.exists(step["screenshot"]):
            b64 = _img_to_base64(step["screenshot"])
            if b64:
                screenshot_html = (
                    f'<div class="screenshot-container">'
                    f'<img src="{b64}" alt="Step {step["number"]}" class="screenshot" '
                    f'onclick="this.classList.toggle(\'expanded\')" title="Click to expand"/>'
                    f'</div>'
                )

        assertions_html = ""
        if step["assertions"]:
            items = "".join(
                f'<li class="{"assertion-pass" if a["passed"] else "assertion-fail"}">'
                f'{"OK" if a["passed"] else "FAIL"} {a["description"]}</li>'
                for a in step["assertions"]
            )
            assertions_html = f'<ul class="assertions">{items}</ul>'

        # ── Record links (clickable Salesforce record URLs) ──
        records_html = ""
        records = step.get("records") or []
        if records:
            items = []
            for rec in records:
                label = (rec.get("label") or "Record").replace("<", "&lt;").replace(">", "&gt;")
                name = (rec.get("name") or "").replace("<", "&lt;").replace(">", "&gt;")
                url = rec.get("url") or ""
                if url:
                    items.append(
                        f'<a class="record-link" href="{url}" target="_blank" '
                        f'rel="noopener noreferrer" title="Open in Salesforce">'
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
            records_html = f'<div class="records">{"".join(items)}</div>'

        error_html = ""
        if step["error"]:
            # Show a simplified error message — strip DOM details, locator
            # dumps, and long stack traces that clutter the report.
            raw = step["error"]
            # Take only the first meaningful line (before locator / DOM dumps)
            first_line = raw.split("\n")[0].strip()
            # Truncate overly long messages (e.g. Playwright locator dumps)
            if len(first_line) > 300:
                first_line = first_line[:300] + "..."
            safe = first_line.replace("<", "&lt;").replace(">", "&gt;")
            error_html = (
                f'<div class="error-box"><strong>Error:</strong> '
                f'<span style="font-size:13px">{safe}</span></div>'
            )

        # ── Visual-regression diffs (current / golden / diff side-by-side) ──
        golden_html = ""
        golden_diffs = step.get("golden_diffs") or []
        if golden_diffs:
            tiles = []
            for g in golden_diffs:
                status = g.get("status", "match")
                badge_cls = (
                    "pass" if status == "match"
                    else ("fail" if status == "diff" else "skipped")
                )
                pct = g.get("pixel_diff_pct", 0.0)
                note = g.get("note", "")
                imgs = []
                for label, key in (("Current", "current_path"),
                                   ("Golden", "golden_path"),
                                   ("Diff",   "diff_path")):
                    p = g.get(key)
                    if p and os.path.exists(p):
                        b64 = _img_to_base64(p)
                        if b64:
                            imgs.append(
                                f'<div class="golden-tile">'
                                f'<div class="golden-tile-label">{label}</div>'
                                f'<img src="{b64}" alt="{label}" class="screenshot" '
                                f'onclick="this.classList.toggle(\'expanded\')"/>'
                                f'</div>'
                            )
                if imgs:
                    safe_name = (g.get("name", "snapshot")
                                 .replace("<", "&lt;").replace(">", "&gt;"))
                    tiles.append(
                        f'<div class="golden-row">'
                        f'<div class="golden-meta">'
                        f'<strong>{safe_name}</strong> '
                        f'<span class="badge {badge_cls}">{status.upper()}</span> '
                        f'<span class="golden-pct">{pct:.3f}% pixels differ</span>'
                        f'{(" — " + note) if note else ""}'
                        f'</div>'
                        f'<div class="golden-tiles">{"".join(imgs)}</div>'
                        f'</div>'
                    )
            if tiles:
                golden_html = f'<div class="golden-block">{"".join(tiles)}</div>'

        step_rows += f'''
        <div class="step-card {status_class}">
            <div class="step-header">
                <div class="step-number">Step {step["number"]}</div>
                <div class="step-name">{step["name"]}</div>
                {status_badge}
                <div class="step-duration">{_format_duration(step["duration_sec"])}</div>
            </div>
            {f'<div class="step-desc">{step["description"]}</div>' if step["description"] else ""}
            {records_html}
            {assertions_html}
            {error_html}
            {golden_html}
            {screenshot_html}
        </div>'''

    # ── Overall status ──
    overall_class = "pass" if tracker.overall_status == "PASS" else "fail"
    overall_icon = "PASS" if tracker.overall_status == "PASS" else "FAIL"

    # ── Failure summary ──
    # Removed: the duplicate "Failure Details" block that used to appear at the
    # top of the report. The error is already shown inside the failing step card,
    # so repeating it at the top was redundant. To re-enable, uncomment below.
    failure_summary = ""

    # ── Extra test data table ──
    test_data_html = ""
    if extra_data:
        rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in extra_data.items()
        )
        test_data_html = f'''
    <div class="test-data">
        <h3>Test Data</h3>
        <table>{rows}</table>
    </div>'''

    # ── Full HTML ──
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Test Report - {tracker.test_name}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f7fa; color:#333; padding:24px; }}

.brand-strip {{ display:flex; align-items:center; gap:14px; padding:12px 20px; background:white; border-radius:12px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.brand-strip img.logo-ih {{ height:32px; width:auto; display:block; }}
.brand-strip img.logo-fidium {{ height:32px; width:auto; display:block; }}
.brand-strip .brand-divider {{ width:1px; height:24px; background:#e5e7eb; align-self:center; }}
.brand-strip .brand-label {{ font-size:13px; font-weight:600; color:#6b7280; letter-spacing:.3px; line-height:1; }}

.report-header {{ background:linear-gradient(135deg,#FE763C 0%,#e5642e 100%); color:white; padding:32px; border-radius:12px; margin-bottom:24px; }}
.report-header h1 {{ font-size:24px; margin-bottom:8px; }}
.report-header .subtitle {{ opacity:.8; font-size:14px; }}

.summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }}
.summary-card {{ background:white; border-radius:10px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,.06); text-align:center; }}
.summary-card .label {{ font-size:12px; text-transform:uppercase; color:#888; letter-spacing:.5px; }}
.summary-card .value {{ font-size:28px; font-weight:700; }}
.summary-card .value.pass {{ color:#22c55e; }}
.summary-card .value.fail {{ color:#ef4444; }}
.summary-card .value.neutral {{ color:#FE763C; }}

/* failure-summary section removed — errors shown in step cards only */

.video-section {{ background:white; border-radius:10px; padding:20px; margin-bottom:24px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.video-title {{ font-size:18px; font-weight:600; color:#1a1a2e; margin-bottom:12px; }}
.test-video {{ width:100%; max-height:500px; border-radius:8px; background:#000; }}

.test-data {{ background:white; border-radius:10px; padding:20px; margin-bottom:24px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.test-data h3 {{ margin-bottom:10px; color:#1a1a2e; }}
.test-data table {{ width:100%; border-collapse:collapse; }}
.test-data td {{ padding:6px 12px; font-size:13px; border-bottom:1px solid #f0f0f0; }}
.test-data td:first-child {{ font-weight:600; color:#555; width:200px; }}

.steps-title {{ font-size:20px; font-weight:600; margin-bottom:16px; color:#1a1a2e; }}

.step-card {{ background:white; border-radius:10px; padding:20px; margin-bottom:12px; box-shadow:0 2px 8px rgba(0,0,0,.06); border-left:4px solid #ccc; }}
.step-card.pass {{ border-left-color:#22c55e; }}
.step-card.fail {{ border-left-color:#ef4444; }}

.step-header {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.step-number {{ background:#e5e7eb; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; color:#555; }}
.step-name {{ font-weight:600; font-size:15px; flex:1; }}
.step-duration {{ font-size:13px; color:#888; }}
.step-desc {{ margin-top:8px; font-size:13px; color:#666; }}

.badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }}
.badge.pass {{ background:#dcfce7; color:#166534; }}
.badge.fail {{ background:#fef2f2; color:#dc2626; }}

.assertions {{ list-style:none; margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px; }}
.assertions li {{ padding:3px 0; font-size:13px; }}
.assertion-pass {{ color:#166534; }}
.assertion-fail {{ color:#dc2626; font-weight:600; }}

.records {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:8px; }}
.record-link {{ display:inline-flex; align-items:center; gap:6px; padding:6px 12px; background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px; font-size:13px; color:#1e40af; text-decoration:none; transition:all .15s ease; }}
.record-link:hover {{ background:#dbeafe; border-color:#60a5fa; box-shadow:0 1px 4px rgba(30,64,175,.15); }}
.record-link .record-label {{ font-weight:600; color:#1e3a8a; }}
.record-link .record-name {{ font-family:'SF Mono',Menlo,Consolas,monospace; font-size:12px; }}
.record-link .record-ext {{ font-size:11px; opacity:.6; }}
.record-link-plain {{ background:#f3f4f6; border-color:#e5e7eb; color:#374151; cursor:default; }}
.record-link-plain:hover {{ background:#f3f4f6; border-color:#e5e7eb; box-shadow:none; }}

.error-box {{ margin-top:10px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px; padding:12px; }}
.error-box pre {{ font-size:12px; white-space:pre-wrap; word-break:break-word; color:#991b1b; margin-top:4px; }}

.screenshot-container {{ margin-top:12px; }}
.screenshot {{ max-width:100%; border-radius:8px; border:1px solid #e5e7eb; cursor:pointer; transition:all .3s ease; max-height:300px; object-fit:contain; }}
.screenshot:hover {{ box-shadow:0 4px 16px rgba(0,0,0,.15); }}
.screenshot.expanded {{ max-height:none; }}
/* Visual-regression diff block — current / golden / diff side-by-side. */
.golden-block {{ margin-top:12px; }}
.golden-row {{ background:#fafafa; border:1px solid #e5e7eb; border-radius:8px; padding:10px 12px; margin-bottom:10px; }}
.golden-meta {{ font-size:13px; margin-bottom:8px; color:#374151; }}
.golden-meta .badge {{ margin:0 6px; font-size:11px; }}
.golden-pct {{ color:#6b7280; font-size:12px; margin-left:4px; }}
.golden-tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; }}
.golden-tile {{ background:#fff; border:1px solid #e5e7eb; border-radius:6px; padding:6px; }}
.golden-tile-label {{ font-size:10px; font-weight:700; letter-spacing:.4px; color:#6b7280; text-transform:uppercase; margin-bottom:4px; }}
.golden-tile img {{ max-height:220px; }}

.footer {{ text-align:center; margin-top:32px; padding:16px; font-size:12px; color:#aaa; }}
</style>
</head>
<body>

<div class="brand-strip">
    <img class="logo-ih" src="https://ideahelix.com/wp-content/uploads/2025/07/haeder_logo.svg" alt="ideaHelix" />
    <img class="logo-fidium" src="https://d191tlbtp8692k.cloudfront.net/prod/fcom/global/Logo-Header-76x36.svg" alt="Fidium Fiber" />
    <div class="brand-divider"></div>
    <span class="brand-label">Salesforce Communication Cloud — Python + Playwright Tests</span>
</div>

<div class="report-header">
    <h1>{tracker.test_name}</h1>
    <div class="subtitle">
        CCI Salesforce Test Report &nbsp;|&nbsp;
        {tracker.start_time.strftime('%B %d, %Y at %I:%M:%S %p')} &nbsp;|&nbsp;
        Org: {_resolve_org_name()}
    </div>
</div>

<div class="summary-grid">
    <div class="summary-card"><div class="label">Result</div><div class="value {overall_class}">{overall_icon}</div></div>
    <div class="summary-card"><div class="label">Steps Passed</div><div class="value pass">{tracker.passed_steps}</div></div>
    <div class="summary-card"><div class="label">Steps Failed</div><div class="value {"fail" if tracker.failed_steps > 0 else "neutral"}">{tracker.failed_steps}</div></div>
    <div class="summary-card"><div class="label">Total Steps</div><div class="value neutral">{len(tracker.steps)}</div></div>
    <div class="summary-card"><div class="label">Duration</div><div class="value neutral">{_format_duration(tracker.total_duration)}</div></div>
</div>

{video_html}

{failure_summary}

{test_data_html}

<h2 class="steps-title">Test Steps</h2>
{step_rows}

<div class="footer">CCI Test Automation | ideaHelix | Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>

</body>
</html>'''

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
