"""Evaluated ``gate_outcome`` for the append-only footer seam (t_d184f84f).

Display-only: the outcome names the EVALUATED action (never escalation_*/advice_* bookkeeping),
claim verdict counts, the chain budget ``block_count``, THIS turn's ``turn_blocks``, and the
escalation reason that native gate.py writes only on the escalation_pending row.
"""


def _gate():
    from tests.completion_gate_support import module
    return module('gate')


def _file_claim(path):
    return {"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(path)}


def test_deliver_counts_reproduced_claims(tmp_path):
    gate = _gate()
    target = tmp_path / "r.txt"
    target.write_text("x")
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "g.db")},
                  extract=lambda _: [_file_claim(target)] * 3)
    assert g.evaluate("done", profile="p", task_id="t", turn_id="one") is None
    o = g.last_outcome
    assert o["action"] == "deliver"
    assert o["claims"] == {"reproduced": 3} and o["claim_count"] == 3
    assert o["block_count"] == 0 and o["turn_blocks"] == 0
    assert o["escalation_reason"] is None
    assert o["event_id"] == g.events()[-1]["id"]


def test_fail_open_with_escalation_reports_evaluated_action_and_reason(tmp_path):
    """Native row order is escalation_pending(reason) -> fail_open(no reason) -> escalation_<status>."""
    gate = _gate()
    settings = {"enabled": True, "db_path": str(tmp_path / "g.db"), "max_blocks": 2}
    def run(ref, turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / ref)],
                      escalate=lambda e: {"status": "delivered"})
        g.evaluate("answer", profile="p", task_id="t", turn_id=turn)
        return g
    run("a", "one")
    g = run("a", "two")
    actions = [r["action"] for r in g.events() if r["turn_id"] == "two"]
    assert actions == ["escalation_pending", "fail_open", "escalation_delivered"]
    o = g.last_outcome
    assert o["action"] == "fail_open"
    assert o["escalation_reason"] == "repeat_reason"
    assert o["claims"] == {"failed": 1}
    assert o["event_id"] == next(r["id"] for r in g.events() if r["action"] == "fail_open")
    assert o["turn_blocks"] == 0  # the block belonged to turn "one"


def test_deduped_escalation_still_reports_reason(tmp_path):
    gate = _gate()
    settings = {"enabled": True, "db_path": str(tmp_path / "g.db"), "max_blocks": 0}
    sent = []
    def run(ref, turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / ref)], escalate=sent.append)
        g.evaluate("answer", profile="p", task_id="t", turn_id=turn)
        return g.last_outcome
    assert run("a", "one")["escalation_reason"] == "retry_ceiling"
    second = run("b", "two")
    assert len(sent) == 1  # deduplicated, not re-sent
    assert second["action"] == "fail_open" and second["escalation_reason"] == "retry_ceiling"


def test_same_turn_rework_counts_turn_blocks_and_kinds(tmp_path):
    gate = _gate()
    target = tmp_path / "r.txt"
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "g.db")},
                  extract=lambda _: [_file_claim(target)])
    assert g.evaluate("draft", profile="p", task_id="t", turn_id="one")["action"] == "block"
    assert g.last_outcome["action"] == "block" and g.last_outcome["turn_blocks"] == 0
    target.write_text("x")
    assert g.evaluate("fixed", profile="p", task_id="t", turn_id="one") is None
    o = g.last_outcome
    assert o["action"] == "reentrant_pass"
    assert o["turn_blocks"] == 1 and o["last_block_failed_kinds"] == ["file"]
    assert o["block_count"] == 1


def test_clean_later_turn_is_not_a_rewrite(tmp_path):
    """Gurney round 2: old block + old reentrant + new clean deliver must not read as rewritten."""
    gate = _gate()
    target = tmp_path / "r.txt"
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "g.db")},
                  extract=lambda _: [_file_claim(target)])
    g.evaluate("draft", profile="p", task_id="t", turn_id="one")
    target.write_text("x")
    g.evaluate("fixed", profile="p", task_id="t", turn_id="one")
    g.extract = lambda _: []
    assert g.evaluate("hello", profile="p", task_id="t", turn_id="two") is None
    o = g.last_outcome
    assert o["action"] == "deliver" and o["claims"] == {} and o["claim_count"] == 0
    assert o["block_count"] == 1  # chain budget history is preserved...
    assert o["turn_blocks"] == 0 and o["last_block_failed_kinds"] == []  # ...but not attributed


def test_gate_error_outcome_carries_cause_only(tmp_path):
    gate = _gate()
    def broken(_):
        raise RuntimeError("PRIVATE ANSWER TEXT")
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "g.db")}, extract=broken)
    assert g.evaluate("secret", profile="p", task_id="t", turn_id="one") is None
    o = g.last_outcome
    assert o["action"] == "gate_error" and o["claims"] == {"unverified": 1}
    assert isinstance(o["cause"], str) and "PRIVATE" not in repr(o)


def test_disabled_gate_has_no_outcome(tmp_path):
    gate = _gate()
    g = gate.Gate({"enabled": False, "db_path": str(tmp_path / "g.db")}, extract=lambda _: [])
    assert g.evaluate("x", profile="p", task_id="t", turn_id="one") is None
    assert g.last_outcome is None


def test_outcome_never_contains_claim_text_or_refs(tmp_path):
    gate = _gate()
    secret = tmp_path / "SECRET-PATH.txt"
    claim = {"claim": "SECRET CLAIM TEXT", "artefact_kind": "file", "artefact_ref": str(secret)}
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "g.db")}, extract=lambda _: [claim])
    g.evaluate("a", profile="p", task_id="t", turn_id="one")
    g.evaluate("a", profile="p", task_id="t", turn_id="one")
    text = repr(g.last_outcome)
    assert "SECRET" not in text and "missing" not in text


def test_hook_returns_outcome_without_changing_control_flow():
    from tests.completion_gate_support import load
    pkg = load()
    with_outcome = pkg.with_outcome
    assert with_outcome(None, None) is None
    delivered = with_outcome(None, {"action": "deliver"})
    assert "action" not in delivered and delivered["gate_outcome"] == {"action": "deliver"}
    blocked = with_outcome({"action": "block", "message": "m"}, {"action": "block"})
    assert blocked["action"] == "block" and blocked["message"] == "m"
