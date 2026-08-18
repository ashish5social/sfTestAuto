"""
Generate an index.html that summarizes all test reports from a run.

Usage:
    python scripts/generate_index.py <reports_dir> <output_dir> [--run-id RUN_ID]

The index page lists each test with its status (pass/fail), duration,
and links to the individual HTML report. Video files from screenshots/
are copied alongside the reports so GitHub Pages can serve them.
"""

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


def parse_report_html(report_path: Path) -> dict:
    """Extract metadata from an HTML report file."""
    content = report_path.read_text(encoding="utf-8", errors="replace")

    # Extract test name from <title> or <h1>
    title_match = re.search(r"<title>(.*?)</title>", content)
    name = title_match.group(1) if title_match else report_path.stem

    # Extract step counts from badge spans — these are the source of truth for status.
    # Match specifically <span class="badge pass">PASS</span> / <span class="badge fail">FAIL</span>
    # to avoid double-counting the result summary card which also contains PASS/FAIL text.
    pass_count = len(re.findall(r'class="badge pass">\s*PASS\s*<', content))
    fail_count = len(re.findall(r'class="badge fail">\s*FAIL\s*<', content))
    total_steps = pass_count + fail_count

    # Determine status: any failed step means the test FAILED
    if total_steps > 0:
        status = "FAIL" if fail_count > 0 else "PASS"
    else:
        # No step markers found — fall back to text heuristic
        top_section = content[:3000]
        if re.search(r"\bFAIL\b", top_section):
            status = "FAIL"
        elif re.search(r"\bPASS\b", top_section):
            status = "PASS"
        else:
            status = "UNKNOWN"

    # Extract duration from the summary card. html_reporter formats durations
    # as either "58s", "58.5s", or "8m 41.2s", so we capture the entire inner
    # text of the value div rather than assuming a pure numeric format.
    # Example markup: <div class="label">Duration</div><div class="value neutral">8m 41.2s</div>
    duration_match = re.search(r'Duration</div>\s*<div[^>]*>([^<]+)</div>', content)
    duration = duration_match.group(1).strip() if duration_match else "—"

    return {
        "name": name,
        "file": report_path.name,
        "status": status,
        "steps_passed": pass_count,
        "steps_total": total_steps,
        "duration": duration,
    }


def generate_index(reports_dir: Path, output_dir: Path, run_id: str = None):
    """Generate index.html summarizing all reports."""
    reports = sorted(reports_dir.glob("test_report_*.html"))
    if not reports:
        print("No reports found.")
        return

    # Copy reports to output
    output_dir.mkdir(parents=True, exist_ok=True)
    report_data = []
    for rpt in reports:
        shutil.copy2(str(rpt), str(output_dir / rpt.name))
        report_data.append(parse_report_html(rpt))

    total = len(report_data)
    passed = sum(1 for r in report_data if r["status"] == "PASS")
    failed = total - passed
    overall = "PASSED" if failed == 0 else "FAILED"
    run_label = run_id or datetime.now().strftime("%Y-%m-%d %H:%M")

    # Build HTML
    rows = ""
    for i, r in enumerate(report_data, 1):
        status_class = "pass" if r["status"] == "PASS" else "fail"
        status_icon = "✔" if r["status"] == "PASS" else "✖"
        steps_text = f"{r['steps_passed']}/{r['steps_total']}" if r["steps_total"] else "—"
        # duration already carries its unit ("8m 41.2s" / "58s" / "—"),
        # so don't append another "s" here.
        rows += f"""
        <tr class="{status_class}">
          <td>{i}</td>
          <td><a href="{r['file']}">{r['name']}</a></td>
          <td class="status">{status_icon} {r['status']}</td>
          <td>{steps_text}</td>
          <td>{r['duration']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCI Test Run — {run_label}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f7fa; color: #333; padding: 2rem; }}
  .container {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; color: #1B4F72; }}
  .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .summary {{ display: flex; gap: 1.5rem; margin-bottom: 2rem; }}
  .summary .card {{ background: white; border-radius: 8px; padding: 1rem 1.5rem;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; flex: 1; }}
  .summary .card .num {{ font-size: 2rem; font-weight: 700; }}
  .summary .card .label {{ font-size: 0.8rem; color: #666; text-transform: uppercase; }}
  .card.overall-pass .num {{ color: #27ae60; }}
  .card.overall-fail .num {{ color: #e74c3c; }}
  .card.pass-count .num {{ color: #27ae60; }}
  .card.fail-count .num {{ color: #e74c3c; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background: #1B4F72; color: white; padding: 0.75rem 1rem; text-align: left;
       font-size: 0.85rem; text-transform: uppercase; }}
  td {{ padding: 0.75rem 1rem; border-bottom: 1px solid #eee; font-size: 0.9rem; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.pass .status {{ color: #27ae60; font-weight: 600; }}
  tr.fail .status {{ color: #e74c3c; font-weight: 600; }}
  a {{ color: #2E75B6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .footer {{ margin-top: 2rem; text-align: center; font-size: 0.8rem; color: #999; }}
</style>
</head>
<body>
<div class="container">
  <h1>CCI Test Run Results</h1>
  <p class="meta">Run: {run_label}</p>

  <div class="summary">
    <div class="card overall-{'pass' if failed == 0 else 'fail'}">
      <div class="num">{overall}</div>
      <div class="label">Overall</div>
    </div>
    <div class="card pass-count">
      <div class="num">{passed}</div>
      <div class="label">Passed</div>
    </div>
    <div class="card fail-count">
      <div class="num">{failed}</div>
      <div class="label">Failed</div>
    </div>
    <div class="card">
      <div class="num">{total}</div>
      <div class="label">Total Tests</div>
    </div>
  </div>

  <table>
    <thead>
      <tr><th>#</th><th>Test</th><th>Status</th><th>Steps</th><th>Duration</th></tr>
    </thead>
    <tbody>{rows}
    </tbody>
  </table>

  <p class="footer">Generated by CCI Test Automation — GitHub Actions</p>
</div>
</body>
</html>"""

    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"Index generated: {index_path}")

    # Write summary JSON for email step
    summary = {
        "run_id": run_label,
        "overall": overall,
        "total": total,
        "passed": passed,
        "failed": failed,
        "tests": report_data,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Summary JSON: {summary_path}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate test run index page")
    parser.add_argument("reports_dir", help="Directory containing HTML reports")
    parser.add_argument("output_dir", help="Output directory for index + copied reports")
    parser.add_argument("--run-id", help="Run identifier label", default=None)
    args = parser.parse_args()
    generate_index(Path(args.reports_dir), Path(args.output_dir), args.run_id)
