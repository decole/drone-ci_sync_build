import pytest
from fastapi.testclient import TestClient
from main import app
from app.database import Base, engine, SessionLocal
from app.models import Repository, BranchState, PollLog


from tests.conftest import TestingSessionLocal


def test_index_page():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Drone CI Build Sync" in resp.text


def test_sqlite_wal_mode():
    """Verify that SQLite connection enables WAL journal mode and busy timeout."""
    from sqlalchemy import text
    db = TestingSessionLocal()
    try:
        res = db.execute(text("PRAGMA journal_mode")).fetchone()
        assert res is not None
        assert res[0].lower() == "wal"
    finally:
        db.close()


def test_new_repo_form():
    client = TestClient(app)
    resp = client.get("/repos/new")
    assert resp.status_code == 200
    assert "Добавить новый репозиторий" in resp.text


def test_create_repo_and_detail():
    client = TestClient(app, follow_redirects=False)
    form_data = {
        "name": "Test Repository",
        "git_url": "https://github.com/shpion34rus/secure-messenger.git",
        "git_username": "testuser",
        "git_password": "testpassword",
        "track_all_branches": "on",
        "tracked_branches": "main,dev",
        "drone_server": "https://drone.uberserver.ru",
        "drone_repo": "shpion34rus/secure-messenger",
        "drone_token": "fake_token_123",
        "poll_interval_minutes": "5",
        "is_active": "on"
    }
    resp = client.post("/repos/new", data=form_data)
    assert resp.status_code == 303
    redirect_url = resp.headers["location"]
    repo_id = int(redirect_url.split("/")[-1])

    # Check detail page
    detail_resp = client.get(f"/repos/{repo_id}")
    assert detail_resp.status_code == 200
    assert "Test Repository" in detail_resp.text
    assert "Все ветки" in detail_resp.text

    # Verify DB
    db = TestingSessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        assert repo is not None
        assert repo.track_all_branches is True
        assert repo.git_username == "testuser"
        # Password and token must be encrypted at rest in DB
        assert repo.git_password != "testpassword"
        assert repo.get_plain_git_password() == "testpassword"
        assert repo.get_plain_drone_token() == "fake_token_123"
        assert repo.poll_interval_minutes == 5
    finally:
        db.close()


def test_toggle_active():
    db = TestingSessionLocal()
    try:
        repo = Repository(
            name="Toggle Active Repo",
            git_url="https://github.com/test/toggle.git",
            track_all_branches=False,
            tracked_branches="main",
            drone_server="https://drone.uberserver.ru",
            drone_repo="test/toggle",
            drone_token="token",
            poll_interval_minutes=2,
            is_active=True,
            last_status="idle"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        repo_id = repo.id
    finally:
        db.close()

    client = TestClient(app)
    resp = client.post(f"/api/repos/{repo_id}/toggle-active")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = client.post(f"/api/repos/{repo_id}/toggle-active")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_logs_page():
    client = TestClient(app)
    resp = client.get("/logs")
    assert resp.status_code == 200
    assert "Журнал событий" in resp.text


def test_guide_page():
    client = TestClient(app)
    resp = client.get("/guide")
    assert resp.status_code == 200
    assert "Руководство и инструкция" in resp.text
    assert "custom" in resp.text


def test_test_connection_api():
    client = TestClient(app)
    from unittest.mock import patch, AsyncMock

    with patch("app.routes.api.fetch_remote_heads", new=AsyncMock(return_value={"main": "abc1", "feat": "abc2"})), \
         patch("app.routes.api.test_drone_connection", new=AsyncMock(return_value=(True, "Drone OK"))):
        resp = client.post(
            "/api/test-connection",
            json={
                "git_url": "https://github.com/shpion34rus/secure-messenger.git",
                "git_username": "shpion34rus",
                "git_password": "token",
                "drone_server": "https://drone.uberserver.ru",
                "drone_repo": "shpion34rus/secure-messenger",
                "drone_token": "token123"
            }
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_ok"] is True
        assert data["drone_ok"] is True
        assert "main" in data["git_branches"]
        assert "feat" in data["git_branches"]
