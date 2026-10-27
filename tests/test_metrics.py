from fastapi.testclient import TestClient
from main import app
from app.models import Repository, BranchState
from app.metrics import POLL_TOTAL, BUILDS_TRIGGERED_TOTAL
from tests.conftest import TestingSessionLocal


def test_metrics_endpoint_status_and_content():
    """Verify that /metrics endpoint returns Prometheus formatted metrics."""
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")

    body = response.text
    assert "drone_sync_repositories_total" in body
    assert "drone_sync_branches_tracked_total" in body
    assert "drone_sync_service_info" in body


def test_metrics_gauges_update():
    """Verify that update_db_gauges accurately reports database repository counts."""
    db = TestingSessionLocal()
    try:
        # Seed test repositories
        r1 = Repository(
            name="metrics-test-active",
            git_url="https://github.com/org/repo1.git",
            drone_server="https://drone.example.com",
            drone_repo="org/repo1",
            drone_token="token1",
            is_active=True
        )
        r2 = Repository(
            name="metrics-test-paused",
            git_url="https://github.com/org/repo2.git",
            drone_server="https://drone.example.com",
            drone_repo="org/repo2",
            drone_token="token2",
            is_active=False
        )
        db.add_all([r1, r2])
        db.commit()
        db.refresh(r1)

        b1 = BranchState(
            repo_id=r1.id,
            branch_name="main",
            last_sha="abc1234567890",
            is_stale=False
        )
        db.add(b1)
        db.commit()

        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text

        # Verify active and paused counts
        assert 'drone_sync_repositories_total{status="active"}' in text
        assert 'drone_sync_repositories_total{status="paused"}' in text
        assert 'drone_sync_branches_tracked_total' in text

    finally:
        db.close()


def test_metrics_counters_increment():
    """Verify that incrementing counters reflects in Prometheus output."""
    POLL_TOTAL.labels(repo="test-repo", status="success").inc()
    BUILDS_TRIGGERED_TOTAL.labels(repo="test-repo", branch="feature").inc()

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text

    assert 'drone_sync_poll_total{repo="test-repo",status="success"}' in text
    assert 'drone_sync_builds_triggered_total{branch="feature",repo="test-repo"}' in text
