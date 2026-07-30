import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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
