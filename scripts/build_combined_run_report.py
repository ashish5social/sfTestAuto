"""Build a single combined per-run HTML report from a folder of
individual per-test reports.

The dashboard's /run endpoint builds this report in real time during a
run. GitHub Actions runs pytest directly (no FastAPI involvement) so we
need a CLI to stitch the per-test reports into the same combined HTML
after the run completes.

Usage:
    python scripts/build_combined_run_report.py \
        --reports-dir /tmp/cci_output/reports \
        --output-dir /tmp/cci_pages \
        --run-id run-1234-20260521-1430 \
        --started-at 2026-05-21T14:30:00 \
        --duration 0

Side effects:
  - Writes `<output-dir>/run_<run-id>.html`
  - Prints the resulting absolute path to stdout (so the shell can
    capture it for the email step's attachment)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Make src.* imports work when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.web.combined_report import build_combined_report  # noqa: E402


# Pull status + duration out of an existing per-test HTML report. The
# reporter writes badge spans like `<span class="badge pass">PASS</span>`
# and a summary card containing the duration label. We don't need to be
# clever — parse_report_html() in scripts/generate_index.py already
# established this pattern; we reuse the same regexes.

_BADGE_PASS_RE = re.compile(r'class="badge pass">\s*PASS\s*<')
_BADGE_FAIL_RE = re.compile(r'class="badge fail">\s*FAIL\s*<')
_DURATION_RE = re.compile(r'Duration</div>\s*<div[^>]*>([^<]+)</div>')

# Friendly test name lives in the per-test HTML's <title> tag. UI
# reports write "<title>Test Report - {test_name}</title>"; API
# reports write "<title>API Test Report - {test_name}</title>". The
# {test_name} portion comes from the test class's docstring (or the
# method name, title-cased), which gives us "TC1 - Create Enterprise
# Quote with DIA" rather than a random timestamp hash.
_TITLE_RE = re.compile(
    r"<title>\s*(?:API\s+)?Test\s+Report\s*[-–—]\s*(.+?)\s*</title>",
    re.IGNORECASE | re.DOTALL,
)
# Fallback to the first H1 if the title doesn't match the expected
# pattern (e.g. someone edited the reporter template).
_H1_RE = re.compile(r"<h1[^>]*>\s*(.+?)\s*</h1>", re.IGNORECASE | re.DOTALL)


def parse_display_name(html: str) -> str | None:
    """Extract the friendly test name from a per-test HTML report.
    Returns None if neither the <title> nor an <h1> yields anything."""
    m = _TITLE_RE.search(html)
    if m:
        name = _strip_tags(m.group(1)).strip()
        if name and name.lower() != "test report":
            return name
    m = _H1_RE.search(html)
    if m:
        name = _strip_tags(m.group(1)).strip()
        if name:
            return name
    return None


def _strip_tags(s: str) -> str:
    """Best-effort: drop any nested HTML tags from a title/h1 fragment."""
    return re.sub(r"<[^>]+>", "", s)


def parse_status_and_duration(html: str) -> tuple[str, float | None]:
    pass_count = len(_BADGE_PASS_RE.findall(html))
    fail_count = len(_BADGE_FAIL_RE.findall(html))
    if pass_count + fail_count > 0:
        status = "failed" if fail_count > 0 else "passed"
    elif re.search(r"\bFAIL\b", html[:3000]):
        status = "failed"
    elif re.search(r"\bPASS\b", html[:3000]):
        status = "passed"
    else:
        status = "failed"  # unknown → conservative

    duration_s: float | None = None
    m = _DURATION_RE.search(html)
    if m:
        text = m.group(1).strip()
        # Examples: "58s", "58.5s", "8m 41.2s"
        total = 0.0
        mm = re.search(r"(\d+(?:\.\d+)?)m", text)
        ss = re.search(r"(\d+(?:\.\d+)?)s", text)
        if mm:
            total += float(mm.group(1)) * 60
        if ss:
            total += float(ss.group(1))
        if total > 0:
            duration_s = round(total, 1)

    return status, duration_s


def classify_filename(name: str) -> str:
    """UI tests use `test_report_*.html`, API tests `test_report_api_*.html`."""
    return "api" if name.startswith("test_report_api_") else "ui"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build combined per-run report from per-test HTML reports.")
    parser.add_argument("--reports-dir", required=True, help="Directory of test_report_*.html files")
    parser.add_argument("--output-dir", required=True, help="Where to write run_<id>.html")
    parser.add_argument("--run-id", required=True, help="Run identifier (used in filename + title)")
    parser.add_argument("--started-at", default=None, help="ISO timestamp; defaults to now")
    parser.add_argument("--duration", type=float, default=0.0, help="Total run duration in seconds")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = args.started_at or datetime.now().isoformat()

    # Sort by mtime so the gutter order matches execution order — useful
    # because parallel runs may not match alphabetical order.
    report_paths = sorted(
        reports_dir.glob("test_report_*.html"),
        key=lambda p: p.stat().st_mtime,
    )

    entries: list[dict] = []
    overall_passed = True
    for rpt in report_paths:
        html = rpt.read_text(encoding="utf-8", errors="replace")
        status, duration_s = parse_status_and_duration(html)
        if status != "passed":
            overall_passed = False
        display_name = parse_display_name(html)
        # The timestamp portion (e.g. "0521_085633_1ce1") makes a
        # decent "sub-title" filename — it lets you correlate the
        # gutter row with the underlying per-test HTML file.
        sub = rpt.stem.replace("test_report_", "").replace("api_", "")
        entries.append({
            "filename": sub,
            "display_name": display_name,
            "type": classify_filename(rpt.name),
            "status": status,
            "duration_s": duration_s,
            "report_path": rpt,
        })

    final_status = "passed" if overall_passed else "failed"

    combined = build_combined_report(
        run_id=args.run_id,
        started_at=started_at,
        duration_s=args.duration,
        status=final_status,
        entries=entries,
        out_dir=output_dir,
    )

    # Also write a small summary.json next to the report so downstream
    # CI steps (email notification, Slack webhook, etc.) can pull
    # counts + status without re-parsing the HTML themselves.
    summary = {
        "run_id": args.run_id,
        "status": final_status,
        "total": len(entries),
        "passed": sum(1 for e in entries if e["status"] == "passed"),
        "failed": sum(1 for e in entries if e["status"] == "failed"),
        "cancelled": sum(1 for e in entries if e["status"] == "cancelled"),
        "tests": [
            {
                "name": e.get("display_name") or e.get("filename") or "",
                "type": e.get("type"),
                "status": e.get("status"),
                "duration_s": e.get("duration_s"),
            }
            for e in entries
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    # Print the absolute path so the shell can grab it for the email
    # body and the GH Pages copy step.
    print(str(combined.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
