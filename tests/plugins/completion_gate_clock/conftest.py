"""R2 single-clock suite: sibling imports (test_metrics) resolve from this directory."""
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
