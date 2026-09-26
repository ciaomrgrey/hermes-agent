"""Native extraction-child import provenance after relocation."""
import json
from pathlib import Path
import subprocess


def test_child_keeps_invoking_install_without_cwd_or_pythonpath(tmp_path, monkeypatch):
    import hermes_constants
    from tests.completion_gate_support import module
    extractor = module('extraction')
    monkeypatch.setenv('PYTHONPATH', '')
    probe = tmp_path / 'probe.py'
    probe.write_text('import json, hermes_constants\n'
        'print(json.dumps([{"claim":"origin", "artefact_kind":"file", '
        '"artefact_ref":hermes_constants.__file__}]))\n')
    run = subprocess.run
    def child(command, **kwargs):
        # Execute a real interpreter in an unrelated cwd using exactly the child env.
        return run([command[0], str(probe)], cwd=tmp_path, **kwargs)
    monkeypatch.setattr(extractor.subprocess, 'run', child)
    claims = extractor.bounded_extract('origin', timeout=10)
    assert Path(claims[0]['artefact_ref']).resolve() == Path(hermes_constants.__file__).resolve()
