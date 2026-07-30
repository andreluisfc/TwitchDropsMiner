import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services.free_games_service import SECRET_PLACEHOLDER, FreeGamesService
from src.web.managers.settings import SettingsManager


def make_twitch(settings):
    return SimpleNamespace(settings=settings, telegram=SimpleNamespace(queue_status_update=MagicMock()))


def test_free_games_service_reads_epic_claims(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", temp_path)
        account_dir = temp_path / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "db.json").write_text(
            json.dumps(
                {
                    "EpicUser": {
                        "game-a": {
                            "title": "Game A",
                            "url": "https://example.com/game-a",
                            "time": "2026-07-29 12:00:00.000",
                            "status": "claimed",
                        },
                        "game-b": {
                            "title": "Game B",
                            "url": "https://example.com/game-b",
                            "time": "2026-07-29 13:00:00.000",
                            "status": "failed",
                        },
                    }
                }
            ),
            encoding="utf8",
        )
        settings = SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:latest",
            free_games_schedule_hours=24,
            free_games_accounts=[{"id": "main", "name": "Main", "email": "main@example.com"}],
        )

        status = FreeGamesService(make_twitch(settings)).get_status()

        assert status["accounts"][0]["known_games_count"] == 2
        assert status["accounts"][0]["claimed_games"][0]["title"] == "Game A"
        assert status["accounts"][0]["failed_games"][0]["title"] == "Game B"


def test_free_games_status_exposes_module_metadata_and_account_lookup():
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:latest",
        free_games_schedule_hours=24,
        free_games_accounts=[{"id": "main", "name": "Main", "email": "main@example.com"}],
    )

    service = FreeGamesService(make_twitch(settings))
    status = service.get_status()

    assert status["module"]["id"] == "free-games-epic"
    assert status["module"]["upstream"] == "https://github.com/vogler/free-games-claimer"
    assert "run_account" in status["module"]["actions"]
    assert status["vnc"] == {
        "enabled": True,
        "url": None,
        "bind": "127.0.0.1:6080",
        "active": False,
    }
    assert service.account_exists("main")
    assert not service.account_exists("missing")


def test_free_games_docker_command_uses_account_env_without_secret_args(monkeypatch):
    monkeypatch.setenv("HOST_DATA_DIR", "/opt/tdm/data")
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:latest",
        free_games_schedule_hours=24,
        free_games_accounts=[],
    )
    service = FreeGamesService(make_twitch(settings))
    account = {
        "id": "main",
        "email": "main@example.com",
        "password": "secret-password",
        "otpkey": "otp-secret",
        "parental_pin": "1234",
        "vnc_password": "vnc-secret",
    }

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        command = service._docker_command(account, temp_path)
        env = service._account_env(account, temp_path)

    assert "secret-password" not in command
    assert "otp-secret" not in command
    assert "1234" not in command
    assert "vnc-secret" not in command
    assert env["EG_PASSWORD"] == "secret-password"
    assert env["VNC_PASSWORD"] == "vnc-secret"
    assert "/opt/tdm/data/free-games/accounts/main:/fgc/data" in command
    assert "127.0.0.1:6080:6080" in command


def test_free_games_docker_command_can_join_hub_network(monkeypatch):
    monkeypatch.setenv("HOST_DATA_DIR", "/opt/tdm/data")
    monkeypatch.setenv("HUB_DOCKER_NETWORK", "tdm-hub")
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:latest",
        free_games_schedule_hours=24,
        free_games_accounts=[],
    )
    service = FreeGamesService(make_twitch(settings))

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        command = service._docker_command({"id": "main"}, Path(temp_dir))

    assert "--network" in command
    assert "tdm-hub" in command
    assert "127.0.0.1:6080:6080" not in command


def test_free_games_vnc_target_uses_active_account_and_hub_network(monkeypatch):
    monkeypatch.setenv("HUB_DOCKER_NETWORK", "tdm-hub")
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:latest",
        free_games_schedule_hours=24,
        free_games_accounts=[{"id": "main"}],
    )
    service = FreeGamesService(make_twitch(settings))
    service._state["running"] = True
    service._state["active_account_id"] = "main"

    assert service.get_status()["vnc"] == {
        "enabled": True,
        "url": "/api/free-games/vnc/",
        "bind": "tdm-hub",
        "active": True,
    }
    assert (
        service.get_vnc_target_url("vnc.html", "autoconnect=1")
        == "http://fgc-epic-main:6080/vnc.html?autoconnect=1"
    )
    assert (
        service.get_vnc_target_url("websockify", "token=abc", websocket=True)
        == "ws://fgc-epic-main:6080/websockify?token=abc"
    )


def test_free_games_run_now_requires_enabled_account(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    twitch = make_twitch(
        SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:latest",
            free_games_schedule_hours=24,
            free_games_accounts=[{"id": "disabled", "enabled": False}],
        )
    )
    service = FreeGamesService(twitch)

    assert not service.run_now()
    assert service.get_status()["last_error"] == "No enabled Epic accounts configured."
    twitch.telegram.queue_status_update.assert_called_once()


@pytest.mark.asyncio
async def test_free_games_prepare_docker_container_removes_stale_container(monkeypatch):
    calls = []

    class FakeProcess:
        def __init__(self, returncode, output):
            self.returncode = returncode
            self._output = output

        async def communicate(self):
            return self._output, None

    async def fake_exec(*command, **kwargs):
        calls.append(command)
        if command[:2] == ("docker", "inspect"):
            return FakeProcess(0, b"false\n")
        return FakeProcess(0, b"removed\n")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[],
            )
        )
    )

    assert await service._prepare_docker_container("main", {})
    assert calls == [
        ("docker", "inspect", "-f", "{{.State.Running}}", "fgc-epic-main"),
        ("docker", "rm", "-f", "fgc-epic-main"),
    ]


@pytest.mark.asyncio
async def test_free_games_prepare_docker_container_refuses_active_container(monkeypatch):
    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"true\n", None

    async def fake_exec(*command, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[],
            )
        )
    )
    account_state = {}

    assert not await service._prepare_docker_container("main", account_state)
    assert account_state["last_run_success"] is False
    assert account_state["last_error"] == "Epic runner container is already active."


def test_settings_manager_masks_and_preserves_free_games_secrets(monkeypatch):
    settings = SimpleNamespace(
        free_games_accounts=[
            {
                "id": "main@example.com",
                "name": "Main",
                "email": "main@example.com",
                "password": "secret-password",
                "otpkey": "otp-secret",
                "parental_pin": "1234",
                "vnc_password": "vnc-secret",
                "enabled": True,
            }
        ],
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_panel_url="",
    )
    settings.save = MagicMock()
    broadcaster = SimpleNamespace(emit=MagicMock())
    manager = SettingsManager(broadcaster, settings, SimpleNamespace(print=MagicMock()))
    monkeypatch.setattr("asyncio.create_task", MagicMock())

    masked = manager.get_settings()["free_games_accounts"][0]
    assert masked["password"] == SECRET_PLACEHOLDER
    assert masked["otpkey"] == SECRET_PLACEHOLDER
    assert masked["parental_pin"] == SECRET_PLACEHOLDER
    assert masked["vnc_password"] == SECRET_PLACEHOLDER

    manager.update_settings(
        {
            "free_games_accounts": [
                {
                    "id": "main@example.com",
                    "name": "Main renamed",
                    "email": "main@example.com",
                    "password": SECRET_PLACEHOLDER,
                    "otpkey": SECRET_PLACEHOLDER,
                    "parental_pin": SECRET_PLACEHOLDER,
                    "vnc_password": SECRET_PLACEHOLDER,
                    "enabled": True,
                }
            ]
        }
    )

    assert settings.free_games_accounts[0]["name"] == "Main renamed"
    assert settings.free_games_accounts[0]["password"] == "secret-password"
    assert settings.free_games_accounts[0]["otpkey"] == "otp-secret"
    assert settings.free_games_accounts[0]["parental_pin"] == "1234"
    assert settings.free_games_accounts[0]["vnc_password"] == "vnc-secret"
