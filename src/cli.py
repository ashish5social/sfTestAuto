"""CLI for sfauto.

Commands:
  test       - Run one or more Playwright test files via pytest
  generate   - Generate Playwright test scripts from YAML
  list       - List all test definitions
  history    - Show run history
  server     - Start the web dashboard
  doctor     - Preflight: verify env, deps, browsers and org connectivity
  new        - Scaffold a new test (UI or API) from the reference template
  profiles   - List available org profiles
"""

import argparse
import subprocess
import sys
from pathlib import Path

from src.core.config import config


def main():
    parser = argparse.ArgumentParser(
        description="sfauto CLI",
        epilog="Examples:\n"
               "  sfauto doctor                          # check setup before anything else\n"
               "  sfauto new my_case --ui                # scaffold tests/ui/test_my_case.py\n"
               "  sfauto test tests/ui/test_create_account.py\n"
               "  sfauto test tests/api/                  # run all API tests\n"
               "  sfauto test tests/ --headless           # run everything headless\n"
               "  sfauto server\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # test command (run Playwright tests via pytest)
    test_parser = subparsers.add_parser("test", help="Run Playwright test(s) via pytest")
    test_parser.add_argument(
        "path",
        nargs="?",
        default="tests/",
        help="Path to test file or directory (default: tests/, which "
             "recursively picks up tests/ui/ and tests/api/)",
    )
    test_parser.add_argument(
        "--headless", action="store_true",
        help="Run in headless mode (no visible browser). Default: headed",
    )

    # generate command
    gen_parser = subparsers.add_parser("generate", help="Generate Playwright test script from YAML or text")
    gen_parser.add_argument("test", help="Path to YAML test file or plain-text test description")
    gen_parser.add_argument("--output", "-o", help="Output directory (default: tests/ui/ or tests/api/ based on YAML 'type')")
    gen_parser.add_argument("--name", help="Test name (for plain-text input)")

    # generate-all command
    gen_all_parser = subparsers.add_parser("generate-all", help="Generate Playwright scripts for all YAML definitions")
    gen_all_parser.add_argument("--output", "-o", help="Output directory (default: tests/ui/ or tests/api/ based on YAML 'type')")

    # list command
    subparsers.add_parser("list", help="List all test definitions")

    # history command
    hist_parser = subparsers.add_parser("history", help="Show run history")
    hist_parser.add_argument("--limit", type=int, default=20)
    hist_parser.add_argument("--status", help="Filter by status")

    # server command
    subparsers.add_parser("server", help="Start the web dashboard")

    # doctor — preflight checks
    subparsers.add_parser("doctor", help="Verify environment, deps, browsers, org connectivity")

    # profiles — list org profiles
    subparsers.add_parser("profiles", help="List available org profiles")

    # new — scaffold a test from the reference template
    new_parser = subparsers.add_parser("new", help="Scaffold a new test file + data JSON")
    new_parser.add_argument("name", help="Snake_case test name, e.g. create_opportunity")
    grp = new_parser.add_mutually_exclusive_group()
    grp.add_argument("--ui", action="store_true", help="Scaffold a UI test (default)")
    grp.add_argument("--api", action="store_true", help="Scaffold an API test")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # ── test command ──
    if args.command == "test":
        test_path = Path(args.path)
        if not test_path.exists():
            print(f"Error: Path not found: {test_path}")
            sys.exit(1)

        cmd = [
            sys.executable, "-m", "pytest",
            str(test_path),
            "-v", "-s",
            "--tb=short",
        ]
        if args.headless:
            cmd.append("--headless")

        print(f"\nRunning: pytest {test_path} {'(headless)' if args.headless else '(headed)'}")
        print("=" * 60)
        result = subprocess.run(cmd)
        sys.exit(result.returncode)

    # ── server command ──
    if args.command == "server":
        from src.web.app import start
        start()
        return

    # ── list command ──
    if args.command == "list":
        from src.core.test_runner import TestRunner
        runner = TestRunner()
        tests = runner.list_tests()
        if not tests:
            print("No test definitions found.")
            return
        print(f"\n{'Name':<40} {'Steps':<8} {'Tags'}")
        print("-" * 70)
        for t in tests:
            tags = ", ".join(t.get("tags", []))
            steps = len(t.get("steps", []))
            print(f"{t['name']:<40} {steps:<8} {tags}")
        print()
        return

    # ── history command ──
    if args.command == "history":
        from src.core.database import Database
        db = Database()
        runs = db.get_runs(limit=args.limit, status=args.status)
        if not runs:
            print("No runs found.")
            return
        print(f"\n{'Run ID':<30} {'Test':<30} {'Status':<10} {'Duration'}")
        print("-" * 85)
        for r in runs:
            print(f"{r['run_id']:<30} {r['test_name']:<30} {r['status']:<10} {r.get('duration', '-')}s")
        print()
        return

    # ── generate command ──
    if args.command == "profiles":
        from src.core.org_profile import available_profiles, load_profile
        names = available_profiles()
        if not names:
            print("No profiles found in profiles/")
            return
        active = load_profile().name
        print("Org profiles:")
        for n in names:
            p = load_profile(n)
            mark = "*" if n == active else " "
            print(f"  {mark} {n:<16} tz={p.timezone:<20} ns={p.namespace or '-':<14} prefix={p.record_prefix}")
        print("\n  * = active (set SFAUTO_PROFILE to change)")
        return

    if args.command == "doctor":
        sys.exit(_doctor())

    if args.command == "new":
        sys.exit(_scaffold(args.name, api=args.api))

    if args.command == "generate":
        from src.core.playwright_generator import PlaywrightGenerator
        test_path = Path(args.test)
        generator = PlaywrightGenerator()
        output_dir = Path(args.output) if args.output else config.PROJECT_ROOT / "tests" / "generated"
        output_dir.mkdir(parents=True, exist_ok=True)

        if test_path.exists() and test_path.suffix in (".yaml", ".yml"):
            script = generator.generate_from_yaml(test_path)
            out_name = f"test_{test_path.stem}.py"
        else:
            script = generator.generate_from_text(args.test, name=args.name)
            safe = args.name.lower().replace(" ", "_") if args.name else "adhoc"
            out_name = f"test_{safe}.py"

        out_path = output_dir / out_name
        out_path.write_text(script)
        print(f"\nGenerated: {out_path}")
        print(f"Run with:  sfauto test {out_path}")
        print()
        return

    # ── generate-all command ──
    if args.command == "generate-all":
        from src.core.playwright_generator import generate_all_tests
        output_dir = args.output if args.output else str(config.PROJECT_ROOT / "tests" / "generated")
        paths = generate_all_tests(output_dir)
        print(f"\nGenerated {len(paths)} test scripts:")
        for p in paths:
            print(f"  {p}")
        print(f"\nRun all:  sfauto test {output_dir}/")
        print()
        return



# ── doctor ─────────────────────────────────────────────────────────────

def _doctor() -> int:
    """Preflight check. Returns a shell exit code (0 = all good)."""
    import importlib, os, shutil

    ok, warn, fail = "  \033[32m✔\033[0m", "  \033[33m!\033[0m", "  \033[31m✘\033[0m"
    problems = 0

    print("\nsfauto doctor\n" + "─" * 52)

    # 1. Python
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        print(f"{ok} Python {v.major}.{v.minor}.{v.micro}")
    else:
        print(f"{fail} Python {v.major}.{v.minor} — need >= 3.11"); problems += 1

    # 2. Dependencies
    for mod, pkg in (("playwright", "playwright"), ("pytest", "pytest"),
                     ("fastapi", "fastapi"), ("yaml", "pyyaml"),
                     ("simple_salesforce", "simple-salesforce"), ("dotenv", "python-dotenv")):
        try:
            importlib.import_module(mod); print(f"{ok} {pkg}")
        except ImportError:
            print(f"{fail} {pkg} missing — run: pip install -e ."); problems += 1

    # 3. Playwright browsers
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            if path and os.path.exists(path):
                print(f"{ok} Chromium installed")
            else:
                print(f"{fail} Chromium missing — run: playwright install chromium"); problems += 1
    except Exception as e:
        print(f"{fail} Playwright check failed: {e}"); problems += 1

    # 4. Org profile
    try:
        from src.core.org_profile import load_profile
        p = load_profile()
        print(f"{ok} Profile '{p.name}' (tz={p.timezone}, ns={p.namespace or 'none'})")
    except Exception as e:
        print(f"{fail} Profile load failed: {e}"); problems += 1

    # 5. Credentials
    missing = config.validate()
    if missing:
        print(f"{fail} Credentials: {', '.join(missing)}")
        print("       Create a .env from .env.example"); problems += 1
    else:
        print(f"{ok} Credentials present (SF_USERNAME, SF_PASSWORD)")

    # 6. Live org connectivity (only if creds exist)
    if not missing:
        try:
            from src.api.sf_api_client import SFApiClient
            c = SFApiClient(); c.connect()
            print(f"{ok} Connected to org as user {c.current_user_id}")
        except Exception as e:
            print(f"{warn} Could not reach org: {str(e)[:90]}")
            print("       (credentials/URL/token or IP restrictions)")

    print("─" * 52)
    print("All checks passed.\n" if problems == 0
          else f"{problems} problem(s) found.\n")
    return 0 if problems == 0 else 1


# ── scaffold ───────────────────────────────────────────────────────────

def _scaffold(name: str, *, api: bool = False) -> int:
    """Copy the reference test + data JSON under a new name."""
    import re, shutil
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if not slug:
        print("Invalid name."); return 1

    kind = "api" if api else "ui"
    src_test = Path(f"tests/{kind}/") / ("test_account_api.py" if api else "test_create_account.py")
    src_data = Path(f"tests/{kind}/data/") / ("account_api.json" if api else "create_account.json")
    if not src_test.exists():
        print(f"Reference template missing: {src_test}"); return 1

    dst_test = Path(f"tests/{kind}/test_{slug}.py")
    dst_data = Path(f"tests/{kind}/data/{slug}.json")
    if dst_test.exists():
        print(f"Already exists: {dst_test}"); return 1

    cls = "".join(w.capitalize() for w in slug.split("_"))
    body = src_test.read_text()
    body = body.replace("class TestCreateAccount:", f"class Test{cls}:")
    body = body.replace("class TestAccountApi:", f"class Test{cls}:")
    body = body.replace("def test_create_account(self)", f"def test_{slug}(self)")
    body = body.replace("def test_account_crud(self)", f"def test_{slug}(self)")
    body = body.replace('"data" / "create_account.json"', f'"data" / "{slug}.json"')
    body = body.replace('"data" / "account_api.json"', f'"data" / "{slug}.json"')

    dst_data.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_data, dst_data)
    dst_test.write_text(body)

    print(f"\n  Created {dst_test}")
    print(f"  Created {dst_data}")
    print(f"\n  Next:  edit the steps, then run")
    print(f"         sfauto test {dst_test}\n")
    return 0


if __name__ == "__main__":
    main()
