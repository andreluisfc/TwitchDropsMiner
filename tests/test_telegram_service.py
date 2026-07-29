from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.telegram_service import TelegramService


def test_telegram_status_message_includes_watching_drop_and_queue():
    channel = SimpleNamespace(
        id=1,
        name="streamer",
        url="https://www.twitch.tv/streamer",
        game=SimpleNamespace(
            name="Game A",
        ),
    )
    campaign = SimpleNamespace(
        first_drop=None,
        game=SimpleNamespace(name="Game A"),
        campaign_url="https://www.twitch.tv/drops/campaigns?dropID=campaign-a",
    )
    drop = SimpleNamespace(
        name="Drop A", current_minutes=10, required_minutes=30, campaign=campaign
    )
    campaign.first_drop = drop
    twitch = SimpleNamespace(
        settings=SimpleNamespace(
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_enabled=False,
            telegram_panel_url="",
            telegram_notifications={},
        ),
        watching_channel=MagicMock(),
        gui=MagicMock(),
        get_active_campaign=MagicMock(return_value=campaign),
    )
    twitch.watching_channel.get_with_default.return_value = channel
    twitch.gui.get_wanted_game_tree.return_value = [
        {
            "game_name": "Game A",
            "campaigns": [
                {
                    "url": "https://www.twitch.tv/drops/campaigns?dropID=campaign-a",
                    "drops": [{"name": "Drop A"}, {"name": "Drop B"}],
                }
            ],
        }
    ]

    message = TelegramService(twitch)._format_status_message()

    assert '📺 <b>Watching:</b> <a href="https://www.twitch.tv/streamer">streamer</a>' in message
    assert (
        '🎮 <b>Campaign:</b> <a href="https://www.twitch.tv/drops/campaigns?dropID=campaign-a">Game A</a>'
        in message
    )
    assert "🎁 <b>Current loot:</b> Drop A" in message
    assert "⏱ <b>Progress:</b> 10/30 min (33%)" in message
    assert (
        '🎮 <a href="https://www.twitch.tv/drops/campaigns?dropID=campaign-a">Game A</a>'
        in message
    )
    assert message.count("🎮 <a") == 1
    assert "  • 🎁 Drop A" in message
    assert "  • 🎁 Drop B" in message
    assert "Open panel" not in message


def test_telegram_queue_groups_loot_by_game():
    twitch = SimpleNamespace(
        settings=SimpleNamespace(
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_enabled=False,
            telegram_panel_url="",
            telegram_notifications={},
        ),
        watching_channel=MagicMock(),
        gui=MagicMock(),
        get_active_campaign=MagicMock(return_value=None),
    )
    twitch.watching_channel.get_with_default.return_value = None
    twitch.gui.get_wanted_game_tree.return_value = [
        {
            "game_name": "Game A",
            "campaigns": [
                {
                    "url": "https://example.com/campaign-a",
                    "drops": [{"name": "Drop A"}, {"name": "Drop B"}],
                }
            ],
        },
        {
            "game_name": "Game B",
            "campaigns": [{"url": "https://example.com/campaign-b", "drops": [{"name": "Drop C"}]}],
        },
    ]

    lines = TelegramService(twitch)._format_queue_lines()

    assert lines == [
        '🎮 <a href="https://example.com/campaign-a">Game A</a>',
        "  • 🎁 Drop A",
        "  • 🎁 Drop B",
        '🎮 <a href="https://example.com/campaign-b">Game B</a>',
        "  • 🎁 Drop C",
    ]


@pytest.mark.asyncio
async def test_telegram_start_command_resends_status_message():
    twitch = SimpleNamespace(
        settings=SimpleNamespace(
            telegram_bot_token="123:secret",
            telegram_chat_id="42",
            telegram_enabled=True,
            telegram_panel_url="",
            telegram_notifications={"status_message": True},
        ),
        watching_channel=MagicMock(),
        gui=MagicMock(),
        get_active_campaign=MagicMock(return_value=None),
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._sync_panel_button = AsyncMock()
    service.resend_status_message = AsyncMock(return_value=True)

    await service._handle_update(
        {"message": {"chat": {"id": 42}, "text": "/start"}}
    )

    service._sync_panel_button.assert_awaited_once()
    service.resend_status_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_resend_status_deletes_old_message_and_sends_new_one():
    twitch = SimpleNamespace(
        settings=SimpleNamespace(
            telegram_bot_token="123:secret",
            telegram_chat_id="42",
            telegram_enabled=True,
            telegram_panel_url="",
            telegram_notifications={"status_message": True},
        ),
        watching_channel=MagicMock(),
        gui=MagicMock(),
        get_active_campaign=MagicMock(return_value=None),
    )
    twitch.watching_channel.get_with_default.return_value = None
    twitch.gui.get_wanted_game_tree.return_value = []
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._state["status_message_id"] = 99
    service._api = AsyncMock(
        side_effect=[
            {"ok": True, "result": True},
            {"ok": True, "result": {"message_id": 100}},
        ]
    )

    assert await service.resend_status_message()

    assert service._state["status_message_id"] == 100
    service._api.assert_any_call("deleteMessage", chat_id="42", message_id=99)
