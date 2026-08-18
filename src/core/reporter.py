"""Report generation for test results."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import config


class Reporter:
    """Generates HTML and JSON reports from test results."""

    def generate_html_report(self, result: dict, test_def=None) -> Path:
        """Generate an HTML report for a test run."""
        config.ensure_dirs()
        run_id = result.get("run_id", "unknown")
        report_path = config.REPORTS_DIR / f"{run_id}.html"

        status = result.get("status", "unknown")
        status_color = {
            "passed": "#22c55e",
            "failed": "#ef4444",
            "error": "#f97316",
            "timeout": "#eab308",
            "running": "#3b82f6",
        }.get(status, "#6b7280")

        steps_html = ""
        for step in result.get("steps", []):
            step_icon = "&#x2705;" if step.get("status") != "failed" else "&#x274C;"
            steps_html += f"""
            <div class="step">
                <span class="step-icon">{step_icon}</span>
                <span class="step-num">Step {step.get('step_number', '?')}</span>
                <span class="step-action">{step.get('action', 'N/A')}</span>
            </div>"""

        screenshots_html = ""
        for ss in result.get("screenshots", []):
            screenshots_html += f'<img src="{ss}" class="screenshot" />'

        test_name = test_def.name if test_def else result.get("run_id", "Test")
        description = test_def.description if test_def else ""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Test Report - {run_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; padding: 2rem; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .header {{ background: white; border-radius: 12px; padding: 2rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .header h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
        .header p {{ color: #64748b; }}
        .status-badge {{ display: inline-block; padding: 0.25rem 1rem; border-radius: 20px; color: white; font-weight: 600; background: {status_color}; font-size: 0.875rem; text-transform: uppercase; }}
        .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-top: 1rem; }}
        .meta-item {{ background: #f1f5f9; padding: 0.75rem 1rem; border-radius: 8px; }}
        .meta-item label {{ font-size: 0.75rem; color: #64748b; text-transform: uppercase; font-weight: 600; }}
        .meta-item span {{ display: block; font-size: 1rem; margin-top: 0.25rem; }}
        .card {{ background: white; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .card h2 {{ font-size: 1.125rem; margin-bottom: 1rem; color: #1e40af; }}
        .step {{ display: flex; align-items: center; gap: 0.75rem; padding: 0.75rem; border-bottom: 1px solid #f1f5f9; }}
        .step:last-child {{ border-bottom: none; }}
        .step-icon {{ font-size: 1.25rem; }}
        .step-num {{ font-weight: 600; color: #64748b; min-width: 60px; }}
        .step-action {{ flex: 1; }}
        .result-box {{ background: #f8fafc; border-radius: 8px; padding: 1rem; white-space: pre-wrap; font-family: monospace; font-size: 0.875rem; max-height: 400px; overflow-y: auto; }}
        .screenshot {{ max-width: 100%; border-radius: 8px; margin: 0.5rem 0; border: 1px solid #e2e8f0; }}
        .error {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 1rem; color: #991b1b; }}
        .footer {{ text-align: center; padding: 2rem; color: #94a3b8; font-size: 0.875rem; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h1>{test_name}</h1>
                <span class="status-badge">{status}</span>
            </div>
            <p>{description}</p>
            <div class="meta">
                <div class="meta-item"><label>Run ID</label><span>{run_id}</span></div>
                <div class="meta-item"><label>Duration</label><span>{result.get('duration', 0)}s</span></div>
                <div class="meta-item"><label>Started</label><span>{result.get('start_time', 'N/A')}</span></div>
                <div class="meta-item"><label>Ended</label><span>{result.get('end_time', 'N/A')}</span></div>
            </div>
        </div>

        {"<div class='card'><h2>Steps</h2>" + steps_html + "</div>" if steps_html else ""}

        {"<div class='card error'><h2>Error</h2><p>" + str(result.get('error', '')) + "</p></div>" if result.get('error') else ""}

        <div class="card">
            <h2>Result</h2>
            <div class="result-box">{result.get('result', 'No result captured')}</div>
        </div>

        {"<div class='card'><h2>Screenshots</h2>" + screenshots_html + "</div>" if screenshots_html else ""}

        <div class="footer">
            Generated by CCI Test Automation &mdash; {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>"""

        report_path.write_text(html)

        # Also save JSON
        json_path = config.REPORTS_DIR / f"{run_id}.json"
        json_path.write_text(json.dumps(result, indent=2))

        return report_path

    def generate_summary_report(self, results: list[dict]) -> Path:
        """Generate a summary report for multiple test runs."""
        config.ensure_dirs()
        timestamp = datetime.now().strftime('%m%d_%H%M')
        report_path = config.REPORTS_DIR / f"summary_{timestamp}.html"

        passed = sum(1 for r in results if r.get("status") == "passed")
        failed = sum(1 for r in results if r.get("status") == "failed")
        total = len(results)

        rows = ""
        for r in results:
            status = r.get("status", "unknown")
            color = {"passed": "#22c55e", "failed": "#ef4444"}.get(status, "#6b7280")
            rows += f"""
            <tr>
                <td>{r.get('run_id', 'N/A')}</td>
                <td><span style="color:{color};font-weight:600">{status.upper()}</span></td>
                <td>{r.get('duration', 0)}s</td>
                <td>{r.get('error', '-')}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html><head><title>Test Summary</title>
<style>
body {{ font-family: sans-serif; padding: 2rem; background: #f8fafc; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; }}
th, td {{ padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid #e2e8f0; }}
th {{ background: #1e40af; color: white; }}
.summary {{ display: flex; gap: 2rem; margin-bottom: 2rem; }}
.stat {{ background: white; padding: 1.5rem; border-radius: 8px; text-align: center; min-width: 120px; }}
.stat .num {{ font-size: 2rem; font-weight: bold; }}
</style></head>
<body>
<h1>Test Run Summary</h1>
<div class="summary">
    <div class="stat"><div class="num">{total}</div>Total</div>
    <div class="stat"><div class="num" style="color:#22c55e">{passed}</div>Passed</div>
    <div class="stat"><div class="num" style="color:#ef4444">{failed}</div>Failed</div>
    <div class="stat"><div class="num">{round(passed/total*100) if total else 0}%</div>Pass Rate</div>
</div>
<table><thead><tr><th>Run ID</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""

        report_path.write_text(html)
        return report_path
