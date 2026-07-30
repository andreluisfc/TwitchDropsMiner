from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from yarl import URL

from src.config import DEFAULT_LANG, SETTINGS_PATH
from src.utils import json_load, json_save


class InventoryFilters(TypedDict):
    game_name_search: list[str]
    show_active: bool
    show_benefit_badge: bool
    show_benefit_emote: bool
    show_benefit_item: bool
    show_benefit_other: bool
    show_expired: bool
    show_finished: bool
    show_not_linked: bool
    show_upcoming: bool


class TelegramNotifications(TypedDict):
    drop_claimed: bool
    channel_switch: bool
    status_message: bool
    link_updates: bool
    errors: bool


class FreeGamesAccount(TypedDict, total=False):
    id: str
    name: str
    email: str
    password: str
    otpkey: str
    parental_pin: str
    vnc_password: str
    enabled: bool


default_settings = {
    "connection_quality": 1,
    "dark_mode": False,
    "games_to_watch": [],
    "language": DEFAULT_LANG,
    "priority_list_only": True,
    "inventory_filters": {
        "game_name_search": [],
        "show_active": False,
        "show_benefit_badge": True,
        "show_benefit_emote": True,
        "show_benefit_item": True,
        "show_benefit_other": True,
        "show_expired": False,
        "show_finished": False,
        "show_not_linked": True,
        "show_upcoming": True,
    },
    "inventory_list_view": False,
    "minimum_refresh_interval_minutes": 30,
    "mining_benefits": {
        "BADGE": True,
        "DIRECT_ENTITLEMENT": True,
        "EMOTE": True,
        "UNKNOWN": True,
    },
    "proxy": "",
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_panel_url": "",
    "telegram_notifications": {
        "drop_claimed": True,
        "channel_switch": True,
        "status_message": True,
        "link_updates": True,
        "errors": True,
    },
    "free_games_enabled": False,
    "free_games_runner": "docker",
    "free_games_image": "ghcr.io/vogler/free-games-claimer:latest",
    "free_games_claimer_path": "",
    "free_games_schedule_hours": 24,
    "free_games_accounts": [],
}


@dataclass
class Settings:
    connection_quality: int
    dark_mode: bool
    games_to_watch: list[str]
    language: str
    priority_list_only: bool
    inventory_filters: InventoryFilters
    inventory_list_view: bool
    minimum_refresh_interval_minutes: int
    mining_benefits: dict[str, bool]
    proxy: str
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_panel_url: str
    telegram_notifications: TelegramNotifications
    free_games_enabled: bool
    free_games_runner: str
    free_games_image: str
    free_games_claimer_path: str
    free_games_schedule_hours: int
    free_games_accounts: list[FreeGamesAccount]

    def __init__(self):
        self.load()

    def load(self):
        # TODO: remvoe customized serde in the future
        settings = json_load(SETTINGS_PATH, default_settings, merge=True)
        for key, value in settings.items():
            if value is URL:
                setattr(self, key, str(value))
            else:
                setattr(self, key, value)

    def save(self) -> None:
        json_save(SETTINGS_PATH, vars(self), sort=True)
