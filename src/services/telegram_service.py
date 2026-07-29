"""Telegram bot integration for miner notifications and status updates."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
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
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
BOX_ART_SIZE_PATTERN = re.compile(r"-\d+x\d+(?=\.(?:jpg|png|gif)(?:\?|$))", re.I)


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

    async def resend_status_message(self) -> bool:
        """Delete the remembered status message and send a fresh one."""
        if not self.is_enabled or not self._notification_enabled("status_message"):
            return False
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        if message_id := self._state.get("status_message_id"):
            await self._api("deleteMessage", chat_id=self._chat_id, message_id=message_id)

        self._save_status_state(None, "text", None)
        return await self._send_or_edit_status()

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

    async def _send_or_edit_status(self) -> bool:
        photo_url = self._get_status_photo_url()
        message = self._format_status_message(queue_limit=5 if photo_url else 8)
        message_id = self._state.get("status_message_id")
        message_kind = self._state.get("status_message_kind")
        if message_id is not None and photo_url:
            result = await self._api(
                "editMessageMedia",
                chat_id=self._chat_id,
                message_id=message_id,
                media={
                    "type": "photo",
                    "media": photo_url,
                    "caption": self._truncate_caption(message),
                    "parse_mode": "HTML",
                },
            )
            if result:
                self._save_status_state(message_id, "photo", photo_url)
                return True
            await self._api("deleteMessage", chat_id=self._chat_id, message_id=message_id)
        elif message_id is not None:
            method = "editMessageCaption" if message_kind == "photo" else "editMessageText"
            payload = {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "parse_mode": "HTML",
            }
            if method == "editMessageCaption":
                payload["caption"] = self._truncate_caption(message)
            else:
                payload["text"] = message
                payload["disable_web_page_preview"] = True
            result = await self._api(
                method,
                **payload,
            )
            if result:
                self._save_status_state(message_id, message_kind or "text", None)
                return True

        if photo_url:
            result = await self._api(
                "sendPhoto",
                chat_id=self._chat_id,
                photo=photo_url,
                caption=self._truncate_caption(message),
                parse_mode="HTML",
            )
            message_kind = "photo"
        else:
            result = await self._api(
                "sendMessage",
                chat_id=self._chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            message_kind = "text"
        if result and isinstance(result.get("result"), dict):
            self._save_status_state(
                result["result"].get("message_id"),
                message_kind,
                photo_url if message_kind == "photo" else None,
            )
            return True
        return False

    def _format_status_message(self, queue_limit: int = 8) -> str:
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None:
            watching = "😴 <b>Status:</b> idle"
        else:
            game_name = (
                watching_channel.game.name if watching_channel.game is not None else "Unknown game"
            )
            watching = (
                f"📺 <b>Watching:</b> {self._html(watching_channel.name)}\n"
                f"🎮 <b>Game:</b> {self._html(game_name)}"
            )

        active_drop = None
        if watching_channel is not None:
            active_campaign = self._twitch.get_active_campaign(watching_channel)
            if active_campaign is not None:
                active_drop = active_campaign.first_drop

        if active_drop is None:
            progress = "🎁 <b>Current loot:</b> none"
        else:
            percent = 0
            if active_drop.required_minutes:
                percent = int(active_drop.current_minutes / active_drop.required_minutes * 100)
            progress = (
                f"🎁 <b>Current loot:</b> {self._html(active_drop.name)}\n"
                f"⏱ <b>Progress:</b> {active_drop.current_minutes}/"
                f"{active_drop.required_minutes} min ({percent}%)\n"
                f"{self._progress_bar(percent)}"
            )

        queue_lines = self._format_queue_lines(limit=queue_limit)
        panel_line = (
            f'\n🔗 <a href="{self._html(self._panel_url)}">Open panel</a>'
            if self._panel_url.startswith("https://")
            else ""
        )
        return "\n".join(
            [
                "⚡ <b>Twitch Drops Miner</b>",
                "━━━━━━━━━━━━━━━━",
                watching,
                "",
                progress,
                "",
                "🧭 <b>Next loot queue</b>",
                *queue_lines,
                panel_line,
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
                    lines.append(
                        f"• 🎮 {self._html(game_name)}: 🎁 "
                        f"{self._html(drop.get('name', 'Unknown drop'))}"
                    )
                    if len(lines) >= limit:
                        return lines
        return lines or ["✨ No loot queued right now."]

    def _get_status_photo_url(self) -> str | None:
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if (
            watching_channel is not None
            and watching_channel.game is not None
            and (
                photo_url := self._normalize_box_art_url(
                    getattr(watching_channel.game, "box_art_url", None)
                )
            )
        ):
            return photo_url

        try:
            tree = self._twitch.gui.get_wanted_game_tree()
        except Exception:
            logger.debug("Could not find Telegram status image", exc_info=True)
            return None

        for game in tree:
            if photo_url := self._normalize_box_art_url(game.get("game_icon")):
                return photo_url
        return None

    def _save_status_state(
        self, message_id: int | None, message_kind: str, photo_url: str | None
    ) -> None:
        self._state["status_message_id"] = message_id
        self._state["status_message_kind"] = message_kind
        self._state["status_photo_url"] = photo_url
        json_save(TELEGRAM_STATE_PATH, self._state, sort=True)

    def _normalize_box_art_url(self, url: str | None) -> str | None:
        if not url:
            return None
        sized_url = url.replace("{width}", "600").replace("{height}", "800")
        return BOX_ART_SIZE_PATTERN.sub("-600x800", sized_url)

    def _progress_bar(self, percent: int) -> str:
        filled = min(10, max(0, round(percent / 10)))
        return "▰" * filled + "▱" * (10 - filled)

    def _truncate_caption(self, caption: str) -> str:
        if len(caption) <= TELEGRAM_PHOTO_CAPTION_LIMIT:
            return caption
        return caption[: TELEGRAM_PHOTO_CAPTION_LIMIT - 1].rstrip() + "…"

    def _html(self, value: object) -> str:
        return html.escape(str(value), quote=True)

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
                description = str(data.get("description", "")).lower()
                if "message is not modified" in description:
                    return {"ok": True, "result": None}
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
