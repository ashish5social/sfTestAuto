"""Delete published runs older than a retention window.

Retention counts **today as day 1**:

    keep_days = 0   delete every run, including today's
    keep_days = 1   keep today only
    keep_days = 2   keep today and yesterday
    keep_days = 7   keep today and the previous six days

So the cutoff is ``today - (keep_days - 1)`` and anything dated strictly
before it is removed. "Today" is the *org's* local date, not the
runner's UTC date — GitHub runners are UTC, and for a team in IST that
is a different calendar day for five and a half hours every night, which
would otherwise delete runs made this evening.

Usage:
    python scripts/gh_pages_prune.py <site-root> --keep-days 7 [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _today() -> date:
    try:
        from src.core.org_profile import load_profile
        return datetime.now(load_profile().tz).date()
    except Exception:
        return datetime.now().date()


def _run_date(d: Path) -> date | None:
    """Prefer meta.json's date; fall back to the YYYY-MM-DD dir prefix."""
    meta = d / "meta.json"
    if meta.is_file():
        try:
            raw = json.loads(meta.read_text(encoding="utf-8")).get("date")
            if raw:
                return datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            pass
    try:
        return datetime.strptime(d.name.split("_")[0], "%Y-%m-%d").date()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?", default=".")
    ap.add_argument("--keep-days", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.keep_days < 0:
        print("keep-days must be >= 0", file=sys.stderr)
        return 2

    site = Path(args.site)
    runs_dir = site / "runs"
    if not runs_dir.is_dir():
        print("nothing to prune — no runs/ directory")
        return 0

    today = _today()
    delete_all = args.keep_days == 0
    cutoff = today - timedelta(days=args.keep_days - 1) if not delete_all else None

    if delete_all:
        print(f"keep_days=0 → deleting ALL runs (today is {today})")
    else:
        print(f"keep_days={args.keep_days} → keeping runs dated {cutoff} "
              f"or newer (today is {today})")

    kept = removed = unknown = 0
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        rd = _run_date(d)
        if rd is None:
            # Undatable directories are left alone rather than guessed at;
            # deleting something we cannot identify is the worse error.
            print(f"  ?  {d.name} — no readable date, keeping")
            unknown += 1
            continue
        doomed = delete_all or rd < cutoff
        if doomed:
            print(f"  -  {d.name} ({rd})")
            if not args.dry_run:
                shutil.rmtree(d)
            removed += 1
        else:
            kept += 1

    if delete_all and not args.dry_run and runs_dir.is_dir():
        try:
            next(runs_dir.iterdir())
        except StopIteration:
            runs_dir.rmdir()

    verb = "would remove" if args.dry_run else "removed"
    print(f"\n{verb} {removed} run(s); kept {kept}"
          + (f"; {unknown} undatable left in place" if unknown else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
