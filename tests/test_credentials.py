import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from main import app
from app.models import Credential, Repository
from tests.conftest import TestingSessionLocal


def test_credential_encryption_in_model():
    """Verify that Credential model encrypts secret and decrypts via get_plain_secret()."""
    db = TestingSessionLocal()
    try:
        cred = Credential(
            name="GitHub Token",
            cred_type="git",
            username="testuser",
            description="Work account"
        )
        cred.set_secret("ghp_secret_token_12345")
        db.add(cred)
        db.commit()
        db.refresh(cred)

        assert cred.secret != "ghp_secret_token_12345"
        assert cred.secret.startswith("gAAAAA")
        assert cred.get_plain_secret() == "ghp_secret_token_12345"
    finally:
        db.close()


def test_credentials_web_crud_flow():
    """Test full web flow: create, view list, edit, and delete credential."""
    client = TestClient(app, follow_redirects=False)

    # 1. Create Git Credential via web form
    res = client.post("/credentials/new", data={
        "name": "My Git Cred",
        "cred_type": "git",
        "username": "octocat",
        "secret": "super_pat_pass",
        "description": "Primary PAT"
    })
    assert res.status_code == 303
    assert res.headers["location"] == "/credentials"

    # 2. View credentials list
    list_res = client.get("/credentials")
    assert list_res.status_code == 200
    assert "My Git Cred" in list_res.text
    assert "octocat" in list_res.text

    # 3. Find created cred in DB
    db = TestingSessionLocal()
    try:
        cred = db.query(Credential).filter(Credential.name == "My Git Cred").first()
        assert cred is not None
        cred_id = cred.id
    finally:
        db.close()

    # 4. Edit view
    edit_page = client.get(f"/credentials/{cred_id}/edit")
    assert edit_page.status_code == 200
    assert "Редактировать креденшел" in edit_page.text
    assert "My Git Cred" in edit_page.text

    # 5. Submit update (change name, keep secret)
    update_res = client.post(f"/credentials/{cred_id}/edit", data={
        "name": "Renamed Git Cred",
        "cred_type": "git",
        "username": "octocat_updated",
        "secret": "",  # empty to keep previous
        "description": "Updated description"
    })
    assert update_res.status_code == 303

    db = TestingSessionLocal()
    try:
        updated = db.query(Credential).filter(Credential.id == cred_id).first()
        assert updated.name == "Renamed Git Cred"
        assert updated.username == "octocat_updated"
        # Secret should still be original
        assert updated.get_plain_secret() == "super_pat_pass"
    finally:
        db.close()

    # 6. Delete credential
    del_res = client.post(f"/credentials/{cred_id}/delete")
    assert del_res.status_code == 303

    db = TestingSessionLocal()
    try:
        deleted = db.query(Credential).filter(Credential.id == cred_id).first()
        assert deleted is None
    finally:
        db.close()


def test_credentials_api_crud_flow():
    """Test REST API for credentials."""
    client = TestClient(app)

    # 1. Create Drone Credential via API
    create_resp = client.post("/api/credentials", json={
        "name": "Drone Staging Server",
        "cred_type": "drone",
        "server_url": "https://drone-staging.example.com",
        "secret": "drone_secret_api_token",
        "description": "Staging cluster"
    })
    assert create_resp.status_code == 200
    data = create_resp.json()
    assert data["name"] == "Drone Staging Server"
    assert data["cred_type"] == "drone"
    assert data["server_url"] == "https://drone-staging.example.com"
    assert data["secret_masked"] == "••••••••"
    cred_id = data["id"]

    # 2. Get single
    get_resp = client.get(f"/api/credentials/{cred_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == cred_id

    # 3. List
    list_resp = client.get("/api/credentials")
    assert list_resp.status_code == 200
    ids = [c["id"] for c in list_resp.json()]
    assert cred_id in ids

    # 4. Update
    put_resp = client.put(f"/api/credentials/{cred_id}", json={
        "name": "Drone Staging Server v2",
        "secret": "new_drone_secret"
    })
    assert put_resp.status_code == 200
    assert put_resp.json()["name"] == "Drone Staging Server v2"

    db = TestingSessionLocal()
    try:
        db_cred = db.query(Credential).filter(Credential.id == cred_id).first()
        assert db_cred.get_plain_secret() == "new_drone_secret"
    finally:
        db.close()

    # 5. Delete
    del_resp = client.delete(f"/api/credentials/{cred_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    get_after = client.get(f"/api/credentials/{cred_id}")
    assert get_after.status_code == 404


def test_repository_linked_credentials_resolution_and_unlinking():
    """Verify that Repository resolves username, password, server and token from linked credentials,
    and safely unlinks when credential is deleted."""
    db = TestingSessionLocal()
    try:
        git_cred = Credential(
            name="Shared GitHub PAT",
            cred_type="git",
            username="company-bot",
            description="Bot user"
        )
        git_cred.set_secret("bot_pat_token_999")

        drone_cred = Credential(
            name="Drone Cluster Main",
            cred_type="drone",
            server_url="https://drone-cluster.internal",
            description="Prod drone"
        )
        drone_cred.set_secret("drone_cluster_token_888")

        db.add_all([git_cred, drone_cred])
        db.commit()
        db.refresh(git_cred)
        db.refresh(drone_cred)

        repo = Repository(
            name="Repo with Linked Creds",
            git_url="https://github.com/company/core.git",
            git_credential_id=git_cred.id,
            drone_server="https://fallback-drone.com",
            drone_repo="company/core",
            drone_credential_id=drone_cred.id,
            drone_token="",
            poll_interval_minutes=3,
            is_active=True
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        # 1. Effective credential resolution
        assert repo.get_effective_git_username() == "company-bot"
        assert repo.get_plain_git_password() == "bot_pat_token_999"
        assert repo.get_effective_drone_server() == "https://drone-cluster.internal"
        assert repo.get_plain_drone_token() == "drone_cluster_token_888"

        # 2. Delete Git Credential via API and verify repo is unlinked without deletion
        client = TestClient(app)
        res = client.delete(f"/api/credentials/{git_cred.id}")
        assert res.status_code == 200

        db.refresh(repo)
        assert repo.git_credential_id is None
        # Should now fallback to repo direct fields
        assert repo.get_effective_git_username() is None
        assert repo.get_plain_git_password() is None
        # Drone credential remains linked
        assert repo.drone_credential_id == drone_cred.id
        assert repo.get_plain_drone_token() == "drone_cluster_token_888"

    finally:
        db.close()


@pytest.mark.anyio
async def test_test_connection_resolves_credentials():
    """Verify that /api/test-connection resolves credentials by ID."""
    db = TestingSessionLocal()
    try:
        git_cred = Credential(
            name="Test Git Cred",
            cred_type="git",
            username="ci-user",
        )
        git_cred.set_secret("ci-password-123")

        drone_cred = Credential(
            name="Test Drone Cred",
            cred_type="drone",
            server_url="https://drone-custom.domain",
        )
        drone_cred.set_secret("drone-api-token-456")

        db.add_all([git_cred, drone_cred])
        db.commit()
        db.refresh(git_cred)
        db.refresh(drone_cred)

        captured_git_args = {}
        captured_drone_args = {}

        async def fake_fetch_remote_heads(git_url, username=None, password=None, timeout=15):
            captured_git_args["git_url"] = git_url
            captured_git_args["username"] = username
            captured_git_args["password"] = password
            return {"main": "sha12345", "develop": "sha67890"}

        async def fake_test_drone_connection(drone_server, drone_repo, drone_token):
            captured_drone_args["drone_server"] = drone_server
            captured_drone_args["drone_repo"] = drone_repo
            captured_drone_args["drone_token"] = drone_token
            return True, "Drone OK"

        with patch("app.routes.api.fetch_remote_heads", side_effect=fake_fetch_remote_heads), \
             patch("app.routes.api.test_drone_connection", side_effect=fake_test_drone_connection):

            client = TestClient(app)
            response = client.post("/api/test-connection", json={
                "git_url": "https://github.com/myorg/myrepo.git",
                "git_credential_id": git_cred.id,
                "drone_server": "https://default-drone.com",
                "drone_repo": "myorg/myrepo",
                "drone_credential_id": drone_cred.id
            })

            assert response.status_code == 200
            data = response.json()
            assert data["git_ok"] is True
            assert "main" in data["git_branches"]
            assert data["drone_ok"] is True

            # Verify that credentials were accurately extracted and decrypted
            assert captured_git_args["username"] == "ci-user"
            assert captured_git_args["password"] == "ci-password-123"
            assert captured_drone_args["drone_server"] == "https://drone-custom.domain"
            assert captured_drone_args["drone_token"] == "drone-api-token-456"

    finally:
        db.close()


def test_create_repo_with_linked_credentials_and_empty_drone_token():
    """Verify creating a repository via POST /repos/new with linked credentials and empty drone_token."""
    db = TestingSessionLocal()
    try:
        git_cred = Credential(name="Git PAT", cred_type="git", username="gituser")
        git_cred.set_secret("gitpass123")
        drone_cred = Credential(name="Drone Token", cred_type="drone", server_url="https://drone.uberserver.ru")
        drone_cred.set_secret("dronesecret456")
        db.add_all([git_cred, drone_cred])
        db.commit()
        db.refresh(git_cred)
        db.refresh(drone_cred)

        client = TestClient(app, follow_redirects=False)
        form_data = {
            "name": "smart-home",
            "poll_interval_minutes": "2",
            "is_active": "on",
            "git_url": "https://github.com/decole/smart-home.git",
            "git_credential_id": str(git_cred.id),
            "git_username": "",
            "git_password": "",
            "track_all_branches": "on",
            "tracked_branches": "main",
            "drone_credential_id": str(drone_cred.id),
            "drone_server": "https://drone.uberserver.ru",
            "drone_repo": "decole/smart-home",
            "drone_token": ""
        }
        res = client.post("/repos/new", data=form_data)
        assert res.status_code == 303
        redirect_url = res.headers["location"]
        repo_id = int(redirect_url.split("/")[-1])

        created = db.query(Repository).filter(Repository.id == repo_id).first()
        assert created is not None
        assert created.name == "smart-home"
        assert created.git_credential_id == git_cred.id
        assert created.drone_credential_id == drone_cred.id
        assert created.drone_token == ""
        assert created.get_plain_drone_token() == "dronesecret456"
    finally:
        db.close()


@pytest.mark.anyio
async def test_poller_uses_linked_credentials():
    """Verify that poll_single_repository uses username, password, server and token from linked credentials."""
    from app.poller import poll_single_repository
    from app.models import BranchState

    db = TestingSessionLocal()
    try:
        git_cred = Credential(
            name="Poller Git Cred",
            cred_type="git",
            username="git-poller-user"
        )
        git_cred.set_secret("git-poller-secret-pw")

        drone_cred = Credential(
            name="Poller Drone Cred",
            cred_type="drone",
            server_url="https://drone-poll.domain"
        )
        drone_cred.set_secret("drone-poller-secret-token")

        db.add_all([git_cred, drone_cred])
        db.commit()
        db.refresh(git_cred)
        db.refresh(drone_cred)

        repo = Repository(
            name="Poller Cred Repo",
            git_url="https://github.com/org/poll-repo.git",
            git_credential_id=git_cred.id,
            drone_server="https://fallback.domain",
            drone_repo="org/poll-repo",
            drone_credential_id=drone_cred.id,
            drone_token="",
            track_all_branches=True,
            tracked_branches="main",
            poll_interval_minutes=2,
            is_active=True,
            last_status="idle"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        # Pre-seed branch state with an old SHA
        old_state = BranchState(
            repo_id=repo.id,
            branch_name="main",
            last_sha="1111111111111111111111111111111111111111"
        )
        db.add(old_state)
        db.commit()

        captured_git = {}
        captured_drone = {}

        async def fake_fetch(git_url, username=None, password=None, timeout=20):
            captured_git["user"] = username
            captured_git["pw"] = password
            return {"main": "2222222222222222222222222222222222222222"}

        async def fake_trigger(drone_server, drone_repo, drone_token, branch):
            captured_drone["server"] = drone_server
            captured_drone["token"] = drone_token
            captured_drone["branch"] = branch
            return 99, "Triggered #99"

        with patch("app.poller.fetch_remote_heads", side_effect=fake_fetch), \
             patch("app.poller.trigger_drone_build", side_effect=fake_trigger):

            result = await poll_single_repository(repo.id, db)
            assert result["status"] == "ok"
            assert result["triggers_count"] == 1

            assert captured_git["user"] == "git-poller-user"
            assert captured_git["pw"] == "git-poller-secret-pw"
            assert captured_drone["server"] == "https://drone-poll.domain"
            assert captured_drone["token"] == "drone-poller-secret-token"

    finally:
        db.close()
