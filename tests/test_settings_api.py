import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.settings import Settings
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


class TestSettingsAPI(unittest.IsolatedAsyncioTestCase):
    def test_settings_update_model(self):
        # Verify model accepts new fields
        update_data = {
            "inventory_filters": {"show_upcoming": True},
            "mining_benefits": {"BADGE": True},
            "priority_list_only": True,
            "telegram_enabled": True,
            "telegram_bot_token": "123:abc",
            "telegram_chat_id": "42",
            "telegram_panel_url": "https://example.com/panel",
            "telegram_notifications": {"drop_claimed": True},
        }
        model = SettingsUpdate(**update_data)
        self.assertEqual(model.inventory_filters, update_data["inventory_filters"])
        self.assertEqual(model.mining_benefits, update_data["mining_benefits"])
        self.assertEqual(model.priority_list_only, update_data["priority_list_only"])
        self.assertTrue(model.telegram_enabled)
        self.assertEqual(model.telegram_bot_token, update_data["telegram_bot_token"])

    async def test_settings_manager_networking(self):
        # Mock dependencies
        mock_broadcaster = AsyncMock()
        mock_settings = MagicMock(spec=Settings)
        # Initialize mock attributes with default values for comparison
        mock_settings.inventory_filters = {}
        mock_settings.mining_benefits = {}
        mock_settings.games_to_watch = []
        mock_settings.priority_list_only = False
        mock_settings.telegram_enabled = False
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_chat_id = ""
        mock_settings.telegram_panel_url = ""
        mock_settings.telegram_notifications = {}

        mock_console = MagicMock()
        mock_callback = MagicMock()

        manager = SettingsManager(
            mock_broadcaster, mock_settings, mock_console, on_change=mock_callback
        )

        # 1. Update Inventory Filters (does NOT trigger callback per implementation)
        inv_filters = {"show_upcoming": False}
        manager.update_settings({"inventory_filters": inv_filters})
        mock_callback.assert_not_called()  # inventory_filters has should_trigger_update=False
        self.assertEqual(mock_settings.inventory_filters, inv_filters)
        mock_console.print.assert_called_with(
            "Setting changed: inventory_filters = {'show_upcoming': False}"
        )

        # 2. Update Mining Benefits (SHOULD trigger callback)
        benefits = {"BADGE": False}
        manager.update_settings({"mining_benefits": benefits})
        mock_callback.assert_called_once()
        self.assertEqual(mock_settings.mining_benefits, benefits)
        mock_console.print.assert_called_with("Setting changed: mining_benefits = {'BADGE': False}")
        mock_callback.reset_mock()

        # 3. Update Games to Watch (SHOULD trigger callback)
        games = ["Game 1"]
        manager.update_settings({"games_to_watch": games})
        mock_callback.assert_called_once()
        mock_callback.reset_mock()

        # 4. Update Priority List Only (SHOULD trigger callback)
        manager.update_settings({"priority_list_only": True})
        mock_callback.assert_called_once()

    async def test_settings_manager_masks_telegram_token_and_env_config(self):
        mock_broadcaster = AsyncMock()
        mock_settings = SimpleNamespace(
            telegram_bot_token="",
            telegram_enabled=True,
            telegram_chat_id="",
            telegram_panel_url="",
            telegram_notifications={},
        )
        mock_console = MagicMock()

        manager = SettingsManager(mock_broadcaster, mock_settings, mock_console)

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "123:secret",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_PANEL_URL": "https://example.com",
            },
        ):
            settings = manager.get_settings()

        self.assertTrue(settings["telegram_configured"])
        self.assertEqual(settings["telegram_bot_token"], "********")
        self.assertEqual(settings["telegram_chat_id"], "42")
        self.assertEqual(settings["telegram_panel_url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
