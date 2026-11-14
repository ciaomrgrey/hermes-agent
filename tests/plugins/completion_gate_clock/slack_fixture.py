"""Temporary estate for the Slack-bridge escalation lane; no real profile home or network."""
import json
import os
from pathlib import Path
import subprocess
import sys

CHILD = Path(__file__).with_name('slack_escalation_child.py')


def estate(root):
    """root/profiles/{generalist,gurney,cody}; generalist owns a fake estate Slack bot."""
    root = Path(root)
    for name in ('generalist', 'gurney', 'cody'):
        (root / 'profiles' / name).mkdir(parents=True, exist_ok=True)
    sender = root / 'profiles/generalist'
    (sender / '.env').write_text('SLACK_BOT_TOKEN=xoxb-offline-fixture\nSLACK_HOME_CHANNEL=C0BTEFMAAJX\n')
    (sender / 'config.yaml').write_text('{}\n')
    return root


def child_env(root):
    """What served_profile_child_env yields for the sender, pinned to the disposable estate."""
    repo = str(Path(__file__).resolve().parents[3])
    return {'PATH': os.environ['PATH'], 'HOME': str(root), 'PYTHONPATH': repo,
            'HERMES_HOME': str(Path(root) / 'profiles/generalist'), 'PYTHONDONTWRITEBYTECODE': '1'}


def requests(root):
    path = Path(root) / 'profiles/generalist/slack-requests.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def receipts(root, target='gurney'):
    folder = Path(root) / 'profiles' / target / 'runtime/completion_gate_escalations'
    return [json.loads(p.read_text()) for p in sorted(folder.glob('*.json'))]


def popen(root, escalation_file, mode):
    """Prewarmed real child (imports done before the gate clock starts)."""
    return subprocess.Popen([sys.executable, str(CHILD), str(escalation_file), mode],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=child_env(root))
