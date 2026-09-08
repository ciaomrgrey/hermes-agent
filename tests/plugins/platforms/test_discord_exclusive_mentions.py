"""Discord explicit bot mentions route to one profile at ingress."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig
import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter, _GATE_ENV_KEYS


class TextChannel:
    id = 123
    name = "routing-test"
    topic = None
    parent_id = None
    parent = None

    def __init__(self, guild):
        self.guild = guild

    def history(self, **_kwargs):
        async def empty():
            if False:
                yield None

        return empty()


class DMChannel:
    id = 789
    name = "dm"
    guild = None


def _message(channel, content, *, mentions=None, message_id=1):
    return SimpleNamespace(
        id=message_id,
        content=content,
        clean_content=content,
        mentions=list(mentions or []),
        attachments=[],
        message_snapshots=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        guild=getattr(channel, "guild", None),
        author=SimpleNamespace(id=42, name="Lars", display_name="Lars", bot=False),
        type=discord_platform.discord.MessageType.default,
    )


def _adapter(user, users, require_mention):
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="fixture",
            extra={
                "require_mention": require_mention,
                "allow_all_users": True,
                "history_backfill": False,
            },
        )
    )
    adapter._client = SimpleNamespace(
        user=user,
        get_user=lambda user_id: users.get(int(user_id)),
    )
    adapter._ready_event.set()
    adapter._gate_env_snapshot = {key: "" for key in _GATE_ENV_KEYS}
    adapter._is_allowed_user = lambda *_args, **_kwargs: True
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    return adapter


class TestDiscordExclusiveMentions(IsolatedAsyncioTestCase):
    def setUp(self):
        self.dm_patch = patch.object(discord_platform.discord, "DMChannel", DMChannel)
        self.env_patch = patch.dict(
            os.environ,
            {
                "DISCORD_REQUIRE_MENTION": "false",
                "DISCORD_AUTO_THREAD": "false",
                "DISCORD_HISTORY_BACKFILL": "false",
            },
        )
        self.dm_patch.start()
        self.env_patch.start()
        self.addCleanup(self.dm_patch.stop)
        self.addCleanup(self.env_patch.stop)

        bots = {
            bot_id: SimpleNamespace(
                id=bot_id,
                name=f"bot-{bot_id}",
                display_name=f"bot-{bot_id}",
                bot=True,
            )
            for bot_id in range(900, 905)
        }
        self.human = SimpleNamespace(
            id=42, name="human", display_name="human", bot=False
        )
        users = {**bots, self.human.id: self.human}
        guild = SimpleNamespace(id=456, name="Routing Test", get_member=users.get)
        self.channel = TextChannel(guild)
        self.adapters = [
            _adapter(bots[bot_id], users, require_mention=bot_id != 900)
            for bot_id in bots
        ]
        co_residents = [*self.adapters, SimpleNamespace(_client=None)]
        for adapter in self.adapters:
            adapter.set_co_resident_adapters_provider(lambda: co_residents)

    async def test_raw_bot_mention_routes_only_to_selected_profile(self):
        selected = self.adapters[2]._client.user

        for cache_state in ("warm", "cold"):
            if cache_state == "cold":
                self.channel.guild.get_member = lambda _user_id: None
                for adapter in self.adapters:
                    adapter._client.get_user = lambda _user_id: None
                    adapter.handle_message.reset_mock()

            results = [
                await adapter._dispatch_discord_message(
                    _message(
                        self.channel,
                        f"<@{selected.id}> /voice join",
                        mentions=[],
                        message_id=100 + index + (10 if cache_state == "cold" else 0),
                    )
                )
                for index, adapter in enumerate(self.adapters)
            ]

            self.assertEqual(results, [False, False, True, False, False])
            self.assertEqual(
                [adapter.handle_message.await_count for adapter in self.adapters],
                [0, 0, 1, 0, 0],
            )

    async def test_raw_human_mention_does_not_suppress_mention_free_profile(self):
        for cache_state in ("warm", "cold"):
            if cache_state == "cold":
                self.channel.guild.get_member = lambda _user_id: None
                self.adapters[0]._client.get_user = lambda _user_id: None

            result = await self.adapters[0]._dispatch_discord_message(
                _message(
                    self.channel,
                    f"<@{self.human.id}> /voice join",
                    mentions=[],
                    message_id=200 + (1 if cache_state == "cold" else 0),
                )
            )

            self.assertTrue(result)
        self.assertEqual(self.adapters[0].handle_message.await_count, 2)


if __name__ == "__main__":
    main()
