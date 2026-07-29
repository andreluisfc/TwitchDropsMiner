from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.telegram_service import TelegramService


def test_telegram_status_message_includes_watching_drop_and_queue():
    channel = SimpleNamespace(id=1, name="streamer", game=SimpleNamespace(name="Game A"))
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

    assert "Watching: streamer (Game A)" in message
    assert "Current drop: Drop A (10/30 min)" in message
    assert "- Game A: Drop A" in message
    assert "- Game A: Drop B" in message
