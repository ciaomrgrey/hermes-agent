"""``hermes browser`` subcommand parser."""

from __future__ import annotations

import sys


def build_browser_parser(subparsers) -> None:
    """Attach the ``browser`` subcommand to ``subparsers``."""
    browser_parser = subparsers.add_parser(
        "browser", help="Real-profile browsing helpers (close a browser locking its profile)",
        description="Helpers for real-profile browsing (browser.use_real_profile). "
            "close-profile terminates the browser process tree holding your "
            "default profile so Hermes can copy it — DESTRUCTIVE (unsaved tabs "
            "in that browser are lost). The agent runs this only after you "
            "approve closing the browser.")
    browser_subparsers = browser_parser.add_subparsers(dest="browser_action")
    browser_close = browser_subparsers.add_parser(
        "close-profile",
        help="Close the browser locking your real profile (asks nothing — "
             "run only with the user's explicit OK; loses unsaved tabs)")
    browser_close.add_argument(
        "--browser",
        help="Override detected default browser (chrome/edge/brave/brave-origin/chromium)")
    browser_sweep = browser_subparsers.add_parser(
        "sweep-clones",
        help="macOS: list (or with --apply remove) orphaned Google Chrome code-sign clones "
             "that a killed/crashed Chrome left behind; in-use clones are never touched")
    browser_sweep.add_argument("--apply", action="store_true",
                               help="Remove eligible clones (default: dry run)")
    browser_sweep.add_argument("--min-age-minutes", type=float, default=15.0,
                               help="Preserve clones changed within this many minutes (default 15)")
    browser_sweep.add_argument("--keep", action="append", default=[], metavar="NAME",
                               help="Clone directory name to preserve (repeatable)")
    browser_sweep.add_argument("--only", action="append", default=None, metavar="NAME",
                               help="Restrict to these clone directory names (repeatable)")
    browser_sweep.add_argument("--max-remove", type=int, default=8,
                               help="Per-run removal cap (default 8)")
    browser_sweep.add_argument("--json", action="store_true", help="Print the result as JSON")

    def _dispatch_sweep(_args):
        import json
        from tools.chrome_code_sign_clones import sweep_code_sign_clones
        res = sweep_code_sign_clones(
            apply=bool(_args.apply), min_age_s=max(0.0, _args.min_age_minutes) * 60,
            max_remove=_args.max_remove, keep=_args.keep, only=_args.only)
        if _args.json:
            print(json.dumps(res.as_dict(), indent=2, sort_keys=True))
        else:
            print(f"root: {res.root}")
            for name in (res.removed if res.applied else res.would_remove):
                print(f"  {'removed' if res.applied else 'would remove'}: {name}")
            for name, why in sorted(res.preserved.items()):
                print(f"  preserved: {name} ({why})")
            if res.applied:
                print(f"host free-space delta (statvfs): {res.host_free_delta_bytes} bytes")
        if res.error:
            print(f"✗ {res.error}", file=sys.stderr)
            return 1
        return 0

    def _dispatch_browser(_args):
        from hermes_cli.browser_connect import (
            UNSUPPORTED_CHANNEL, close_browser_holding_profile, detect_default_chromium,
            real_profile_data_dir)

        action = getattr(_args, "browser_action", None)
        if action == "sweep-clones":
            return _dispatch_sweep(_args)
        if action != "close-profile":
            browser_parser.print_help()
            return 2
        browser = getattr(_args, "browser", None) or detect_default_chromium()
        if not browser or browser == UNSUPPORTED_CHANNEL:
            print("✗ No supported Chromium default browser detected.", file=sys.stderr)
            return 1
        src = real_profile_data_dir(browser)
        if not src:
            print(f"✗ Could not resolve the {browser} profile directory.", file=sys.stderr)
            return 1
        closed, msg = close_browser_holding_profile(src)
        if sys.platform == "darwin":
            # close-profile SIGKILLs stragglers, which skips Chrome's own clone cleanup.
            from tools.chrome_code_sign_clones import sweep_code_sign_clones
            sweep_code_sign_clones(apply=True)
        if closed:
            print(f"✓ {msg}")
            return 0
        print(f"✗ {msg}", file=sys.stderr)
        return 1

    browser_parser.set_defaults(func=_dispatch_browser)
