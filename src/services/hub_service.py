"""Hub-level module catalog.

The hub owns coordination and observability across built-in and external modules.
Each module still owns its own behavior; this service only normalizes the status
surface so the web panel and Telegram can treat them consistently.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

from src.config import State
from src.version import __version__


if TYPE_CHECKING:
    from src.core.client import Twitch


class HubService:
    """Builds a normalized catalog of modules controlled by the hub."""

    def __init__(
        self,
        twitch: Twitch | None = None,
        *,
        modules: Iterable[HubModuleAdapter] | None = None,
    ) -> None:
        if modules is None:
            if twitch is None:
                raise ValueError("twitch is required when hub modules are not provided")
            modules = (
                TwitchDropsModuleAdapter(twitch),
                EpicFreeGamesModuleAdapter(twitch.free_games),
            )
        self._modules: dict[str, HubModuleAdapter] = {
            module.module_id: module for module in modules
        }

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "TDM Hub",
            "version": __version__,
            "actions": ["update_all"],
            "modules": [module.get_status() for module in self._modules.values()],
        }

    def run_hub_action(self, action: str) -> dict[str, Any]:
        if action != "update_all":
            return {
                "success": False,
                "status_code": 404,
                "detail": f"Unsupported hub action: {action}",
            }

        results = [module.run_update() for module in self._modules.values()]
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
        module = self._modules.get(module_id)
        if module is not None:
            return module.run_action(action, params)
        return {
            "success": False,
            "status_code": 404,
            "detail": f"Hub module not found: {module_id}",
        }


class HubModuleAdapter(Protocol):
    """Normalizes one tool behind the hub."""

    module_id: str

    def get_status(self) -> dict[str, Any]:
        """Return the module status in hub catalog format."""

    def run_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one module action."""

    def run_update(self) -> dict[str, Any]:
        """Run the module update action for hub-level update all."""


class TwitchDropsModuleAdapter:
    """Hub adapter for the built-in Twitch drops tool."""

    module_id = "twitch-drops"

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    def _run_twitch_action(self, action: str) -> dict[str, Any]:
        if action != "reload":
            return {
                "success": False,
                "status_code": 404,
                "detail": f"Unsupported Twitch Drops action: {action}",
            }
        self._twitch.change_state(State.INVENTORY_FETCH)
        return {"success": True, "module_id": "twitch-drops", "action": action}

    def run_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._run_twitch_action(action)

    def run_update(self) -> dict[str, Any]:
        return {
            "success": True,
            "module_id": "twitch-drops",
            "action": "update",
            "skipped": True,
            "detail": "Built-in module updates are applied by deploying the app branch.",
        }

    def get_status(self) -> dict[str, Any]:
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
            "id": self.module_id,
            "name": "Twitch Drops",
            "kind": "builtin",
            "enabled": True,
            "running": watching_channel is not None,
            "updating": False,
            "status": status,
            "upstream": "https://github.com/rangermix/TwitchDropsMiner",
            "update_strategy": "git-fork",
            "actions": ["reload"],
            "metrics": {
                "channels": len(getattr(self._twitch, "channels", {})),
                "campaigns": len(getattr(self._twitch, "inventory", [])),
                "wanted_games": len(getattr(self._twitch, "wanted_games", [])),
            },
            "details": {
                "source": {
                    "repository": "https://github.com/rangermix/TwitchDropsMiner",
                    "version": __version__,
                    "revision": None,
                    "revision_url": None,
                    "detected_from": "app",
                },
                "update": {
                    "strategy": "git-fork",
                    "managed_by": "app_deploy",
                    "last_started_at": None,
                    "last_finished_at": None,
                    "last_success": None,
                },
                "login": login,
                "manual_mode": manual_mode,
                "watching_channel": getattr(watching_channel, "name", None),
            },
        }


class EpicFreeGamesModuleAdapter:
    """Hub adapter for the external Epic freebies runner."""

    module_id = "free-games-epic"

    def __init__(self, free_games: Any) -> None:
        self._free_games = free_games

    def run_update(self) -> dict[str, Any]:
        return self.run_action("update", {})

    def run_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        free_games = self._free_games
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

        if action == "clear_attention":
            account_id = str(params.get("account_id") or "").strip()
            if not account_id:
                return {
                    "success": False,
                    "status_code": 400,
                    "detail": "account_id is required",
                }
            if not free_games.account_exists(account_id):
                return {
                    "success": False,
                    "status_code": 404,
                    "detail": "Epic account not found",
                }
            if not free_games.clear_attention(account_id):
                return {
                    "success": False,
                    "status_code": 409,
                    "detail": "Epic attention could not be cleared",
                }
            return {
                "success": True,
                "module_id": "free-games-epic",
                "action": action,
            }

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

    def get_status(self) -> dict[str, Any]:
        status = self._free_games.get_status()
        metadata = status.get("module") or {}
        accounts = status.get("accounts") or []
        automation = status.get("automation") or {}
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
                "scheduled_accounts": automation.get("scheduled_accounts", 0),
                "claimed_games": sum(
                    len(account.get("claimed_games") or []) for account in accounts
                ),
            },
            "details": {
                "image": status.get("image"),
                "source": status.get("source"),
                "update": self._free_games_update_details(status),
                "attention": status.get("attention"),
                "automation": automation,
                "logs": status.get("logs"),
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
        automation = status.get("automation") or {}
        if automation.get("paused"):
            return "Automation paused"
        return "Idle"

    def _free_games_update_details(self, status: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy": status.get("runner"),
            "managed_by": "external_runner",
            "last_started_at": status.get("last_update_started_at"),
            "last_finished_at": status.get("last_update_finished_at"),
            "last_success": status.get("last_update_success"),
        }
