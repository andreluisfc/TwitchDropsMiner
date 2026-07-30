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
    assert "🏆 <b>Recently claimed</b>" in message
    assert "No drops claimed yet." in message
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


def test_telegram_recent_claimed_drops_are_grouped_by_game():
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
    service = TelegramService(twitch)
    service._state["recent_claimed_drops"] = [
        {
            "drop_id": "drop-a",
            "drop_name": "Drop A",
            "game_name": "Game A",
            "campaign_url": "https://example.com/campaign-a",
            "claimed_at": "2026-07-29T13:10:00-03:00",
        },
        {
            "drop_id": "drop-c",
            "drop_name": "Drop C",
            "game_name": "Game B",
            "campaign_url": "https://example.com/campaign-b",
            "claimed_at": "2026-07-29T13:30:00-03:00",
        },
        {
            "drop_id": "drop-b",
            "drop_name": "Drop B",
            "game_name": "Game A",
            "campaign_url": "https://example.com/campaign-a",
            "claimed_at": "2026-07-29T13:20:00-03:00",
        },
    ]

    lines = service._format_claimed_lines()

    assert lines == [
        '🎮 <a href="https://example.com/campaign-a">Game A</a>',
        "  • ✅ Drop A · 29/07 13:10",
        "  • ✅ Drop B · 29/07 13:20",
        '🎮 <a href="https://example.com/campaign-b">Game B</a>',
        "  • ✅ Drop C · 29/07 13:30",
    ]


def test_telegram_drop_claimed_updates_status_without_separate_message(monkeypatch):
    monkeypatch.setattr("src.services.telegram_service.json_save", MagicMock())
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
    service.queue_status_update = MagicMock()
    service._send_message = AsyncMock()
    campaign = SimpleNamespace(
        game=SimpleNamespace(name="Game A"),
        campaign_url="https://example.com/campaign-a",
    )
    drop = SimpleNamespace(id="drop-a", name="Drop A", campaign=campaign)

    service.notify_drop_claimed(drop)
    service.notify_drop_claimed(drop)

    service._send_message.assert_not_called()
    service.queue_status_update.assert_called()
    assert service._state["recent_claimed_drops"] == [
        {
            "drop_id": "drop-a",
            "drop_name": "Drop A",
            "game_name": "Game A",
            "campaign_url": "https://example.com/campaign-a",
            "claimed_at": service._state["recent_claimed_drops"][0]["claimed_at"],
        }
    ]


def test_telegram_channel_switch_updates_status_without_separate_message():
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
    service.queue_status_update = MagicMock()
    service._send_message = AsyncMock()

    service.notify_channel_switch(SimpleNamespace(id=1))

    service._send_message.assert_not_called()
    service.queue_status_update.assert_called_once()


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
    service._api.assert_any_call(
        "sendMessage",
        chat_id="42",
        text=service._format_status_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=service._status_reply_markup(),
    )


@pytest.mark.asyncio
async def test_telegram_callback_runs_epic_module():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"enabled": True, "running": False}),
        run_now=MagicMock(return_value=True),
        update_runner=MagicMock(),
    )
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
        free_games=free_games,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "free_games:run",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    free_games.run_now.assert_called_once_with()
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-1",
        text="Epic run started.",
    )


def test_telegram_status_keyboard_includes_epic_account_buttons():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "accounts": [
                    {"id": "main", "name": "Main"},
                    {"id": "disabled", "name": "Disabled", "enabled": False},
                ],
            }
        )
    )
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
        free_games=free_games,
    )
    markup = TelegramService(twitch)._status_reply_markup()

    assert [{"text": "▶️ Main", "callback_data": "fg:r:0"}] in markup["inline_keyboard"]
    assert all(
        button.get("callback_data") != "fg:r:1"
        for row in markup["inline_keyboard"]
        for button in row
    )


@pytest.mark.asyncio
async def test_telegram_callback_runs_specific_epic_account():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "running": False,
                "accounts": [{"id": "main", "name": "Main", "enabled": True}],
            }
        ),
        run_now=MagicMock(return_value=True),
        update_runner=MagicMock(),
    )
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
        free_games=free_games,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-account",
                "data": "fg:r:0",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    free_games.run_now.assert_called_once_with("main")
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-account",
        text="Epic run started for Main.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_updates_epic_module():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"running": False, "updating": False}),
        run_now=MagicMock(),
        update_runner=MagicMock(return_value=True),
    )
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
        free_games=free_games,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-2",
                "data": "free_games:update",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    free_games.update_runner.assert_called_once_with()
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-2",
        text="Epic module update started.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_refreshes_status_message():
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
    service._api = AsyncMock()
    service._send_or_edit_status = AsyncMock(return_value=True)

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-3",
                "data": "status:refresh",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-3",
        text="Status refreshed.",
    )
    service._send_or_edit_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_callback_ignores_other_chats():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"enabled": True, "running": False}),
        run_now=MagicMock(return_value=True),
    )
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
        free_games=free_games,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-4",
                "data": "free_games:run",
                "message": {"chat": {"id": 7}},
            }
        }
    )

    free_games.run_now.assert_not_called()
    service._api.assert_not_awaited()
