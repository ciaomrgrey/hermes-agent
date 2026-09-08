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

        self.bots = {
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
        self.users = {**self.bots, self.human.id: self.human}
        self.channel = TextChannel(
            SimpleNamespace(id=456, name="Routing Test", get_member=self.users.get)
        )
        self.adapters = [
            _adapter(self.bots[bot_id], self.users, require_mention=bot_id != 900)
            for bot_id in self.bots
        ]
        co_residents = [*self.adapters, SimpleNamespace(_client=None)]
        for adapter in self.adapters:
            adapter.set_co_resident_adapters_provider(lambda: co_residents)

    async def _dispatch_all(self, content, *, mentions=None, base_id=100):
        for adapter in self.adapters:
            adapter.handle_message.reset_mock()
        results = [
            await adapter._dispatch_discord_message(
                _message(
                    self.channel,
                    content,
                    mentions=mentions,
                    message_id=base_id + index,
                )
            )
            for index, adapter in enumerate(self.adapters)
        ]
        return results, [a.handle_message.await_count for a in self.adapters]

    async def test_profile_routing_survives_missing_discord_identity_caches(self):
        selected = self.adapters[2]._client.user
        only_selected = [False, False, True, False, False]

        for index, cache_state in enumerate(("warm", "cold", "no_get_member")):
            if cache_state == "cold":
                self.channel.guild.get_member = lambda _user_id: None
                for adapter in self.adapters:
                    adapter._client.get_user = lambda _user_id: None
            elif cache_state == "no_get_member":
                self.channel.guild = SimpleNamespace(id=456, name="Routing Test")

            results, counts = await self._dispatch_all(
                f"<@{selected.id}> /voice join", base_id=100 + index * 10
            )
            self.assertEqual(results, only_selected, cache_state)
            self.assertEqual(counts, [0, 0, 1, 0, 0], cache_state)

        results, _ = await self._dispatch_all("/voice join", base_id=200)
        self.assertEqual(results, [True, False, False, False, False])

        result = await self.adapters[0]._dispatch_discord_message(
            _message(
                self.channel,
                f"<@{self.human.id}> /voice join",
                mentions=[],
                message_id=300,
            )
        )
        self.assertTrue(result)

        result = await self.adapters[0]._dispatch_discord_message(
            _message(
                self.channel,
                "<@777777> /voice join",
                mentions=[],
                message_id=301,
            )
        )
        self.assertTrue(result)

        dm_result = await self.adapters[1]._dispatch_discord_message(
            _message(DMChannel(), "/voice status", message_id=302)
        )
        self.assertTrue(dm_result)

        self.adapters[0]._is_allowed_user = lambda *_args, **_kwargs: False
        denied = await self.adapters[0]._dispatch_discord_message(
            _message(self.channel, "/voice join", message_id=303)
        )
        self.assertFalse(denied)


if __name__ == "__main__":
    main()
