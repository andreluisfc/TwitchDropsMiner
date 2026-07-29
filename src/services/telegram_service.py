"""Telegram bot integration for miner notifications and status updates."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp

from src.config import DATA_DIR
from src.utils import json_load, json_save


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.channel import Channel
    from src.models.drop import TimedDrop


logger = logging.getLogger("TwitchDrops")

TELEGRAM_STATE_PATH = DATA_DIR / "telegram_state.json"
TELEGRAM_TOKEN_PLACEHOLDER = "********"


class TelegramService:
    """Sends Telegram notifications and keeps one status message up to date."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._session: aiohttp.ClientSession | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._settings_task: asyncio.Task[None] | None = None
        self._last_channel_id: int | None = None
        self._last_panel_url: str = self._panel_url
        self._state: dict[str, Any] = json_load(
            TELEGRAM_STATE_PATH, {"status_message_id": None}, merge=True
        )

    @property
    def _token(self) -> str:
        return (
            self._twitch.settings.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        ).strip()

    @property
    def _chat_id(self) -> str:
        return (
            self._twitch.settings.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or ""
        ).strip()

    @property
    def _panel_url(self) -> str:
        return (
            self._twitch.settings.telegram_panel_url
            or os.getenv("TELEGRAM_PANEL_URL")
            or os.getenv("PUBLIC_PANEL_URL")
            or ""
        ).strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def is_enabled(self) -> bool:
        return bool(self._twitch.settings.telegram_enabled and self.is_configured)

    async def start(self) -> None:
        if not self.is_enabled:
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        await self._sync_panel_button()
        self.queue_status_update(immediate=True)

    async def stop(self) -> None:
        current_task = asyncio.current_task()
        for task in (self._status_task, self._settings_task):
            if task is not None and task is not current_task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._status_task = None
        self._settings_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    def on_settings_changed(self) -> None:
        self._settings_task = self._create_task(self._apply_settings_change())

    async def _apply_settings_change(self) -> None:
        previous_panel_url = self._last_panel_url
        if self.is_enabled:
            await self.start()
            if previous_panel_url != self._panel_url:
                await self._sync_panel_button()
                if self._notification_enabled("link_updates"):
                    await self._send_message("Telegram panel link updated.")
            self.queue_status_update(immediate=True)
        else:
            await self.stop()
        self._last_panel_url = self._panel_url

    def notify_channel_switch(self, channel: Channel) -> None:
        if not self.is_enabled or not self._notification_enabled("channel_switch"):
            return
        if self._last_channel_id == channel.id:
            return
        self._last_channel_id = channel.id
        game_name = channel.game.name if channel.game is not None else "Unknown game"
        self._create_task(self._send_message(f"Now watching {channel.name} for {game_name}."))
        self.queue_status_update()

    def notify_drop_claimed(self, drop: TimedDrop) -> None:
        if not self.is_enabled or not self._notification_enabled("drop_claimed"):
            return
        message = f"Drop claimed: {drop.name}\nGame: {drop.campaign.game.name}"
        self._create_task(self._send_message(message))
        self.queue_status_update()

    def notify_error(self, message: str) -> None:
        if not self.is_enabled or not self._notification_enabled("errors"):
            return
        self._create_task(self._send_message(f"Miner error: {message}"))

    def queue_status_update(self, *, immediate: bool = False) -> None:
        if not self.is_enabled or not self._notification_enabled("status_message"):
            return
        if self._status_task is not None and not self._status_task.done():
            return
        delay = 0.0 if immediate else 2.0
        self._status_task = self._create_task(self._delayed_status_update(delay))

    async def _delayed_status_update(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        await self._send_or_edit_status()

    async def _send_or_edit_status(self) -> None:
        message = self._format_status_message()
        message_id = self._state.get("status_message_id")
        if message_id is not None:
            result = await self._api(
                "editMessageText",
                chat_id=self._chat_id,
                message_id=message_id,
                text=message,
                disable_web_page_preview=True,
            )
            if result:
                return

        result = await self._api(
            "sendMessage",
            chat_id=self._chat_id,
            text=message,
            disable_web_page_preview=True,
        )
        if result and isinstance(result.get("result"), dict):
            self._state["status_message_id"] = result["result"].get("message_id")
            json_save(TELEGRAM_STATE_PATH, self._state, sort=True)

    def _format_status_message(self) -> str:
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None:
            watching = "Watching: idle"
        else:
            game_name = (
                watching_channel.game.name if watching_channel.game is not None else "Unknown game"
            )
            watching = f"Watching: {watching_channel.name} ({game_name})"

        active_drop = None
        if watching_channel is not None:
            active_campaign = self._twitch.get_active_campaign(watching_channel)
            if active_campaign is not None:
                active_drop = active_campaign.first_drop

        if active_drop is None:
            progress = "Current drop: none"
        else:
            progress = (
                f"Current drop: {active_drop.name} "
                f"({active_drop.current_minutes}/{active_drop.required_minutes} min)"
            )

        queue_lines = self._format_queue_lines()
        return "\n".join(
            [
                "Twitch Drops Miner",
                watching,
                progress,
                "",
                "Loot queue:",
                *queue_lines,
            ]
        )

    def _format_queue_lines(self, limit: int = 8) -> list[str]:
        lines: list[str] = []
        try:
            tree = self._twitch.gui.get_wanted_game_tree()
        except Exception:
            logger.debug("Could not build Telegram loot queue", exc_info=True)
            return ["No loot queued."]

        for game in tree:
            game_name = game.get("game_name", "Unknown game")
            for campaign in game.get("campaigns", []):
                for drop in campaign.get("drops", []):
                    lines.append(f"- {game_name}: {drop.get('name', 'Unknown drop')}")
                    if len(lines) >= limit:
                        return lines
        return lines or ["No loot queued."]

    async def _sync_panel_button(self) -> None:
        panel_url = self._panel_url
        if not panel_url.startswith("https://"):
            return
        await self._api(
            "setChatMenuButton",
            chat_id=self._chat_id,
            menu_button={
                "type": "web_app",
                "text": "Panel",
                "web_app": {"url": panel_url},
            },
        )

    async def _send_message(self, text: str) -> None:
        await self._api(
            "sendMessage",
            chat_id=self._chat_id,
            text=text,
            disable_web_page_preview=True,
        )

    async def _api(self, method: str, **payload: Any) -> dict[str, Any] | None:
        if not self.is_configured:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            async with self._session.post(url, json=payload, timeout=15) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok", False):
                    logger.warning("Telegram API call failed for %s: %s", method, data)
                    return None
                return data
        except Exception:
            logger.warning("Telegram API call failed for %s", method, exc_info=True)
            return None

    def _notification_enabled(self, key: str) -> bool:
        return bool(self._twitch.settings.telegram_notifications.get(key, True))

    def _create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None] | None:
        try:
            return asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            logger.debug("No running event loop for Telegram task")
            return None
