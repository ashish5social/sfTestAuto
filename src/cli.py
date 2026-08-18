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
import os
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
    # server — start / stop / status / restart
    srv = subparsers.add_parser("server", help="Start/stop the web dashboard")
    srv_sub = srv.add_subparsers(dest="server_cmd")
    srv_start = srv_sub.add_parser("start", help="Start the dashboard")
    srv_start.add_argument("-d", "--detach", action="store_true",
                           help="Run in the background and return to the shell")
    srv_start.add_argument("--port", type=int, default=None, help="Override port")
    srv_sub.add_parser("stop", help="Stop a running dashboard")
    srv_sub.add_parser("status", help="Show whether the dashboard is running")
    srv_restart = srv_sub.add_parser("restart", help="Stop then start")
    srv_restart.add_argument("-d", "--detach", action="store_true")
    srv_restart.add_argument("--port", type=int, default=None)

    # doctor — preflight checks
    subparsers.add_parser("doctor", help="Verify environment, deps, browsers, org connectivity")

    # profiles — list org profiles
    subparsers.add_parser("profiles", help="List available org profiles")

    # auth — capture a browser session for SSO/MFA orgs
    auth_parser = subparsers.add_parser("auth", help="Manage saved browser sessions")
    auth_sub = auth_parser.add_subparsers(dest="auth_cmd")
    cap = auth_sub.add_parser("capture", help="Open a browser, log in yourself, save the session")
    cap.add_argument("--out", default=None, help="Where to write the session JSON")
    auth_sub.add_parser("status", help="Show whether a saved session exists and is fresh")

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
        sys.exit(_server(args))

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

    if args.command == "auth":
        sys.exit(_auth(args))

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
    from pathlib import Path

    warnings = 0
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
    # Probe the browser path without starting a driver connection, so we
    # don't emit Playwright's async-teardown warnings into a diagnostic.
    try:
        import glob as _glob
        from pathlib import Path as _P
        cache = _P.home() / "Library/Caches/ms-playwright"          # macOS
        if not cache.exists():
            cache = _P.home() / ".cache/ms-playwright"              # Linux
        headed = _glob.glob(str(cache / "chromium-*"))
        shell  = _glob.glob(str(cache / "chromium_headless_shell-*"))
        if headed:
            print(f"{ok} Chromium installed ({_P(sorted(headed)[-1]).name}, headed-capable)")
        elif shell:
            print(f"{warn} Only the headless shell is installed — headed runs "
                  f"(BROWSER_HEADLESS=false) will fail.")
            print( "       Fix: playwright install chromium")
            warnings += 1
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

    # 6. UI login strategy — the piece people most often have half-configured
    try:
        from src.core.org_profile import load_profile as _lp
        strategy = _lp().ui_login
        key_file = os.getenv("SF_JWT_KEY_FILE", "").strip()
        if strategy == "frontdoor" and not key_file:
            print(f"{warn} ui_login=frontdoor but SF_JWT_KEY_FILE is not set.")
            print( "       Client-credentials tokens carry only the 'api' scope,")
            print( "       which frontdoor.jsp rejects. See docs/AUTHENTICATION.md")
            warnings += 1
        elif key_file and not Path(key_file).exists():
            print(f"{fail} SF_JWT_KEY_FILE points at a missing file: {key_file}")
            problems += 1
        elif key_file:
            print(f"{ok} UI login '{strategy}' via JWT bearer ({key_file})")
        else:
            print(f"{ok} UI login strategy '{strategy}'")
    except Exception as e:
        print(f"{warn} Could not check UI login strategy: {e}"); warnings += 1

    # 7. Live org connectivity (only if creds exist)
    if not missing:
        try:
            from src.api.sf_api_client import SFApiClient
            c = SFApiClient(); c.connect()
            print(f"{ok} Connected to org as user {c.current_user_id} "
                  f"[{c._auth_method}]")
        except Exception as e:
            lines = str(e).splitlines()
            print(f"{warn} Could not reach org — {lines[0]}")
            for ln in lines[1:]:
                if ln.strip():
                    print(f"       {ln}")
            warnings += 1

    print("─" * 52)
    if problems:
        print(f"{problems} problem(s) must be fixed before tests can run.\n")
    elif warnings:
        print("Local setup is fine, but the org is not reachable "
              "— see the warning above.\n")
    else:
        print("All checks passed.\n")
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



# ── auth: capture / status ─────────────────────────────────────────────

def _session_path(explicit=None) -> Path:
    from src.core.config import config as _cfg
    if explicit:
        return Path(explicit)
    return Path(_cfg.PROFILE.ui_login_options.get(
        "storage_state_path", ".auth/storage_state.json"))


def _auth(args) -> int:
    from src.core.config import config as _cfg
    path = _session_path(getattr(args, "out", None))

    if getattr(args, "auth_cmd", None) == "status":
        if not path.exists():
            print(f"\n  No saved session at {path}")
            print("  Create one:  sfauto auth capture\n"); return 1
        import json, time
        age_h = (time.time() - path.stat().st_mtime) / 3600
        n = len(json.loads(path.read_text()).get("cookies", []))
        print(f"\n  Session : {path}")
        print(f"  Cookies : {n}")
        print(f"  Age     : {age_h:.1f}h  {'(likely expired)' if age_h > 12 else '(probably still valid)'}\n")
        return 0

    # capture
    url = _cfg.PROFILE.login_url
    timeout_s = int(os.getenv("SFAUTO_CAPTURE_TIMEOUT", "300"))
    print(f"\n  Opening {url}")
    print("  Log in in the browser window — including any SSO / MFA.")
    print(f"  Capture completes automatically once you reach Lightning "
          f"(waiting up to {timeout_s//60} min).\n")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed"); return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    landed = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(url)
        # Poll for a logged-in Lightning URL instead of blocking on input(),
        # so this works when driven by a tool/agent with no TTY.
        import time as _t
        deadline = _t.time() + timeout_s
        while _t.time() < deadline:
            try:
                u = page.url
            except Exception:
                break
            if ("lightning.force.com" in u or "/lightning/" in u
                    or "salesforce-setup.com" in u):
                landed = u
                break
            _t.sleep(2)
        if not landed:
            print("  ! Timed out before reaching Lightning — saving anyway.")
        else:
            print(f"  ✔ Detected login: {landed[:70]}")
        _t.sleep(3)          # let post-login cookies settle
        ctx.storage_state(path=str(path))
        browser.close()

    import json
    n = len(json.loads(path.read_text()).get("cookies", []))
    print(f"\n  Saved {n} cookies to {path}")
    print("  Set  ui_login: storage_state  in your profile to use it.\n")
    return 0



# ── server lifecycle ───────────────────────────────────────────────────

PID_FILE = Path(".run/server.pid")


def _server_pids(port: int) -> list[int]:
    """PIDs serving this dashboard, found by PID file then by port.

    The PID file is authoritative when present and live; the port scan is
    the fallback for a server someone started by hand with uvicorn.
    """
    import subprocess as sp
    pids: list[int] = []
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)          # raises if the process is gone
            pids.append(pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            PID_FILE.unlink(missing_ok=True)
    try:
        out = sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True).stdout
        pids += [int(x) for x in out.split() if x.isdigit()]
    except FileNotFoundError:
        pass
    return sorted(set(pids))


def _server(args) -> int:
    import os as _os, signal, subprocess as sp, time
    from src.core.config import config as _cfg

    cmd = getattr(args, "server_cmd", None) or "start"   # bare `server` == start
    port = getattr(args, "port", None) or _cfg.DASHBOARD_PORT
    host = _cfg.DASHBOARD_HOST

    if cmd == "status":
        pids = _server_pids(port)
        if pids:
            print(f"\n  ● running on http://{host}:{port}  (pid {', '.join(map(str, pids))})\n")
            return 0
        print(f"\n  ○ not running (port {port} free)\n")
        return 1

    if cmd in ("stop", "restart"):
        pids = _server_pids(port)
        if not pids:
            print(f"  ○ nothing to stop on port {port}")
        else:
            for pid in pids:
                try: _os.kill(pid, signal.SIGTERM)
                except OSError: pass
            time.sleep(2)
            still = _server_pids(port)
            for pid in still:
                try: _os.kill(pid, signal.SIGKILL)
                except OSError: pass
            PID_FILE.unlink(missing_ok=True)
            print(f"  ✔ stopped (pid {', '.join(map(str, pids))})")
        if cmd == "stop":
            return 0
        time.sleep(1)

    # ── start ──────────────────────────────────────────────────────────
    if _server_pids(port):
        print(f"\n  ! already running on port {port}. Use:  sfauto server restart\n")
        return 1

    _cfg.ensure_dirs()

    if getattr(args, "detach", False):
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        log = Path(".run/server.log")
        with open(log, "ab") as fh:
            proc = sp.Popen(
                [sys.executable, "-m", "uvicorn", "src.web.app:app",
                 "--host", host, "--port", str(port)],
                stdout=fh, stderr=fh, start_new_session=True,
            )
        PID_FILE.write_text(str(proc.pid))
        time.sleep(3)
        if proc.poll() is not None:
            print(f"\n  ✘ failed to start — see {log}\n"); return 1
        print(f"\n  ● started  http://{host}:{port}   (pid {proc.pid})")
        print(f"    logs : {log}")
        print(f"    stop : sfauto server stop\n")
        return 0

    # foreground
    print(f"\n{'=' * 58}")
    print(f"  sfauto Test Runner")
    print(f"  http://{host}:{port}")
    print(f"  Ctrl-C to stop")
    print(f"{'=' * 58}\n")
    import uvicorn
    uvicorn.run("src.web.app:app", host=host, port=port, reload=True)
    return 0


if __name__ == "__main__":
    main()
