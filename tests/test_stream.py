"""Tests for real-time Server-Sent Events (SSE) log streaming."""
import json
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Repository, PollLog
from main import app

client = TestClient(app)


def test_stream_connection_headers():
    """Verify SSE endpoint returns proper streaming headers."""
    with client.stream("GET", "/api/logs/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"
        line = next(resp.iter_lines())
        assert line == ": connected"


def test_stream_receives_new_log_event():
    """Verify new database log entries are broadcasted via SSE."""
    with client.stream("GET", "/api/logs/stream") as resp:
        line_iter = resp.iter_lines()
        # initial : connected
        assert next(line_iter) == ": connected"
        assert next(line_iter) == ""

        # Insert a log
        db = SessionLocal()
        log = PollLog(
            repo_name="stream-demo",
            branch_name="feature/live",
            commit_sha="abcdef123456",
            build_number=99,
            level="success",
            message="Triggered build #99 for feature/live"
        )
        db.add(log)
        db.commit()
        log_id = log.id
        db.close()

        # Read SSE response
        event_line = next(line_iter)
        data_line = next(line_iter)
        assert event_line == "event: log"
        assert data_line.startswith("data: ")

        data = json.loads(data_line[6:])
        assert data["id"] == log_id
        assert data["repo_name"] == "stream-demo"
        assert data["branch_name"] == "feature/live"
        assert data["commit_sha"] == "abcdef123456"
        assert data["build_number"] == 99
        assert data["level"] == "success"
        assert "Triggered build #99" in data["message"]


def test_stream_repo_and_level_filtering():
    """Verify stream query parameters filter events appropriately."""
    db = SessionLocal()
    r1 = Repository(
        name="filter-repo-1",
        git_url="https://github.com/test/repo1.git",
        drone_server="https://drone.example.com",
        drone_repo="test/repo1"
    )
    r2 = Repository(
        name="filter-repo-2",
        git_url="https://github.com/test/repo2.git",
        drone_server="https://drone.example.com",
        drone_repo="test/repo2"
    )
    db.add_all([r1, r2])
    db.commit()
    r1_id = r1.id
    r2_id = r2.id
    db.close()

    # Client connects with filter repo_id=r1_id & level=error
    with client.stream("GET", f"/api/logs/stream?repo_id={r1_id}&level=error") as resp:
        line_iter = resp.iter_lines()
        assert next(line_iter) == ": connected"
        assert next(line_iter) == ""

        db = SessionLocal()
        # Log 1: matches repo and level -> should be delivered
        log_match = PollLog(
            repo_id=r1_id,
            repo_name="filter-repo-1",
            level="error",
            message="Matches filter"
        )
        # Log 2: different repo -> should be ignored
        log_diff_repo = PollLog(
            repo_id=r2_id,
            repo_name="filter-repo-2",
            level="error",
            message="Diff repo"
        )
        # Log 3: same repo, different level -> should be ignored
        log_diff_level = PollLog(
            repo_id=r1_id,
            repo_name="filter-repo-1",
            level="info",
            message="Diff level"
        )
        db.add_all([log_diff_repo, log_diff_level, log_match])
        db.commit()
        match_id = log_match.id
        db.close()

        # The stream should only receive log_match
        event_line = next(line_iter)
        data_line = next(line_iter)
        assert event_line == "event: log"
        data = json.loads(data_line[6:])
        assert data["id"] == match_id
        assert data["repo_id"] == r1_id
        assert data["level"] == "error"
        assert data["message"] == "Matches filter"


def test_stream_clear_event():
    """Verify log clear broadcasts a 'clear' event to connected SSE clients."""
    with client.stream("GET", "/api/logs/stream") as resp:
        line_iter = resp.iter_lines()
        assert next(line_iter) == ": connected"
        assert next(line_iter) == ""

        # Trigger clear logs API
        del_resp = client.delete("/api/logs")
        assert del_resp.status_code == 200

        event_line = next(line_iter)
        data_line = next(line_iter)
        assert event_line == "event: clear"
        assert data_line.startswith("data: ")
