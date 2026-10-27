import pytest
from unittest.mock import patch, AsyncMock
from app.poller import (
    build_authenticated_git_url,
    mask_git_url,
    is_branch_tracked,
    poll_single_repository
)
from app.models import Repository, BranchState, PollLog
from tests.conftest import TestingSessionLocal


def test_build_authenticated_git_url():
    # Public URL without credentials
    assert build_authenticated_git_url("https://github.com/foo/bar.git") == "https://github.com/foo/bar.git"

    # URL with username and password
    url = build_authenticated_git_url(
        "https://github.com/foo/bar.git",
        username="myuser",
        password="mypassword"
    )
    assert url == "https://myuser:mypassword@github.com/foo/bar.git"

    # URL with special characters in credentials
    url_special = build_authenticated_git_url(
        "https://github.com/foo/bar.git",
        username="user@domain.com",
        password="p@$$word:!# "
    )
    assert "user%40domain.com:p%40%24%24word%3A%21%23%20" in url_special


def test_mask_git_url():
    url = "https://user:secrettoken@github.com/owner/repo.git"
    masked = mask_git_url(url)
    assert "secrettoken" not in masked
    assert "***:***@github.com" in masked


def test_is_branch_tracked():
    # Track all
    assert is_branch_tracked("main", True, []) is True
    assert is_branch_tracked("feature/test", True, []) is True

    # Selective tracking
    patterns = ["main", "release/*", "v1.*"]
    assert is_branch_tracked("main", False, patterns) is True
    assert is_branch_tracked("release/2.0", False, patterns) is True
    assert is_branch_tracked("v1.5", False, patterns) is True
    assert is_branch_tracked("develop", False, patterns) is False
    assert is_branch_tracked("feature/abc", False, patterns) is False


@pytest.mark.anyio
async def test_poll_single_repository_flow():
    db = TestingSessionLocal()
    try:
        # Create test repo
        repo = Repository(
            name="Poller Test Repo",
            git_url="https://github.com/test/repo.git",
            track_all_branches=False,
            tracked_branches="main,develop",
            drone_server="https://drone.example.com",
            drone_repo="test/repo",
            drone_token="drone-token-xyz",
            poll_interval_minutes=2,
            is_active=True,
            last_status="idle"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        repo_id = repo.id

        # 1. First poll: initialize main at SHA1
        mock_heads_1 = {
            "main": "1111111111111111111111111111111111111111",
            "develop": "2222222222222222222222222222222222222222",
            "ignored_branch": "9999999999999999999999999999999999999999"
        }

        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=mock_heads_1)):
            res1 = await poll_single_repository(repo_id, db)
            assert res1["status"] == "ok"
            assert res1["triggers_count"] == 0  # Initialized, no build triggered

        # Check DB branch states
        branches = db.query(BranchState).filter(BranchState.repo_id == repo_id).all()
        branch_map = {b.branch_name: b.last_sha for b in branches}
        assert "main" in branch_map
        assert "develop" in branch_map
        assert "ignored_branch" not in branch_map
        assert branch_map["main"] == "1111111111111111111111111111111111111111"

        # 2. Second poll: SHA has NOT changed -> no builds
        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=mock_heads_1)):
            res2 = await poll_single_repository(repo_id, db)
            assert res2["status"] == "ok"
            assert res2["triggers_count"] == 0

        # 3. Third poll: main has new commit -> trigger Drone CI!
        mock_heads_new_commit = {
            "main": "3333333333333333333333333333333333333333",
            "develop": "2222222222222222222222222222222222222222"
        }

        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=mock_heads_new_commit)), \
             patch("app.poller.trigger_drone_build", new=AsyncMock(return_value=(42, "Build #42 started"))):
            res3 = await poll_single_repository(repo_id, db)
            assert res3["status"] == "ok"
            assert res3["triggers_count"] == 1

        # Check updated state
        main_state = db.query(BranchState).filter(
            BranchState.repo_id == repo_id,
            BranchState.branch_name == "main"
        ).first()
        assert main_state.last_sha == "3333333333333333333333333333333333333333"
        assert main_state.last_build_number == 42

        # Check log
        log_entry = db.query(PollLog).filter(
            PollLog.repo_id == repo_id,
            PollLog.branch_name == "main",
            PollLog.level == "success"
        ).first()
        assert log_entry is not None
        assert "Triggered build #42" in log_entry.message or "3333333" in log_entry.message

    finally:
        db.close()


@pytest.mark.anyio
async def test_concurrent_lock_prevents_duplicate_poll():
    """Verify that concurrent poll requests on the same repository return busy."""
    import asyncio
    from app.poller import get_repo_lock

    db = TestingSessionLocal()
    try:
        repo = Repository(
            name="Lock Test Repo",
            git_url="https://github.com/test/lock.git",
            track_all_branches=True,
            tracked_branches="main",
            drone_server="https://drone.example.com",
            drone_repo="test/lock",
            drone_token="token",
            poll_interval_minutes=2,
            is_active=True,
            last_status="idle"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        lock = get_repo_lock(repo.id)
        # Acquire lock simulating long poll
        await lock.acquire()
        try:
            # Second attempt while lock is held
            res = await poll_single_repository(repo.id, db)
            assert res["status"] == "busy"
            assert "уже выполняется" in res["message"]
        finally:
            lock.release()

    finally:
        db.close()


@pytest.mark.anyio
async def test_stale_branch_marking():
    """Verify that branches deleted on remote are marked as is_stale."""
    db = TestingSessionLocal()
    try:
        repo = Repository(
            name="Stale Branch Repo",
            git_url="https://github.com/test/stale.git",
            track_all_branches=True,
            tracked_branches="main",
            drone_server="https://drone.example.com",
            drone_repo="test/stale",
            drone_token="token",
            poll_interval_minutes=2,
            is_active=True,
            last_status="idle"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        # 1. First poll with main and feature-x
        heads_1 = {
            "main": "1111111111111111111111111111111111111111",
            "feature-x": "2222222222222222222222222222222222222222"
        }
        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=heads_1)):
            await poll_single_repository(repo.id, db)

        feat_state = db.query(BranchState).filter(
            BranchState.repo_id == repo.id,
            BranchState.branch_name == "feature-x"
        ).first()
        assert feat_state is not None
        assert feat_state.is_stale is False

        # 2. Second poll: feature-x is deleted from remote
        heads_2 = {
            "main": "1111111111111111111111111111111111111111"
        }
        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=heads_2)):
            await poll_single_repository(repo.id, db)

        db.refresh(feat_state)
        assert feat_state.is_stale is True

        # 3. Third poll: feature-x is restored
        with patch("app.poller.fetch_remote_heads", new=AsyncMock(return_value=heads_1)):
            await poll_single_repository(repo.id, db)

        db.refresh(feat_state)
        assert feat_state.is_stale is False

    finally:
        db.close()


def test_cleanup_old_logs():
    """Verify log retention purging works accurately."""
    from datetime import datetime, timezone, timedelta
    from app.poller import cleanup_old_logs

    db = TestingSessionLocal()
    try:
        now = datetime.now(timezone.utc)
        # Old log (20 days ago)
        old_log = PollLog(
            repo_id=None,
            repo_name="old-repo",
            level="info",
            message="Old entry",
            created_at=now - timedelta(days=20)
        )
        # Recent log (2 days ago)
        new_log = PollLog(
            repo_id=None,
            repo_name="new-repo",
            level="info",
            message="Recent entry",
            created_at=now - timedelta(days=2)
        )
        db.add_all([old_log, new_log])
        db.commit()

        deleted = cleanup_old_logs(db, max_age_days=14)
        assert deleted >= 1

        remaining = db.query(PollLog).all()
        messages = [r.message for r in remaining]
        assert "Recent entry" in messages
        assert "Old entry" not in messages

    finally:
        db.close()


@pytest.mark.anyio
async def test_git_option_injection_safety():
    """Verify that git ls-remote includes '--' argument separator."""
    import asyncio
    from app.poller import fetch_remote_heads

    captured_cmds = []

    async def fake_subprocess(*cmd, **kwargs):
        captured_cmds.append(list(cmd))
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"abc1234\trefs/heads/main\n", b""))
        mock_proc.returncode = 0
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        heads = await fetch_remote_heads("https://github.com/foo/bar.git")
        assert heads.get("main") == "abc1234"
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        # Must contain '--' before the URL to prevent Option Injection
        dash_idx = cmd.index("--")
        assert dash_idx > 0
        assert cmd[dash_idx + 1].startswith("https://")
