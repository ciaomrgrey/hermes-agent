#!/usr/bin/env python3
"""Run immediately after every update attempt; never restarts or fabricates activity."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('gate_health', Path(__file__).with_name('completion-gate-health.py'))
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    try:
        homes = [p for p in sorted((args.root / 'profiles').iterdir()) if (p / 'config.yaml').is_file()]
        result = health.post_update_check(homes, args.root / 'state/completion-gate/gate.db')
    except Exception as exc:
        result = {'ok': False, 'status': 'FAILED', 'reasons': ['check_error:' + type(exc).__name__]}
    print(json.dumps(result, sort_keys=True), flush=True)
    if not result['ok'] and args.report:
        from hermes_cli.profiles import get_profile_dir
        from tools.environments.local import served_profile_child_env
        env = served_profile_child_env(target_home=get_profile_dir('generalist'), inherit_credentials=True)
        receipt = subprocess.run([sys.executable, '-m', 'hermes_cli.main', '-p', 'generalist',
            'send', '-t', 'slack:C0BTEFMAAJX', '--json', '-f', '-'],
            input='Hermes update FAILED completion-gate check: ' + ', '.join(result['reasons']),
            text=True, capture_output=True, timeout=30, env=env)
        print(receipt.stdout, flush=True)
        if receipt.returncode:
            print('FAILED report delivery; preserve this local receipt.', file=sys.stderr)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
