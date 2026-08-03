"""Free games module orchestration.

This module intentionally treats vogler/free-games-claimer as an external runner.
Keeping the upstream project at arm's length makes updates much easier: the hub owns
configuration, scheduling, state and UI; the claimer owns store automation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Coroutine
from contextlib import suppress
from datetime import datetime, timedelta, timezone
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
FREE_GAMES_BROWSER_WIDTH = 800
FREE_GAMES_BROWSER_HEIGHT = 600
FREE_GAMES_DOCKER_CPUS = "0.30"
FREE_GAMES_DOCKER_MEMORY = "512m"


class FreeGamesService:
    """Runs and summarizes external free-game claiming modules."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._started_at = datetime.now().astimezone()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_requested = False
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
        for task in (self._scheduler_task, self._run_task, self._update_task, self._stop_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._scheduler_task = None
        self._run_task = None
        self._stop_task = None

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
                "actions": ["run", "run_account", "clear_attention", "stop", "update"],
            },
            "source": self._source_info(),
            "attention": self._attention_info(),
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
            "automation": self._automation_status(),
            "next_run_at": self._status_next_run_at(),
            "logs": self._logs_info(),
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

    def clear_attention(self, account_id: str) -> bool:
        if not self.account_exists(account_id):
            return False
        account_state = self._state.setdefault("accounts", {}).setdefault(account_id, {})
        account_state["attention_cleared_at"] = datetime.now().astimezone().isoformat()
        account_state["last_error"] = None
        if self._state.get("active_account_id") in {None, "", account_id}:
            self._state["last_error"] = None
        self._save_state()
        self._twitch.telegram.queue_status_update()
        return True

    def get_log(
        self,
        kind: str,
        *,
        account_id: str | None = None,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        log_path = self._log_path_for_kind(kind, account_id)
        info = self._log_info(log_path)
        normalized_max_chars = min(max(1000, int(max_chars or 8000)), 20000)
        if not info["available"] or log_path is None:
            return {
                **info,
                "kind": kind,
                "content": "",
                "truncated": False,
            }

        text = log_path.read_text(encoding="utf8", errors="replace")
        truncated = len(text) > normalized_max_chars
        return {
            **info,
            "kind": kind,
            "content": self._redact_log_text(text[-normalized_max_chars:]),
            "truncated": truncated,
        }

    def stop_run(self) -> bool:
        if not self._state.get("running"):
            return False
        if self._stop_task is not None and not self._stop_task.done():
            return False
        self._stop_requested = True
        self._stop_task = self._create_task(
            self._stop_active_run(str(self._state.get("active_account_id") or ""))
        )
        return self._stop_task is not None

    async def _scheduler_loop(self) -> None:
        while True:
            if self._state.get("running") and (
                self._run_task is None or self._run_task.done()
            ):
                await self._recover_interrupted_state()
                await self._adopt_active_docker_run_if_needed()
            if self._enabled and self._due_for_scheduled_run():
                self._run_scheduled_now()
            await asyncio.sleep(60)

    def _run_scheduled_now(self) -> bool:
        if self._run_task is not None and not self._run_task.done():
            return False
        if not self._scheduled_accounts():
            return False
        self._run_task = self._create_task(self._run_accounts(scheduled=True))
        return self._run_task is not None

    async def _run_accounts(self, account_id: str | None = None, *, scheduled: bool = False) -> None:
        accounts = self._enabled_accounts(account_id)
        if scheduled and account_id is None:
            accounts = self._scheduled_accounts()

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
        self._stop_requested = False
        try:
            for account in accounts:
                self._state["active_account_id"] = account["id"]
                self._save_state()
                account_success = await self._run_account(account)
                success = success and account_success
                if not account_success:
                    failed_accounts.append(str(account.get("name") or account["id"]))
                if self._stop_requested:
                    break
            if failed_accounts:
                self._state["last_error"] = (
                    "Epic run stopped by user."
                    if self._stop_requested
                    else self._format_failed_accounts(failed_accounts)
                )
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
            self._stop_requested = False

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
            output_text = output.decode(errors="replace")[-12000:]
            log_path.write_text(output_text, encoding="utf8")
            success = process.returncode == 0
            if success:
                self._write_update_source_info(output_text)
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
        account_state.update(
            {
                "last_run_started_at": started_at,
                "last_error": None,
                "attention_cleared_at": datetime.now().astimezone().isoformat(),
            }
        )
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
        success = process.returncode == 0 and not timed_out and not self._stop_requested
        attention = self._classify_attention(text, account_id)
        last_error = None
        if not success:
            last_error = attention.get("message") or self._format_run_error(
                process.returncode, timed_out, self._stop_requested
            )

        account_state.update(
            {
                "last_run_finished_at": self._now(),
                "last_run_success": success,
                "last_error": last_error,
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
            "--cpus",
            FREE_GAMES_DOCKER_CPUS,
            "--memory",
            FREE_GAMES_DOCKER_MEMORY,
            "--shm-size",
            "128m",
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
        for key in (
            "EG_EMAIL",
            "EG_PASSWORD",
            "EG_OTPKEY",
            "EG_PARENTALPIN",
            "VNC_PASSWORD",
            "LOGIN_TIMEOUT",
            "TIMEOUT",
            "WIDTH",
            "HEIGHT",
        ):
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
            "LOGIN_TIMEOUT": str(self._claimer_login_timeout_seconds()),
            "TIMEOUT": str(self._claimer_action_timeout_seconds()),
            "WIDTH": str(FREE_GAMES_BROWSER_WIDTH),
            "HEIGHT": str(FREE_GAMES_BROWSER_HEIGHT),
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

    def _claimer_login_timeout_seconds(self) -> int:
        return max(180, self._run_timeout_seconds() - 60)

    def _claimer_action_timeout_seconds(self) -> int:
        return max(60, min(180, self._claimer_login_timeout_seconds()))

    def _account_status(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = str(account.get("id"))
        account_state = self._state.get("accounts", {}).get(account_id, {})
        claims = self._read_epic_claims(account_id)
        attention = self._account_attention_info(account_id)
        automation = self._account_automation_info(account, attention)
        return {
            "id": account_id,
            "name": account.get("name") or account.get("email") or account_id,
            "email": account.get("email") or "",
            "enabled": account.get("enabled", True),
            "last_run_started_at": account_state.get("last_run_started_at"),
            "last_run_finished_at": account_state.get("last_run_finished_at"),
            "last_run_success": account_state.get("last_run_success"),
            "last_error": account_state.get("last_error"),
            "attention_cleared_at": account_state.get("attention_cleared_at"),
            "attention": attention,
            "automation": automation,
            "logs": {
                "last_run": self._log_info(self._account_run_log_path(account_id)),
            },
            "claimed_games": claims["claimed"],
            "failed_games": claims["failed"],
            "known_games_count": claims["known_count"],
        }

    def _source_info(self) -> dict[str, Any]:
        update_source = self._source_info_from_update()
        run_source = self._source_info_from_run_log()
        if (
            self._source_updated_at(update_source) >= self._source_updated_at(run_source)
            and (update_source.get("revision") or not run_source.get("revision"))
        ):
            return update_source
        return run_source

    def _empty_source_info(self) -> dict[str, Any]:
        return {
            "repository": "https://github.com/vogler/free-games-claimer",
            "image": None,
            "revision": None,
            "revision_url": None,
            "build": None,
            "detected_from": None,
            "updated_at": None,
        }

    def _source_info_from_run_log(self) -> dict[str, Any]:
        info: dict[str, Any] = self._empty_source_info()
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
        info["updated_at"] = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone().isoformat(
            timespec="seconds"
        )
        return info

    def _source_info_from_update(self) -> dict[str, Any]:
        info: dict[str, Any] = self._empty_source_info()
        data = json_load(self._source_info_path(), {}, merge=False)
        if not isinstance(data, dict):
            return info
        for key in info:
            if key in data:
                info[key] = data[key]
        return info

    def _write_update_source_info(self, update_output: str) -> None:
        digest_match = re.search(r"^Digest:\s*(?P<digest>sha256:[0-9a-f]{64})$", update_output, re.MULTILINE)
        source = self._empty_source_info()
        source.update(
            {
                "image": self._image,
                "revision": digest_match.group("digest") if digest_match else None,
                "revision_url": None,
                "detected_from": "last-update.log",
                "updated_at": self._now(),
            }
        )
        source_path = self._source_info_path()
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            json.dumps(source, indent=2, sort_keys=True),
            encoding="utf8",
        )

    def _source_info_path(self) -> Path:
        return FREE_GAMES_DATA_DIR / "last-source.json"

    def _source_updated_at(self, source: dict[str, Any]) -> datetime:
        value = source.get("updated_at")
        if not value:
            return datetime.fromtimestamp(0, timezone.utc).astimezone()
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.fromtimestamp(0, timezone.utc).astimezone()

    def _attention_info(self) -> dict[str, Any]:
        info = self._empty_attention_info()
        log_path = self._latest_run_log_path()
        if log_path is None:
            return info
        account_id = log_path.parent.name
        text = log_path.read_text(encoding="utf8", errors="replace")
        account_id = self._account_id_for_data_dir_name(account_id)
        attention = self._classify_attention(text, account_id)
        if attention and self._attention_was_cleared(account_id, log_path):
            return info
        return attention or info

    def _account_attention_info(self, account_id: str) -> dict[str, Any]:
        log_path = self._account_run_log_path(account_id)
        if log_path is None:
            return self._empty_attention_info(account_id)
        text = log_path.read_text(encoding="utf8", errors="replace")
        attention = self._classify_attention(text, account_id)
        if attention and self._attention_was_cleared(account_id, log_path):
            return self._empty_attention_info(account_id)
        return attention or self._empty_attention_info(account_id)

    def _empty_attention_info(self, account_id: str | None = None) -> dict[str, Any]:
        return {
            "required": False,
            "reason": None,
            "message": None,
            "account_id": account_id,
        }

    def _account_automation_info(
        self, account: dict[str, Any], attention: dict[str, Any]
    ) -> dict[str, Any]:
        if account.get("enabled") is False:
            return {"eligible": False, "blocked_reason": "disabled"}
        if attention.get("required"):
            return {"eligible": False, "blocked_reason": "attention_required"}
        return {"eligible": True, "blocked_reason": None}

    def _classify_attention(self, text: str, account_id: str | None = None) -> dict[str, Any]:
        lower_text = text.lower()
        if "captcha" in lower_text:
            return {
                "required": True,
                "reason": "captcha_required",
                "message": "Epic captcha required. Start a manual run and use Browser to solve it.",
                "account_id": account_id,
            }
        if "login timeout" in lower_text:
            return {
                "required": True,
                "reason": "login_timeout",
                "message": "Epic login timed out. Start a manual run and use Browser to sign in.",
                "account_id": account_id,
            }
        if "not signed in anymore" in lower_text or "please login" in lower_text:
            return {
                "required": True,
                "reason": "login_required",
                "message": "Epic login required. Start a manual run and use Browser to sign in.",
                "account_id": account_id,
            }
        return {}

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

    def _account_run_log_path(self, account_id: str) -> Path | None:
        log_path = self._account_data_dir(account_id) / "last-run.log"
        return log_path if log_path.is_file() else None

    def _account_id_for_data_dir_name(self, data_dir_name: str) -> str:
        for account in self._accounts:
            account_id = str(account["id"])
            if self._safe_id(account_id) == data_dir_name:
                return account_id
        return data_dir_name

    def _attention_was_cleared(self, account_id: str, log_path: Path) -> bool:
        account_state = self._state.get("accounts", {}).get(account_id, {})
        cleared_at = account_state.get("attention_cleared_at") if isinstance(account_state, dict) else None
        if not cleared_at:
            return False
        try:
            cleared = datetime.fromisoformat(str(cleared_at))
        except ValueError:
            return False
        log_updated = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone()
        return cleared >= log_updated

    def _logs_info(self) -> dict[str, Any]:
        return {
            "latest_run": self._log_info(self._latest_run_log_path()),
            "last_update": self._log_info(FREE_GAMES_DATA_DIR / "last-update.log"),
        }

    def _log_path_for_kind(self, kind: str, account_id: str | None = None) -> Path | None:
        normalized_kind = kind.replace("-", "_")
        if normalized_kind == "latest_run":
            return self._latest_run_log_path()
        if normalized_kind == "last_update":
            return FREE_GAMES_DATA_DIR / "last-update.log"
        if normalized_kind == "account_run":
            if not account_id:
                return None
            return self._account_run_log_path(account_id)
        return None

    def _log_info(self, log_path: Path | None) -> dict[str, Any]:
        if log_path is None or not log_path.is_file():
            return {
                "available": False,
                "path": None,
                "updated_at": None,
                "size_bytes": 0,
            }
        stat = log_path.stat()
        try:
            relative_path = log_path.relative_to(FREE_GAMES_DATA_DIR).as_posix()
        except ValueError:
            relative_path = log_path.name
        return {
            "available": True,
            "path": relative_path,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
                timespec="seconds"
            ),
            "size_bytes": stat.st_size,
        }

    def _redact_log_text(self, text: str) -> str:
        redacted = text
        for account in self._accounts:
            for key in ("password", "otpkey", "parental_pin", "vnc_password"):
                value = str(account.get(key) or "").strip()
                if len(value) >= 4:
                    redacted = redacted.replace(value, "[redacted]")
        redaction_patterns = [
            r"(?i)(bearer\s+)[^\s]+",
            r"(?i)(authorization:\s*)[^\r\n]+",
            r"(?i)(cookie:\s*)[^\r\n]+",
            r"(?i)((?:password|passwd|otpkey|parentalpin|vnc_password)\s*[=:]\s*)[^\s]+",
            r"(?i)((?:EG_PASSWORD|EG_OTPKEY|EG_PARENTALPIN|VNC_PASSWORD)=)[^\s]+",
        ]
        for pattern in redaction_patterns:
            redacted = re.sub(pattern, r"\1[redacted]", redacted)
        return redacted

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

    def _scheduled_accounts(self) -> list[dict[str, Any]]:
        return [
            account
            for account in self._enabled_accounts()
            if not self._account_attention_info(str(account["id"])).get("required")
        ]

    def _due_for_scheduled_run(self) -> bool:
        startup_elapsed = datetime.now().astimezone() - self._started_at
        if startup_elapsed < timedelta(minutes=FREE_GAMES_STARTUP_GRACE_MINUTES):
            return False
        if self._run_task is not None and not self._run_task.done():
            return False
        if not self._scheduled_accounts():
            return False
        next_run = self._next_run_at()
        if next_run is None:
            return bool(self._accounts)
        return datetime.fromisoformat(next_run) <= datetime.now().astimezone()

    def _status_next_run_at(self) -> str | None:
        if not self._scheduled_accounts():
            return None
        return self._next_run_at()

    def _automation_status(self) -> dict[str, Any]:
        enabled_accounts = self._enabled_accounts()
        scheduled_accounts = self._scheduled_accounts()
        paused = self._enabled and bool(enabled_accounts) and not scheduled_accounts
        return {
            "scheduled_accounts": len(scheduled_accounts),
            "paused": paused,
            "pause_reason": "attention_required" if paused else None,
        }

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

    async def _stop_active_run(self, account_id: str) -> None:
        if self._runner == "docker" and account_id:
            await self._docker_output("docker", "rm", "-f", self._docker_container_name(account_id))

    def _format_run_error(
        self, returncode: int | str | None, timed_out: bool, stopped: bool = False
    ) -> str:
        if stopped:
            return "Stopped by user."
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
                    else self._format_run_error(
                        returncode_text or wait[0], timed_out, self._stop_requested
                    ),
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
                    else self._format_run_error(
                        returncode_text or wait[0], timed_out, self._stop_requested
                    ),
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
            self._stop_requested = False

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
