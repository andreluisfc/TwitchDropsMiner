from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.telegram_service import TelegramService


def test_telegram_status_message_includes_watching_drop_and_queue():
    channel = SimpleNamespace(
        id=1,
        name="streamer",
        game=SimpleNamespace(
            name="Game A",
            box_art_url="https://example.com/game-{width}x{height}.jpg",
        ),
    )
    drop = SimpleNamespace(name="Drop A", current_minutes=10, required_minutes=30)
    campaign = SimpleNamespace(first_drop=drop)
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
            "campaigns": [{"drops": [{"name": "Drop A"}, {"name": "Drop B"}]}],
        }
    ]

    message = TelegramService(twitch)._format_status_message()

    assert "📺 <b>Watching:</b> streamer" in message
    assert "🎮 <b>Game:</b> Game A" in message
    assert "🎁 <b>Current loot:</b> Drop A" in message
    assert "⏱ <b>Progress:</b> 10/30 min (33%)" in message
    assert "• 🎮 Game A: 🎁 Drop A" in message
    assert "• 🎮 Game A: 🎁 Drop B" in message


def test_telegram_status_image_uses_current_game_art():
    channel = SimpleNamespace(
        id=1,
        name="streamer",
        game=SimpleNamespace(
            name="Game A",
            box_art_url="https://example.com/game-{width}x{height}.jpg",
        ),
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
    )
    twitch.watching_channel.get_with_default.return_value = channel

    photo_url = TelegramService(twitch)._get_status_photo_url()

    assert photo_url == "https://example.com/game-600x800.jpg"
