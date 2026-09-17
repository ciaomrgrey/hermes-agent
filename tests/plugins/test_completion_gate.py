"""Completion gate invariants, against real temporary artefacts and SQLite."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "completion-gate"


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "completion_gate_test", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and (PLUGIN / "__init__.py").exists(), "completion-gate plugin missing"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return sys.modules[spec.name + ".gate"]


def test_false_file_blocks_then_truthful_rework_delivers(tmp_path):
    gate = load_gate()
    target = tmp_path / "result.txt"
    claims = [{"claim": "Result exists and is nonempty", "artefact_kind": "file", "artefact_ref": str(target)}]
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")}, extract=lambda _: claims)
    first = g.evaluate("Result ready", profile="cody", task_id="task", turn_id="turn")
    assert first["action"] == "block"
    assert "missing" in first["message"]
    target.write_text("actual result")
    assert g.evaluate("Result ready", profile="cody", task_id="task", turn_id="turn") is None
    assert g.evaluate("Result ready", profile="cody", task_id="task", turn_id="next") is None
    rows = g.events()
    assert [r["action"] for r in rows] == ["block", "reentrant_pass", "deliver"]
    assert rows[-1]["claims"][0]["verdict"] == "reproduced"
    assert rows[-1]["block_count"] == 1


def test_persistent_ceiling_repeat_reason_and_error_are_fail_open(tmp_path):
    gate = load_gate()
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 2}
    sent = []
    def run(ref, turn, **kwargs):
        claim = {"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(tmp_path / ref)}
        g = gate.Gate(settings, extract=lambda _: [claim], escalate=lambda event: sent.append(event))
        return g.evaluate("answer", profile="cody", task_id="task", turn_id=turn, **kwargs)
    assert run("a", "one")["action"] == "block"
    assert run("a", "two") is None  # restart/new instance cannot block same reason
    assert sent[-1]["reason"] == "repeat_reason"
    assert run("b", "three")["action"] == "block"
    assert run("c", "four") is None
    assert sent[-1]["reason"] == "retry_ceiling"
    assert sent[-1]["target"] == "generalist"
    g = gate.Gate(settings, extract=lambda _: 1 / 0)
    assert g.evaluate("answer", profile="generalist", task_id="error", turn_id="e") is None
    assert g.events()[-1]["action"] == "gate_error"
    assert g.events()[-1]["claims"][0]["verdict"] == "unverified"
    settings["enabled"] = False
    assert g.evaluate("answer", profile="cody", task_id="disabled", turn_id="d") is None
    assert not any(r["task_id"] == "disabled" for r in g.events())


def test_unknown_claim_passes_and_unverified_rate_is_first_class(tmp_path):
    gate = load_gate()
    claim = {"claim": "A subordinate reported success", "artefact_kind": "agent_report", "artefact_ref": "child-1"}
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")}, extract=lambda _: [claim])
    assert g.evaluate("answer", profile="cody", task_id="task", turn_id="turn") is None
    assert g.events()[-1]["claims"][0]["verdict"] == "unverified"
    assert g.metrics()["unverified_rate"] == 1.0


def test_advice_cycle_escalates_only_to_gurney_and_dedupes(tmp_path):
    gate = load_gate()
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 0}
    sent = []
    claim = {"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(tmp_path / "missing")}
    g = gate.Gate(settings, extract=lambda _: [claim], escalate=lambda e: sent.append(e))
    assert g.evaluate("answer", profile="generalist", task_id="task", turn_id="one") is None
    assert sent[-1]["target"] == "gurney"
    g.evaluate("answer", profile="generalist", task_id="task", turn_id="two")
    assert len(sent) == 1
    g.mark_advice(profile="generalist", task_id="task", marker="advised")
    g.mark_advice(profile="generalist", task_id="task", marker="tried")
    g.evaluate("answer", profile="generalist", task_id="task", turn_id="three")
    assert sent[-1]["reason"] == "advice_failed_again"
    assert sent[-1]["target"] == "gurney"
    g.evaluate("answer", profile="generalist", task_id="task", turn_id="four")
    assert len(sent) == 2


def test_gate_reentrancy_kill_and_concurrent_ceiling(tmp_path):
    import concurrent.futures
    gate = load_gate()
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 2}
    g = gate.Gate(settings, extract=lambda _: [])
    def run(n):
        c = {"claim": f"file {n}", "artefact_kind": "file", "artefact_ref": str(tmp_path / str(n))}
        worker = gate.Gate(settings, extract=lambda _: [c])
        return worker.evaluate("answer", profile="cody", task_id="task", turn_id=str(n))
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run, range(10)))
    assert sum(r is not None for r in results) == 2
    assert g.metrics()["blocks"] == 2


def test_no_secrets_and_normalized_repeat_reason(tmp_path):
    gate = load_gate()
    secret = "PRIVATE_SENTINEL_MUST_NOT_PERSIST"
    claim = {"claim": "FILE READY", "artefact_kind": "file", "artefact_ref": str(tmp_path / secret)}
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db")}
    g = gate.Gate(settings, extract=lambda _: [claim])
    assert g.evaluate(secret, profile="cody", task_id="t", turn_id="one")["action"] == "block"
    claim["claim"] = " file   ready "
    assert g.evaluate(secret, profile="cody", task_id="t", turn_id="two") is None
    assert g.metrics()["blocks"] == 1
    claim.update(artefact_kind=secret, artefact_ref=secret)
    assert g.evaluate(secret, profile="cody", task_id="unknown", turn_id="three") is None
    assert secret.encode() not in (tmp_path / "gate.db").read_bytes()


def test_recursive_callback_cannot_reenter_extraction(tmp_path):
    gate = load_gate()
    calls = []
    g = None
    def extract(_):
        calls.append(True)
        if len(calls) < 3:
            assert g.evaluate("inner", profile="cody", task_id="t", turn_id="turn") is None
        return []
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")}, extract=extract)
    assert g.evaluate("outer", profile="cody", task_id="t", turn_id="turn") is None
    assert len(calls) == 1
