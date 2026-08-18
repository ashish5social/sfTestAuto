"""Assemble a run's per-test reports into one file, plus an email body.

Two artefacts, because they have different jobs:

  combined.html  the full self-contained report — every screenshot,
                 video and API log embedded. Attached to the email and
                 uploaded as a CI artifact. Opened in a browser.

  email_body.html  a static summary. Mail clients strip <script> and
                 <iframe>, and the combined report is built on both, so
                 pasting it into a message body would render an empty
                 shell. This is plain tables and inline styles, which
                 survives Gmail/Outlook intact.

Usage:
    python scripts/ci_report.py --reports-dir reports --out-dir ci_out
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.combined_report import build_combined_report  # noqa: E402


def load_entries(reports_dir: Path) -> list[dict]:
    """Read every report sidecar, newest last, into combined-report shape."""
    entries: list[dict] = []
    for side in sorted(reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        report = reports_dir / (meta.get("report") or "")
        entries.append({
            "filename": meta.get("report") or side.name,
            "display_name": meta.get("test_name") or side.stem,
            "type": meta.get("kind") or "ui",
            "status": meta.get("status") or "failed",
            "duration_s": meta.get("duration_s"),
            "report_path": report if report.exists() else None,
            "error": meta.get("error"),
        })
    return entries


def _fmt_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds - int(seconds // 60) * 60:.0f}s"


def build_email_body(entries: list[dict], *, ctx: dict,
                     public_url: str = "") -> str:
    """Static, inline-styled summary that renders in any mail client."""
    passed = sum(1 for e in entries if e["status"] == "passed")
    failed = sum(1 for e in entries if e["status"] == "failed")
    overall = "PASSED" if failed == 0 and entries else "FAILED"
    accent = "#16a34a" if overall == "PASSED" else "#dc2626"

    rows = []
    for e in entries:
        ok = e["status"] == "passed"
        colour = "#16a34a" if ok else "#dc2626"
        label = "PASS" if ok else "FAIL"
        err = ""
        if e.get("error"):
            err = (
                f'<div style="margin-top:6px;font:12px/1.5 monospace;'
                f'color:#b91c1c;white-space:pre-wrap">'
                f'{html.escape(str(e["error"])[:500])}</div>'
            )
        rows.append(f"""
        <tr>
          <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb">
            <div style="font:600 14px system-ui,sans-serif;color:#111827">
              {html.escape(e['display_name'])}</div>
            <div style="font:12px system-ui,sans-serif;color:#6b7280;margin-top:2px">
              {html.escape(e['type'].upper())} &middot; {html.escape(e['filename'])}</div>
            {err}
          </td>
          <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;
                     text-align:right;white-space:nowrap">
            <span style="display:inline-block;padding:3px 10px;border-radius:999px;
                         background:{colour};color:#fff;font:700 11px system-ui,sans-serif">
              {label}</span>
            <div style="font:12px system-ui,sans-serif;color:#6b7280;margin-top:4px">
              {_fmt_duration(e['duration_s'])}</div>
          </td>
        </tr>""")

    meta_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;font:12px system-ui,sans-serif;'
        f'color:#6b7280">{html.escape(k)}</td>'
        f'<td style="font:12px system-ui,sans-serif;color:#111827">'
        f'{html.escape(str(v))}</td></tr>'
        for k, v in ctx.items() if v
    )

    link_block = ""
    if public_url:
        link_block = f"""
  <tr><td style="padding:18px 24px;text-align:center;border-top:1px solid #e5e7eb">
    <a href="{html.escape(public_url)}"
       style="display:inline-block;padding:11px 22px;background:#111827;color:#fff;
              border-radius:8px;text-decoration:none;
              font:600 14px system-ui,sans-serif">View the full report online</a>
  </td></tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Salesforce Test Run — {overall}</title></head>
<body style="margin:0;padding:24px;background:#f3f4f6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:720px;margin:0 auto;background:#fff;border-radius:12px;
              overflow:hidden;border:1px solid #e5e7eb">
  <tr><td style="padding:22px 24px;border-bottom:3px solid {accent}">
    <div style="font:700 19px system-ui,sans-serif;color:#111827">
      Salesforce Test Run — {overall}</div>
    <div style="font:13px system-ui,sans-serif;color:#6b7280;margin-top:4px">
      {passed} passed &middot; {failed} failed &middot; {len(entries)} total</div>
  </td></tr>
  <tr><td style="padding:16px 24px;background:#f9fafb;border-bottom:1px solid #e5e7eb">
    <table role="presentation" cellpadding="0" cellspacing="0">{meta_rows}</table>
  </td></tr>
  <tr><td style="padding:0 10px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {''.join(rows) or '<tr><td style="padding:20px;font:14px system-ui,sans-serif;color:#6b7280">No test reports were produced.</td></tr>'}
    </table>
  </td></tr>
  {link_block}
  <tr><td style="padding:18px 24px;background:#f9fafb;
                 font:12px/1.6 system-ui,sans-serif;color:#6b7280">
    The full report — every screenshot, video and API request/response — is
    attached as a single self-contained HTML file. Download it and open it
    in a browser; it needs no network access.
  </td></tr>
</table>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--out-dir", default="ci_out")
    ap.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", "local"))
    ap.add_argument("--public-url", default=os.getenv("SFAUTO_PUBLIC_URL", ""),
                    help="Published (gh-pages) URL of this run's report")
    args = ap.parse_args()

    reports_dir = Path(args.reports_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = load_entries(reports_dir)
    passed = sum(1 for e in entries if e["status"] == "passed")
    failed = sum(1 for e in entries if e["status"] == "failed")
    overall = "passed" if failed == 0 and entries else "failed"

    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    run_url = f"{server}/{repo}/actions/runs/{args.run_id}" if repo else ""
    ctx = {
        "Repository": repo,
        "Branch": os.getenv("GITHUB_REF_NAME", ""),
        "Commit": (os.getenv("GITHUB_SHA", "") or "")[:8],
        "Selection": os.getenv("SFAUTO_SELECTION", ""),
        "Triggered by": os.getenv("GITHUB_ACTOR", ""),
        "Finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Workflow run": run_url,
        "Report": args.public_url,
    }

    combined = None
    if entries:
        combined = build_combined_report(
            run_id=str(args.run_id),
            started_at=datetime.now().isoformat(timespec="seconds"),
            duration_s=sum(e["duration_s"] or 0 for e in entries),
            status=overall,
            entries=entries,
            out_dir=out_dir,
        )
        target = out_dir / "sfauto_report.html"
        if Path(combined) != target:
            target.write_text(Path(combined).read_text(encoding="utf-8"),
                              encoding="utf-8")
        combined = target

    (out_dir / "email_body.html").write_text(
        build_email_body(entries, ctx=ctx, public_url=args.public_url),
        encoding="utf-8")

    subject = (
        f"[sfauto] {overall.upper()} — {passed} passed, {failed} failed"
        f"{' — ' + repo if repo else ''}"
    )

    # Runs are foldered by *local* date, not UTC. GitHub runners are UTC,
    # and for a team in IST that is a different calendar day for 5.5h
    # every night — so "delete anything older than today" would delete
    # today's runs. The profile timezone is the org's, which is the one
    # people mean.
    try:
        from src.core.org_profile import load_profile
        tz = load_profile().tz
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    run_dir_name = f"{run_date}_{args.run_id}"

    # Mail servers reject oversized messages (Gmail caps at 25MB), and
    # video-per-test grows the report fast. Past the threshold we rely on
    # the published link instead of attaching.
    attach = False
    size_mb = 0.0
    if combined and combined.exists():
        size_mb = combined.stat().st_size / (1024 * 1024)
        attach = size_mb <= float(os.getenv("SFAUTO_MAX_ATTACH_MB", "20"))

    (out_dir / "run_meta.json").write_text(json.dumps({
        "run_id": str(args.run_id),
        "run_dir": run_dir_name,
        "date": run_date,
        "finished_at": now.isoformat(timespec="seconds"),
        "status": overall,
        "passed": passed,
        "failed": failed,
        "total": len(entries),
        "branch": os.getenv("GITHUB_REF_NAME", ""),
        "commit": (os.getenv("GITHUB_SHA", "") or "")[:8],
        "selection": os.getenv("SFAUTO_SELECTION", ""),
        "actor": os.getenv("GITHUB_ACTOR", ""),
        "size_mb": round(size_mb, 2),
    }, indent=2), encoding="utf-8")

    # Hand the numbers back to the workflow.
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"status={overall}\n")
            fh.write(f"passed={passed}\n")
            fh.write(f"failed={failed}\n")
            fh.write(f"total={len(entries)}\n")
            fh.write(f"subject={subject}\n")
            fh.write(f"has_report={'true' if combined else 'false'}\n")
            fh.write(f"run_dir={run_dir_name}\n")
            fh.write(f"attach={'true' if attach else 'false'}\n")
            fh.write(f"size_mb={size_mb:.2f}\n")

    print(f"tests={len(entries)} passed={passed} failed={failed} overall={overall}")
    print(f"combined: {combined}")
    print(f"email body: {out_dir / 'email_body.html'}")
    print(f"run_dir={run_dir_name} size={size_mb:.2f}MB attach={attach}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
