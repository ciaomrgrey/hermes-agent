"""Requested attachments must all succeed; receipts make partial sends recoverable."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.slack import adapter


@pytest.fixture
def client(monkeypatch):
    import sys
    from types import ModuleType

    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ok": True, "ts": "111.222"}),
        files_upload_v2=AsyncMock(return_value={"ok": False, "error": "missing_scope"}),
    )
    # The SDK is optional; stub its transport entry point, never the sender.
    async_client = ModuleType("slack_sdk.web.async_client")
    async_client.AsyncWebClient = lambda **kw: client
    monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", async_client)
    return client


@pytest.mark.parametrize("case", [
    "denied", "missing", "mixed", "media-only", "caption", "caption-failed",
    "text-failed", "exception", "complete", "benign-warning",
])
def test_attachment_receipts(client, tmp_path, caplog, case):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
    missing = str(tmp_path / "missing.pdf")
    good = {"ok": True, "files": [{"id": "F123"}]}
    bad = {"ok": False, "error": "missing_scope"}
    files = [(str(media), False)]
    text, caption = "Report", None
    if case in {"missing", "caption", "caption-failed"}:
        files = [(missing, False)]
    if case == "mixed":
        files *= 2
        client.files_upload_v2.side_effect = [good, bad]
    if case in {"media-only", "caption", "caption-failed"}:
        text = ""
    if case in {"caption", "caption-failed"}:
        caption = "Report"
    if case in {"text-failed", "caption-failed"}:
        client.chat_postMessage.return_value = {"ok": False, "error": "channel_not_found"}
    if case == "exception":
        client.files_upload_v2.side_effect = RuntimeError("upload failed https://example.test?token=private-fixture-value")
    if case in {"complete", "benign-warning"}:
        client.files_upload_v2.return_value = good
        client.chat_postMessage.return_value = {"ok": True, "ts": "111.222", "warning": "missing_charset"}
    result = asyncio.run(adapter._standalone_send(
        SimpleNamespace(token="xoxb-test", extra={}), "C012AB3CD", text,
        media_files=files, caption=caption,
    ))
    if case == "text-failed":
        assert "channel_not_found" in result["error"]
        client.files_upload_v2.assert_not_awaited()
        return
    success = case in {"complete", "benign-warning"}
    assert result["success"] is success
    assert len(result["delivered_attachments"]) == (1 if success or case == "mixed" else 0)
    assert len(result["failed_attachments"]) == (0 if success else 1)
    if success or case == "mixed":
        assert result["delivered_attachments"][0]["file_ids"] == ["F123"]
    if not success:
        assert result["error_code"] == ("delivery_failed" if case in {"media-only", "caption-failed"} else "partial_delivery")
    if case not in {"media-only", "caption-failed"}:
        assert result["message_id"] == "111.222"
    assert "private-fixture-value" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("mode", ["--json", "--quiet", "human"])
@pytest.mark.parametrize("case", ["denied", "mixed", "complete", "missing"])
def test_cli_uses_real_sender(client, tmp_path, monkeypatch, capsys, mode, case):
    import argparse
    from hermes_cli.send_cmd import register_send_subparser
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("platforms:\n  slack:\n    enabled: true\n    token: xoxb-test\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    discover_plugins()
    from pathlib import Path
    entry = platform_registry.get("slack")
    assert entry is not None
    assert Path(entry.standalone_sender_fn.__code__.co_filename).resolve() == Path(adapter.__file__).resolve()
    media = tmp_path / "report.pdf"
    if case != "missing":
        media.write_bytes(b"%PDF-1.4 test")
    # Two files force a separate text post through native caption splitting.
    second = tmp_path / "second.pdf"
    second.write_bytes(b"%PDF-1.4 second")
    good = {"ok": True, "files": [{"id": "F123"}]}
    bad = {"ok": False, "error": "missing_scope"}
    client.files_upload_v2.side_effect = [good, bad] if case == "mixed" else None
    if case in {"complete", "missing"}:
        client.files_upload_v2.return_value = good
    parser = argparse.ArgumentParser()
    register_send_subparser(parser.add_subparsers())
    args = parser.parse_args(["send", "-t", "slack:C012AB3CD", f"Report\nMEDIA:{media}\nMEDIA:{second}",
                              *([] if mode == "human" else [mode])])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    captured = capsys.readouterr()
    assert exc.value.code == (0 if case == "complete" else 1)
    if case == "missing":
        assert not client.files_upload_v2.called
        assert not client.chat_postMessage.called
    else:
        client.chat_postMessage.assert_awaited_once()
        assert client.files_upload_v2.await_count == 2
    if mode == "--json":
        result = json.loads(captured.out)
        assert result.get("success", False) is (case == "complete")
        if case in {"denied", "mixed"}:
            assert result["error_code"] == "partial_delivery"
            assert result["message_id"] == "111.222"
            assert len(result["delivered_attachments"]) == (1 if case == "mixed" else 0)
    elif mode == "human":
        assert ("sent" in captured.out) if case == "complete" else captured.err
    else:
        assert captured.out == ""
    if mode == "--json" and case != "missing":
        # Cron consumes the same native sender but returns a delivery-error string.
        from cron.scheduler import _deliver_result
        client.files_upload_v2.reset_mock()
        client.chat_postMessage.reset_mock()
        client.files_upload_v2.side_effect = [good, bad] if case == "mixed" else None
        err = _deliver_result(
            {"id": "partial-delivery-test", "name": "test", "deliver": "slack:C012AB3CD"},
            f"Report\nMEDIA:{media}\nMEDIA:{second}", adapters=None, loop=None,
        )
        assert (err is None) is (case == "complete")
        if err:
            assert "partial_delivery" in err
        assert client.files_upload_v2.await_count == 2
