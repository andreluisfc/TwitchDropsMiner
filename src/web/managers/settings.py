"""Settings manager for application configuration."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.i18n.translator import _
from src.models.game import Game
from src.services.free_games_service import SECRET_PLACEHOLDER
from src.services.telegram_service import TELEGRAM_TOKEN_PLACEHOLDER


logger = logging.getLogger("TwitchDrops")


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.web.managers.broadcaster import WebSocketBroadcaster
    from src.web.managers.console import ConsoleOutputManager


class SettingsManager:
    """Manages application settings in the web interface.

    Provides access to and modification of user preferences including
    game priorities, proxy configuration, and UI preferences.
    """

    def __init__(
        self,
        broadcaster: WebSocketBroadcaster,
        settings: Settings,
        console: ConsoleOutputManager,
        on_change: Callable[[], None] | None = None,
    ):
        self._broadcaster = broadcaster
        self._settings = settings
        self._console = console
        self._on_change = on_change
        self._available_games: list[str] = []

    def get_settings(self) -> dict[str, Any]:
        """Get current settings for display.

        Returns:
            Dictionary containing all user-configurable settings
        """
        settings = vars(self._settings).copy()
        env_token = os.getenv("TELEGRAM_BOT_TOKEN")
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        env_panel_url = os.getenv("TELEGRAM_PANEL_URL") or os.getenv("PUBLIC_PANEL_URL")
        settings["telegram_configured"] = bool(
            settings.get("telegram_bot_token") or env_token
        )
        if settings.get("telegram_bot_token") or env_token:
            settings["telegram_bot_token"] = TELEGRAM_TOKEN_PLACEHOLDER
        if not settings.get("telegram_chat_id") and env_chat_id:
            settings["telegram_chat_id"] = env_chat_id
        if not settings.get("telegram_panel_url") and env_panel_url:
            settings["telegram_panel_url"] = env_panel_url
        settings["free_games_accounts"] = [
            self._mask_free_games_account(account)
            for account in settings.get("free_games_accounts", [])
            if isinstance(account, dict)
        ]
        return settings

    def get_languages(self) -> dict[str, Any]:
        """Get available languages and current selection.

        Returns:
            Dictionary with available languages and current language
        """
        return {
            "available": _.get_languages(),
            "current": _.current_language,
        }

    def _log_change(self, message: str):
        """Log setting change to both console and system logger."""
        self._console.print(message)

    def update_settings(self, settings_data: dict[str, Any]):
        """Update settings from user input.

        Args:
            settings_data: Dictionary of settings to update
        """
        should_trigger_update = False
        should_trigger_update |= self.check_and_update_setting(
            "games_to_watch", settings_data.get("games_to_watch"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "priority_list_only", settings_data.get("priority_list_only"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "dark_mode", settings_data.get("dark_mode")
        )
        should_trigger_update |= self.check_and_update_setting(
            "language", settings_data.get("language"), False, self._set_language
        )
        should_trigger_update |= self.check_and_update_setting(
            "connection_quality", settings_data.get("connection_quality")
        )
        if "proxy" in settings_data:
            proxy_value = settings_data["proxy"]
            should_trigger_update |= self.check_and_update_setting(
                "proxy",
                str(proxy_value).strip() if proxy_value else "",
                True,
                lambda proxy: self._log_change("Proxy cleared") if proxy == "" else None,
            )
        should_trigger_update |= self.check_and_update_setting(
            "minimum_refresh_interval_minutes",
            settings_data.get("minimum_refresh_interval_minutes"),
        )
        should_trigger_update |= self.check_and_update_setting(
            "inventory_filters", settings_data.get("inventory_filters")
        )
        should_trigger_update |= self.check_and_update_setting(
            "inventory_list_view", settings_data.get("inventory_list_view")
        )
        should_trigger_update |= self.check_and_update_setting(
            "mining_benefits", settings_data.get("mining_benefits"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "telegram_enabled", settings_data.get("telegram_enabled"), True
        )
        if "telegram_bot_token" in settings_data:
            token_value = str(settings_data["telegram_bot_token"]).strip()
            if token_value != TELEGRAM_TOKEN_PLACEHOLDER:
                should_trigger_update |= self.check_and_update_setting(
                    "telegram_bot_token", token_value, True
                )
        if "telegram_chat_id" in settings_data:
            should_trigger_update |= self.check_and_update_setting(
                "telegram_chat_id",
                str(settings_data["telegram_chat_id"]).strip()
                if settings_data.get("telegram_chat_id")
                else "",
                True,
            )
        if "telegram_panel_url" in settings_data:
            should_trigger_update |= self.check_and_update_setting(
                "telegram_panel_url",
                str(settings_data["telegram_panel_url"]).strip()
                if settings_data.get("telegram_panel_url")
                else "",
                True,
            )
        if "telegram_notifications" in settings_data:
            should_trigger_update |= self.check_and_update_setting(
                "telegram_notifications", settings_data.get("telegram_notifications"), True
            )
        should_trigger_update |= self.check_and_update_setting(
            "free_games_enabled", settings_data.get("free_games_enabled"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "free_games_runner", settings_data.get("free_games_runner"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "free_games_image", settings_data.get("free_games_image"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "free_games_claimer_path", settings_data.get("free_games_claimer_path"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "free_games_schedule_hours", settings_data.get("free_games_schedule_hours"), True
        )
        if "free_games_accounts" in settings_data:
            accounts = self._merge_free_games_accounts(settings_data.get("free_games_accounts"))
            if getattr(self._settings, "free_games_accounts", None) != accounts:
                self._settings.free_games_accounts = accounts
                self._log_change(f"Setting changed: free_games_accounts = {len(accounts)} account(s)")
                should_trigger_update = True

        self._settings.save()
        asyncio.create_task(self._broadcaster.emit("settings_updated", self.get_settings()))

        if should_trigger_update and self._on_change:
            self._on_change()

    def check_and_update_setting(
        self,
        key: str,
        new_value: Any,
        should_trigger_update: bool = False,
        action: Callable[[Any], None] = lambda x: None,
    ):
        if new_value is None or getattr(self._settings, key, None) == new_value:
            return False
        setattr(self._settings, key, new_value)
        self._log_change(f"Setting changed: {key} = {new_value}")
        action(new_value)
        return should_trigger_update

    def _set_language(self, language: str):
        _.set_language(language)
        # Notify clients that translations need to be reloaded
        asyncio.create_task(self._broadcaster.emit("language_changed", {"language": language}))

    def _mask_free_games_account(self, account: dict[str, Any]) -> dict[str, Any]:
        masked = account.copy()
        for key in ("password", "otpkey", "parental_pin"):
            if masked.get(key):
                masked[key] = SECRET_PLACEHOLDER
        return masked

    def _merge_free_games_accounts(self, incoming: Any) -> list[dict[str, Any]]:
        if not isinstance(incoming, list):
            return []
        existing = {
            str(account.get("id")): account
            for account in getattr(self._settings, "free_games_accounts", [])
            if isinstance(account, dict) and account.get("id")
        }
        accounts: list[dict[str, Any]] = []
        for raw_account in incoming:
            if not isinstance(raw_account, dict):
                continue
            account_id = str(raw_account.get("id") or raw_account.get("email") or "").strip()
            if not account_id:
                continue
            current = existing.get(account_id, {})
            account = {
                "id": account_id,
                "name": str(raw_account.get("name") or raw_account.get("email") or account_id).strip(),
                "email": str(raw_account.get("email") or "").strip(),
                "enabled": bool(raw_account.get("enabled", True)),
                "password": str(raw_account.get("password") or "").strip(),
                "otpkey": str(raw_account.get("otpkey") or "").strip(),
                "parental_pin": str(raw_account.get("parental_pin") or "").strip(),
            }
            for secret_key in ("password", "otpkey", "parental_pin"):
                if account[secret_key] == SECRET_PLACEHOLDER:
                    account[secret_key] = str(current.get(secret_key) or "")
            accounts.append(account)
        return accounts

    def set_games(self, games: set[Game]):
        """Update the list of available games for settings panel.

        Args:
            games: Set of Game objects discovered from campaigns
        """
        # Store and broadcast available games for settings panel
        game_names = sorted([g.name for g in games])
        self._available_games = game_names
        asyncio.create_task(self._broadcaster.emit("games_available", {"games": game_names}))
