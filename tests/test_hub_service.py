from types import SimpleNamespace

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
                "accounts": [
                    {"id": "main", "enabled": True, "claimed_games": [{"title": "Game A"}]},
                    {"id": "disabled", "enabled": False, "claimed_games": []},
                ],
                "image": "ghcr.io/vogler/free-games-claimer:latest",
                "next_run_at": "2026-07-31T10:00:00-03:00",
                "last_error": None,
                "vnc": {"enabled": True, "active": False},
            }
        ),
    )

    status = HubService(twitch).get_status()

    assert status["name"] == "TDM Hub"
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
    epic_module = status["modules"][1]
    assert epic_module["status"] == "Idle"
    assert epic_module["metrics"] == {
        "accounts": 2,
        "enabled_accounts": 1,
        "claimed_games": 1,
    }


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
