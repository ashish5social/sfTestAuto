#!/usr/bin/env python3
"""
Sync the GitHub Actions workflow_dispatch dropdown with actual test files.

Scans tests/ui/ and tests/api/ for test_*.py files and updates the
'options:' block in .github/workflows/run-tests.yml so the "Run workflow"
dropdown always reflects the current set of tests.

The generated dropdown starts with the static convenience options:
  - all          (everything under tests/ui + tests/api)
  - ui-only      (everything under tests/ui)
  - api-only     (everything under tests/api)

…followed by every test as a repo-relative path, alphabetically. Paths
rather than bare filenames: the workflow's resolve step feeds the value
straight to pytest, and a path is unambiguous when tests/ui and
tests/api both contain a file of the same name.

Usage:
  python scripts/sync_test_dropdown.py          # from project root
  python scripts/sync_test_dropdown.py --check  # exit 1 if out of sync (for CI)
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
TEST_SUBFOLDERS = ("ui", "api")
WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "run-tests.yml"

# Static options that bracket the generated list. These strings must match
# the case labels in run-tests.yml's "Resolve test selection" step.
STATIC_OPTIONS = ["All tests", "All UI tests", "All API tests"]
TRAILING_OPTIONS = ["Custom (fill in custom_target below)"]


def get_test_files() -> list[str]:
    """Return sorted repo-relative paths of every test_*.py under
    tests/ui/ and tests/api/."""
    seen: list[str] = []
    for sub in TEST_SUBFOLDERS:
        d = TESTS_DIR / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("test_*.py")):
            rel = f.relative_to(PROJECT_ROOT).as_posix()
            if rel not in seen:
                seen.append(rel)
    return sorted(seen)


def build_options_block(test_files: list[str], indent: str = "          ") -> str:
    """Build the YAML options block: static options + each test file."""
    # Quoted: several options contain spaces and parentheses, which YAML
    # would otherwise be free to interpret.
    lines = [f'{indent}- "{opt}"' for opt in STATIC_OPTIONS]
    lines += [f'{indent}- "{f}"' for f in test_files]
    lines += [f'{indent}- "{opt}"' for opt in TRAILING_OPTIONS]
    return "\n".join(lines)


def update_workflow(check_only: bool = False) -> bool:
    """
    Update the options: block in the workflow file.
    Returns True if the file was changed (or would be changed in check mode).
    """
    test_files = get_test_files()
    if not test_files:
        print("WARNING: No test files found under tests/ui/ or tests/api/")
        return False

    content = WORKFLOW_FILE.read_text()

    # Match the FIRST options: block under workflow_dispatch > inputs >
    # tests. There may be other options: blocks later in the file
    # (e.g. the workers selector), so we anchor on the first one only.
    pattern = r"(        options:\n)((?:          - .+\n)+)"
    match = re.search(pattern, content)

    if not match:
        print("ERROR: Could not find 'options:' block in workflow file")
        sys.exit(1)

    new_options = build_options_block(test_files) + "\n"
    current_options = match.group(2)

    if current_options == new_options:
        print(f"Dropdown is in sync ({len(STATIC_OPTIONS) + len(TRAILING_OPTIONS)} static + {len(test_files)} tests)")
        return False

    if check_only:
        print("Dropdown is OUT OF SYNC")
        print(f"  Current:  {[l.strip('- ').strip() for l in current_options.strip().splitlines()]}")
        print(f"  Expected: {STATIC_OPTIONS + test_files + TRAILING_OPTIONS}")
        return True

    updated = content[:match.start(2)] + new_options + content[match.end(2):]
    WORKFLOW_FILE.write_text(updated)
    print(f"Updated dropdown: {STATIC_OPTIONS + test_files + TRAILING_OPTIONS}")
    return True


if __name__ == "__main__":
    check_only = "--check" in sys.argv
    changed = update_workflow(check_only=check_only)
    if check_only and changed:
        sys.exit(1)
