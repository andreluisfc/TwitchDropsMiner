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
FREE_GAMES_VNC_PROXY_PATH = "/api/free-games/vnc/"
FREE_GAMES_STARTUP_GRACE_MINUTES = 10


class FreeGamesService:
    """Runs and summarizes external free-game claiming modules."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._started_at = datetime.now().astimezone()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        defaults = self._default_state()
        loaded = json_load(FREE_GAMES_STATE_PATH, defaults, merge=False)
        state = defaults.copy()
        if isinstance(loaded, dict):
            for key in defaults:
                if key in loaded:
                    state[key] = loaded[key]
        if not isinstance(state.get("accounts"), dict):
            state["accounts"] = {}
        return state

    def _default_state(self) -> dict[str, Any]:
        return {
            "running": False,
            "updating": False,
            "active_account_id": None,
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "last_run_success": None,
            "last_update_started_at": None,
            "last_update_finished_at": None,
            "last_update_success": None,
            "last_error": None,
            "accounts": {},
        }

    async def start(self) -> None:
        await self._recover_interrupted_state()
        await self._adopt_active_docker_run_if_needed()
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = self._create_task(self._scheduler_loop())

    async def stop(self) -> None:
        for task in (self._scheduler_task, self._run_task, self._update_task):
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
            "module": {
                "id": "free-games-epic",
                "name": "Epic Freebies",
                "upstream": "https://github.com/vogler/free-games-claimer",
                "update_strategy": self._runner,
                "actions": ["run", "run_account", "update"],
            },
            "source": self._source_info(),
            "enabled": self._enabled,
            "runner": self._runner,
            "image": self._image,
            "schedule_hours": self._schedule_hours,
            "run_timeout_minutes": self._run_timeout_minutes,
            "running": bool(self._state.get("running")),
            "updating": bool(self._state.get("updating")),
            "active_account_id": self._state.get("active_account_id"),
            "last_run_started_at": self._state.get("last_run_started_at"),
            "last_run_finished_at": self._state.get("last_run_finished_at"),
            "last_run_success": self._state.get("last_run_success"),
            "last_update_started_at": self._state.get("last_update_started_at"),
            "last_update_finished_at": self._state.get("last_update_finished_at"),
            "last_update_success": self._state.get("last_update_success"),
            "last_error": self._state.get("last_error"),
            "next_run_at": self._next_run_at(),
            "vnc": self._vnc_status(),
            "accounts": [self._account_status(account) for account in self._accounts],
        }

    def run_now(self, account_id: str | None = None) -> bool:
        if not self._enabled:
            return False
        if not self.has_enabled_accounts(account_id):
            self._state["last_error"] = "No enabled Epic accounts configured."
            self._save_state()
            self._twitch.telegram.queue_status_update()
            return False
        if self._run_task is not None and not self._run_task.done():
            return False
        self._run_task = self._create_task(self._run_accounts(account_id))
        return self._run_task is not None

    def update_runner(self) -> bool:
        if self._run_task is not None and not self._run_task.done():
            return False
        if self._update_task is not None and not self._update_task.done():
            return False
        self._update_task = self._create_task(self._update_runner())
        return self._update_task is not None

    async def _scheduler_loop(self) -> None:
        while True:
            if self._state.get("running") and (
                self._run_task is None or self._run_task.done()
            ):
                await self._recover_interrupted_state()
                await self._adopt_active_docker_run_if_needed()
            if self._enabled and self._due_for_scheduled_run():
                self.run_now()
            await asyncio.sleep(60)

    async def _run_accounts(self, account_id: str | None = None) -> None:
        accounts = self._enabled_accounts(account_id)

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
        failed_accounts: list[str] = []
        try:
            for account in accounts:
                self._state["active_account_id"] = account["id"]
                self._save_state()
                account_success = await self._run_account(account)
                success = success and account_success
                if not account_success:
                    failed_accounts.append(str(account.get("name") or account["id"]))
            if failed_accounts:
                self._state["last_error"] = self._format_failed_accounts(failed_accounts)
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

    async def _update_runner(self) -> None:
        self._state.update(
            {
                "updating": True,
                "last_update_started_at": self._now(),
                "last_update_finished_at": None,
                "last_update_success": None,
                "last_error": None,
            }
        )
        self._save_state()
        self._twitch.telegram.queue_status_update()

        command: list[str] | None = None
        if self._runner == "docker":
            command = ["docker", "pull", self._image]
        elif self._runner == "local":
            repo_dir = str(self._twitch.settings.free_games_claimer_path or "").strip()
            if repo_dir:
                command = ["git", "-C", repo_dir, "pull", "--ff-only"]

        success = False
        try:
            if command is None:
                raise RuntimeError("Free games runner is not configured.")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            log_path = FREE_GAMES_DATA_DIR / "last-update.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output.decode(errors="replace")[-12000:], encoding="utf8")
            success = process.returncode == 0
            if not success:
                self._state["last_error"] = f"Update exited with {process.returncode}"
        except Exception as exc:
            self._state["last_error"] = str(exc)
            logger.warning("Free games module update failed", exc_info=True)
        finally:
            self._state.update(
                {
                    "updating": False,
                    "last_update_finished_at": self._now(),
                    "last_update_success": success,
                }
            )
            self._save_state()
            self._twitch.telegram.queue_status_update()

    def _format_failed_accounts(self, accounts: list[str]) -> str:
        shown_accounts = ", ".join(accounts[:3])
        remaining = len(accounts) - 3
        if remaining > 0:
            shown_accounts = f"{shown_accounts}, +{remaining} more"
        return f"Failed Epic accounts: {shown_accounts}"

    async def _run_account(self, account: dict[str, Any]) -> bool:
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
            return False

        if self._runner == "docker" and not await self._prepare_docker_container(
            account_id, account_state
        ):
            return False

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
        timed_out = False
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=self._run_timeout_seconds()
            )
        except TimeoutError:
            timed_out = True
            await self._stop_timed_out_process(process, account_id)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            except TimeoutError:
                output = b""
                logger.warning("Timed out while stopping Epic runner process")
        text = output.decode(errors="replace")[-12000:]
        log_path.write_text(text, encoding="utf8")
        success = process.returncode == 0 and not timed_out

        account_state.update(
            {
                "last_run_finished_at": self._now(),
                "last_run_success": success,
                "last_error": None
                if success
                else self._format_run_error(process.returncode, timed_out),
            }
        )
        self._save_state()
        return success

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
        container_name = self._docker_container_name(account_id)
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
        if self._docker_network:
            command.extend(["--network", self._docker_network])
        else:
            command.extend(["-p", "127.0.0.1:6080:6080"])
        env = self._account_env(account, account_dir)
        for key in ("EG_EMAIL", "EG_PASSWORD", "EG_OTPKEY", "EG_PARENTALPIN", "VNC_PASSWORD"):
            if env.get(key):
                command.extend(["-e", key])
        command.extend([self._image, "node", "epic-games"])
        return command

    async def _prepare_docker_container(
        self, account_id: str, account_state: dict[str, Any]
    ) -> bool:
        container_name = self._docker_container_name(account_id)
        inspect = await self._docker_output(
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            container_name,
        )
        if inspect[0] != 0:
            return True

        if inspect[1].strip().lower() == "true":
            account_state.update(
                {
                    "last_run_finished_at": self._now(),
                    "last_run_success": False,
                    "last_error": "Epic runner container is already active.",
                }
            )
            self._save_state()
            return False

        remove = await self._docker_output("docker", "rm", "-f", container_name)
        if remove[0] == 0:
            return True

        account_state.update(
            {
                "last_run_finished_at": self._now(),
                "last_run_success": False,
                "last_error": "Could not remove stale Epic runner container.",
            }
        )
        self._save_state()
        return False

    async def _docker_output(self, *command: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode, output.decode(errors="replace")

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
            "vnc_password": "VNC_PASSWORD",
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

    def _source_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "repository": "https://github.com/vogler/free-games-claimer",
            "revision": None,
            "revision_url": None,
            "build": None,
            "detected_from": None,
        }
        log_path = self._latest_run_log_path()
        if log_path is None:
            return info
        text = log_path.read_text(encoding="utf8", errors="replace")
        version_match = re.search(r"^Version:\s*(?P<value>.+)$", text, re.MULTILINE)
        build_match = re.search(r"^Build:\s*(?P<value>.+)$", text, re.MULTILINE)
        if version_match:
            version = version_match.group("value").strip()
            info["revision_url"] = version
            revision_match = re.search(r"/tree/(?P<revision>[0-9a-f]{7,40})$", version)
            info["revision"] = revision_match.group("revision") if revision_match else version
        if build_match:
            info["build"] = build_match.group("value").strip()
        info["detected_from"] = log_path.relative_to(FREE_GAMES_DATA_DIR).as_posix()
        return info

    def _latest_run_log_path(self) -> Path | None:
        account_dirs = FREE_GAMES_DATA_DIR / "accounts"
        if not account_dirs.exists():
            return None
        logs = [
            path
            for path in account_dirs.glob("*/last-run.log")
            if path.is_file()
        ]
        if not logs:
            return None
        return max(logs, key=lambda path: path.stat().st_mtime)

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
    def _run_timeout_minutes(self) -> int:
        return max(
            1,
            int(getattr(self._twitch.settings, "free_games_run_timeout_minutes", 15) or 15),
        )

    def _run_timeout_seconds(self) -> int:
        return self._run_timeout_minutes * 60

    def _remaining_run_timeout_seconds(self) -> int:
        started_at = self._state.get("last_run_started_at")
        if not started_at:
            return self._run_timeout_seconds()
        try:
            started = datetime.fromisoformat(str(started_at))
        except ValueError:
            return self._run_timeout_seconds()
        elapsed = (datetime.now().astimezone() - started).total_seconds()
        return max(1, int(self._run_timeout_seconds() - elapsed))

    @property
    def _accounts(self) -> list[dict[str, Any]]:
        accounts = getattr(self._twitch.settings, "free_games_accounts", []) or []
        return [account for account in accounts if isinstance(account, dict) and account.get("id")]

    def account_exists(self, account_id: str) -> bool:
        return any(account.get("id") == account_id for account in self._accounts)

    def has_enabled_accounts(self, account_id: str | None = None) -> bool:
        return bool(self._enabled_accounts(account_id))

    def _enabled_accounts(self, account_id: str | None = None) -> list[dict[str, Any]]:
        accounts = [account for account in self._accounts if account.get("enabled", True)]
        if account_id is not None:
            accounts = [account for account in accounts if account.get("id") == account_id]
        return accounts

    def _due_for_scheduled_run(self) -> bool:
        startup_elapsed = datetime.now().astimezone() - self._started_at
        if startup_elapsed < timedelta(minutes=FREE_GAMES_STARTUP_GRACE_MINUTES):
            return False
        if self._run_task is not None and not self._run_task.done():
            return False
        next_run = self._next_run_at()
        if next_run is None:
            return bool(self._accounts)
        return datetime.fromisoformat(next_run) <= datetime.now().astimezone()

    def _next_run_at(self) -> str | None:
        reference_at = self._last_attempt_at()
        if not reference_at:
            return None
        try:
            reference = datetime.fromisoformat(str(reference_at))
        except ValueError:
            return None
        return (reference + timedelta(hours=self._schedule_hours)).isoformat(timespec="seconds")

    def _last_attempt_at(self) -> str | None:
        timestamps = [
            self._state.get("last_run_finished_at"),
            self._state.get("last_run_started_at"),
        ]
        for account_state in self._state.get("accounts", {}).values():
            if isinstance(account_state, dict):
                timestamps.extend(
                    [
                        account_state.get("last_run_finished_at"),
                        account_state.get("last_run_started_at"),
                    ]
                )
        valid_timestamps: list[str] = []
        for timestamp in timestamps:
            if not timestamp:
                continue
            try:
                datetime.fromisoformat(str(timestamp))
            except ValueError:
                continue
            valid_timestamps.append(str(timestamp))
        return max(valid_timestamps) if valid_timestamps else None

    async def _recover_interrupted_state(self) -> None:
        if not self._state.get("running") and not self._state.get("updating"):
            return

        if self._state.get("running"):
            active_account_id = str(self._state.get("active_account_id") or "")
            if (
                self._runner == "docker"
                and active_account_id
                and await self._docker_container_running(active_account_id)
            ):
                return
            self._state["last_error"] = "Previous Epic run was interrupted."
            self._state["last_run_finished_at"] = self._now()
            self._state["last_run_success"] = False
        elif self._state.get("updating"):
            self._state["last_error"] = "Previous Epic module update was interrupted."
        self._state["running"] = False
        self._state["updating"] = False
        self._state["active_account_id"] = None
        self._save_state()

    async def _docker_container_running(self, account_id: str) -> bool:
        inspect = await self._docker_output(
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            self._docker_container_name(account_id),
        )
        return inspect[0] == 0 and inspect[1].strip().lower() == "true"

    async def _stop_timed_out_process(
        self, process: asyncio.subprocess.Process, account_id: str
    ) -> None:
        if self._runner == "docker":
            await self._docker_output("docker", "rm", "-f", self._docker_container_name(account_id))
        if process.returncode is None:
            process.kill()

    def _format_run_error(self, returncode: int | str | None, timed_out: bool) -> str:
        if timed_out:
            return f"Timed out after {self._run_timeout_minutes} minute(s)."
        return f"Exited with {returncode}"

    async def _adopt_active_docker_run_if_needed(self) -> bool:
        if self._runner != "docker" or not self._state.get("running"):
            return False
        if self._run_task is not None and not self._run_task.done():
            return True

        account_id = str(self._state.get("active_account_id") or "")
        if not account_id or not await self._docker_container_running(account_id):
            return False

        self._run_task = self._create_task(self._monitor_adopted_docker_container(account_id))
        return self._run_task is not None

    async def _monitor_adopted_docker_container(self, account_id: str) -> None:
        container_name = self._docker_container_name(account_id)
        account_state = self._state.setdefault("accounts", {}).setdefault(account_id, {})
        returncode_text = ""
        timed_out = False
        try:
            try:
                wait = await asyncio.wait_for(
                    self._docker_output("docker", "wait", container_name),
                    timeout=self._remaining_run_timeout_seconds(),
                )
            except TimeoutError:
                timed_out = True
                await self._docker_output("docker", "rm", "-f", container_name)
                wait = (0, "")
            returncode_text = wait[1].strip()
            logs = await self._docker_output("docker", "logs", container_name)
            if logs[1]:
                log_path = self._account_data_dir(account_id) / "last-run.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(logs[1][-12000:], encoding="utf8")
            success = not timed_out and wait[0] == 0 and returncode_text == "0"
            account_state.update(
                {
                    "last_run_finished_at": self._now(),
                    "last_run_success": success,
                    "last_error": None
                    if success
                    else self._format_run_error(returncode_text or wait[0], timed_out),
                }
            )
            self._state.update(
                {
                    "running": False,
                    "active_account_id": None,
                    "last_run_finished_at": self._now(),
                    "last_run_success": success,
                    "last_error": None
                    if success
                    else self._format_run_error(returncode_text or wait[0], timed_out),
                }
            )
            self._save_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            account_state.update(
                {
                    "last_run_finished_at": self._now(),
                    "last_run_success": False,
                    "last_error": str(exc),
                }
            )
            self._state.update(
                {
                    "running": False,
                    "active_account_id": None,
                    "last_run_finished_at": self._now(),
                    "last_run_success": False,
                    "last_error": str(exc),
                }
            )
            self._save_state()
            logger.warning("Adopted Epic runner monitoring failed", exc_info=True)
        finally:
            self._twitch.telegram.queue_status_update()

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

    @property
    def _docker_network(self) -> str:
        return str(os.getenv("HUB_DOCKER_NETWORK") or "").strip()

    def _docker_container_name(self, account_id: str) -> str:
        return f"fgc-epic-{self._safe_id(account_id)}"

    def _vnc_status(self) -> dict[str, Any]:
        active = bool(self._state.get("running") and self._state.get("active_account_id"))
        status = {
            "enabled": self._runner == "docker",
            "url": FREE_GAMES_VNC_PROXY_PATH if active else None,
            "bind": self._docker_network or "127.0.0.1:6080",
            "active": active,
        }
        return status

    def get_vnc_target_url(self, path: str = "", query: str = "", *, websocket: bool = False) -> str | None:
        if self._runner != "docker" or not self._state.get("running"):
            return None
        account_id = str(self._state.get("active_account_id") or "")
        if not account_id:
            return None
        scheme = "ws" if websocket else "http"
        base = f"{scheme}://{self._docker_container_name(account_id)}:6080"
        target = f"{base}/{path.lstrip('/')}"
        if query:
            target = f"{target}?{query}"
        return target

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
