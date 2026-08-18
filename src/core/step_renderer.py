"""Render YAML step templates against a test's JSON data file.

Templates use curly-brace placeholders like ``{product.bandwidth}``.
Both dotted paths (``a.b.c``) and list indexing (``addresses[0].street``)
are supported. Missing paths are left verbatim in the output so mistakes
are visible rather than silently producing "None".

This single source of truth is used by:
  * ``src/web/routes/generated_tests.py`` — to render step text for the
    dashboard info popup.
  * The generated Playwright test files — to render step text for the
    HTML report's step titles (each generated test inlines a small copy
    so it stays decoupled from framework modules; keep them in sync).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List

# A token is either an identifier (dict key / attribute) or a
# bracketed integer index (list index). Together they describe a path
# like ``addresses[0].street``.
_TOKEN_RE = re.compile(r"[A-Za-z_][\w]*|\[\d+\]")
# A placeholder is any run of characters between a single pair of
# curly braces that doesn't itself contain curly braces.
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def _lookup(path: str, data: Any) -> Any:
    """Walk ``path`` (e.g. ``product.bandwidth`` or ``addresses[0].street``)
    against ``data``. Raises ``KeyError`` / ``IndexError`` / ``AttributeError``
    if the path can't be resolved."""
    parts = _TOKEN_RE.findall(path.strip())
    if not parts:
        raise KeyError(path)
    cur: Any = data
    for p in parts:
        if p.startswith("["):
            cur = cur[int(p[1:-1])]
        else:
            if isinstance(cur, dict):
                cur = cur[p]
            else:
                cur = getattr(cur, p)
    return cur


def render_step(template: str, data: Any) -> str:
    """Substitute ``{path}`` placeholders in ``template`` with values from
    ``data``. Unresolvable placeholders are left intact so the problem is
    visible in reports / the dashboard."""
    if not template:
        return template

    def _sub(match: re.Match) -> str:
        path = match.group(1)
        try:
            return str(_lookup(path, data))
        except (KeyError, IndexError, TypeError, AttributeError):
            return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


def render_steps(templates: Iterable[str], data: Any) -> List[str]:
    """Render each template in ``templates`` against ``data``."""
    return [render_step(t, data) for t in (templates or [])]
