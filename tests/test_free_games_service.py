import asyncio
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    assert status["run_timeout_minutes"] == 15
    assert status["vnc"] == {
        "enabled": True,
        "url": None,
        "bind": "127.0.0.1:6080",
        "active": False,
    }
    assert service.account_exists("main")
    assert not service.account_exists("missing")


def test_free_games_status_reads_upstream_revision_from_run_log(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "\n".join(
                [
                    "Version: https://github.com/vogler/free-games-claimer/tree/99c1f05302aeece21a628797cfdffb561ee38956",
                    "Build: Thu, 15 May 2025 22:16:05 +0000",
                    "started checking epic-games",
                ]
            ),
            encoding="utf8",
        )
        (data_dir / "last-update.log").write_text("Image is up to date", encoding="utf8")
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main"}],
                )
            )
        )
        status = service.get_status()
        source = status["source"]

    assert source["repository"] == "https://github.com/vogler/free-games-claimer"
    assert source["revision"] == "99c1f05302aeece21a628797cfdffb561ee38956"
    assert source["revision_url"].endswith("/99c1f05302aeece21a628797cfdffb561ee38956")
    assert source["build"] == "Thu, 15 May 2025 22:16:05 +0000"
    assert source["detected_from"] == "accounts/main/last-run.log"
    assert status["logs"]["latest_run"]["available"] is True
    assert status["logs"]["latest_run"]["path"] == "accounts/main/last-run.log"
    assert status["logs"]["latest_run"]["updated_at"]
    assert status["logs"]["last_update"]["available"] is True
    assert status["logs"]["last_update"]["path"] == "last-update.log"
    assert status["accounts"][0]["logs"]["last_run"]["path"] == "accounts/main/last-run.log"


def test_free_games_status_detects_epic_captcha_attention(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "\n".join(
                [
                    "Not signed in anymore. Please login in the browser or here in the terminal.",
                    "Got a captcha during login (likely due to too many attempts)!",
                ]
            ),
            encoding="utf8",
        )
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main"}],
                )
            )
        )
        attention = service.get_status()["attention"]

    assert attention == {
        "required": True,
        "reason": "captcha_required",
        "message": "Epic captcha required. Start a manual run and use Browser to solve it.",
        "account_id": "main",
    }


def test_free_games_status_marks_automation_paused_when_all_accounts_need_attention(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "Got a captcha during login!",
            encoding="utf8",
        )
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main"}],
                )
            )
        )
        service._started_at = service._started_at - timedelta(minutes=11)
        status = service.get_status()
        due = service._due_for_scheduled_run()

    assert status["automation"] == {
        "scheduled_accounts": 0,
        "paused": True,
        "pause_reason": "attention_required",
    }
    assert status["next_run_at"] is None
    assert status["accounts"][0]["attention"]["reason"] == "captcha_required"
    assert not due


def test_free_games_state_loader_preserves_dynamic_account_ids(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        state_path = Path(temp_dir) / "free_games_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "running": False,
                    "updating": False,
                    "active_account_id": None,
                    "last_run_started_at": "2026-07-30T10:00:00+00:00",
                    "last_run_finished_at": "2026-07-30T10:05:00+00:00",
                    "last_run_success": False,
                    "last_error": "Failed Epic accounts: main",
                    "accounts": {
                        "main": {
                            "last_run_finished_at": "2026-07-30T10:05:00+00:00",
                            "last_run_success": False,
                            "last_error": "Exited with 1",
                        }
                    },
                }
            ),
            encoding="utf8",
        )
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_STATE_PATH", state_path)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main"}],
                )
            )
        )

    status = service.get_status()

    assert status["last_run_finished_at"] == "2026-07-30T10:05:00+00:00"
    assert status["accounts"][0]["last_error"] == "Exited with 1"


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


def test_free_games_stop_run_requests_active_container_stop():
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[{"id": "main"}],
            )
        )
    )
    service._state.update({"running": True, "active_account_id": "main"})

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: False)

    service._create_task = MagicMock(side_effect=fake_create_task)

    assert service.stop_run()
    assert service._stop_requested
    service._create_task.assert_called_once()


def test_free_games_next_run_uses_started_at_when_run_was_interrupted():
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[{"id": "main"}],
            )
        )
    )
    service._state["last_run_finished_at"] = None
    service._state["last_run_started_at"] = "2026-07-30T10:00:00+00:00"

    assert service._next_run_at() == "2026-07-31T10:00:00+00:00"


def test_free_games_next_run_uses_account_attempt_when_global_state_is_incomplete():
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[{"id": "main"}],
            )
        )
    )
    service._state["last_run_finished_at"] = None
    service._state["last_run_started_at"] = None
    service._state["accounts"] = {
        "main": {"last_run_finished_at": "2026-07-30T10:00:00+00:00"}
    }

    assert service._next_run_at() == "2026-07-31T10:00:00+00:00"


def test_free_games_scheduler_waits_during_startup_grace_window():
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[{"id": "main"}],
            )
        )
    )

    assert not service._due_for_scheduled_run()

    service._started_at = service._started_at - timedelta(minutes=11)

    assert service._due_for_scheduled_run()


@pytest.mark.asyncio
async def test_free_games_scheduled_run_skips_accounts_requiring_attention(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        blocked_dir = data_dir / "accounts" / "blocked"
        blocked_dir.mkdir(parents=True)
        (blocked_dir / "last-run.log").write_text(
            "Epic login timed out while waiting for captcha.",
            encoding="utf8",
        )
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[
                        {"id": "blocked", "enabled": True},
                        {"id": "ready", "enabled": True},
                    ],
                )
            )
        )
        service._run_account = AsyncMock(return_value=True)

        await service._run_accounts(scheduled=True)
        automation = service.get_status()["automation"]

    service._run_account.assert_awaited_once_with({"id": "ready", "enabled": True})
    assert service._state["last_run_success"] is True
    assert automation["scheduled_accounts"] == 1


@pytest.mark.asyncio
async def test_free_games_run_accounts_marks_global_failure_when_account_fails(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    twitch = make_twitch(
        SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:latest",
            free_games_schedule_hours=24,
            free_games_accounts=[{"id": "main", "enabled": True}],
        )
    )
    service = FreeGamesService(twitch)
    service._run_account = AsyncMock(return_value=False)

    await service._run_accounts()

    assert service._state["running"] is False
    assert service._state["active_account_id"] is None
    assert service._state["last_run_success"] is False
    assert service._state["last_error"] == "Failed Epic accounts: main"
    assert service._state["last_run_finished_at"] is not None
    service._run_account.assert_awaited_once_with({"id": "main", "enabled": True})
    assert twitch.telegram.queue_status_update.call_count == 2


@pytest.mark.asyncio
async def test_free_games_run_account_times_out_and_stops_container(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())

    class FakeProcess:
        returncode = None
        killed = False

        async def communicate(self):
            if self.returncode is None:
                await asyncio.sleep(3600)
            return b"runner still waiting", None

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = FakeProcess()

    async def fake_exec(*command, **kwargs):
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_run_timeout_minutes=1,
                free_games_accounts=[],
            )
        )
    )
    service._run_timeout_seconds = lambda: 0.01
    service._prepare_docker_container = AsyncMock(return_value=True)

    async def fake_docker_output(*command):
        assert command == ("docker", "rm", "-f", "fgc-epic-main")
        process.returncode = 137
        return 0, "removed"

    service._docker_output = fake_docker_output

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", Path(temp_dir))
        success = await service._run_account({"id": "main"})

    assert not success
    account_state = service._state["accounts"]["main"]
    assert account_state["last_run_success"] is False
    assert account_state["last_error"] == "Timed out after 1 minute(s)."


@pytest.mark.asyncio
async def test_free_games_run_account_reports_captcha_error(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return b"Got a captcha during login!", None

    async def fake_exec(*command, **kwargs):
        return FakeProcess()

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
    service._prepare_docker_container = AsyncMock(return_value=True)

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", Path(temp_dir))
        assert not await service._run_account({"id": "main"})

    assert service._state["accounts"]["main"]["last_error"] == (
        "Epic captcha required. Start a manual run and use Browser to solve it."
    )


@pytest.mark.asyncio
async def test_free_games_recovers_interrupted_state(monkeypatch):
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
    service._state.update({"running": True, "updating": False, "active_account_id": "main"})
    service._docker_container_running = AsyncMock(return_value=False)

    await service._recover_interrupted_state()

    assert service._state["running"] is False
    assert service._state["active_account_id"] is None
    assert service._state["last_error"] == "Previous Epic run was interrupted."
    assert service._state["last_run_success"] is False
    assert service._state["last_run_finished_at"] is not None
    assert not service._due_for_scheduled_run()


@pytest.mark.asyncio
async def test_free_games_recovery_preserves_active_docker_container(monkeypatch):
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
    service._state.update({"running": True, "updating": False, "active_account_id": "main"})
    service._docker_container_running = AsyncMock(return_value=True)

    await service._recover_interrupted_state()

    assert service._state["running"] is True
    assert service._state["active_account_id"] == "main"
    assert "last_error" not in service._state or service._state["last_error"] is None


@pytest.mark.asyncio
async def test_free_games_adopts_active_docker_container(monkeypatch):
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
    service._state.update({"running": True, "active_account_id": "main"})
    service._docker_container_running = AsyncMock(return_value=True)

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: False)

    service._create_task = MagicMock(side_effect=fake_create_task)

    assert await service._adopt_active_docker_run_if_needed()
    service._create_task.assert_called_once()


@pytest.mark.asyncio
async def test_free_games_monitor_adopted_container_updates_state(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", temp_path)
        twitch = make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                free_games_schedule_hours=24,
                free_games_accounts=[],
            )
        )
        service = FreeGamesService(twitch)
        service._state.update(
            {
                "running": True,
                "active_account_id": "main",
                "accounts": {"main": {}},
            }
        )
        calls = []

        async def fake_docker_output(*command):
            calls.append(command)
            if command[:2] == ("docker", "wait"):
                return 0, "0\n"
            return 0, "claimed output"

        service._docker_output = fake_docker_output

        await service._monitor_adopted_docker_container("main")

        assert calls == [
            ("docker", "wait", "fgc-epic-main"),
            ("docker", "logs", "fgc-epic-main"),
        ]
        assert service._state["running"] is False
        assert service._state["last_run_success"] is True
        assert service._state["accounts"]["main"]["last_run_success"] is True
        assert (temp_path / "accounts" / "main" / "last-run.log").read_text(
            encoding="utf8"
        ) == "claimed output"
        twitch.telegram.queue_status_update.assert_called_once()


@pytest.mark.asyncio
async def test_free_games_monitor_adopted_container_times_out(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    twitch = make_twitch(
        SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:latest",
            free_games_schedule_hours=24,
            free_games_run_timeout_minutes=1,
            free_games_accounts=[],
        )
    )
    service = FreeGamesService(twitch)
    service._state.update(
        {
            "running": True,
            "active_account_id": "main",
            "accounts": {"main": {}},
        }
    )
    service._remaining_run_timeout_seconds = lambda: 0.01
    calls = []

    async def fake_docker_output(*command):
        calls.append(command)
        if command[:2] == ("docker", "wait"):
            await asyncio.sleep(3600)
        return 0, ""

    service._docker_output = fake_docker_output

    await service._monitor_adopted_docker_container("main")

    assert ("docker", "rm", "-f", "fgc-epic-main") in calls
    assert service._state["running"] is False
    assert service._state["last_run_success"] is False
    assert service._state["last_error"] == "Timed out after 1 minute(s)."
    assert service._state["accounts"]["main"]["last_error"] == "Timed out after 1 minute(s)."
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
