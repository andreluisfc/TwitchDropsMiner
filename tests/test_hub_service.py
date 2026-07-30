from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import State
from src.services.hub_service import HubService


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
                    "actions": ["run", "run_account", "update"],
                },
                "enabled": True,
                "running": False,
                "updating": False,
                "runner": "docker",
                "source": {
                    "repository": "https://github.com/vogler/free-games-claimer",
                    "revision": "99c1f05302aeece21a628797cfdffb561ee38956",
                    "revision_url": "https://github.com/vogler/free-games-claimer/tree/99c1f05302aeece21a628797cfdffb561ee38956",
                    "build": "Thu, 15 May 2025 22:16:05 +0000",
                    "detected_from": "accounts/main/last-run.log",
                },
                "accounts": [
                    {"id": "main", "enabled": True, "claimed_games": [{"title": "Game A"}]},
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
    }
    assert epic_module["details"]["source"]["revision"] == (
        "99c1f05302aeece21a628797cfdffb561ee38956"
    )
    assert epic_module["details"]["update"] == {
        "strategy": "docker",
        "managed_by": "external_runner",
        "last_started_at": "2026-07-30T08:00:00-03:00",
        "last_finished_at": "2026-07-30T08:01:00-03:00",
        "last_success": True,
    }
    assert epic_module["details"]["attention"]["reason"] == "captcha_required"
    assert epic_module["details"]["automation"]["scheduled_accounts"] == 1


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
    free_games.get_status.return_value = {"enabled": True, "running": True}
    assert hub.run_action("free-games-epic", "stop") == {
        "success": True,
        "module_id": "free-games-epic",
        "action": "stop",
    }
    free_games.update_runner.assert_called_once_with()
    free_games.run_now.assert_called_once_with("main")
    free_games.stop_run.assert_called_once_with()


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
    free_games.get_status.return_value = {"enabled": True, "running": False}
    assert hub.run_action("free-games-epic", "stop") == {
        "success": False,
        "status_code": 409,
        "detail": "Free games module is not running",
    }
