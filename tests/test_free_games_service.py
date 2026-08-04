import asyncio
import json
import tempfile
from datetime import datetime, timedelta
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


def test_free_games_service_reads_upstream_epic_games_database(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", temp_path)
        account_dir = temp_path / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "epic-games.json").write_text(
            json.dumps(
                {
                    "EpicUser": {
                        "offer-a": {
                            "title": "Claimed Upstream Game",
                            "url": "https://example.com/claimed",
                            "time": "2026-08-03 12:00:00.000",
                            "status": "claimed",
                        },
                        "offer-b": {
                            "title": "Submitted Upstream Game",
                            "url": "https://example.com/submitted",
                            "time": "2026-08-03 12:05:00.000",
                            "status": "submitted",
                        },
                    }
                }
            ),
            encoding="utf8",
        )
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main", "name": "Main"}],
                )
            )
        )

        status = service.get_status()

    titles = [game["title"] for game in status["accounts"][0]["claimed_games"]]
    assert titles == ["Submitted Upstream Game", "Claimed Upstream Game"]
    assert status["accounts"][0]["known_games_count"] == 2


def test_free_games_status_hides_failed_claims_that_are_pending_again(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", temp_path)
        account_dir = temp_path / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "epic-games.json").write_text(
            json.dumps(
                {
                    "EpicUser": {
                        "offer-a": {
                            "title": "Retry Me",
                            "url": "https://example.com/retry",
                            "time": "2026-08-03 12:00:00.000",
                            "status": "failed:no-checkout",
                        },
                        "offer-b": {
                            "title": "Real Failure",
                            "url": "https://example.com/failed",
                            "time": "2026-08-03 12:05:00.000",
                            "status": "failed:no-checkout",
                        },
                    }
                }
            ),
            encoding="utf8",
        )
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main", "name": "Main"}],
                )
            )
        )
        monkeypatch.setattr(
            service,
            "_catalog_claim_targets",
            lambda account_id: [
                {
                    "id": "offer-a",
                    "title": "Retry Me",
                    "url": "https://example.com/retry",
                    "checkout_url": "https://example.com/checkout",
                }
            ],
        )

        status = service.get_status()

    assert [game["title"] for game in status["accounts"][0]["pending_claim_games"]] == [
        "Retry Me"
    ]
    assert [game["title"] for game in status["accounts"][0]["failed_games"]] == [
        "Real Failure"
    ]
    assert status["accounts"][0]["known_games_count"] == 2


def test_free_games_service_parses_epic_catalog_freebies(monkeypatch):
    monkeypatch.setenv("EPIC_CATALOG_LOCALE", "pt-BR")
    monkeypatch.setenv("EPIC_CATALOG_COUNTRY", "BR")
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

    parsed = service._parse_epic_catalog(
        {
            "data": {
                "Catalog": {
                    "searchStore": {
                        "elements": [
                            {
                                "id": "offer-current",
                                "namespace": "namespace-current",
                                "title": "Current Free Game",
                                "offerMappings": [
                                    {
                                        "pageSlug": "current-free-game",
                                        "pageType": "productHome",
                                    }
                                ],
                                "keyImages": [
                                    {
                                        "type": "OfferImageWide",
                                        "url": "https://cdn.example/current.jpg",
                                    }
                                ],
                                "price": {
                                    "totalPrice": {
                                        "fmtPrice": {
                                            "originalPrice": "R$ 10,00",
                                            "discountPrice": "0",
                                        }
                                    }
                                },
                                "promotions": {
                                    "promotionalOffers": [
                                        {
                                            "promotionalOffers": [
                                                {
                                                    "startDate": "2026-07-30T15:00:00.000Z",
                                                    "endDate": "2026-08-06T15:00:00.000Z",
                                                    "discountSetting": {
                                                        "discountType": "PERCENTAGE",
                                                        "discountPercentage": 0,
                                                    },
                                                }
                                            ]
                                        }
                                    ],
                                    "upcomingPromotionalOffers": [],
                                },
                            },
                            {
                                "id": "offer-upcoming",
                                "namespace": "namespace-upcoming",
                                "title": "Upcoming Free Game",
                                "productSlug": "upcoming-free-game/home",
                                "promotions": {
                                    "promotionalOffers": [],
                                    "upcomingPromotionalOffers": [
                                        {
                                            "promotionalOffers": [
                                                {
                                                    "startDate": "2026-08-06T15:00:00.000Z",
                                                    "endDate": "2026-08-13T15:00:00.000Z",
                                                    "discountSetting": {
                                                        "discountType": "PERCENTAGE",
                                                        "discountPercentage": 0,
                                                    },
                                                }
                                            ]
                                        }
                                    ],
                                },
                            },
                        ]
                    }
                }
            }
        }
    )

    assert parsed["current"][0]["title"] == "Current Free Game"
    assert parsed["current"][0]["url"] == "https://store.epicgames.com/pt-BR/p/current-free-game"
    assert parsed["current"][0]["checkout_url"] == (
        "https://store.epicgames.com/pt-BR/purchase"
        "?offers=1-namespace-current-offer-current"
    )
    assert parsed["current"][0]["image_url"] == "https://cdn.example/current.jpg"
    assert parsed["current"][0]["original_price"] == "R$ 10,00"
    assert parsed["upcoming"][0]["title"] == "Upcoming Free Game"
    assert parsed["upcoming"][0]["url"] == "https://store.epicgames.com/pt-BR/p/upcoming-free-game"


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
    assert status["manual_run_timeout_minutes"] == 120
    assert status["vnc"] == {
        "enabled": True,
        "url": None,
        "bind": "127.0.0.1:6080",
        "active": False,
        "running": False,
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


def test_free_games_status_reads_source_revision_from_successful_update(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
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
        service._write_update_source_info(
            "latest: Pulling from vogler/free-games-claimer\n"
            "Digest: sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
            "Status: Downloaded newer image"
        )
        source = service.get_status()["source"]

    assert source["image"] == "ghcr.io/vogler/free-games-claimer:latest"
    assert source["revision"] == (
        "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    )
    assert source["detected_from"] == "last-update.log"
    assert source["updated_at"]


def test_free_games_status_keeps_run_revision_when_update_has_no_digest(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "Version: https://github.com/vogler/free-games-claimer/tree/99c1f05302aeece21a628797cfdffb561ee38956",
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
        service._write_update_source_info("Status: Image is up to date")
        source = service.get_status()["source"]

    assert source["revision"] == "99c1f05302aeece21a628797cfdffb561ee38956"
    assert source["detected_from"] == "accounts/main/last-run.log"


def test_free_games_log_reader_redacts_configured_secrets_and_headers(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "\n".join(
                [
                    "normal line",
                    "password=secret-password",
                    "EG_OTPKEY=otp-secret",
                    "VNC_PASSWORD=vnc-secret",
                    "Authorization: Bearer abc123",
                    "Cookie: session=secret-cookie",
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
                    free_games_accounts=[
                        {
                            "id": "main",
                            "password": "secret-password",
                            "otpkey": "otp-secret",
                            "vnc_password": "vnc-secret",
                        }
                    ],
                )
            )
        )

        log = service.get_log("account-run", account_id="main")

    assert log["available"] is True
    assert log["path"] == "accounts/main/last-run.log"
    assert "normal line" in log["content"]
    assert "secret-password" not in log["content"]
    assert "otp-secret" not in log["content"]
    assert "vnc-secret" not in log["content"]
    assert "abc123" not in log["content"]
    assert "secret-cookie" not in log["content"]
    assert log["content"].count("[redacted]") >= 5


def test_free_games_log_reader_clamps_to_sanitized_tail(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "last-update.log").write_text(
            f"{'x' * 1500}\npassword=tail-secret",
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
                    free_games_accounts=[{"id": "main", "password": "tail-secret"}],
                )
            )
        )

        log = service.get_log("last-update", max_chars=100)

    assert log["available"] is True
    assert log["truncated"] is True
    assert len(log["content"]) <= 1100
    assert "tail-secret" not in log["content"]


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


def test_free_games_status_detects_epic_store_navigation_timeout(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text(
            "locator.getAttribute: Timeout 180000ms exceeded.\n"
            "Call log:\n  - waiting for locator('egs-navigation')\n"
            "name: 'TimeoutError'",
            encoding="utf8",
        )
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:dev",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main"}],
                )
            )
        )
        service._started_at = service._started_at - timedelta(minutes=11)
        status = service.get_status()

    assert status["attention"] == {
        "required": True,
        "reason": "epic_store_unavailable",
        "message": (
            "Epic Store did not load in the claimer browser. "
            "Try an interactive Epic run from the panel or change network/image."
        ),
        "account_id": "main",
    }
    assert status["automation"] == {
        "scheduled_accounts": 0,
        "paused": True,
        "pause_reason": "attention_required",
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
    assert status["accounts"][0]["automation"] == {
        "eligible": False,
        "blocked_reason": "attention_required",
    }
    assert not due


def test_free_games_status_marks_active_interactive_run_as_manual_attention(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    twitch = make_twitch(
        SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:dev",
            free_games_schedule_hours=24,
            free_games_accounts=[{"id": "main@example.com"}],
        )
    )
    twitch.is_twitch_worker_paused = MagicMock(return_value=True)
    service = FreeGamesService(twitch)
    service._state.update(
        {
            "running": True,
            "active_account_id": "main@example.com",
            "active_interactive": True,
            "last_run_started_at": (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(
                timespec="seconds"
            ),
        }
    )
    monkeypatch.setattr(service, "_is_vnc_ready", lambda target: True)

    status = service.get_status()

    assert status["attention"] == {
        "required": True,
        "reason": "manual_epic_browser",
        "message": (
            "Epic browser is open for this account. "
            "Use Browser to finish login, captcha, or MFA if Epic asks."
        ),
        "account_id": "main@example.com",
    }
    assert status["accounts"][0]["attention"]["reason"] == "manual_epic_browser"
    assert status["active_run"]["account_id"] == "main@example.com"
    assert status["active_run"]["interactive"] is True
    assert status["active_run"]["timeout_minutes"] == 120
    assert status["active_run"]["expires_at"] is not None
    assert 0 < status["active_run"]["remaining_seconds"] <= 120 * 60
    assert status["exclusive"] == {
        "twitch_paused": True,
        "reason": "Epic Freebies exclusive run",
    }
    assert status["automation"] == {
        "scheduled_accounts": 0,
        "paused": True,
        "pause_reason": "attention_required",
    }


def test_free_games_status_marks_timed_out_pending_claim_as_manual_attention(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    service = FreeGamesService(
        make_twitch(
            SimpleNamespace(
                free_games_enabled=True,
                free_games_runner="docker",
                free_games_image="ghcr.io/vogler/free-games-claimer:dev",
                free_games_schedule_hours=24,
                free_games_accounts=[{"id": "main@example.com"}],
            )
        )
    )
    service._state["accounts"] = {
        "main@example.com": {
            "attention_cleared_at": (
                datetime.now().astimezone() - timedelta(minutes=40)
            ).isoformat(),
            "last_run_finished_at": (
                datetime.now().astimezone() - timedelta(minutes=10)
            ).isoformat(),
            "last_run_success": False,
            "last_error": "Timed out after 30 minute(s).",
        }
    }
    monkeypatch.setattr(
        service,
        "_catalog_claim_targets",
        lambda account_id: [{"id": "game", "title": "Pending Game"}],
    )

    status = service.get_status()

    assert status["attention"] == {
        "required": True,
        "reason": "manual_login_timeout",
        "message": (
            "Epic run timed out before pending freebies were claimed. "
            "Start a manual Epic run and use Browser to finish login, captcha, or MFA."
        ),
        "account_id": "main@example.com",
    }
    assert status["automation"] == {
        "scheduled_accounts": 0,
        "paused": True,
        "pause_reason": "attention_required",
    }

    assert service.clear_attention("main@example.com")
    assert service.get_status()["attention"]["required"] is False


def test_free_games_clear_attention_resumes_scheduling_without_deleting_log(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main-example.com"
        account_dir.mkdir(parents=True)
        log_path = account_dir / "last-run.log"
        log_path.write_text("Got a captcha during login!", encoding="utf8")
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        service = FreeGamesService(
            make_twitch(
                SimpleNamespace(
                    free_games_enabled=True,
                    free_games_runner="docker",
                    free_games_image="ghcr.io/vogler/free-games-claimer:latest",
                    free_games_schedule_hours=24,
                    free_games_accounts=[{"id": "main@example.com"}],
                )
            )
        )
        service._started_at = service._started_at - timedelta(minutes=11)

        assert service.get_status()["attention"]["account_id"] == "main@example.com"
        assert service.get_status()["automation"]["paused"] is True
        assert service.clear_attention("main@example.com")
        status = service.get_status()
        due = service._due_for_scheduled_run()
        log_exists = log_path.exists()

    assert log_exists
    assert status["attention"]["required"] is False
    assert status["accounts"][0]["attention"]["required"] is False
    assert status["accounts"][0]["attention_cleared_at"]
    assert status["accounts"][0]["automation"] == {
        "eligible": True,
        "blocked_reason": None,
    }
    assert status["automation"] == {
        "scheduled_accounts": 1,
        "paused": False,
        "pause_reason": None,
    }
    assert due


def test_free_games_clear_attention_rejects_unknown_account():
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

    assert not service.clear_attention("missing")


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
        background_command = service._docker_command(account, temp_path, interactive=False)
        env = service._account_env(account, temp_path)
        background_env = service._account_env(account, temp_path, interactive=False)

    assert "secret-password" not in command
    assert "otp-secret" not in command
    assert "1234" not in command
    assert "vnc-secret" not in command
    assert env["EG_PASSWORD"] == "secret-password"
    assert env["VNC_PASSWORD"] == "vnc-secret"
    assert env["LOGIN_TIMEOUT"] == "7140"
    assert background_env["LOGIN_TIMEOUT"] == "840"
    assert env["TIMEOUT"] == "180"
    assert env["WIDTH"] == "800"
    assert env["HEIGHT"] == "600"
    assert "--cpus" in command
    assert "0.30" in command
    assert "--memory" in command
    assert "512m" in command
    assert "--shm-size" in command
    assert "128m" in command
    assert command.count("-e") >= 9
    assert "LOGIN_TIMEOUT" in command
    assert "TIMEOUT" in command
    assert "WIDTH" in command
    assert "HEIGHT" in command
    assert "SHOW=1" in command
    assert "SHOW=0" in background_command
    assert "/opt/tdm/data/free-games/accounts/main:/fgc/data" in command
    assert "127.0.0.1:6080:6080" in command


def test_free_games_docker_command_uses_direct_catalog_runner_for_pending_claims(monkeypatch):
    monkeypatch.setenv("HOST_DATA_DIR", "/opt/tdm/data")
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:dev",
        free_games_schedule_hours=24,
        free_games_accounts=[],
    )
    service = FreeGamesService(make_twitch(settings))
    service._state["catalog"] = {
        "current": [
            {
                "id": "offer-a",
                "namespace": "namespace-a",
                "title": "Free Game A",
                "url": "https://store.epicgames.com/pt-BR/p/free-game-a",
                "checkout_url": (
                    "https://store.epicgames.com/pt-BR/purchase"
                    "?offers=1-namespace-a-offer-a"
                ),
                "end_at": "2026-08-06T15:00:00.000Z",
            }
        ],
        "upcoming": [],
    }

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        temp_path = Path(temp_dir)
        command = service._docker_command({"id": "main"}, temp_path)
        env = service._account_env({"id": "main"}, temp_path)
        script = (temp_path / "tdm-epic-direct.js").read_text(encoding="utf8")

        assert (temp_path / "tdm-epic-direct.js").is_file()

    assert command[-2:] == ["node", "/fgc/data/tdm-epic-direct.js"]
    assert "TDM_EPIC_CLAIM_TARGETS" in command
    assert env["TDM_EPIC_ACCOUNT_ID"] == "main"
    targets = json.loads(env["TDM_EPIC_CLAIM_TARGETS"])
    assert targets[0]["id"] == "offer-a"
    assert targets[0]["checkout_url"].endswith("offers=1-namespace-a-offer-a")
    assert "waitForManualCheckoutCaptcha" in script
    assert "Manual Epic checkout captcha required" in script
    assert "cfg.interactive && await waitForManualCheckoutCaptcha" in script


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
    service._state["active_interactive"] = True
    monkeypatch.setattr(service, "_is_vnc_ready", lambda target: True)

    assert service.get_status()["vnc"] == {
        "enabled": True,
        "url": (
            "/api/free-games/vnc/vnc.html"
            "?autoconnect=true&resize=remote&path=api/free-games/vnc/websockify"
        ),
        "bind": "tdm-hub",
        "active": True,
        "running": True,
    }
    assert (
        service.get_vnc_target_url("vnc.html", "autoconnect=1")
        == "http://fgc-epic-main:6080/vnc.html?autoconnect=1"
    )
    assert (
        service.get_vnc_target_url("websockify", "token=abc", websocket=True)
        == "ws://fgc-epic-main:6080/websockify?token=abc"
    )

    service._state["active_interactive"] = False
    assert service.get_status()["vnc"] == {
        "enabled": True,
        "url": None,
        "bind": "tdm-hub",
        "active": False,
        "running": False,
    }


def test_free_games_vnc_status_waits_until_browser_is_ready(monkeypatch):
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
    service._state["active_interactive"] = True
    monkeypatch.setattr(service, "_is_vnc_ready", lambda target: False)

    assert service.get_status()["vnc"] == {
        "enabled": True,
        "url": None,
        "bind": "tdm-hub",
        "active": False,
        "running": True,
    }


def test_free_games_vnc_status_caches_ready_probe(monkeypatch):
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
    service._state["active_interactive"] = True
    calls = 0

    def fake_ready(target):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(service, "_is_vnc_ready", fake_ready)

    assert service.get_status()["vnc"]["active"] is True
    assert service.get_status()["vnc"]["active"] is True
    assert calls == 1


def test_free_games_vnc_ready_uses_light_get_request(monkeypatch):
    settings = SimpleNamespace(
        free_games_enabled=True,
        free_games_runner="docker",
        free_games_image="ghcr.io/vogler/free-games-claimer:latest",
        free_games_schedule_hours=24,
        free_games_accounts=[],
    )
    service = FreeGamesService(make_twitch(settings))
    calls = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            calls["read_size"] = size
            return b"<"

    def fake_urlopen(request, timeout):
        calls["method"] = request.get_method()
        calls["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("src.services.free_games_service.urlopen", fake_urlopen)

    assert service._is_vnc_ready("http://fgc-epic-main:6080/vnc.html")
    assert calls == {"method": "GET", "timeout": 2, "read_size": 1}


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


def test_free_games_run_now_requires_enough_available_memory(monkeypatch):
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
    monkeypatch.setattr(service, "_available_memory_mb", lambda: 420)

    assert not service.run_now()
    assert "Not enough free-tier VM memory" in service.get_status()["last_error"]
    twitch.telegram.queue_status_update.assert_called_once()


def test_free_games_exclusive_run_can_start_with_low_initial_memory(monkeypatch):
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
    monkeypatch.setattr(service, "_available_memory_mb", lambda: 420)

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: False)

    service._create_task = MagicMock(side_effect=fake_create_task)

    assert service.run_now(exclusive=True)
    service._create_task.assert_called_once()


@pytest.mark.asyncio
async def test_free_games_exclusive_run_pauses_and_resumes_twitch(monkeypatch):
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
    twitch.pause_twitch_worker = AsyncMock()
    twitch.resume_twitch_worker = MagicMock()
    service = FreeGamesService(twitch)
    monkeypatch.setattr(service, "_exclusive_settle_seconds", lambda: 0)
    monkeypatch.setattr(service, "_resources_allow_run", MagicMock(return_value=True))
    service._run_accounts = AsyncMock()

    await service._run_accounts_exclusive("main", interactive=False)

    twitch.pause_twitch_worker.assert_awaited_once_with("Epic Freebies exclusive run")
    service._run_accounts.assert_awaited_once_with(
        "main", scheduled=False, interactive=False
    )
    twitch.resume_twitch_worker.assert_called_once_with()


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

    service._run_account.assert_awaited_once_with(
        {"id": "ready", "enabled": True}, interactive=False
    )
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
    service._run_account.assert_awaited_once_with(
        {"id": "main", "enabled": True}, interactive=True
    )
    assert twitch.telegram.queue_status_update.call_count == 2


@pytest.mark.asyncio
async def test_free_games_run_account_clears_previous_attention_at_start(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"checked epic-games successfully", None

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
                free_games_accounts=[{"id": "main"}],
            )
        )
    )
    service._prepare_docker_container = AsyncMock(return_value=True)

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        account_dir = data_dir / "accounts" / "main"
        account_dir.mkdir(parents=True)
        (account_dir / "last-run.log").write_text("Got a captcha during login!", encoding="utf8")
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)

        assert service.get_status()["accounts"][0]["attention"]["required"] is True
        assert await service._run_account({"id": "main"})
        status = service.get_status()

    assert status["accounts"][0]["attention_cleared_at"]
    assert status["accounts"][0]["attention"]["required"] is False
    assert status["accounts"][0]["last_run_success"] is True


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
    service._run_timeout_seconds = lambda **_: 0.01
    service._prepare_docker_container = AsyncMock(return_value=True)
    calls = []

    async def fake_docker_output(*command):
        calls.append(command)
        if command == ("docker", "logs", "fgc-epic-main"):
            return 0, "manual login still pending"
        if command == ("docker", "rm", "-f", "fgc-epic-main"):
            process.returncode = 137
            return 0, "removed"
        return 1, "unexpected command"

    service._docker_output = fake_docker_output

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
        success = await service._run_account({"id": "main"})
        last_run_log = (data_dir / "accounts" / "main" / "last-run.log").read_text(
            encoding="utf8"
        )

    assert not success
    assert calls == [
        ("docker", "logs", "fgc-epic-main"),
        ("docker", "rm", "-f", "fgc-epic-main"),
    ]
    assert last_run_log == "manual login still pending"
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
async def test_free_games_adopts_active_interactive_container_pauses_twitch(monkeypatch):
    twitch = make_twitch(
        SimpleNamespace(
            free_games_enabled=True,
            free_games_runner="docker",
            free_games_image="ghcr.io/vogler/free-games-claimer:latest",
            free_games_schedule_hours=24,
            free_games_accounts=[],
        )
    )
    twitch.pause_twitch_worker = AsyncMock()
    twitch.resume_twitch_worker = MagicMock()
    service = FreeGamesService(twitch)
    service._state.update(
        {
            "running": True,
            "active_account_id": "main",
            "active_interactive": True,
        }
    )
    service._docker_container_running = AsyncMock(return_value=True)
    captured = {}

    def fake_create_task(coro):
        captured["coro"] = coro
        coro.close()
        return SimpleNamespace(done=lambda: False)

    service._create_task = MagicMock(side_effect=fake_create_task)

    assert await service._adopt_active_docker_run_if_needed()
    twitch.pause_twitch_worker.assert_awaited_once_with("Epic Freebies exclusive run")
    assert captured["coro"].cr_code.co_name == "_monitor_adopted_docker_container"
    twitch.resume_twitch_worker.assert_not_called()


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

        twitch.resume_twitch_worker = MagicMock()

        await service._monitor_adopted_docker_container("main", resume_twitch=True)

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
        twitch.resume_twitch_worker.assert_called_once_with()
        twitch.telegram.queue_status_update.assert_called_once()


@pytest.mark.asyncio
async def test_free_games_monitor_adopted_container_times_out(monkeypatch):
    monkeypatch.setattr("src.services.free_games_service.json_save", MagicMock())
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        data_dir = Path(temp_dir)
        monkeypatch.setattr("src.services.free_games_service.FREE_GAMES_DATA_DIR", data_dir)
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
            if command[:2] == ("docker", "logs"):
                return 0, "manual timeout log"
            return 0, ""

        service._docker_output = fake_docker_output

        await service._monitor_adopted_docker_container("main")
        last_run_log = (data_dir / "accounts" / "main" / "last-run.log").read_text(
            encoding="utf8"
        )

    assert ("docker", "rm", "-f", "fgc-epic-main") in calls
    assert calls.index(("docker", "logs", "fgc-epic-main")) < calls.index(
        ("docker", "rm", "-f", "fgc-epic-main")
    )
    assert service._state["running"] is False
    assert service._state["last_run_success"] is False
    assert service._state["last_error"] == "Timed out after 1 minute(s)."
    assert service._state["accounts"]["main"]["last_error"] == "Timed out after 1 minute(s)."
    assert last_run_log == "manual timeout log"
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
