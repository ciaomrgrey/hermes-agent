"""Summarize existing history and configured provider targets without copying ledgers."""
import json
from pathlib import Path
from datetime import datetime, timezone
import yaml
from hermes_constants import get_default_hermes_root

root = get_default_hermes_root()
path = root/'profiles/gurney/cron/burn-rate-watch/state.json'
state = json.loads(path.read_text())
history = state.get('history', [])
at = sorted(row['at'] for row in history)
gaps = [b-a for a,b in zip(at, at[1:])]
weekly = [(row['at'], row.get('quota', {}).get('anthropic/seven_day')) for row in history]
weekly = [(t, v) for t,v in weekly if v is not None]
targets = {}
for home in sorted((root/'profiles').iterdir()):
    if (home/'config.yaml').is_file():
        cfg = yaml.safe_load((home/'config.yaml').read_text()) or {}
        provider = (cfg.get('model') or {}).get('provider')
        targets.setdefault(str(provider), []).append(home.name)
out = dict(
    inspected_at=datetime.now(timezone.utc).isoformat(), source=str(path),
    history_samples=len(history), anthropic_weekly_samples=len(weekly),
    first=datetime.fromtimestamp(at[0], timezone.utc).isoformat() if at else None,
    last=datetime.fromtimestamp(at[-1], timezone.utc).isoformat() if at else None,
    minimum_gap_seconds=min(gaps) if gaps else None, maximum_gap_seconds=max(gaps) if gaps else None,
    retrospective_60second_coverage=False,
    import_decision='Not imported: old history lacks grant identity and Codex weekly duration; sparse timestamps cannot establish sampled rolling six-hour maxima.',
    configured_provider_targets=targets,
)
print(json.dumps(out, indent=2))
