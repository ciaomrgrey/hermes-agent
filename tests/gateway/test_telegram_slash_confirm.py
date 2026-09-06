"""Regression guard: send_slash_confirm must use format_message + MARKDOWN_V2."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestSendSlashConfirm:

    @pytest.mark.asyncio
    async def test_uses_markdown_v2_and_escapes_special_chars(self):
        """send_slash_confirm must pass preview through format_message and use
        MARKDOWN_V2 — so commands with underscores, dots, or brackets don't
        raise BadRequest: Can't parse entities."""
        adapter = _make_adapter()
        sent = {}

        async def mock_send(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=7)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send)

        result = await adapter.send_slash_confirm(
            chat_id="100",
            title="Confirm",
            message="/run script_name.sh --flag=value [option]",
            session_key="sk",
            confirm_id="cid1",
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        # Underscores and dots must be escaped by format_message
        assert "script\\_name" in sent["text"]
        assert "\\." in sent["text"]

    @pytest.mark.asyncio
    async def test_one_shot_confirmation_omits_always_button(self, monkeypatch):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=8))
        labels = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: labels.append(text) or text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows,
        )

        result = await adapter.send_slash_confirm(
            chat_id="100", title="Confirm", message="Pause exact job",
            session_key="sk", confirm_id="cid2", allow_always=False,
        )

        assert result.success is True
        assert labels == ["✅ Approve Once", "❌ Cancel"]

    @pytest.mark.asyncio
    async def test_expired_confirmation_never_claims_approval(self):
        adapter = _make_adapter()
        adapter._slash_confirm_state["expired"] = "session"
        query = AsyncMock()
        query.data = "sc:once:expired"
        query.message = MagicMock(chat_id=100)
        query.from_user = MagicMock(id=100, first_name="Tester")

        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.slash_confirm.resolve", new=AsyncMock(return_value=None)):
                await adapter._handle_callback_query(
                    MagicMock(callback_query=query), MagicMock()
                )

        answer = query.answer.call_args.kwargs["text"]
        edited = query.edit_message_text.call_args.kwargs["text"]
        assert "expired" in answer.lower()
        assert "approved" not in edited.lower()


