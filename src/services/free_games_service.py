"""Free games module orchestration.

This module intentionally treats vogler/free-games-claimer as an external runner.
Keeping the upstream project at arm's length makes updates much easier: the hub owns
configuration, scheduling, state and UI; the claimer owns store automation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Coroutine
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import DATA_DIR
from src.utils import json_load, json_save


if TYPE_CHECKING:
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")

FREE_GAMES_STATE_PATH = DATA_DIR / "free_games_state.json"
FREE_GAMES_DATA_DIR = DATA_DIR / "free-games"
SECRET_PLACEHOLDER = "********"
DEFAULT_FREE_GAMES_IMAGE = "ghcr.io/vogler/free-games-claimer:latest"


class FreeGamesService:
    """Runs and summarizes external free-game claiming modules."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._scheduler_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._state: dict[str, Any] = json_load(
            FREE_GAMES_STATE_PATH,
            {
                "running": False,
                "active_account_id": None,
                "last_run_started_at": None,
                "last_run_finished_at": None,
                "last_run_success": None,
                "last_error": None,
                "accounts": {},
            },
            merge=True,
        )

    async def start(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = self._create_task(self._scheduler_loop())

    async def stop(self) -> None:
        for task in (self._scheduler_task, self._run_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._scheduler_task = None
        self._run_task = None

    def on_settings_changed(self) -> None:
        self._save_state()
        self._twitch.telegram.queue_status_update()

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "runner": self._runner,
            "image": self._image,
            "schedule_hours": self._schedule_hours,
            "running": bool(self._state.get("running")),
            "active_account_id": self._state.get("active_account_id"),
            "last_run_started_at": self._state.get("last_run_started_at"),
            "last_run_finished_at": self._state.get("last_run_finished_at"),
            "last_run_success": self._state.get("last_run_success"),
            "last_error": self._state.get("last_error"),
            "next_run_at": self._next_run_at(),
            "accounts": [self._account_status(account) for account in self._accounts],
        }

    def run_now(self, account_id: str | None = None) -> bool:
        if not self._enabled:
            return False
        if self._run_task is not None and not self._run_task.done():
            return False
        self._run_task = self._create_task(self._run_accounts(account_id))
        return self._run_task is not None

    async def _scheduler_loop(self) -> None:
        while True:
            if self._enabled and self._due_for_scheduled_run():
                self.run_now()
            await asyncio.sleep(60)

    async def _run_accounts(self, account_id: str | None = None) -> None:
        accounts = [account for account in self._accounts if account.get("enabled", True)]
        if account_id is not None:
            accounts = [account for account in accounts if account.get("id") == account_id]

        if not accounts:
            self._state["last_error"] = "No enabled Epic accounts configured."
            self._save_state()
            self._twitch.telegram.queue_status_update()
            return

        self._state.update(
            {
                "running": True,
                "active_account_id": None,
                "last_run_started_at": self._now(),
                "last_run_finished_at": None,
                "last_run_success": None,
                "last_error": None,
            }
        )
        self._save_state()
        self._twitch.telegram.queue_status_update()

        success = True
        try:
            for account in accounts:
                self._state["active_account_id"] = account["id"]
                self._save_state()
                await self._run_account(account)
        except Exception as exc:
            success = False
            self._state["last_error"] = str(exc)
            logger.warning("Free games module run failed", exc_info=True)
        finally:
            self._state.update(
                {
                    "running": False,
                    "active_account_id": None,
                    "last_run_finished_at": self._now(),
                    "last_run_success": success,
                }
            )
            self._save_state()
            self._twitch.telegram.queue_status_update()

    async def _run_account(self, account: dict[str, Any]) -> None:
        account_id = str(account["id"])
        started_at = self._now()
        account_state = self._state.setdefault("accounts", {}).setdefault(account_id, {})
        account_state.update({"last_run_started_at": started_at, "last_error": None})
        self._save_state()

        account_dir = self._account_data_dir(account_id)
        account_dir.mkdir(parents=True, exist_ok=True)
        log_path = account_dir / "last-run.log"

        command = self._build_command(account, account_dir)
        if command is None:
            account_state.update(
                {
                    "last_run_finished_at": self._now(),
                    "last_run_success": False,
                    "last_error": "Free games runner is not configured.",
                }
            )
            self._save_state()
            return

        logger.info("Running free games claimer for Epic account %s", account.get("name"))
        env = os.environ.copy()
        env.update(self._account_env(account, account_dir))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(account_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode(errors="replace")[-12000:]
        log_path.write_text(text, encoding="utf8")

        account_state.update(
            {
                "last_run_finished_at": self._now(),
                "last_run_success": process.returncode == 0,
                "last_error": None if process.returncode == 0 else f"Exited with {process.returncode}",
            }
        )
        self._save_state()

    def _build_command(self, account: dict[str, Any], account_dir: Path) -> list[str] | None:
        if self._runner == "docker":
            return self._docker_command(account, account_dir)
        if self._runner == "local":
            repo_dir = str(self._twitch.settings.free_games_claimer_path or "").strip()
            if not repo_dir:
                return None
            return ["node", str(Path(repo_dir) / "epic-games.js")]
        return None

    def _docker_command(self, account: dict[str, Any], account_dir: Path) -> list[str]:
        account_id = str(account["id"])
        host_account_dir = self._host_account_dir(account_id, account_dir)
        container_name = f"fgc-epic-{self._safe_id(account_id)}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "-v",
            f"{host_account_dir}:/fgc/data",
            "-e",
            "BROWSER_DIR=/fgc/data/browser",
            "-e",
            "SCREENSHOTS_DIR=/fgc/data/screenshots",
            "-e",
            "SHOW=1",
        ]
        env = self._account_env(account, account_dir)
        for key in ("EG_EMAIL", "EG_PASSWORD", "EG_OTPKEY", "EG_PARENTALPIN"):
            if env.get(key):
                command.extend(["-e", key])
        command.extend([self._image, "node", "epic-games"])
        return command

    def _account_env(self, account: dict[str, Any], account_dir: Path) -> dict[str, str]:
        env = {
            "BROWSER_DIR": str(account_dir / "browser"),
            "SCREENSHOTS_DIR": str(account_dir / "screenshots"),
        }
        mapping = {
            "email": "EG_EMAIL",
            "password": "EG_PASSWORD",
            "otpkey": "EG_OTPKEY",
            "parental_pin": "EG_PARENTALPIN",
        }
        for setting_key, env_key in mapping.items():
            value = str(account.get(setting_key) or "").strip()
            if value:
                env[env_key] = value
        return env

    def _account_status(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = str(account.get("id"))
        account_state = self._state.get("accounts", {}).get(account_id, {})
        claims = self._read_epic_claims(account_id)
        return {
            "id": account_id,
            "name": account.get("name") or account.get("email") or account_id,
            "email": account.get("email") or "",
            "enabled": account.get("enabled", True),
            "last_run_started_at": account_state.get("last_run_started_at"),
            "last_run_finished_at": account_state.get("last_run_finished_at"),
            "last_run_success": account_state.get("last_run_success"),
            "last_error": account_state.get("last_error"),
            "claimed_games": claims["claimed"],
            "failed_games": claims["failed"],
            "known_games_count": claims["known_count"],
        }

    def _read_epic_claims(self, account_id: str, limit: int = 8) -> dict[str, Any]:
        db_path = self._account_data_dir(account_id) / "db.json"
        data = json_load(db_path, {}, merge=False)
        claimed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        known_count = 0

        if not isinstance(data, dict):
            return {"claimed": claimed, "failed": failed, "known_count": known_count}

        for user_games in data.values():
            if not isinstance(user_games, dict):
                continue
            for game_id, game in user_games.items():
                if not isinstance(game, dict):
                    continue
                known_count += 1
                item = {
                    "id": str(game_id),
                    "title": game.get("title") or str(game_id),
                    "url": game.get("url") or "",
                    "time": game.get("time") or "",
                    "status": game.get("status") or "",
                }
                if item["status"] == "claimed":
                    claimed.append(item)
                elif str(item["status"]).startswith("failed"):
                    failed.append(item)

        claimed.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
        failed.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
        return {
            "claimed": claimed[:limit],
            "failed": failed[:limit],
            "known_count": known_count,
        }

    @property
    def _enabled(self) -> bool:
        return bool(getattr(self._twitch.settings, "free_games_enabled", False))

    @property
    def _runner(self) -> str:
        return str(getattr(self._twitch.settings, "free_games_runner", "docker") or "docker")

    @property
    def _image(self) -> str:
        return (
            str(getattr(self._twitch.settings, "free_games_image", "") or "").strip()
            or DEFAULT_FREE_GAMES_IMAGE
        )

    @property
    def _schedule_hours(self) -> int:
        return max(1, int(getattr(self._twitch.settings, "free_games_schedule_hours", 24) or 24))

    @property
    def _accounts(self) -> list[dict[str, Any]]:
        accounts = getattr(self._twitch.settings, "free_games_accounts", []) or []
        return [account for account in accounts if isinstance(account, dict) and account.get("id")]

    def _due_for_scheduled_run(self) -> bool:
        if self._run_task is not None and not self._run_task.done():
            return False
        next_run = self._next_run_at()
        if next_run is None:
            return bool(self._accounts)
        return datetime.fromisoformat(next_run) <= datetime.now().astimezone()

    def _next_run_at(self) -> str | None:
        finished_at = self._state.get("last_run_finished_at")
        if not finished_at:
            return None
        try:
            finished = datetime.fromisoformat(str(finished_at))
        except ValueError:
            return None
        return (finished + timedelta(hours=self._schedule_hours)).isoformat(timespec="seconds")

    def _save_state(self) -> None:
        json_save(FREE_GAMES_STATE_PATH, self._state, sort=True)

    def _account_data_dir(self, account_id: str) -> Path:
        return FREE_GAMES_DATA_DIR / "accounts" / self._safe_id(account_id)

    def _host_account_dir(self, account_id: str, account_dir: Path) -> str:
        host_data_dir = str(os.getenv("HOST_DATA_DIR") or "").strip()
        if host_data_dir:
            return "/".join(
                [host_data_dir.rstrip("/"), "free-games", "accounts", self._safe_id(account_id)]
            )
        return str(account_dir)

    def _safe_id(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "account"

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None] | None:
        try:
            return asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            logger.debug("No running event loop for free games task")
            return None
