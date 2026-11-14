"""t_e50e3a51: block path regressions from recorded gate.db incidents (14 Nov 2026 read).

1. unsupported_inaction fired on factual / third-party negation (event ids cited).
2. max_blocks counted over a session's lifetime: a long-lived DM blocked twice on
   27 Sep and then failed open on every later turn (61 fail_opens).
3. Every escalation came back escalation_unavailable, and that undelivered attempt
   then suppressed every later escalation with the same reason in the chain.
"""
import json

import pytest


def module(name):
    from tests.completion_gate_support import module as load
    return load(name)


# Sentences from recorded unsupported_inaction events (generalist, 1-14 Nov 2026).
# Each is status, product information or a reasoned no-op, not the agent's own inaction.
FALSE_POSITIVES = [
    (11025, "- **Why it can't be live yet:** the gateway has run since 5 Oct and only loads tools when it starts."),
    (11050, "- **The watcher is paused overnight,** so it can't message Lars. A one-off job switches it back on at 08:00."),
    (11992, "1️⃣ **Emma can't read your calendar yet:** your approval is needed. The code I sent you expired unused at 17:59."),
    (11992, "- 💡 **Recommended:** approve read-only calendar access. It can't write or send anything."),
    (11992, "- ⚠️ **If you don't:** Emma can't check your availability, and she can't confirm the booking."),
    (11992, "This digest is logged on t_89d2618c (comment 6394) so it won't be repeated."),
    (12230, "- **Why it's blocked:** only Chat can edit its memory mirror, so Hermes can't do the merge."),
    (12314, "- **Why it got stuck:** Gabriel can't reassign cards because the reassign tool (t_d5af70db) isn't live yet."),
    (12388, "The main downside: unlike hyaluronic acid fillers, it can't simply be dissolved."),
    (12391, "- ⚠️ **Downside:** it can't be dissolved, and small lumps under the skin can occasionally form."),
    (12673, "❌ The calendar access didn't go through, but not because of anything you did."),
    (12673, "The check will also try again on its own in future, so a Microsoft outage can't waste your approval."),
    (12681, "- 🔒 **Read-only:** she can't send email or change the calendar from this account."),
    (12798, "These show token use, not the % meter on the Claude or ChatGPT subscriptions, which I can't see."),
    (12826, "I still owe Lars the one-line free-space update I promised at 07:19, but it can't go out until 08:00 (no messages at night). It's on card t_8107f15b."),
    (12881, "The likeliest suspect is a Time Machine local snapshot from 22:46, but I can't measure it without deleting it."),
    (12898, "- **Why agents can't do it:** restarting needs his password, and Hermes blocks reboot commands from agents outright."),
    (12915, "That alert is real: last night’s encrypted backup of Claudia 2 didn’t run."),
    (12917, "⚠️ **Tonight's backup is still at risk:** Claudia 2 has only 3.9 GB free, and the backup won't start below 2 GB."),
    (12918, "- 🌙 **If you don't:** tonight's 03:30 backup probably won't run."),
    (12918, "unattended runs can't read #hermes-bridge history, so decisions relayed through Chat aren't checked."),
]

# First-person inaction about an obligation, and the fixed no-third-state phrases.
TRUE_POSITIVES = [
    (12893, "I did not take the action because agents can't restart the Mac."),
    (None, "I could not finish the migration."),
    (None, "I can't send it until Gurney passes."),
    (None, "I won’t deploy this tonight."),
    (None, "I couldn't reclaim the card."),
    (None, "I was unable to restart the worker."),
    (None, "I still can't proceed without the token."),
    (None, "There is nothing I can do between messages."),
    (None, "No - not between messages."),
    (None, "The board says running but no worker is executing."),
    (None, "This needs a permitted approval path."),
]


@pytest.mark.parametrize("event_id,sentence", FALSE_POSITIVES)
def test_recorded_factual_negation_is_not_inaction(event_id, sentence):
    receipts = module("receipts")
    assert receipts.assess_inaction(sentence, []) is None, event_id
    assert receipts.inaction_match(sentence) is None, event_id


@pytest.mark.parametrize("event_id,sentence", TRUE_POSITIVES)
def test_first_person_inaction_still_fails_without_action(event_id, sentence):
    receipts = module("receipts")
    assert receipts.assess_inaction(sentence, [])["subtype"] == "unsupported_inaction"
    assert receipts.inaction_match(sentence) is not None, sentence


def _file_claim(path):
    return {"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(path)}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_long_lived_session_regains_its_block_budget(tmp_path, monkeypatch):
    """DM 20260924_064429_1e3e717f: 2 blocks on 27 Sep, then fail_open for weeks."""
    gate = module("gate")
    clock = Clock(1790500000.0)  # 27 Sep 2026
    monkeypatch.setattr(gate.time, "time", clock)
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 2}
    sent = []

    def run(ref, turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / ref)], escalate=sent.append)
        return g.evaluate("answer", profile="generalist", task_id="20260924_064429_1e3e717f", turn_id=turn)

    assert run("a", "t1")["action"] == "block"
    assert run("b", "t2")["action"] == "block"
    assert run("c", "t3") is None  # ceiling inside the window still fails open
    assert sent[-1]["reason"] == "retry_ceiling"
    clock.now += 14 * 86400  # a later conversation in the same DM
    result = run("d", "t4")
    assert result is not None and result["action"] == "block"
    # The same missing artefact weeks later is a fresh failure, not a repeat.
    clock.now += 14 * 86400
    assert run("a", "t5")["action"] == "block"


def test_block_window_is_configurable_and_bounded(tmp_path, monkeypatch):
    gate = module("gate")
    clock = Clock(1_000_000.0)
    monkeypatch.setattr(gate.time, "time", clock)
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 1,
                "block_window_seconds": 600}

    def run(ref, turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / ref)])
        return g.evaluate("answer", profile="p", task_id="chain", turn_id=turn)

    assert run("a", "1")["action"] == "block"
    clock.now += 599
    assert run("b", "2") is None
    clock.now += 2
    assert run("c", "3")["action"] == "block"
    for bad in (0, -5, "x", float("nan"), None):
        assert gate.block_window({"block_window_seconds": bad}) == gate.DEFAULT_BLOCK_WINDOW_SECONDS


def test_same_turn_rework_after_window_is_still_reentrant(tmp_path, monkeypatch):
    gate = module("gate")
    clock = Clock(1_000_000.0)
    monkeypatch.setattr(gate.time, "time", clock)
    g = gate.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")},
                  extract=lambda _: [_file_claim(tmp_path / "x")])
    assert g.evaluate("a", profile="p", task_id="c", turn_id="one")["action"] == "block"
    clock.now += 10 * 86400
    assert g.evaluate("a", profile="p", task_id="c", turn_id="one") is None
    assert g.events()[-1]["action"] == "reentrant_pass"


def test_undelivered_escalation_does_not_suppress_the_next(tmp_path):
    """Event 4141: escalation_unavailable, after which no repeat was ever attempted."""
    gate = module("gate")
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 0}
    statuses = iter(["unavailable", "sent", "sent"])
    sent = []

    def escalate(event):
        sent.append(event)
        return {"status": next(statuses)}

    def run(ref, turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / ref)], escalate=escalate)
        g.evaluate("answer", profile="generalist", task_id="dm", turn_id=turn)
        return g

    run("a", "1")
    run("b", "2")  # previous attempt provably undelivered: try again
    g = run("c", "3")  # previous attempt sent: at most once per reason in the window
    assert [e["reason"] for e in sent] == ["retry_ceiling", "retry_ceiling"]
    assert [r["action"] for r in g.events() if r["action"].startswith("escalation_")] == [
        "escalation_pending", "escalation_unavailable", "escalation_pending", "escalation_sent"]


@pytest.mark.parametrize("status", ["failed", "unverified", None])
def test_possibly_delivered_escalation_stays_at_most_once(tmp_path, status):
    gate = module("gate")
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 0}
    sent = []

    def escalate(event):
        sent.append(event)
        if status is None:
            raise TimeoutError()  # pending with no status row: may have been sent
        return {"status": status}

    for turn in ("1", "2"):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / turn)], escalate=escalate)
        g.evaluate("answer", profile="cody", task_id="c", turn_id=turn)
    assert len(sent) == 1


def test_escalation_dedup_expires_with_the_window(tmp_path, monkeypatch):
    gate = module("gate")
    clock = Clock(1_000_000.0)
    monkeypatch.setattr(gate.time, "time", clock)
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db"), "max_blocks": 0}
    sent = []

    def run(turn):
        g = gate.Gate(settings, extract=lambda _: [_file_claim(tmp_path / turn)],
                      escalate=lambda e: sent.append(e) or {"status": "sent"})
        g.evaluate("answer", profile="cody", task_id="c", turn_id=turn)

    run("1")
    run("2")
    clock.now += gate.DEFAULT_BLOCK_WINDOW_SECONDS + 1
    run("3")
    assert len(sent) == 2


def test_escalation_receipt_is_written_in_the_target_profile(tmp_path, monkeypatch):
    """Incident 3 end-to-end in-process: the generalist chain reaches #gurney-bridge."""
    esc = module("escalation")
    from hermes_cli import profiles
    import tools.send_message_tool as smt
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: tmp_path / name)
    monkeypatch.setattr("hermes_cli.send_cmd._load_hermes_env", lambda: None)
    posted = []
    monkeypatch.setattr(smt, "send_message_tool", lambda args: posted.append(args) or json.dumps(
        {"success": True, "platform": "slack", "chat_id": "C0BTH5ZQH61", "message_id": "9.1"}))
    event = {"event_id": 4139, "profile": "generalist", "target": "gurney",
             "task_id": "20260924_064429_1e3e717f", "reason": "retry_ceiling", "block_count": 2}
    result = esc.send(event)
    assert result["status"] == "sent" and posted[0]["target"] == "slack:C0BTH5ZQH61"
    assert esc.read_receipt(tmp_path / "gurney", result["delivery_id"])["message_id"] == "9.1"
