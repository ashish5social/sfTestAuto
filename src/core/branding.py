"""Branding for reports and the dashboard.

Single source of truth for the product name, tagline and logo mark. The
logo is emitted as *inline* SVG rather than an <img src="..."> so that:

  * reports are fully self-contained — they render with no network access,
    which matters when a report is emailed or opened from a CI artifact;
  * the wordmark inherits the surrounding text colour via `currentColor`,
    so one asset works on both the light report and the dark dashboard.

To rebrand, change BRAND_NAME / BRAND_TAGLINE below, or drop your own
SVG path into _MARK_SVG. Nothing else in the codebase hardcodes a brand.
"""

from __future__ import annotations

import os

BRAND_NAME: str = os.getenv("SFAUTO_BRAND_NAME", "Spread Unconditional Love")
BRAND_TAGLINE: str = os.getenv("SFAUTO_BRAND_TAGLINE", "Salesforce Test Automation")

# Gradient id is parameterised so multiple inline copies on one page don't
# collide in the SVG id namespace.
_MARK_SVG = """<svg class="brand-mark" width="{size}" height="{size}" viewBox="0 0 32 32" \
xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{name}">\
<defs><linearGradient id="sul-{uid}" x1="0" y1="0" x2="1" y2="1">\
<stop offset="0%" stop-color="#FF7A7A"/><stop offset="52%" stop-color="#F06595"/>\
<stop offset="100%" stop-color="#7C5CF7"/></linearGradient></defs>\
<path d="M16 28.4C16 28.4 2.6 20.4 2.6 12.1 2.6 7.4 6.3 3.6 10.8 3.6c2.7 0 4.5 1.4 5.2 2.7.7-1.3 2.5-2.7 5.2-2.7 \
4.5 0 8.2 3.8 8.2 8.5 0 8.3-13.4 16.3-13.4 16.3z" fill="url(#sul-{uid})"/>\
<path d="M9.3 10.2c1-1.9 2.9-3.1 5-3.1" fill="none" stroke="#fff" stroke-opacity=".6" \
stroke-width="2.1" stroke-linecap="round"/></svg>"""


def mark_svg(size: int = 30, uid: str = "a") -> str:
    """Inline SVG for the logo mark only (no wordmark)."""
    return _MARK_SVG.format(size=size, uid=uid, name=BRAND_NAME)


def brand_block(uid: str = "a", size: int = 30) -> str:
    """Inline SVG mark + stacked wordmark. Inherits colour from context."""
    first, _, rest = BRAND_NAME.partition(" ")
    return (
        f'<span class="brand-lockup">{mark_svg(size, uid)}'
        f'<span class="brand-words">'
        f'<span class="brand-word-1">{first}</span>'
        f'<span class="brand-word-2">{rest.upper()}</span>'
        f"</span></span>"
    )


BRAND_CSS = """
.brand-lockup { display:inline-flex; align-items:center; gap:9px; }
.brand-lockup .brand-mark { display:block; flex:none; }
.brand-lockup .brand-words { display:flex; flex-direction:column; line-height:1.12; }
.brand-lockup .brand-word-1 { font-size:14px; font-weight:700; letter-spacing:.1px; }
.brand-lockup .brand-word-2 { font-size:9.5px; font-weight:500; letter-spacing:1.4px; opacity:.7; }
"""
