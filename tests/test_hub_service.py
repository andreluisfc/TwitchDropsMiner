from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.services.hub_service import HubService


class StubHubModule:
    module_id = "stub-tool"

    def get_status(self):
        return {
            "id": self.module_id,
            "name": "Stub Tool",
            "enabled": True,
            "running": False,
            "updating": False,
            "status": "Idle",
            "actions": ["run"],
            "metrics": {},
            "details": {},
        }

    def run_action(self, action, params):
        return {"success": True, "module_id": self.module_id, "action": action, "params": params}

    def run_update(self):
        return {"success": True, "module_id": self.module_id, "action": "update"}


def test_hub_service_can_be_built_from_independent_modules():
    hub = HubService(modules=[StubHubModule()])

    assert hub.get_status()["modules"] == [StubHubModule().get_status()]
    assert hub.run_action("stub-tool", "run", {"fast": True}) == {
        "success": True,
        "module_id": "stub-tool",
        "action": "run",
        "params": {"fast": True},
    }
    assert hub.run_hub_action("update_all") == {
        "success": True,
        "action": "update_all",
        "results": [{"success": True, "module_id": "stub-tool", "action": "update"}],
    }


@pytest.mark.asyncio
async def test_hub_service_starts_and_stops_module_lifecycle():
    module = StubHubModule()
    module.start = AsyncMock()
    module.stop = AsyncMock()
    hub = HubService(modules=[module])

    await hub.start()
    await hub.stop()

    module.start.assert_awaited_once_with()
    module.stop.assert_awaited_once_with()


def test_hub_service_returns_twitch_and_epic_modules():
    watching_channel = SimpleNamespace(name="streamer")
    twitch = SimpleNamespace(
        _state="watching",
        gui=SimpleNamespace(
            status=SimpleNamespace(get=lambda: "Watching streamer"),
            login=SimpleNamespace(get_status=lambda: {"status": "Logged in"}),
        ),
        watching_channel=SimpleNamespace(get_with_default=lambda default: watching_channel),
        get_manual_mode_info=lambda: {"active": False},
        channels={1: watching_channel},
        inventory=[SimpleNamespace(id="campaign")],
        wanted_games=[SimpleNamespace(name="Game A")],
        free_games=SimpleNamespace(
            get_status=lambda: {
                "module": {
                    "id": "free-games-epic",
                    "name": "Epic Freebies",
                    "upstream": "https://github.com/vogler/free-games-claimer",
                    "update_strategy": "docker",
                    "actions": ["run", "run_account", "clear_attention", "update"],
                },
                "enabled": True,
                "running": False,
                "updating": False,
                "runner": "docker",
                "catalog": {
                    "current": [{"title": "OTXO"}],
                    "upcoming": [{"title": "Beacon Pines"}],
                },
                "source": {
                    "repository": "https://github.com/vogler/free-games-claimer",
                    "revision": "99c1f05302aeece21a628797cfdffb561ee38956",
                    "revision_url": "https://github.com/vogler/free-games-claimer/tree/99c1f05302aeece21a628797cfdffb561ee38956",
                    "build": "Thu, 15 May 2025 22:16:05 +0000",
                    "detected_from": "accounts/main/last-run.log",
                },
                "accounts": [
                    {
                        "id": "main",
                        "enabled": True,
                        "claimed_games": [{"title": "Game A"}],
                        "pending_claim_games": [{"title": "OTXO"}],
                    },
                    {"id": "disabled", "enabled": False, "claimed_games": []},
                ],
                "automation": {
                    "scheduled_accounts": 1,
                    "paused": False,
                    "pause_reason": None,
                },
                "image": "ghcr.io/vogler/free-games-claimer:latest",
                "next_run_at": "2026-07-31T10:00:00-03:00",
                "last_update_started_at": "2026-07-30T08:00:00-03:00",
                "last_update_finished_at": "2026-07-30T08:01:00-03:00",
                "last_update_success": True,
                "last_error": None,
                "attention": {
                    "required": True,
                    "reason": "captcha_required",
                    "message": "Epic captcha required.",
                    "account_id": "main",
                },
                "logs": {
                    "latest_run": {
                        "available": True,
                        "path": "accounts/main/last-run.log",
                        "updated_at": "2026-07-30T08:02:00-03:00",
                        "size_bytes": 2048,
                    },
                    "last_update": {
                        "available": True,
                        "path": "last-update.log",
                        "updated_at": "2026-07-30T08:01:00-03:00",
                        "size_bytes": 512,
                    },
                },
                "vnc": {"enabled": True, "active": False},
            }
        ),
    )

    status = HubService(twitch).get_status()

    assert status["name"] == "TDM Hub"
    assert status["actions"] == ["update_all"]
    assert [module["id"] for module in status["modules"]] == [
        "twitch-drops",
        "free-games-epic",
    ]
    twitch_module = status["modules"][0]
    assert twitch_module["running"]
    assert twitch_module["metrics"] == {
        "channels": 1,
        "campaigns": 1,
        "wanted_games": 1,
    }
    assert twitch_module["actions"] == ["reload"]
    assert twitch_module["details"]["source"]["version"]
    assert twitch_module["details"]["update"]["managed_by"] == "app_deploy"
    epic_module = status["modules"][1]
    assert epic_module["status"] == "Idle"
    assert epic_module["metrics"] == {
        "accounts": 2,
        "enabled_accounts": 1,
        "scheduled_accounts": 1,
        "claimed_games": 1,
        "pending_claims": 1,
        "current_freebies": 1,
        "upcoming_freebies": 1,
    }
    assert epic_module["details"]["source"]["revision"] == (
        "99c1f05302aeece21a628797cfdffb561ee38956"
    )
    assert epic_module["details"]["catalog"]["current"][0]["title"] == "OTXO"
    assert epic_module["details"]["update"] == {
        "strategy": "docker",
        "managed_by": "external_runner",
        "last_started_at": "2026-07-30T08:00:00-03:00",
        "last_finished_at": "2026-07-30T08:01:00-03:00",
        "last_success": True,
    }
    assert epic_module["details"]["attention"]["reason"] == "captcha_required"
    assert epic_module["details"]["automation"]["scheduled_accounts"] == 1
    assert epic_module["details"]["logs"]["latest_run"]["path"] == (
        "accounts/main/last-run.log"
    )


def test_hub_service_labels_epic_setup_state():
    twitch = SimpleNamespace(
        _state="idle",
        gui=None,
        watching_channel=SimpleNamespace(get_with_default=lambda default: None),
        get_manual_mode_info=lambda: {"active": False},
        channels={},
        inventory=[],
        wanted_games=[],
        free_games=SimpleNamespace(
            get_status=lambda: {
                "module": {},
                "enabled": True,
                "running": False,
                "updating": False,
                "accounts": [],
            }
        ),
    )

    epic_module = HubService(twitch).get_status()["modules"][1]

    assert epic_module["status"] == "No accounts configured"


def test_hub_service_marks_twitch_module_paused():
    twitch = SimpleNamespace(
        _state="watching",
        gui=None,
        watching_channel=SimpleNamespace(
            get_with_default=lambda default: SimpleNamespace(name="streamer")
        ),
        get_manual_mode_info=lambda: {"active": False},
        is_twitch_worker_paused=lambda: True,
        channels={},
        inventory=[],
        wanted_games=[],
        free_games=SimpleNamespace(get_status=lambda: {"module": {}, "accounts": []}),
    )

    twitch_module = HubService(twitch).get_status()["modules"][0]

    assert twitch_module["running"] is False
    assert twitch_module["status"] == "Paused for another hub module"
    assert twitch_module["details"]["paused"] is True


def test_hub_service_runs_twitch_reload_action():
    twitch = SimpleNamespace(
        change_state=MagicMock(),
        watching_channel=SimpleNamespace(get_with_default=lambda default: None),
        get_manual_mode_info=lambda: {"active": False},
        channels={},
        inventory=[],
        wanted_games=[],
        free_games=SimpleNamespace(get_status=lambda: {"module": {}, "accounts": []}),
    )

    result = HubService(twitch).run_action("twitch-drops", "reload")

    assert result == {"success": True, "module_id": "twitch-drops", "action": "reload"}
    twitch.change_state.assert_called_once_with(State.INVENTORY_FETCH)


def test_hub_service_runs_epic_actions():
    free_games = SimpleNamespace(
        update_runner=MagicMock(return_value=True),
        stop_run=MagicMock(return_value=True),
        get_status=MagicMock(return_value={"enabled": True}),
        account_exists=MagicMock(return_value=True),
        has_enabled_accounts=MagicMock(return_value=True),
        run_now=MagicMock(return_value=True),
        clear_attention=MagicMock(return_value=True),
    )
    twitch = SimpleNamespace(free_games=free_games)
    hub = HubService(twitch)

    assert hub.run_action("free-games-epic", "update") == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "update",
    }
    assert hub.run_action("free-games-epic", "run_account", {"account_id": "main"}) == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "run_account",
    }
    assert hub.run_action("free-games-epic", "clear_attention", {"account_id": "main"}) == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "clear_attention",
    }
    free_games.get_status.return_value = {"enabled": True, "running": True}
    assert hub.run_action("free-games-epic", "stop") == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "stop",
    }
    free_games.update_runner.assert_called_once_with()
    free_games.run_now.assert_called_once_with("main", interactive=True, exclusive=True)
    free_games.clear_attention.assert_called_once_with("main")
    free_games.stop_run.assert_called_once_with()


def test_hub_service_can_run_epic_account_in_background():
    free_games = SimpleNamespace(
        get_status=MagicMock(return_value={"enabled": True}),
        account_exists=MagicMock(return_value=True),
        has_enabled_accounts=MagicMock(return_value=True),
        run_now=MagicMock(return_value=True),
    )
    hub = HubService(SimpleNamespace(free_games=free_games))

    assert hub.run_action(
        "free-games-epic",
        "run_account",
        {"account_id": "main", "interactive": False},
    ) == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "run_account",
    }
    free_games.run_now.assert_called_once_with(
        "main", interactive=False, exclusive=True
    )


def test_hub_service_reports_epic_run_preflight_error():
    free_games = SimpleNamespace(
        get_status=MagicMock(
            return_value={
                "enabled": True,
                "last_error": "Not enough free-tier VM memory to start Epic safely.",
            }
        ),
        account_exists=MagicMock(return_value=True),
        has_enabled_accounts=MagicMock(return_value=True),
        run_now=MagicMock(return_value=False),
    )
    hub = HubService(SimpleNamespace(free_games=free_games))

    assert hub.run_action("free-games-epic", "run") == {
        "success": False,
        "status_code": 409,
        "detail": "Not enough free-tier VM memory to start Epic safely.",
    }


def test_hub_service_runs_update_all_action():
    free_games = SimpleNamespace(
        update_runner=MagicMock(return_value=True),
    )
    hub = HubService(SimpleNamespace(free_games=free_games))

    assert hub.run_hub_action("update_all") == {
        "success": True,
        "action": "update_all",
        "results": [
            {
                "success": True,
                "module_id": "twitch-drops",
                "action": "update",
                "skipped": True,
                "detail": "Built-in module updates are applied by deploying the app branch.",
            },
            {"success": True, "module_id": "free-games-epic", "action": "update"}
        ],
    }
    free_games.update_runner.assert_called_once_with()


def test_hub_service_reports_update_all_failures():
    free_games = SimpleNamespace(
        update_runner=MagicMock(return_value=False),
    )
    hub = HubService(SimpleNamespace(free_games=free_games))

    result = hub.run_hub_action("update_all")

    assert result["success"] is False
    assert result["status_code"] == 409
    assert result["detail"] == "One or more hub modules could not be updated"
    assert result["results"] == [
        {
            "success": True,
            "module_id": "twitch-drops",
            "action": "update",
            "skipped": True,
            "detail": "Built-in module updates are applied by deploying the app branch.",
        },
        {
            "success": False,
            "status_code": 409,
            "detail": "Free games module is already running or updating",
        }
    ]


def test_hub_service_rejects_invalid_action_requests():
    free_games = SimpleNamespace(
        update_runner=MagicMock(return_value=False),
        stop_run=MagicMock(return_value=False),
        get_status=MagicMock(return_value={"enabled": True}),
        account_exists=MagicMock(return_value=False),
        has_enabled_accounts=MagicMock(return_value=False),
        run_now=MagicMock(return_value=False),
        clear_attention=MagicMock(return_value=False),
    )
    hub = HubService(SimpleNamespace(free_games=free_games))

    assert hub.run_action("missing", "run")["status_code"] == 404
    assert hub.run_action("free-games-epic", "run_account") == {
        "success": False,
        "status_code": 400,
        "detail": "account_id is required",
    }
    assert hub.run_action("free-games-epic", "run_account", {"account_id": "missing"}) == {
        "success": False,
        "status_code": 404,
        "detail": "Epic account not found",
    }
    assert hub.run_action("free-games-epic", "clear_attention") == {
        "success": False,
        "status_code": 400,
        "detail": "account_id is required",
    }
    assert hub.run_action("free-games-epic", "clear_attention", {"account_id": "missing"}) == {
        "success": False,
        "status_code": 404,
        "detail": "Epic account not found",
    }
    free_games.get_status.return_value = {"enabled": True, "running": False}
    assert hub.run_action("free-games-epic", "stop") == {
        "success": False,
        "status_code": 409,
        "detail": "Free games module is not running",
    }
