"""CLI for sfauto.

Commands:
  test       - Run one or more Playwright test files via pytest
  generate   - Generate Playwright test scripts from YAML
  list       - List all test definitions
  history    - Show run history
  server     - Start the web dashboard
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
               "  sfauto test tests/ui/test_cci_tc1_create_enterprise_quote_with_dia.py\n"
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


if __name__ == "__main__":
    main()
