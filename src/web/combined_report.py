"""Combined per-run HTML report.

After every dashboard run, we stitch the individual per-test HTML reports
into one self-contained file: reports/run_{run_id}.html. The combined
report has a left gutter listing every test (with type + status badges)
and an iframe viewer on the right. Click a test in the gutter, the
viewer's srcdoc is populated from the embedded report.

Why this layout:
  - Single download. The user shares ONE .html and the recipient sees
    every test, every screenshot, every video, every API log, with no
    external dependencies.
  - No build step. Per-test reports already embed media as base64 data
    URIs, so we can inline them verbatim inside <script type="text/x-..."
    blocks. Script tags with a non-JS type don't execute but preserve
    their text content for srcdoc retrieval.
  - Collapsible gutter so the viewer can use the full page width when
    you want to focus on a single test.
"""

from __future__ import annotations

import html as _html
from datetime import datetime
from pathlib import Path
from typing import Iterable


# ── Helpers ────────────────────────────────────────────────────────────


def _escape_for_script(html: str) -> str:
    """Make per-test HTML safe to embed inside a <script type="text/x-...">.

    A script tag is closed by the literal text `</script>` (case-insensitive,
    optionally followed by whitespace or `>`). Any of those inside the
    embedded HTML would terminate our wrapper script and dump the rest of
    the file into the DOM as plain text. Replacing the slash with a
    backslash-slash sequence is harmless inside the data we feed back into
    srcdoc, but stops the parser from recognising the close tag.
    """
    # Cover all the variants the HTML5 parser treats as end-tags:
    # </script, </SCRIPT, </Script, etc.
    out = []
    i = 0
    lower = html.lower()
    needle = "</script"
    while i < len(html):
        j = lower.find(needle, i)
        if j == -1:
            out.append(html[i:])
            break
        out.append(html[i:j])
        # Insert the escape between the < and the /
        out.append("<\\")
        out.append(html[j + 1: j + len(needle)])  # /script (preserving case)
        i = j + len(needle)
    return "".join(out)


def _status_class(status: str) -> str:
    s = (status or "").lower()
    if s == "passed":
        return "pass"
    if s == "failed":
        return "fail"
    if s == "cancelled":
        return "cancelled"
    return "fail"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ── Public API ─────────────────────────────────────────────────────────


def build_combined_report(
    *,
    run_id: str,
    started_at: str,
    duration_s: float,
    status: str,
    entries: Iterable[dict],
    out_dir: Path,
) -> Path:
    """Render reports/run_{run_id}.html and return the path.

    Each `entries[i]` is expected to have:
      filename       (str)         e.g. "test_cci_tc1_*.py"
      display_name   (str or None) friendly title from YAML
      type           ("ui"|"api")
      status         ("passed"|"failed"|"cancelled")
      duration_s     (float or None)
      report_path    (Path or None) per-test HTML to embed; None if missing
    """
    entries = list(entries)
    n_total = len(entries)
    n_passed = sum(1 for e in entries if e.get("status") == "passed")
    n_failed = sum(1 for e in entries if e.get("status") == "failed")
    n_cancelled = sum(1 for e in entries if e.get("status") == "cancelled")

    items_html_parts: list[str] = []
    scripts_html_parts: list[str] = []

    for i, e in enumerate(entries):
        tid = f"t{i}"
        display = e.get("display_name") or e.get("filename") or f"Test {i+1}"
        filename = e.get("filename") or ""
        ttype = (e.get("type") or "ui").lower()
        status_class = _status_class(e.get("status"))
        status_label = (e.get("status") or "—").upper()
        dur = e.get("duration_s")
        dur_label = (
            f"{dur:.1f}s" if isinstance(dur, (int, float)) and dur < 60
            else f"{int(dur // 60)}m {dur - int(dur // 60) * 60:.0f}s"
            if isinstance(dur, (int, float)) else ""
        )

        items_html_parts.append(f"""
        <div class="test-item" data-id="{tid}" onclick="showTest('{tid}', this)">
          <div class="idx idx-{status_class}">{i + 1}</div>
          <div class="info">
            <div class="name">{_html.escape(display)}</div>
            <div class="sub">{_html.escape(filename)}</div>
          </div>
          <div class="badges">
            <span class="badge {ttype}">{ttype.upper()}</span>
            <span class="badge {status_class}">{status_label}</span>
            {f'<span class="dur">{dur_label}</span>' if dur_label else ''}
          </div>
        </div>
        """)

        report_path = e.get("report_path")
        if report_path and isinstance(report_path, Path) and report_path.exists():
            inner = _read(report_path) or ""
            if inner:
                escaped = _escape_for_script(inner)
                scripts_html_parts.append(
                    f'<script type="text/x-report-html" id="report-{tid}">{escaped}</script>'
                )

    items_html = "".join(items_html_parts)
    scripts_html = "\n".join(scripts_html_parts)

    overall_class = _status_class(status)
    overall_label = (status or "—").upper()
    page_title = f"Run {run_id} — {overall_label}"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(page_title)}</title>
<style>
  :root {{
    --orange:#FE763C; --orange-light:#FFF3ED; --orange-hover:#e5642e;
    --border:#E5E7EB; --muted:#6B7280; --text:#1A1A1A;
    --bg:#FAFAFA; --surface:#FFFFFF; --surface2:#F5F5F5;
    --success:#16A34A; --success-bg:#DCFCE7;
    --danger:#DC2626;  --danger-bg:#FEE2E2;
    --warn:#EA580C;    --warn-bg:#FFEDD5;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ height:100%; }}
  body {{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    color:var(--text); background:var(--bg);
  }}
  .shell {{ display:flex; height:100vh; }}

  /* ── Left gutter ── */
  .gutter {{
    width:320px; min-width:320px;
    background:var(--surface); border-right:1px solid var(--border);
    transition:width .2s ease, min-width .2s ease;
    display:flex; flex-direction:column; overflow:hidden;
  }}
  .gutter.collapsed {{ width:48px; min-width:48px; }}
  .gutter.collapsed > :not(.gutter-bar) {{ display:none; }}

  .gutter-bar {{
    display:flex; align-items:center; justify-content:space-between;
    padding:10px 12px; border-bottom:1px solid var(--border);
    background:var(--surface); gap:6px;
  }}
  .gutter.collapsed .gutter-bar {{ justify-content:center; padding:14px 0; }}
  .gutter.collapsed .download-btn {{ display:none; }}
  .gutter-toggle {{
    width:28px; height:28px; border-radius:50%; border:1px solid var(--border);
    background:var(--surface); color:var(--muted); cursor:pointer;
    font:700 13px Arial,sans-serif; line-height:1;
    display:inline-flex; align-items:center; justify-content:center;
    transition:all .12s;
  }}
  .gutter-toggle:hover {{ background:var(--orange-light); color:var(--orange); border-color:var(--orange); }}
  /* Download button — serializes the current page DOM into a Blob and
     triggers a save dialog with the run_<id>.html filename. Works the
     same whether the page is served as run_<id>.html from the local
     dashboard OR as index.html from GitHub Pages — uses the page's
     own DOM as the source rather than the URL. */
  .download-btn {{
    display:inline-flex; align-items:center; gap:5px;
    padding:5px 11px; border-radius:6px; cursor:pointer;
    border:1px solid var(--orange); background:var(--surface);
    color:var(--orange); font-size:0.72rem; font-weight:600;
    font-family:'Inter',sans-serif; line-height:1;
    text-decoration:none; white-space:nowrap;
    transition:all .12s;
  }}
  .download-btn:hover {{ background:var(--orange); color:#fff; }}

  .gutter-header {{ padding:0.9rem 1.1rem 0.6rem; border-bottom:1px solid var(--border); }}
  .gutter-header h1 {{ font-size:1rem; font-weight:700; }}
  .gutter-header .meta {{
    font-family:'SF Mono','Fira Code',Menlo,monospace;
    font-size:0.7rem; color:var(--muted); margin-top:4px;
    word-break:break-all;
  }}
  .gutter-summary {{
    display:flex; align-items:center; gap:0.8rem;
    padding:0.6rem 1.1rem; border-bottom:1px solid var(--border);
    font-size:0.73rem; color:var(--muted);
  }}
  .gutter-summary .stat strong {{ color:var(--text); font-weight:700; }}
  .gutter-summary .stat.pass strong {{ color:var(--success); }}
  .gutter-summary .stat.fail strong {{ color:var(--danger); }}
  .gutter-summary .stat.cancelled strong {{ color:var(--warn); }}

  .test-list {{ flex:1; overflow-y:auto; padding:0.4rem 0; }}
  .test-item {{
    padding:0.6rem 1.1rem; cursor:pointer; border-left:3px solid transparent;
    display:flex; align-items:flex-start; gap:10px;
    transition:background .12s, border-left-color .12s;
  }}
  .test-item:hover {{ background:var(--orange-light); }}
  .test-item.active {{ background:var(--orange-light); border-left-color:var(--orange); }}

  .test-item .idx {{
    width:22px; height:22px; border-radius:50%; flex-shrink:0;
    display:inline-flex; align-items:center; justify-content:center;
    font-size:0.65rem; font-weight:700; margin-top:1px;
  }}
  .idx-pass {{ background:var(--success-bg); color:#166534; }}
  .idx-fail {{ background:var(--danger-bg); color:#991B1B; }}
  .idx-cancelled {{ background:var(--warn-bg); color:#9A3412; }}

  .test-item .info {{ flex:1; min-width:0; }}
  .test-item .info .name {{
    font-size:0.83rem; font-weight:600; line-height:1.3;
    word-break:break-word;
  }}
  .test-item .info .sub {{
    font-family:'SF Mono','Fira Code',Menlo,monospace;
    font-size:0.66rem; color:var(--muted); margin-top:2px;
    word-break:break-all;
  }}
  .test-item .badges {{
    display:flex; flex-direction:column; align-items:flex-end; gap:4px;
    flex-shrink:0;
  }}
  .badge {{
    display:inline-flex; padding:2px 7px; border-radius:5px;
    font-size:0.6rem; font-weight:700; letter-spacing:0.5px;
    text-transform:uppercase; white-space:nowrap;
  }}
  .badge.ui  {{ background:#DBEAFE; color:#1E3A8A; }}
  .badge.api {{ background:#EDE9FE; color:#5B21B6; }}
  .badge.pass {{ background:var(--success-bg); color:#166534; }}
  .badge.fail {{ background:var(--danger-bg); color:#991B1B; }}
  .badge.cancelled {{ background:var(--warn-bg); color:#9A3412; }}
  .test-item .dur {{ font-size:0.65rem; color:var(--muted); }}

  /* ── Viewer ── */
  .viewer {{ flex:1; background:#fff; position:relative; min-width:0; }}
  .viewer iframe {{ width:100%; height:100%; border:0; display:block; }}
  .viewer .empty {{
    height:100%; display:flex; align-items:center; justify-content:center;
    color:var(--muted); font-size:0.9rem; padding:2rem; text-align:center;
  }}
</style>
</head>
<body>
<div class="shell">
  <aside class="gutter" id="gutter">
    <div class="gutter-bar">
      <button class="download-btn" id="downloadBtn" title="Download this report as a single offline HTML file">
        <span>&#x2B07;</span> Download
      </button>
      <button class="gutter-toggle" id="gutterToggle" title="Collapse / expand">&laquo;</button>
    </div>
    <div class="gutter-header">
      <h1>Run Report</h1>
      <div class="meta">{_html.escape(run_id)}<br/>{_html.escape(started_at)}</div>
    </div>
    <div class="gutter-summary">
      <span class="stat"><strong>{n_total}</strong> total</span>
      <span class="stat pass"><strong>{n_passed}</strong> passed</span>
      <span class="stat fail"><strong>{n_failed}</strong> failed</span>
      {f'<span class="stat cancelled"><strong>{n_cancelled}</strong> cancelled</span>' if n_cancelled else ''}
      <span class="stat" style="margin-left:auto"><strong>{duration_s:.1f}s</strong></span>
    </div>
    <div class="test-list" id="testList">
      {items_html}
    </div>
  </aside>
  <main class="viewer" id="viewer">
    <iframe id="viewerFrame" sandbox="allow-same-origin allow-scripts allow-popups allow-forms"></iframe>
  </main>
</div>

{scripts_html}

<script>
  function showTest(id, btn) {{
    var tmpl = document.getElementById('report-' + id);
    var frame = document.getElementById('viewerFrame');
    document.querySelectorAll('.test-item').forEach(function(el) {{ el.classList.remove('active'); }});
    if (btn) btn.classList.add('active');
    if (!tmpl) {{
      frame.srcdoc = '<div style="height:100vh;display:flex;align-items:center;justify-content:center;color:#6B7280;font-family:Inter,sans-serif;font-size:0.9rem;padding:2rem;text-align:center">No report available for this test (it may have failed before collection).</div>';
      return;
    }}
    frame.srcdoc = tmpl.textContent;
  }}
  function toggleGutter() {{
    var g = document.getElementById('gutter');
    g.classList.toggle('collapsed');
    document.getElementById('gutterToggle').innerHTML = g.classList.contains('collapsed') ? '&raquo;' : '&laquo;';
  }}
  document.getElementById('gutterToggle').addEventListener('click', toggleGutter);

  // Download handler — serializes the current DOM back into HTML and
  // pushes it through a Blob so the browser saves it with the right
  // filename regardless of the URL we're served from (run_<id>.html
  // locally, index.html on GitHub Pages, both work).
  document.getElementById('downloadBtn').addEventListener('click', function() {{
    var html = '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;
    var blob = new Blob([html], {{ type:'text/html;charset=utf-8' }});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'run_{run_id}.html';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() {{ URL.revokeObjectURL(url); }}, 1000);
  }});

  // Show the first failing test if any, else the first test.
  var first =
    document.querySelector('.test-item .badge.fail') ?
      document.querySelector('.test-item .badge.fail').closest('.test-item') :
      document.querySelector('.test-item');
  if (first) first.click();
</script>
</body>
</html>
"""

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"run_{run_id}.html"
    out_path.write_text(page, encoding="utf-8")
    return out_path
