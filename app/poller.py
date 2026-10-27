import re
import os
import asyncio
import fnmatch
import logging
import time
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlsplit, urlunsplit, quote

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Repository, BranchState, PollLog, utc_now
from app.metrics import (
    POLL_TOTAL,
    POLL_DURATION_SECONDS,
    POLL_ERRORS_TOTAL,
    BUILDS_TRIGGERED_TOTAL
)

logger = logging.getLogger("drone_sync.poller")

# In-memory concurrency locks per repository to avoid duplicate simultaneous polls
_repo_locks: Dict[int, asyncio.Lock] = {}

# Shared HTTP client for connection pooling
_http_client: Optional[httpx.AsyncClient] = None


def get_repo_lock(repo_id: int) -> asyncio.Lock:
    """Get or create an asyncio.Lock for the given repository ID."""
    if repo_id not in _repo_locks:
        _repo_locks[repo_id] = asyncio.Lock()
    return _repo_locks[repo_id]


def remove_repo_lock(repo_id: int):
    """Clean up memory lock for a deleted repository."""
    _repo_locks.pop(repo_id, None)


def get_http_client() -> httpx.AsyncClient:
    """Get or create a reusable httpx.AsyncClient with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
        )
    return _http_client


async def close_http_client():
    """Gracefully close shared HTTP client."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


class GitError(Exception):
    pass


class DroneError(Exception):
    pass


def mask_git_url(url: str) -> str:
    """Mask credentials in URL so they never leak in logs, UI, or errors."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            masked_netloc = f"***:***@{parts.hostname}"
            if parts.port:
                masked_netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, masked_netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    # Regex fallback
    return re.sub(r"://([^:@]+):([^@]+)@", "://***:***@", url)


def build_authenticated_git_url(
    git_url: str,
    username: Optional[str] = None,
    password: Optional[str] = None
) -> str:
    """Inject credentials into HTTP/HTTPS git URL safely."""
    git_url = git_url.strip()
    if not git_url.startswith(("http://", "https://")):
        return git_url
    
    if not username and not password:
        return git_url

    parts = urlsplit(git_url)
    user_str = quote(username.strip(), safe="") if username and username.strip() else ""
    pass_str = quote(password, safe="") if password else ""

    if user_str and pass_str:
        auth = f"{user_str}:{pass_str}"
    elif pass_str:
        # Personal access token with default username
        auth = f"x-access-token:{pass_str}"
    else:
        auth = user_str

    netloc = f"{auth}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def fetch_remote_heads(
    git_url: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = 20
) -> Dict[str, str]:
    """
    Fetch all remote branch heads using 'git ls-remote --heads'.
    Returns a mapping: {branch_name: commit_sha}
    """
    auth_url = build_authenticated_git_url(git_url, username, password)
    masked_url = mask_git_url(auth_url)

    env = os.environ.copy()
    # Ensure git never pauses waiting for terminal credentials
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"

    cmd = ["git", "ls-remote", "--heads", "--", auth_url]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise GitError(f"Connection timed out ({timeout}s) while querying {masked_url}")

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            # Clean auth strings from stderr
            err_msg = mask_git_url(err_msg)
            raise GitError(f"git ls-remote failed (exit code {process.returncode}): {err_msg}")

        heads: Dict[str, str] = {}
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                sha, ref = parts[0], parts[1]
                if ref.startswith("refs/heads/"):
                    branch = ref[len("refs/heads/"):]
                    heads[branch] = sha

        return heads

    except GitError:
        raise
    except Exception as e:
        clean_msg = mask_git_url(str(e))
        raise GitError(f"Git execution error: {clean_msg}")


async def trigger_drone_build(
    drone_server: str,
    drone_repo: str,
    drone_token: str,
    branch: str,
    *args,
    commit_sha: Optional[str] = None,
    **kwargs
) -> Tuple[int, str]:
    """
    Trigger a new build for a branch in Drone CI.
    Returns: (build_number, message)
    """
    base_server = drone_server.rstrip("/")
    clean_repo = drone_repo.strip().strip("/")
    url = f"{base_server}/api/repos/{clean_repo}/builds?branch={quote(branch, safe='')}"
    if commit_sha and commit_sha != "HEAD":
        url += f"&commit={quote(commit_sha.strip(), safe='')}"

    headers = {
        "Authorization": f"Bearer {drone_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    client = get_http_client()
    try:
        response = await client.post(url, headers=headers)
    except httpx.RequestError as exc:
        raise DroneError(f"Сетевая ошибка при обращении к Drone CI ({base_server}): {str(exc)}")

    if 200 <= response.status_code < 300:
        data = None
        try:
            data = response.json()
        except Exception:
            pass

        if data is None or not isinstance(data, dict):
            raise DroneError(
                "Drone CI вернул пустой ответ 'null' — билд не был создан. "
                "Причина: триггеры в .drone.yml отклонили запуск. "
                "Drone CI при вызове через REST API присваивает событию тип 'custom'. "
                "Чтобы сборка запускалась, добавьте 'custom' в секцию trigger.event в .drone.yml репозитория "
                "(например: trigger: event: [push, pull_request, custom])."
            )

        build_num = data.get("number")
        if build_num is None:
            raise DroneError(f"Drone CI вернул ответ без номера билда: {response.text[:200]}")

        return build_num, f"Drone build #{build_num} triggered successfully."
    else:
        err_body = response.text[:200]
        raise DroneError(f"Drone API вернул HTTP {response.status_code}: {err_body}")


async def test_drone_connection(
    drone_server: str,
    drone_repo: str,
    drone_token: str
) -> Tuple[bool, str]:
    """Check Drone CI credentials and repository existence."""
    base_server = drone_server.rstrip("/")
    clean_repo = drone_repo.strip().strip("/")
    url = f"{base_server}/api/repos/{clean_repo}"

    headers = {
        "Authorization": f"Bearer {drone_token.strip()}",
        "Accept": "application/json",
    }

    client = get_http_client()
    try:
        resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        return False, f"Network error contacting Drone CI at {base_server}: {str(exc)}"

    if resp.status_code == 200:
        return True, f"Drone repository '{clean_repo}' verified successfully."
    elif resp.status_code == 401:
        return False, "Drone CI authentication failed: Invalid or expired token."
    elif resp.status_code == 404:
        return False, f"Repository '{clean_repo}' was not found in Drone CI."
    else:
        return False, f"Drone CI returned HTTP {resp.status_code}: {resp.text[:150]}"


def is_branch_tracked(branch: str, track_all: bool, patterns: List[str]) -> bool:
    """Determine whether a given branch should be tracked."""
    if track_all:
        return True
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        if pat == branch or fnmatch.fnmatch(branch, pat):
            return True
    return False


async def poll_single_repository(repo_id: int, db: Session, force: bool = False) -> Dict:
    """
    Perform a complete poll cycle for one repository:
    1. Query git remote heads
    2. Compare with branch_states in DB
    3. Trigger Drone CI when SHA differs
    4. Record logs and state updates
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        return {"status": "error", "message": f"Repository {repo_id} not found"}

    if not repo.is_active and not force:
        return {"status": "skipped", "message": "Repository is inactive"}

    if not force:
        from app.schedule import is_repo_in_schedule_window
        in_window, sched_reason = is_repo_in_schedule_window(repo, db)
        if not in_window:
            logger.debug("Repo %d (%s) poll skipped by schedule: %s", repo_id, repo.name, sched_reason)
            return {"status": "skipped", "message": f"Вне расписания: {sched_reason}"}

    lock = get_repo_lock(repo_id)
    if lock.locked():
        logger.warning("Repository %d poll is already in progress, skipping concurrent run.", repo_id)
        return {"status": "busy", "message": "Опрос репозитория уже выполняется"}

    async with lock:
        repo.last_status = "checking"
        db.commit()

        patterns = repo.get_tracked_branch_list()
        log_messages = []
        start_time = time.perf_counter()

        try:
            plain_pass = repo.get_plain_git_password()
            plain_token = repo.get_plain_drone_token()
            effective_user = repo.get_effective_git_username()
            effective_server = repo.get_effective_drone_server()

            remote_heads = await fetch_remote_heads(
                git_url=repo.git_url,
                username=effective_user,
                password=plain_pass,
                timeout=settings.git_timeout
            )

            matched_branches = [
                b for b in remote_heads.keys()
                if is_branch_tracked(b, repo.track_all_branches, patterns)
            ]

            now = utc_now()

            # Query existing branch states for this repo
            existing_states = {
                s.branch_name: s
                for s in db.query(BranchState).filter(BranchState.repo_id == repo.id).all()
            }

            # Update stale branches (deleted from remote)
            for b_name, b_state in existing_states.items():
                if b_name not in remote_heads:
                    if not b_state.is_stale:
                        b_state.is_stale = True
                        b_state.updated_at = now
                else:
                    if b_state.is_stale:
                        b_state.is_stale = False
                        b_state.updated_at = now

            if not matched_branches:
                msg = f"No tracked branches found on remote (available: {len(remote_heads)} branches)."
                repo.last_status = "success"
                repo.last_error = None
                repo.last_polled_at = now
                db.commit()
                return {"status": "ok", "branches_checked": 0, "message": msg}

            triggers_count = 0

            for branch in matched_branches:
                remote_sha = remote_heads[branch]
                state = existing_states.get(branch)

                if state is None:
                    # First time tracking this branch - save SHA without firing build
                    new_state = BranchState(
                        repo_id=repo.id,
                        branch_name=branch,
                        last_sha=remote_sha,
                        is_stale=False,
                        updated_at=now
                    )
                    db.add(new_state)
                    log_entry = PollLog(
                        repo_id=repo.id,
                        repo_name=repo.name,
                        branch_name=branch,
                        commit_sha=remote_sha,
                        level="info",
                        message=f"Branch initialized at SHA {remote_sha[:7]}",
                        created_at=now
                    )
                    db.add(log_entry)
                    log_messages.append(f"Initialized {branch} at {remote_sha[:7]}")

                elif state.last_sha != remote_sha:
                    # SHA changed! Trigger Drone CI
                    old_sha = state.last_sha
                    logger.info(
                        "New commit in %s (%s): %s -> %s",
                        repo.name, branch, old_sha[:7], remote_sha[:7]
                    )

                    try:
                        build_num, build_msg = await trigger_drone_build(
                            drone_server=effective_server,
                            drone_repo=repo.drone_repo,
                            drone_token=plain_token,
                            branch=branch
                        )
                        state.last_sha = remote_sha
                        state.last_build_number = int(build_num) if str(build_num).isdigit() else None
                        state.last_build_status = "triggered"
                        state.is_stale = False
                        state.updated_at = now

                        db.add(PollLog(
                            repo_id=repo.id,
                            repo_name=repo.name,
                            branch_name=branch,
                            commit_sha=remote_sha,
                            build_number=state.last_build_number,
                            level="success",
                            message=f"New commit {remote_sha[:7]} (prev {old_sha[:7]}). Triggered build #{build_num}",
                            created_at=now
                        ))
                        triggers_count += 1
                        log_messages.append(f"Triggered #{build_num} for {branch} ({remote_sha[:7]})")
                        BUILDS_TRIGGERED_TOTAL.labels(repo=repo.name, branch=branch).inc()

                        try:
                            from app.notifier import dispatch_event_notification
                            asyncio.create_task(dispatch_event_notification(
                                event_type="build",
                                title=f"Запущена сборка #{build_num}",
                                message=f"Обнаружен новый коммит. Запущена сборка #{build_num} для ветки {branch}.",
                                repo_name=repo.name,
                                branch=branch,
                                commit_sha=remote_sha,
                                build_number=state.last_build_number
                            ))
                        except Exception:
                            pass

                    except DroneError as d_err:
                        error_text = f"Drone CI trigger failed for branch {branch} ({remote_sha[:7]}): {str(d_err)}"
                        logger.error(error_text)
                        POLL_ERRORS_TOTAL.labels(repo=repo.name, error_type="drone_trigger").inc()
                        db.add(PollLog(
                            repo_id=repo.id,
                            repo_name=repo.name,
                            branch_name=branch,
                            commit_sha=remote_sha,
                            level="error",
                            message=error_text,
                            created_at=now
                        ))
                        log_messages.append(f"Failed to trigger {branch}: {str(d_err)}")

                        try:
                            from app.notifier import dispatch_event_notification
                            asyncio.create_task(dispatch_event_notification(
                                event_type="error",
                                title="Ошибка запуска сборки Drone CI",
                                message=error_text,
                                repo_name=repo.name,
                                branch=branch,
                                commit_sha=remote_sha
                            ))
                        except Exception:
                            pass

            repo.last_status = "success"
            repo.last_error = None
            repo.last_polled_at = now
            db.commit()

            duration = time.perf_counter() - start_time
            POLL_DURATION_SECONDS.labels(repo=repo.name).observe(duration)
            POLL_TOTAL.labels(repo=repo.name, status="success").inc()

            return {
                "status": "ok",
                "branches_checked": len(matched_branches),
                "triggers_count": triggers_count,
                "details": log_messages
            }

        except Exception as e:
            clean_err = mask_git_url(str(e))
            logger.error("Poll error on repository %s: %s", repo.name, clean_err)
            repo.last_status = "error"
            repo.last_error = clean_err
            repo.last_polled_at = utc_now()
            
            db.add(PollLog(
                repo_id=repo.id,
                repo_name=repo.name,
                level="error",
                message=f"Poll error: {clean_err}",
                created_at=utc_now()
            ))
            db.commit()

            duration = time.perf_counter() - start_time
            POLL_DURATION_SECONDS.labels(repo=repo.name).observe(duration)
            POLL_TOTAL.labels(repo=repo.name, status="error").inc()
            err_type = "git" if isinstance(e, GitError) else "drone" if isinstance(e, DroneError) else "exception"
            POLL_ERRORS_TOTAL.labels(repo=repo.name, error_type=err_type).inc()

            try:
                from app.notifier import dispatch_event_notification
                asyncio.create_task(dispatch_event_notification(
                    event_type="error",
                    title="Ошибка опроса Git репозитория",
                    message=clean_err,
                    repo_name=repo.name
                ))
            except Exception:
                pass

            return {"status": "error", "message": clean_err}


def cleanup_old_logs(db: Session, max_age_days: int = 14) -> int:
    """Purge old logs beyond retention period."""
    from datetime import timedelta
    cutoff = utc_now() - timedelta(days=max_age_days)
    deleted = db.query(PollLog).filter(PollLog.created_at < cutoff).delete()
    db.commit()
    logger.info("Purged %d poll logs older than %d days.", deleted, max_age_days)
    return deleted
