from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
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


def test_telegram_dates_default_to_sao_paulo_timezone(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TIMEZONE", raising=False)
    monkeypatch.delenv("TDM_TIMEZONE", raising=False)
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

    assert service._format_datetime("2026-08-03T17:00:00+00:00") == "03/08 14:00"
    assert service._format_claimed_at("2026-08-03T17:15:00+00:00") == "03/08 14:15"


def test_telegram_dates_allow_timezone_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TIMEZONE", "UTC")
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

    assert service._format_datetime("2026-08-03T17:00:00+00:00") == "03/08 17:00"


def test_telegram_free_games_lines_include_attention_message():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "running": False,
                "automation": {
                    "scheduled_accounts": 0,
                    "paused": True,
                    "pause_reason": "attention_required",
                },
                "next_run_at": None,
                "attention": {
                    "required": True,
                    "message": "Epic captcha required. Start a manual run and use Browser to solve it.",
                },
                "accounts": [
                    {
                        "id": "main",
                        "name": "Main",
                        "last_run_success": False,
                        "attention": {
                            "required": True,
                            "message": "Epic captcha required. Start a manual run and use Browser to solve it.",
                        },
                        "automation": {
                            "eligible": False,
                            "blocked_reason": "attention_required",
                        },
                    }
                ],
            }
        )
    )
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
        free_games=free_games,
    )

    lines = TelegramService(twitch)._format_free_games_lines()

    assert "🚨 Epic captcha required. Start a manual run and use Browser to solve it." in lines
    assert "⏸ Automatic Epic runs paused until manual attention is resolved." in lines
    assert "🚨 <b>Main</b>" in lines
    assert "  • ⏸ Automation blocked: attention required" in lines


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
    send_call = service._api.call_args_list[1]
    assert send_call.args == ("sendMessage",)
    assert send_call.kwargs["chat_id"] == "42"
    assert "⚡ <b>Twitch Drops Miner</b>" in send_call.kwargs["text"]
    assert "🕒 <b>Updated:</b>" in send_call.kwargs["text"]
    assert send_call.kwargs["parse_mode"] == "HTML"
    assert send_call.kwargs["disable_web_page_preview"] is True
    assert send_call.kwargs["reply_markup"] == service._status_reply_markup()


@pytest.mark.asyncio
async def test_telegram_callback_runs_epic_module():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"enabled": True, "running": False}),
        run_now=MagicMock(return_value=True),
        update_runner=MagicMock(),
    )
    hub = SimpleNamespace(
        run_action=MagicMock(return_value={"success": True, "module_id": "free-games-epic"})
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
        hub=hub,
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

    hub.run_action.assert_called_once_with("free-games-epic", "run")
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
                    {
                        "id": "main",
                        "name": "Main",
                        "attention": {"required": True},
                    },
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

    assert [
        {"text": "🔄 Run Epic", "callback_data": "free_games:run"},
        {"text": "⬆️ Update modules", "callback_data": "hub:update_all"},
    ] in markup["inline_keyboard"]
    assert [{"text": "▶️ Main", "callback_data": "fg:r:0"}] in markup["inline_keyboard"]
    assert [{"text": "✅ Clear Main", "callback_data": "fg:c:0"}] in markup["inline_keyboard"]
    assert all(
        button.get("callback_data") != "fg:r:1"
        for row in markup["inline_keyboard"]
        for button in row
    )


def test_telegram_status_keyboard_includes_epic_stop_button_when_running():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "running": True,
                "accounts": [{"id": "main", "name": "Main"}],
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

    assert [{"text": "⏹ Stop Epic", "callback_data": "free_games:stop"}] in markup[
        "inline_keyboard"
    ]


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
    hub = SimpleNamespace(
        run_action=MagicMock(return_value={"success": True, "module_id": "free-games-epic"})
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
        hub=hub,
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

    hub.run_action.assert_called_once_with(
        "free-games-epic",
        "run_account",
        {"account_id": "main"},
    )
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-account",
        text="Epic run started for Main.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_clears_epic_account_attention():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "running": False,
                "accounts": [{"id": "main", "name": "Main", "enabled": True}],
            }
        ),
        run_now=MagicMock(),
        clear_attention=MagicMock(return_value=True),
        update_runner=MagicMock(),
    )
    hub = SimpleNamespace(
        run_action=MagicMock(return_value={"success": True, "module_id": "free-games-epic"})
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
        hub=hub,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-clear",
                "data": "fg:c:0",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    hub.run_action.assert_called_once_with(
        "free-games-epic",
        "clear_attention",
        {"account_id": "main"},
    )
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-clear",
        text="Epic attention cleared for Main.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_stops_epic_module():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"enabled": True, "running": True}),
        run_now=MagicMock(),
        stop_run=MagicMock(return_value=True),
        update_runner=MagicMock(),
    )
    hub = SimpleNamespace(
        run_action=MagicMock(return_value={"success": True, "module_id": "free-games-epic"})
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
        hub=hub,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-stop",
                "data": "free_games:stop",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    hub.run_action.assert_called_once_with("free-games-epic", "stop")
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-stop",
        text="Epic stop requested.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_updates_epic_module():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"running": False, "updating": False}),
        run_now=MagicMock(),
        update_runner=MagicMock(return_value=True),
    )
    hub = SimpleNamespace(
        run_action=MagicMock(return_value={"success": True, "module_id": "free-games-epic"})
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
        hub=hub,
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

    hub.run_action.assert_called_once_with("free-games-epic", "update")
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-2",
        text="Epic module update started.",
    )


@pytest.mark.asyncio
async def test_telegram_callback_updates_hub_modules():
    hub = SimpleNamespace(
        run_hub_action=MagicMock(
            return_value={"success": True, "action": "update_all", "results": []}
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
        hub=hub,
    )
    service = TelegramService(twitch)
    service._session = SimpleNamespace(closed=False)
    service._api = AsyncMock()
    service.queue_status_update = MagicMock()

    await service._handle_update(
        {
            "callback_query": {
                "id": "callback-hub",
                "data": "hub:update_all",
                "message": {"chat": {"id": 42}},
            }
        }
    )

    hub.run_hub_action.assert_called_once_with("update_all")
    service.queue_status_update.assert_called_once_with(immediate=True)
    service._api.assert_awaited_once_with(
        "answerCallbackQuery",
        callback_query_id="callback-hub",
        text="Hub module update started.",
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


@pytest.mark.asyncio
async def test_telegram_api_logs_transient_network_errors_without_traceback(monkeypatch, caplog):
    class FailingSession:
        closed = False

        def post(self, *args, **kwargs):
            raise aiohttp.ClientOSError(104, "Connection reset by peer")

    twitch = SimpleNamespace(
        settings=SimpleNamespace(
            telegram_bot_token="123:secret",
            telegram_chat_id="42",
            telegram_enabled=True,
            telegram_panel_url="",
            telegram_notifications={"status_message": True},
        )
    )
    service = TelegramService(twitch)
    monkeypatch.setattr("src.services.telegram_service.aiohttp.ClientSession", FailingSession)

    with caplog.at_level("WARNING", logger="TwitchDrops"):
        assert await service._api("getUpdates") is None

    assert "Telegram API call failed for getUpdates" in caplog.text
    assert "Connection reset by peer" in caplog.text
    assert "Traceback" not in caplog.text
