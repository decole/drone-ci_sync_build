import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from main import app
from app.models import NotificationSettings, get_notification_settings, PollLog, Repository
from tests.conftest import TestingSessionLocal


def test_notification_settings_model():
    """Verify NotificationSettings encryption, decryption and singleton getter."""
    db = TestingSessionLocal()
    try:
        cfg = get_notification_settings(db)
        assert cfg.id == 1

        cfg.set_secrets(
            telegram_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            vk_token="vk1.a.secret_community_token_12345",
            smtp_password="super_secure_smtp_pass"
        )
        db.commit()
        db.refresh(cfg)

        assert cfg.telegram_bot_token != "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        assert cfg.get_plain_telegram_bot_token() == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        assert cfg.get_plain_vk_group_token() == "vk1.a.secret_community_token_12345"
        assert cfg.get_plain_smtp_password() == "super_secure_smtp_pass"
    finally:
        db.close()


def test_notifications_web_views():
    """Verify GET and POST /notifications."""
    client = TestClient(app, follow_redirects=False)

    # 1. GET page
    resp = client.get("/notifications")
    assert resp.status_code == 200
    assert "Настройки нотификаций" in resp.text
    assert "Telegram" in resp.text
    assert "ВКонтакте" in resp.text
    assert "Email" in resp.text
    # Verify new toggle switch components and individual buttons
    assert "toggle-switch" in resp.text
    assert "toggle-knob" in resp.text
    assert "btn_save_telegram" in resp.text
    assert "btn_test_telegram" in resp.text
    assert "btn_save_vk" in resp.text
    assert "btn_test_vk" in resp.text
    assert "btn_save_email" in resp.text
    assert "btn_test_email" in resp.text
    # Old bottom button should not exist
    assert "Сохранить настройки нотификаций" not in resp.text

    # 2. POST single channel settings (Telegram only)
    single_res = client.post("/notifications", data={
        "channel": "telegram",
        "telegram_enabled": "on",
        "telegram_bot_token": "tg_only_token",
        "telegram_chat_id": "777"
    })
    assert single_res.status_code == 303
    assert single_res.headers["location"] == "/notifications?saved=telegram"

    # 3. POST update settings (all / legacy)
    post_data = {
        "notify_on_build": "on",
        "notify_on_error": "on",
        "telegram_enabled": "on",
        "telegram_bot_token": "tg_bot_token_new",
        "telegram_chat_id": "-1001999999999",
        "vk_enabled": "on",
        "vk_group_token": "vk_group_token_new",
        "vk_peer_id": "2000000001",
        "email_enabled": "on",
        "smtp_host": "smtp.domain.ru",
        "smtp_port": "465",
        "smtp_username": "robot@domain.ru",
        "smtp_password": "robot_password",
        "smtp_from": "robot@domain.ru",
        "smtp_to": "admin1@domain.ru, admin2@domain.ru"
    }

    res = client.post("/notifications", data=post_data)
    assert res.status_code == 303
    assert res.headers["location"] == "/notifications?saved=1"

    # Verify DB
    db = TestingSessionLocal()
    try:
        cfg = get_notification_settings(db)
        assert cfg.telegram_enabled is True
        assert cfg.get_plain_telegram_bot_token() == "tg_bot_token_new"
        assert cfg.telegram_chat_id == "-1001999999999"
        assert cfg.vk_enabled is True
        assert cfg.get_plain_vk_group_token() == "vk_group_token_new"
        assert cfg.email_enabled is True
        assert cfg.smtp_host == "smtp.domain.ru"
        assert cfg.get_plain_smtp_password() == "robot_password"
    finally:
        db.close()


@pytest.mark.anyio
async def test_notifications_api_save_channels():
    """Verify /api/notifications/save for saving individual channels without overwriting others."""
    client = TestClient(app)
    db = TestingSessionLocal()
    try:
        # 1. Save Telegram settings
        res = client.post("/api/notifications/save", json={
            "channel": "telegram",
            "telegram_enabled": True,
            "telegram_bot_token": "tg_token_abc",
            "telegram_chat_id": "999888777"
        })
        assert res.status_code == 200
        assert res.json()["ok"] is True

        cfg = get_notification_settings(db)
        db.refresh(cfg)
        assert cfg.telegram_enabled is True
        assert cfg.get_plain_telegram_bot_token() == "tg_token_abc"
        assert cfg.telegram_chat_id == "999888777"

        # 2. Save VK settings without altering Telegram
        res = client.post("/api/notifications/save", json={
            "channel": "vk",
            "vk_enabled": True,
            "vk_group_token": "vk_group_token_abc",
            "vk_peer_id": "2000000042"
        })
        assert res.status_code == 200
        assert res.json()["ok"] is True

        db.refresh(cfg)
        assert cfg.vk_enabled is True
        assert cfg.get_plain_vk_group_token() == "vk_group_token_abc"
        assert cfg.vk_peer_id == "2000000042"
        # Telegram still intact
        assert cfg.telegram_enabled is True
        assert cfg.get_plain_telegram_bot_token() == "tg_token_abc"

        # 3. Save Email settings
        res = client.post("/api/notifications/save", json={
            "channel": "email",
            "email_enabled": True,
            "smtp_host": "smtp.fastmail.com",
            "smtp_port": 465,
            "smtp_username": "notify@example.com",
            "smtp_password": "mailpassword123",
            "smtp_use_tls": True,
            "smtp_from": "notify@example.com",
            "smtp_to": "alert@example.com"
        })
        assert res.status_code == 200
        assert res.json()["ok"] is True

        db.refresh(cfg)
        assert cfg.email_enabled is True
        assert cfg.smtp_host == "smtp.fastmail.com"
        assert cfg.smtp_port == 465
        assert cfg.get_plain_smtp_password() == "mailpassword123"

        # 4. Save Events
        res = client.post("/api/notifications/save", json={
            "channel": "events",
            "notify_on_build": False,
            "notify_on_error": True
        })
        assert res.status_code == 200
        assert res.json()["ok"] is True

        db.refresh(cfg)
        assert cfg.notify_on_build is False
        assert cfg.notify_on_error is True
    finally:
        db.close()


@pytest.mark.anyio
async def test_notifications_api_test_channels():
    """Verify /api/notifications/test for Telegram, VK and Email."""
    client = TestClient(app)

    # 1. Test Telegram
    with patch("app.notifier.send_telegram", new=AsyncMock(return_value=(True, "TG OK"))):
        resp = client.post("/api/notifications/test", json={
            "channel": "telegram",
            "telegram_bot_token": "123:ABC",
            "telegram_chat_id": "999"
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["message"] == "TG OK"

    # 2. Test VK
    with patch("app.notifier.send_vk", new=AsyncMock(return_value=(True, "VK OK"))):
        resp = client.post("/api/notifications/test", json={
            "channel": "vk",
            "vk_group_token": "vk1.a.xxx",
            "vk_peer_id": "2000000001"
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["message"] == "VK OK"

    # 3. Test Email
    with patch("app.notifier.send_email", new=AsyncMock(return_value=(True, "Email OK"))):
        resp = client.post("/api/notifications/test", json={
            "channel": "email",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_to": "test@example.com"
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["message"] == "Email OK"


@pytest.mark.anyio
async def test_notifier_event_dispatcher():
    """Verify dispatch_event_notification broadcasts to enabled channels."""
    from app.notifier import dispatch_event_notification

    db = TestingSessionLocal()
    try:
        cfg = get_notification_settings(db)
        cfg.notify_on_build = True
        cfg.telegram_enabled = True
        cfg.set_secrets(telegram_token="tg_token_test", vk_token="vk_token_test")
        cfg.telegram_chat_id = "12345"
        cfg.vk_enabled = True
        cfg.vk_peer_id = "54321"
        db.commit()

        with patch("app.notifier.send_telegram", new=AsyncMock(return_value=(True, "OK"))) as mock_tg, \
             patch("app.notifier.send_vk", new=AsyncMock(return_value=(True, "OK"))) as mock_vk:

            await dispatch_event_notification(
                event_type="build",
                title="Build Triggered",
                message="Build #10 triggered for main",
                repo_name="my-repo",
                branch="main",
                commit_sha="abcdef123456",
                build_number=10
            )

            assert mock_tg.called
            assert mock_vk.called

            # Check args
            tg_args = mock_tg.call_args[0]
            assert tg_args[0] == "tg_token_test"
            assert tg_args[1] == "12345"
            assert "my-repo" in tg_args[2]

            vk_args = mock_vk.call_args[0]
            assert vk_args[0] == "vk_token_test"
            assert vk_args[1] == "54321"
            assert "my-repo" in vk_args[2]

    finally:
        db.close()


def test_clear_logs_api_and_web():
    """Verify log clearing via DELETE /api/logs and POST /logs/clear."""
    client = TestClient(app)
    db = TestingSessionLocal()
    try:
        # Seed test logs
        repo = Repository(
            name="Log Test Repo",
            git_url="https://github.com/test/repo.git",
            drone_server="https://drone.example.com",
            drone_repo="test/repo",
            drone_token="token"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        log1 = PollLog(repo_id=repo.id, repo_name=repo.name, level="info", message="Log 1")
        log2 = PollLog(repo_id=None, repo_name="Other", level="error", message="Log 2")
        db.add_all([log1, log2])
        db.commit()

        # 1. Clear by repo_id
        res = client.delete(f"/api/logs?repo_id={repo.id}")
        assert res.status_code == 200
        assert res.json()["deleted_count"] >= 1

        remaining = db.query(PollLog).all()
        rem_msgs = [r.message for r in remaining]
        assert "Log 1" not in rem_msgs
        assert "Log 2" in rem_msgs

        # 2. Clear all logs via web POST
        res_post = client.post("/logs/clear", data={})
        assert res_post.status_code == 200 or res_post.status_code == 303

        all_logs = db.query(PollLog).all()
        assert len(all_logs) == 0

    finally:
        db.close()
