"""Telegram bot integration for miner notifications and status updates."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from collections.abc import Coroutine
from contextlib import suppress
from datetime import datetime
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
RECENT_CLAIMED_LIMIT = 12
CALLBACK_FREE_GAMES_RUN = "free_games:run"
CALLBACK_FREE_GAMES_STOP = "free_games:stop"
CALLBACK_FREE_GAMES_UPDATE = "free_games:update"
CALLBACK_HUB_UPDATE_ALL = "hub:update_all"
CALLBACK_STATUS_REFRESH = "status:refresh"
CALLBACK_FREE_GAMES_RUN_ACCOUNT_PREFIX = "fg:r:"
CALLBACK_FREE_GAMES_CLEAR_ATTENTION_PREFIX = "fg:c:"
TELEGRAM_STATE_DEFAULTS: dict[str, Any] = {
    "status_message_id": None,
    "status_message_kind": None,
    "status_photo_url": None,
    "update_offset": None,
    "recent_claimed_drops": [],
}


class TelegramService:
    """Sends Telegram notifications and keeps one status message up to date."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._session: aiohttp.ClientSession | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._settings_task: asyncio.Task[None] | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._last_channel_id: int | None = None
        self._last_panel_url: str = self._panel_url
        self._state: dict[str, Any] = json_load(TELEGRAM_STATE_PATH, TELEGRAM_STATE_DEFAULTS, merge=True)

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
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = self._create_task(self._poll_updates())

    async def stop(self) -> None:
        current_task = asyncio.current_task()
        for task in (self._status_task, self._settings_task, self._polling_task):
            if task is not None and task is not current_task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._status_task = None
        self._settings_task = None
        self._polling_task = None
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

        self._save_status_state(None)
        return await self._send_or_edit_status()

    async def _apply_settings_change(self) -> None:
        previous_panel_url = self._last_panel_url
        if self.is_enabled:
            await self.start()
            if previous_panel_url != self._panel_url:
                await self._sync_panel_button()
            self.queue_status_update(immediate=True)
        else:
            await self.stop()
        self._last_panel_url = self._panel_url

    def notify_channel_switch(self, channel: Channel) -> None:
        if self._last_channel_id == channel.id:
            return
        self._last_channel_id = channel.id
        self.queue_status_update()

    def notify_drop_claimed(self, drop: TimedDrop) -> None:
        self._remember_claimed_drop(drop)
        self.queue_status_update()

    def notify_error(self, message: str) -> None:
        logger.warning("Miner error reported to Telegram status service: %s", message)

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
        message = self._format_status_message()
        message_id = self._state.get("status_message_id")
        message_kind = self._state.get("status_message_kind")

        if message_id is not None and message_kind == "photo":
            await self._api("deleteMessage", chat_id=self._chat_id, message_id=message_id)
            self._save_status_state(None)
            message_id = None

        if message_id is not None:
            result = await self._api(
                "editMessageText",
                chat_id=self._chat_id,
                message_id=message_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=self._status_reply_markup(),
            )
            if result:
                self._save_status_state(message_id)
                return True

        result = await self._api(
            "sendMessage",
            chat_id=self._chat_id,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=self._status_reply_markup(),
        )
        if result and isinstance(result.get("result"), dict):
            self._save_status_state(result["result"].get("message_id"))
            return True
        return False

    def _status_reply_markup(self) -> dict[str, Any]:
        keyboard = [
            [
                {"text": "🔄 Run Epic", "callback_data": CALLBACK_FREE_GAMES_RUN},
                {"text": "⬆️ Update modules", "callback_data": CALLBACK_HUB_UPDATE_ALL},
            ]
        ]
        free_games = getattr(self._twitch, "free_games", None)
        if free_games is not None:
            try:
                status = free_games.get_status()
            except Exception:
                logger.debug("Could not build Telegram free-games keyboard", exc_info=True)
            else:
                if status.get("enabled"):
                    if status.get("running"):
                        keyboard.append(
                            [{"text": "⏹ Stop Epic", "callback_data": CALLBACK_FREE_GAMES_STOP}]
                        )
                    account_buttons = []
                    attention_buttons = []
                    for index, account in enumerate((status.get("accounts") or [])[:4]):
                        if account.get("enabled") is False:
                            continue
                        name = str(account.get("name") or account.get("id") or "Account")
                        account_buttons.append(
                            {
                                "text": f"▶️ {name[:24]}",
                                "callback_data": f"{CALLBACK_FREE_GAMES_RUN_ACCOUNT_PREFIX}{index}",
                            }
                        )
                        attention = account.get("attention") or {}
                        if attention.get("required"):
                            attention_buttons.append(
                                {
                                    "text": f"✅ Clear {name[:20]}",
                                    "callback_data": f"{CALLBACK_FREE_GAMES_CLEAR_ATTENTION_PREFIX}{index}",
                                }
                            )
                    for index in range(0, len(account_buttons), 2):
                        keyboard.append(account_buttons[index : index + 2])
                    for index in range(0, len(attention_buttons), 2):
                        keyboard.append(attention_buttons[index : index + 2])
        keyboard.append([{"text": "♻️ Refresh", "callback_data": CALLBACK_STATUS_REFRESH}])
        return {
            "inline_keyboard": keyboard
        }

    def _format_status_message(self, queue_limit: int = 8) -> str:
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None:
            watching = "😴 <b>Status:</b> idle"
        else:
            watching = f"📺 <b>Watching:</b> {self._channel_link(watching_channel)}"

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
            game_name = self._html(active_drop.campaign.game.name)
            campaign_url = self._html(active_drop.campaign.campaign_url)
            progress = (
                f'🎮 <b>Campaign:</b> <a href="{campaign_url}">{game_name}</a>\n'
                f"🎁 <b>Current loot:</b> {self._html(active_drop.name)}\n"
                f"⏱ <b>Progress:</b> {active_drop.current_minutes}/"
                f"{active_drop.required_minutes} min ({percent}%)\n"
                f"{self._progress_bar(percent)}"
            )

        queue_lines = self._format_queue_lines(limit=queue_limit)
        return "\n".join(
            [
                "⚡ <b>Twitch Drops Miner</b>",
                "━━━━━━━━━━━━━━━━",
                f"🕒 <b>Updated:</b> {self._html(self._updated_at())}",
                "",
                watching,
                "",
                progress,
                "",
                "🏆 <b>Recently claimed</b>",
                *self._format_claimed_lines(),
                "",
                "🎮 <b>Epic freebies</b>",
                *self._format_free_games_lines(),
                "",
                "🧭 <b>Next loot queue</b>",
                *queue_lines,
            ]
        )

    def _format_queue_lines(self, limit: int = 8) -> list[str]:
        lines: list[str] = []
        shown_drops = 0
        try:
            tree = self._twitch.gui.get_wanted_game_tree()
        except Exception:
            logger.debug("Could not build Telegram loot queue", exc_info=True)
            return ["No loot queued."]

        for game in tree:
            game_name = game.get("game_name", "Unknown game")
            game_lines: list[str] = []
            for campaign in game.get("campaigns", []):
                campaign_url = campaign.get("url")
                for drop in campaign.get("drops", []):
                    if not game_lines:
                        game_lines.append(
                            f"🎮 {self._game_campaign_link(game_name, campaign_url)}"
                        )
                    game_lines.append(f"  • 🎁 {self._html(drop.get('name', 'Unknown drop'))}")
                    shown_drops += 1
                    if shown_drops >= limit:
                        lines.extend(game_lines)
                        return lines
            lines.extend(game_lines)
        return lines or ["✨ No loot queued right now."]

    def _remember_claimed_drop(self, drop: TimedDrop) -> None:
        recent = [
            entry
            for entry in self._state.get("recent_claimed_drops", [])
            if isinstance(entry, dict)
        ]
        drop_id = str(getattr(drop, "id", ""))
        entry = {
            "drop_id": drop_id,
            "drop_name": str(getattr(drop, "name", "Unknown drop")),
            "game_name": str(getattr(drop.campaign.game, "name", "Unknown game")),
            "campaign_url": str(getattr(drop.campaign, "campaign_url", "")),
            "claimed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        recent = [
            item
            for item in recent
            if not self._same_claimed_drop(item, entry)
        ]
        self._state["recent_claimed_drops"] = [entry, *recent][:RECENT_CLAIMED_LIMIT]
        json_save(TELEGRAM_STATE_PATH, self._state, sort=True)

    def _same_claimed_drop(self, existing: dict[str, Any], new: dict[str, str]) -> bool:
        if existing.get("drop_id") and new.get("drop_id"):
            return existing.get("drop_id") == new.get("drop_id")
        return (
            existing.get("drop_name") == new.get("drop_name")
            and existing.get("game_name") == new.get("game_name")
            and existing.get("campaign_url") == new.get("campaign_url")
        )

    def _format_claimed_lines(self, limit: int = 8) -> list[str]:
        recent = [
            entry
            for entry in self._state.get("recent_claimed_drops", [])[:limit]
            if isinstance(entry, dict)
        ]
        if not recent:
            return ["✨ No drops claimed yet."]

        lines: list[str] = []
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in recent:
            game_name = str(entry.get("game_name") or "Unknown game")
            campaign_url = str(entry.get("campaign_url") or "")
            game_key = (game_name, campaign_url)
            grouped.setdefault(game_key, []).append(entry)

        for (game_name, campaign_url), entries in grouped.items():
            lines.append(f"🎮 {self._game_campaign_link(game_name, campaign_url)}")
            for entry in entries:
                claimed_at = self._format_claimed_at(entry.get("claimed_at"))
                lines.append(
                    f"  • ✅ {self._html(entry.get('drop_name') or 'Unknown drop')} · {claimed_at}"
                )
        return lines

    def _format_claimed_at(self, value: object) -> str:
        if not value:
            return "unknown time"
        try:
            claimed_at = datetime.fromisoformat(str(value))
        except ValueError:
            return self._html(value)
        return self._html(claimed_at.astimezone().strftime("%d/%m %H:%M"))

    def _format_free_games_lines(self, account_limit: int = 4, game_limit: int = 3) -> list[str]:
        free_games = getattr(self._twitch, "free_games", None)
        if free_games is None:
            return ["Module unavailable."]
        try:
            status = free_games.get_status()
        except Exception:
            logger.debug("Could not build Telegram free-games status", exc_info=True)
            return ["Module status unavailable."]

        if not status.get("enabled"):
            return ["Disabled."]

        lines = []
        if status.get("updating"):
            lines.append("⬆️ Updating Epic module")
        elif status.get("running"):
            active = status.get("active_account_id") or "all accounts"
            lines.append(f"🔄 Running now: {self._html(active)}")
        elif (status.get("automation") or {}).get("paused"):
            lines.append("⏸ Automatic Epic runs paused until manual attention is resolved.")
        elif status.get("next_run_at"):
            lines.append(f"⏭ Next run: {self._html(self._format_datetime(status['next_run_at']))}")

        attention = status.get("attention") or {}
        if attention.get("required") and attention.get("message"):
            lines.append(f"🚨 {self._html(attention['message'])}")

        accounts = status.get("accounts") or []
        if not accounts:
            lines.append("No Epic accounts configured.")
            return lines

        for account in accounts[:account_limit]:
            icon = "✅" if account.get("last_run_success") else "⚠️"
            if account.get("last_run_success") is None:
                icon = "⏳"
            account_attention = account.get("attention") or {}
            if account_attention.get("required"):
                icon = "🚨"
            lines.append(f"{icon} <b>{self._html(account.get('name') or account.get('id'))}</b>")
            claimed_games = account.get("claimed_games") or []
            if claimed_games:
                for game in claimed_games[:game_limit]:
                    title = game.get("title") or "Unknown game"
                    url = game.get("url") or ""
                    game_link = self._game_campaign_link(title, url)
                    lines.append(f"  • 🛍 {game_link}")
            elif account_attention.get("message"):
                lines.append(f"  • {self._html(account_attention['message'])}")
            elif account.get("last_error"):
                lines.append(f"  • {self._html(account['last_error'])}")
            else:
                lines.append("  • No claimed games recorded yet.")
            account_automation = account.get("automation") or {}
            if account_automation.get("blocked_reason"):
                reason = str(account_automation["blocked_reason"]).replace("_", " ")
                lines.append(f"  • ⏸ Automation blocked: {self._html(reason)}")
        return lines

    def _format_datetime(self, value: object) -> str:
        try:
            stamp = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        return stamp.astimezone().strftime("%d/%m %H:%M")

    def _save_status_state(self, message_id: int | None) -> None:
        self._state["status_message_id"] = message_id
        self._state["status_message_kind"] = "text"
        self._state["status_photo_url"] = None
        json_save(TELEGRAM_STATE_PATH, self._state, sort=True)

    def _progress_bar(self, percent: int) -> str:
        filled = min(10, max(0, round(percent / 10)))
        return "▰" * filled + "▱" * (10 - filled)

    def _html(self, value: object) -> str:
        return html.escape(str(value), quote=True)

    def _updated_at(self) -> str:
        return datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S")

    def _channel_link(self, channel: Channel) -> str:
        channel_url = getattr(channel, "url", None)
        url = str(channel_url) if channel_url is not None else f"https://www.twitch.tv/{channel.name}"
        return f'<a href="{self._html(url)}">{self._html(channel.name)}</a>'

    def _game_campaign_link(self, game_name: object, campaign_url: object | None) -> str:
        if campaign_url:
            return f'<a href="{self._html(campaign_url)}">{self._html(game_name)}</a>'
        return self._html(game_name)

    async def _poll_updates(self) -> None:
        while self.is_enabled:
            offset = self._state.get("update_offset")
            result = await self._api(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
                _timeout=35,
            )
            if result and isinstance(result.get("result"), list):
                for update in result["result"]:
                    self._state["update_offset"] = update["update_id"] + 1
                    await self._handle_update(update)
                json_save(TELEGRAM_STATE_PATH, self._state, sort=True)
            else:
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self._chat_id:
            return

        text = str(message.get("text") or "").strip().lower()
        if text.startswith("/start"):
            await self._sync_panel_button()
            await self.resend_status_message()

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = str(callback_query.get("id") or "")
        if str(chat.get("id")) != self._chat_id:
            return

        data = str(callback_query.get("data") or "")
        if data == CALLBACK_STATUS_REFRESH:
            await self._answer_callback(callback_id, "Status refreshed.")
            await self._send_or_edit_status()
            return

        if data == CALLBACK_HUB_UPDATE_ALL:
            hub = getattr(self._twitch, "hub", None)
            if hub is None:
                await self._answer_callback(callback_id, "Hub is unavailable.")
                return
            result = hub.run_hub_action("update_all")
            if result.get("success"):
                await self._answer_callback(callback_id, "Hub module update started.")
                self.queue_status_update(immediate=True)
            else:
                await self._answer_callback(
                    callback_id,
                    str(result.get("detail") or "Hub module update could not be started."),
                )
            return

        free_games = getattr(self._twitch, "free_games", None)
        if free_games is None:
            await self._answer_callback(callback_id, "Epic module is unavailable.")
            return

        if data.startswith(CALLBACK_FREE_GAMES_RUN_ACCOUNT_PREFIX):
            await self._handle_free_games_account_callback(callback_id, data, free_games)
            return
        if data.startswith(CALLBACK_FREE_GAMES_CLEAR_ATTENTION_PREFIX):
            await self._handle_free_games_clear_attention_callback(callback_id, data, free_games)
            return

        if data == CALLBACK_FREE_GAMES_RUN:
            status = free_games.get_status()
            if not status.get("enabled"):
                await self._answer_callback(callback_id, "Epic module is disabled.")
            elif status.get("running"):
                await self._answer_callback(callback_id, "Epic run is already active.")
            elif free_games.run_now():
                await self._answer_callback(callback_id, "Epic run started.")
                self.queue_status_update(immediate=True)
            else:
                await self._answer_callback(callback_id, "Epic run could not be started.")
            return

        if data == CALLBACK_FREE_GAMES_STOP:
            status = free_games.get_status()
            if not status.get("running"):
                await self._answer_callback(callback_id, "Epic run is not active.")
            elif free_games.stop_run():
                await self._answer_callback(callback_id, "Epic stop requested.")
                self.queue_status_update(immediate=True)
            else:
                await self._answer_callback(callback_id, "Epic run could not be stopped.")
            return

        if data == CALLBACK_FREE_GAMES_UPDATE:
            status = free_games.get_status()
            if status.get("running"):
                await self._answer_callback(callback_id, "Epic run is active.")
            elif status.get("updating"):
                await self._answer_callback(callback_id, "Epic update is already active.")
            elif free_games.update_runner():
                await self._answer_callback(callback_id, "Epic module update started.")
                self.queue_status_update(immediate=True)
            else:
                await self._answer_callback(callback_id, "Epic module update could not be started.")

    async def _handle_free_games_account_callback(
        self, callback_id: str, data: str, free_games: Any
    ) -> None:
        try:
            account_index = int(data.removeprefix(CALLBACK_FREE_GAMES_RUN_ACCOUNT_PREFIX))
        except ValueError:
            await self._answer_callback(callback_id, "Epic account was not found.")
            return

        status = free_games.get_status()
        accounts = status.get("accounts") or []
        if not status.get("enabled"):
            await self._answer_callback(callback_id, "Epic module is disabled.")
            return
        if status.get("running"):
            await self._answer_callback(callback_id, "Epic run is already active.")
            return
        if account_index < 0 or account_index >= len(accounts):
            await self._answer_callback(callback_id, "Epic account was not found.")
            return

        account = accounts[account_index]
        if account.get("enabled") is False:
            await self._answer_callback(callback_id, "Epic account is disabled.")
            return
        account_id = str(account.get("id") or "")
        if account_id and free_games.run_now(account_id):
            await self._answer_callback(callback_id, f"Epic run started for {account.get('name')}.")
            self.queue_status_update(immediate=True)
        else:
            await self._answer_callback(callback_id, "Epic run could not be started.")

    async def _handle_free_games_clear_attention_callback(
        self, callback_id: str, data: str, free_games: Any
    ) -> None:
        try:
            account_index = int(data.removeprefix(CALLBACK_FREE_GAMES_CLEAR_ATTENTION_PREFIX))
        except ValueError:
            await self._answer_callback(callback_id, "Epic account was not found.")
            return

        status = free_games.get_status()
        accounts = status.get("accounts") or []
        if account_index < 0 or account_index >= len(accounts):
            await self._answer_callback(callback_id, "Epic account was not found.")
            return

        account = accounts[account_index]
        account_id = str(account.get("id") or "")
        if account_id and free_games.clear_attention(account_id):
            await self._answer_callback(callback_id, f"Epic attention cleared for {account.get('name')}.")
            self.queue_status_update(immediate=True)
        else:
            await self._answer_callback(callback_id, "Epic attention could not be cleared.")

    async def _answer_callback(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        await self._api("answerCallbackQuery", callback_query_id=callback_id, text=text)

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
        request_timeout = payload.pop("_timeout", 15)
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            async with self._session.post(url, json=payload, timeout=request_timeout) as response:
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
