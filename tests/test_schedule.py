import json
from datetime import datetime, time, timezone
import pytest
from fastapi.testclient import TestClient

from main import app
from tests.conftest import TestingSessionLocal
from app.models import Repository, WorkerScheduleSettings, get_worker_schedule_settings
from app.schedule import (
    is_time_in_range,
    parse_time_string,
    check_schedule_window,
    is_repo_in_schedule_window,
    get_default_schedule,
    normalize_schedule_dict,
)
from app.poller import poll_single_repository


client = TestClient(app)


def test_parse_time_string():
    assert parse_time_string("09:30") == time(9, 30)
    assert parse_time_string("23:59") == time(23, 59)
    assert parse_time_string("invalid", "08:00") == time(8, 0)
    assert parse_time_string("", "12:00") == time(12, 0)


def test_is_time_in_range_daytime():
    # 09:00 - 18:00
    assert is_time_in_range("09:00", "18:00", time(9, 0)) is True
    assert is_time_in_range("09:00", "18:00", time(13, 30)) is True
    assert is_time_in_range("09:00", "18:00", time(18, 0)) is True
    assert is_time_in_range("09:00", "18:00", time(8, 59)) is False
    assert is_time_in_range("09:00", "18:00", time(18, 1)) is False


def test_is_time_in_range_overnight():
    # 22:00 - 06:00 (crossing midnight)
    assert is_time_in_range("22:00", "06:00", time(22, 0)) is True
    assert is_time_in_range("22:00", "06:00", time(23, 59)) is True
    assert is_time_in_range("22:00", "06:00", time(0, 0)) is True
    assert is_time_in_range("22:00", "06:00", time(5, 30)) is True
    assert is_time_in_range("22:00", "06:00", time(6, 0)) is True
    assert is_time_in_range("22:00", "06:00", time(12, 0)) is False
    assert is_time_in_range("22:00", "06:00", time(21, 59)) is False


def test_is_time_in_range_full_day():
    # 00:00 - 00:00 (full 24h)
    assert is_time_in_range("00:00", "00:00", time(0, 0)) is True
    assert is_time_in_range("00:00", "00:00", time(15, 30)) is True
    assert is_time_in_range("00:00", "00:00", time(23, 59)) is True


def test_check_schedule_window():
    sched = get_default_schedule()
    # Monday 12:00 in Moscow
    dt_mon_noon = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)  # UTC 09:00 = MSK 12:00 (Monday)
    allowed, msg, info = check_schedule_window(sched, "Europe/Moscow", dt_mon_noon)
    assert allowed is True
    assert info["day_key"] == "mon"
    assert info["current_time"] == "12:00"

    # Monday 20:00 in Moscow (outside 09:00-18:00)
    dt_mon_evening = datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)  # UTC 17:00 = MSK 20:00
    allowed, msg, info = check_schedule_window(sched, "Europe/Moscow", dt_mon_evening)
    assert allowed is False

    # Sunday (disabled by default)
    dt_sun = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    allowed, msg, info = check_schedule_window(sched, "Europe/Moscow", dt_sun)
    assert allowed is False
    assert "отключен" in msg.lower()


def test_is_repo_in_schedule_window():
    test_db = TestingSessionLocal()
    try:
        global_cfg = get_worker_schedule_settings(test_db)
        global_cfg.enabled = False
        test_db.commit()

        repo = Repository(
            name="Schedule Test Repo",
            git_url="https://github.com/example/repo.git",
            drone_server="https://drone.uberserver.ru",
            drone_repo="example/repo",
            schedule_mode="global"
        )
        test_db.add(repo)
        test_db.commit()

        # 1. Global disabled -> 24/7 allowed
        allowed, reason = is_repo_in_schedule_window(repo, test_db)
        assert allowed is True

        # 2. Global enabled, Monday 12:00 MSK (inside window)
        global_cfg.enabled = True
        test_db.commit()
        dt_mon_noon = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
        allowed, reason = is_repo_in_schedule_window(repo, test_db, dt_mon_noon)
        assert allowed is True

        # 3. Monday 21:00 MSK (outside window)
        dt_mon_night = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        allowed, reason = is_repo_in_schedule_window(repo, test_db, dt_mon_night)
        assert allowed is False

        # 4. Repo with mode="always" (ignores schedule)
        repo.schedule_mode = "always"
        test_db.commit()
        allowed, reason = is_repo_in_schedule_window(repo, test_db, dt_mon_night)
        assert allowed is True
        assert "24/7" in reason

        # 5. Repo with mode="custom"
        repo.schedule_mode = "custom"
        custom_sched = get_default_schedule()
        # Enable night work on Monday: 20:00 - 23:00
        custom_sched["mon"] = {"enabled": True, "start": "20:00", "end": "23:00"}
        repo.set_custom_schedule(custom_sched)
        test_db.commit()

        allowed, reason = is_repo_in_schedule_window(repo, test_db, dt_mon_night)
        assert allowed is True

        # Cleanup global_cfg
        global_cfg.enabled = False
        test_db.commit()
    finally:
        test_db.close()


@pytest.mark.anyio
async def test_poller_skips_outside_schedule():
    test_db = TestingSessionLocal()
    try:
        global_cfg = get_worker_schedule_settings(test_db)
        global_cfg.enabled = True
        # Make all days disabled
        sched = get_default_schedule()
        for d in sched:
            sched[d]["enabled"] = False
        global_cfg.set_schedule(sched)
        test_db.commit()

        repo = Repository(
            name="Poller Sched Repo",
            git_url="https://github.com/example/repo.git",
            drone_server="https://drone.uberserver.ru",
            drone_repo="example/repo",
            schedule_mode="global",
            is_active=True
        )
        test_db.add(repo)
        test_db.commit()

        # Normal polling should skip due to schedule
        res = await poll_single_repository(repo.id, test_db, force=False)
        assert res["status"] == "skipped"
        assert "Вне расписания" in res["message"]

        # Forced polling (e.g. manual click) ignores schedule
        # Note: git fetch will fail or skip, but schedule won't block it
        res_forced = await poll_single_repository(repo.id, test_db, force=True)
        # The status will be error because git_url is fake, NOT skipped by schedule
        assert res_forced["status"] != "skipped"

        # Cleanup
        global_cfg.enabled = False
        test_db.commit()
    finally:
        test_db.close()


def test_api_schedule():
    # 1. GET schedule API
    resp = client.get("/api/schedule")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "timezone" in data
    assert "days" in data
    assert "mon" in data["days"]

    # 2. GET timezones API
    resp_tz = client.get("/api/schedule/timezones")
    assert resp_tz.status_code == 200
    tz_list = resp_tz.json()
    assert any(item["value"] == "Europe/Moscow" for item in tz_list)

    # 3. POST schedule API
    update_payload = {
        "enabled": True,
        "timezone": "Europe/Moscow",
        "days": {
            "mon": {"enabled": True, "start": "10:00", "end": "19:00"},
            "tue": {"enabled": True, "start": "10:00", "end": "19:00"},
            "wed": {"enabled": True, "start": "10:00", "end": "19:00"},
            "thu": {"enabled": True, "start": "10:00", "end": "19:00"},
            "fri": {"enabled": True, "start": "10:00", "end": "19:00"},
            "sat": {"enabled": False, "start": "00:00", "end": "00:00"},
            "sun": {"enabled": False, "start": "00:00", "end": "00:00"}
        }
    }
    resp_post = client.post("/api/schedule", json=update_payload)
    assert resp_post.status_code == 200
    post_data = resp_post.json()
    assert post_data["ok"] is True
    assert post_data["enabled"] is True
    assert post_data["days"]["mon"]["start"] == "10:00"

    # Revert to disabled
    update_payload["enabled"] = False
    client.post("/api/schedule", json=update_payload)


def test_web_schedule_pages():
    # GET /schedule HTML view
    resp = client.get("/schedule")
    assert resp.status_code == 200
    assert "Расписание работы воркера" in resp.text
    assert "Europe/Moscow" in resp.text

    # POST /schedule form
    form_data = {
        "enabled": "on",
        "timezone": "Asia/Yekaterinburg",
        "day_mon_enabled": "on",
        "day_mon_start": "08:30",
        "day_mon_end": "17:30"
    }
    resp_post = client.post("/schedule", data=form_data, follow_redirects=False)
    assert resp_post.status_code == 303
    assert resp_post.headers["location"] == "/schedule?saved=1"

    # Reset
    client.post("/schedule", data={"timezone": "Europe/Moscow"})


def test_repo_with_custom_schedule():
    test_db = TestingSessionLocal()
    try:
        form_payload = {
            "name": "Custom Sched Repo",
            "git_url": "https://github.com/test/custom-sched.git",
            "drone_server": "https://drone.uberserver.ru",
            "drone_repo": "test/custom-sched",
            "poll_interval_minutes": 5,
            "is_active": "on",
            "schedule_mode": "custom",
            "custom_schedule_timezone": "Europe/Kaliningrad",
            "repo_day_mon_enabled": "on",
            "repo_day_mon_start": "08:00",
            "repo_day_mon_end": "16:00"
        }
        resp = client.post("/repos/new", data=form_payload, follow_redirects=False)
        assert resp.status_code == 303

        repo = test_db.query(Repository).filter(Repository.name == "Custom Sched Repo").first()
        assert repo is not None
        assert repo.schedule_mode == "custom"
        assert repo.custom_schedule_timezone == "Europe/Kaliningrad"
        sched = repo.get_custom_schedule()
        assert sched["mon"]["enabled"] is True
        assert sched["mon"]["start"] == "08:00"
        assert sched["mon"]["end"] == "16:00"

        # Verify repo detail view displays schedule
        resp_detail = client.get(f"/repos/{repo.id}")
        assert resp_detail.status_code == 200
        assert "Индивидуальное" in resp_detail.text
    finally:
        test_db.close()

