"""Hub-level module catalog.

The hub owns coordination and observability across built-in and external modules.
Each module still owns its own behavior; this service only normalizes the status
surface so the web panel and Telegram can treat them consistently.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from src.config import State
from src.version import __version__


if TYPE_CHECKING:
    from src.core.client import Twitch


class HubService:
    """Builds a normalized catalog of modules controlled by the hub."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "TDM Hub",
            "version": __version__,
            "actions": ["update_all"],
            "modules": [
                self._twitch_drops_module(),
                self._epic_freebies_module(),
            ],
        }

    def run_hub_action(self, action: str) -> dict[str, Any]:
        if action != "update_all":
            return {
                "success": False,
                "status_code": 404,
                "detail": f"Unsupported hub action: {action}",
            }

        results = [
            self._run_epic_action("update", {}),
        ]
        failed = [result for result in results if not result.get("success")]
        if failed:
            return {
                "success": False,
                "status_code": 409,
                "detail": "One or more hub modules could not be updated",
                "results": results,
            }
        return {"success": True, "action": action, "results": results}

    def run_action(self, module_id: str, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if module_id == "twitch-drops":
            return self._run_twitch_action(action)
        if module_id == "free-games-epic":
            return self._run_epic_action(action, params)
        return {
            "success": False,
            "status_code": 404,
            "detail": f"Hub module not found: {module_id}",
        }

    def _run_twitch_action(self, action: str) -> dict[str, Any]:
        if action != "reload":
            return {
                "success": False,
                "status_code": 404,
                "detail": f"Unsupported Twitch Drops action: {action}",
            }
        self._twitch.change_state(State.INVENTORY_FETCH)
        return {"success": True, "module_id": "twitch-drops", "action": action}

    def _run_epic_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        free_games = self._twitch.free_games
        if action == "stop":
            status = free_games.get_status()
            if not status.get("running"):
                return {
                    "success": False,
                    "status_code": 409,
                    "detail": "Free games module is not running",
                }
            if not free_games.stop_run():
                return {
                    "success": False,
                    "status_code": 409,
                    "detail": "Free games module could not be stopped",
                }
            return {"success": True, "module_id": "free-games-epic", "action": action}

        if action == "update":
            if not free_games.update_runner():
                return {
                    "success": False,
                    "status_code": 409,
                    "detail": "Free games module is already running or updating",
                }
            return {"success": True, "module_id": "free-games-epic", "action": action}

        if action not in {"run", "run_account"}:
            return {
                "success": False,
                "status_code": 404,
                "detail": f"Unsupported Epic Freebies action: {action}",
            }

        account_id = str(params.get("account_id") or "").strip() or None
        if action == "run_account" and not account_id:
            return {
                "success": False,
                "status_code": 400,
                "detail": "account_id is required",
            }
        if not free_games.get_status().get("enabled"):
            return {
                "success": False,
                "status_code": 400,
                "detail": "Free games module is disabled",
            }
        if account_id and not free_games.account_exists(account_id):
            return {
                "success": False,
                "status_code": 404,
                "detail": "Epic account not found",
            }
        if not free_games.has_enabled_accounts(account_id):
            return {
                "success": False,
                "status_code": 400,
                "detail": "No enabled Epic accounts configured",
            }
        if not free_games.run_now(account_id):
            return {
                "success": False,
                "status_code": 409,
                "detail": "Free games module is already running",
            }
        return {"success": True, "module_id": "free-games-epic", "action": action}

    def _twitch_drops_module(self) -> dict[str, Any]:
        login = {}
        status = str(getattr(self._twitch, "_state", "idle"))
        gui = getattr(self._twitch, "gui", None)
        if gui is not None:
            with suppress(Exception):
                status = gui.status.get()
            with suppress(Exception):
                login = gui.login.get_status()

        watching_channel = self._twitch.watching_channel.get_with_default(None)
        manual_mode = self._twitch.get_manual_mode_info()
        return {
            "id": "twitch-drops",
            "name": "Twitch Drops",
            "kind": "builtin",
            "enabled": True,
            "running": watching_channel is not None,
            "updating": False,
            "status": status,
            "upstream": "https://github.com/rangermix/TwitchDropsMiner",
            "update_strategy": "git-fork",
            "actions": ["reload", "watch", "claim", "prioritize"],
            "metrics": {
                "channels": len(getattr(self._twitch, "channels", {})),
                "campaigns": len(getattr(self._twitch, "inventory", [])),
                "wanted_games": len(getattr(self._twitch, "wanted_games", [])),
            },
            "details": {
                "login": login,
                "manual_mode": manual_mode,
                "watching_channel": getattr(watching_channel, "name", None),
            },
        }

    def _epic_freebies_module(self) -> dict[str, Any]:
        status = self._twitch.free_games.get_status()
        metadata = status.get("module") or {}
        accounts = status.get("accounts") or []
        return {
            "id": metadata.get("id", "free-games-epic"),
            "name": metadata.get("name", "Epic Freebies"),
            "kind": "external-runner",
            "enabled": bool(status.get("enabled")),
            "running": bool(status.get("running")),
            "updating": bool(status.get("updating")),
            "status": self._free_games_label(status),
            "upstream": metadata.get("upstream", "https://github.com/vogler/free-games-claimer"),
            "update_strategy": metadata.get("update_strategy") or status.get("runner"),
            "actions": metadata.get("actions", ["run", "run_account", "stop", "update"]),
            "metrics": {
                "accounts": len(accounts),
                "enabled_accounts": sum(1 for account in accounts if account.get("enabled", True)),
                "claimed_games": sum(
                    len(account.get("claimed_games") or []) for account in accounts
                ),
            },
            "details": {
                "image": status.get("image"),
                "source": status.get("source"),
                "next_run_at": status.get("next_run_at"),
                "last_error": status.get("last_error"),
                "vnc": status.get("vnc"),
            },
        }

    def _free_games_label(self, status: dict[str, Any]) -> str:
        if not status.get("enabled"):
            return "Disabled"
        if status.get("updating"):
            return "Updating module"
        if status.get("running"):
            active = status.get("active_account_id") or "all accounts"
            return f"Running {active}"
        if not status.get("accounts"):
            return "No accounts configured"
        return "Idle"
