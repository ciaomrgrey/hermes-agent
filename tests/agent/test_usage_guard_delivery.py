import importlib.util
import json
import pytest


def test_delivery_requires_native_receipt_and_never_accepts_skip(monkeypatch):
    assert importlib.util.find_spec('agent.usage_guard_delivery'), 'native outbox binding missing'
    from agent.usage_guard_delivery import send_notice
    from tools import send_message_tool as native
    from hermes_cli import send_cmd
    monkeypatch.setattr(send_cmd, '_load_hermes_env', lambda: None)
    payloads = []
    def success(payload):
        payloads.append(payload)
        return json.dumps(dict(success=True, message_id='fixture-id'))
    monkeypatch.setattr(native, 'send_message_tool', success)
    cfg = dict(telegram_target='telegram:471605389', timezone='Europe/Zurich')
    notice = json.dumps(dict(id='breach:fixture',provider='anthropic',threshold_pp=10,sampling_seconds=60,
                             window=dict(pp=11,start=1789500000,end=1789521600,coverage='sampled')))
    assert json.loads(send_notice(notice,cfg))['message_id'] == 'fixture-id'
    assert payloads[0]['target'] == cfg['telegram_target']
    assert '11.00 pp' in payloads[0]['message']
    for result in [dict(success=True,skipped=True), dict(success=False,error='failure'), dict(success=True)]:
        monkeypatch.setattr(native, 'send_message_tool', lambda payload: json.dumps(result))
        with pytest.raises(RuntimeError):
            send_notice(notice,cfg)
