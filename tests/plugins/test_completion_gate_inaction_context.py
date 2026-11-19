"""t_1646bc40: quoted negation and promises to preserve the user's decision.

13989/13824 are the sentences supplied on the card (13824 also survives in
state.db message 115296's reasoning). Rejected full drafts were not saved.
The three incident-derived positives below are explicitly reconstructed controls,
not historical draft replays; the surviving fragments alone do not match the
unchanged obligation-verb vocabulary.
"""
import pytest

from tests.completion_gate_support import module


CASES = [
    pytest.param('The blocked line was "I won\'t change that", which honoured your decision on Sophia and promised nothing.', False, id="13989-quoted"),
    pytest.param("Sophia stays on Fable as you set her, and I won't change that.", False, id="13824-user-decision"),
    pytest.param("I did not finish the trace. I'll find out before asking you a third time.", True, id="13156-reconstructed"),
    pytest.param("I can't switch it. I did not restart the gateway; send /model.", True, id="13820-reconstructed"),
    pytest.param("I could not unblock t_9b1e4c4d.", True, id="13834-reconstructed"),
    pytest.param("find out before asking you a third time", False, id="13156-fragment-not-a-trigger"),
    pytest.param("I can't switch it", False, id="13820-switch-not-in-vocabulary"),
    pytest.param('Reported: “I can’t send it.”', False, id="curly-quotes"),
    pytest.param('Reported: `I could not finish it.`', False, id="backticks"),
    pytest.param('Reported: ```text\nI did not restart it.\n```', False, id="fenced-code"),
    pytest.param('Reported: "I can\'t send it." I could not finish it.', True, id="quote-then-inaction"),
    pytest.param('I could not finish it. Reported: "I can\'t send it."', True, id="inaction-then-quote"),
    pytest.param('Reported: "I can\'t send it.', True, id="unclosed-quote"),
    pytest.param('Reported: `I can\'t send it.', True, id="unclosed-backtick"),
    pytest.param("'I can't send it.'", True, id="single-quotes-not-exempt"),
    pytest.param('"nothing I can do"', True, id="fixed-phrase-unchanged"),
    pytest.param("I won’t change your decision.", False, id="explicit-user-decision"),
    pytest.param("I will not change your choice.", False, id="explicit-user-choice"),
    pytest.param("As you decided, I won't change that.", False, id="anaphoric-decision"),
    pytest.param("I won't change that, as you decided.", False, id="following-decision"),
    pytest.param("I won't change that.", True, id="unbound-that"),
    pytest.param("I can't change your decision.", True, id="inability-not-promise"),
    pytest.param("I did not change your decision.", True, id="past-inaction-not-promise"),
    pytest.param("I won't change the code.", True, id="ordinary-refusal"),
    pytest.param("I won't change your decision until tomorrow.", True, id="conditional-not-preservation"),
    pytest.param("I won't change your decision-making code.", True, id="decision-substring"),
    pytest.param("Your decision is final. I won't change the code.", True, id="unrelated-decision"),
    pytest.param('"As you decided," I won\'t change that.', True, id="quoted-attribution-not-authority"),
    pytest.param("As you decided. I won't change that.", True, id="separate-sentence-not-authority"),
    pytest.param("As you decided, I won't change that. I can't finish the migration.", True, id="preservation-then-inaction"),
    pytest.param("As you decided, I won't deploy that.", True, id="other-verb-not-exempt"),
    pytest.param("I won't touch your decision.", False, id="touch-vocabulary-unchanged"),
    pytest.param("I won't alter your choice.", False, id="alter-vocabulary-unchanged"),
    pytest.param("I won't override your decision.", False, id="override-vocabulary-unchanged"),
    pytest.param("Not as you decided, I won't change that.", True, id="negated-attribution"),
    pytest.param("If it is as you decided, I won't change that.", True, id="conditional-attribution"),
    pytest.param('``example `code` I can\'t send it``', False, id="nested-code-span"),
    pytest.param('``example `code` I can\'t send it`` I cannot finish it.', True, id="nested-code-then-inaction"),
]


@pytest.mark.parametrize("text,blocked", CASES)
def test_context_keeps_only_self_reported_obligation_inaction(text, blocked):
    receipts = module("receipts")
    result = receipts.assess_inaction(text, [])
    assert bool(receipts.inaction_match(text)) is blocked
    if blocked:
        assert result["verdict"] == "failed"
        assert result["subtype"] == "unsupported_inaction"
    else:
        assert result is None


@pytest.mark.parametrize("text,blocked", CASES[:5])
def test_incident_sentences_through_real_gate(tmp_path, monkeypatch, text, blocked):
    import json
    from hermes_cli import plugins
    from tests.completion_gate_support import install, no_directive
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["completion-gate"], "entries": {
            "completion-gate": {"settings": {
                "enabled": True, "inaction_enabled": True,
                "db_path": str(tmp_path / "gate.db"),
            }},
        }},
    }))
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    plugin = manager._plugins["completion-gate"].module
    monkeypatch.setattr(plugin, "bounded_extract", lambda *args, **kwargs: [])
    result = manager.invoke_hook(
        "before_turn_end", final_response=text, messages=[], user_message="",
        session_id="test", turn_id="test-turn",
    )
    assert no_directive(result) is not blocked
    events = plugin.Gate({"db_path": str(tmp_path / "gate.db")}, extract=lambda _: []).events()
    assert events[-1]["action"] != "gate_error"
    if blocked:
        assert events[-1]["claims"][0]["subtype"] == "unsupported_inaction"
