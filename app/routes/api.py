import asyncio
import json
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Repository,
    BranchState,
    PollLog,
    Credential,
    WorkerScheduleSettings,
    get_worker_schedule_settings,
    utc_now
)
from app.schedule import (
    DAY_KEYS,
    DAY_NAMES_RU,
    DAY_SHORT_RU,
    TIMEZONE_CHOICES,
    check_schedule_window,
    is_repo_in_schedule_window,
    normalize_schedule_dict,
    get_default_schedule,
    get_timezone_obj
)
from app.schemas import (
    TestConnectionRequest,
    TestConnectionResponse,
    TestNotificationRequest,
    SaveNotificationSettingsRequest,
    WorkerScheduleUpdate,
    WorkerScheduleOut,
    DayScheduleItem,
    RepositoryOut,
    CredentialCreate,
    CredentialUpdate,
    CredentialOut
)
from app.poller import (
    fetch_remote_heads,
    test_drone_connection,
    poll_single_repository,
    trigger_drone_build,
    remove_repo_lock,
    DroneError,
    GitError,
    mask_git_url
)
from app.scheduler import add_or_update_repo_job, remove_repo_job

logger = logging.getLogger("drone_sync.api")
router = APIRouter(prefix="/api", tags=["api"])


def _to_credential_out(c: Credential, db: Session) -> CredentialOut:
    usage = db.query(Repository).filter(
        (Repository.git_credential_id == c.id) | (Repository.drone_credential_id == c.id)
    ).count()
    return CredentialOut(
        id=c.id,
        name=c.name,
        cred_type=c.cred_type,  # type: ignore
        username=c.username,
        server_url=c.server_url,
        description=c.description,
        secret_masked="••••••••",
        usage_count=usage,
        created_at=c.created_at,
        updated_at=c.updated_at
    )


# --- Credentials API ---

@router.get("/credentials", response_model=List[CredentialOut])
def list_credentials(
    cred_type: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """List all stored credentials with usage counts."""
    q = db.query(Credential)
    if cred_type:
        q = q.filter(Credential.cred_type == cred_type)
    items = q.order_by(Credential.name.asc()).all()
    return [_to_credential_out(c, db) for c in items]


@router.post("/credentials", response_model=CredentialOut)
def create_credential(data: CredentialCreate, db: Session = Depends(get_db)):
    """Create a new credential with encrypted secret."""
    cred = Credential(
        name=data.name.strip(),
        cred_type=data.cred_type,
        username=data.username.strip() if data.username else None,
        server_url=data.server_url.strip() if data.server_url else None,
        description=data.description.strip() if data.description else None
    )
    cred.set_secret(data.secret)
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return _to_credential_out(cred, db)


@router.get("/credentials/{cred_id}", response_model=CredentialOut)
def get_credential(cred_id: int, db: Session = Depends(get_db)):
    """Retrieve a single credential."""
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")
    return _to_credential_out(cred, db)


@router.put("/credentials/{cred_id}", response_model=CredentialOut)
def update_credential(cred_id: int, data: CredentialUpdate, db: Session = Depends(get_db)):
    """Update an existing credential."""
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")

    if data.name is not None:
        cred.name = data.name.strip()
    if data.cred_type is not None:
        cred.cred_type = data.cred_type
    if data.username is not None:
        cred.username = data.username.strip() if data.username else None
    if data.server_url is not None:
        cred.server_url = data.server_url.strip() if data.server_url else None
    if data.description is not None:
        cred.description = data.description.strip() if data.description else None
    if data.secret is not None and data.secret.strip():
        cred.set_secret(data.secret)

    cred.updated_at = utc_now()
    db.commit()
    db.refresh(cred)
    return _to_credential_out(cred, db)


@router.delete("/credentials/{cred_id}")
def delete_credential(cred_id: int, db: Session = Depends(get_db)):
    """Delete a credential and safely unlink it from any repositories."""
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")

    # Explicitly clear references in repositories
    db.query(Repository).filter(Repository.git_credential_id == cred.id).update(
        {"git_credential_id": None}
    )
    db.query(Repository).filter(Repository.drone_credential_id == cred.id).update(
        {"drone_credential_id": None}
    )

    db.delete(cred)
    db.commit()
    return {"ok": True, "message": f"Креденшел '{cred.name}' удалён"}


# --- Repositories & Actions API ---

@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_connection(data: TestConnectionRequest, db: Session = Depends(get_db)):
    """
    Test connectivity to both Git repository (via git ls-remote)
    and Drone CI API before saving.
    Supports resolving credentials from Credential registry.
    """
    git_username = data.git_username
    git_password = data.git_password

    if data.git_credential_id:
        cred = db.query(Credential).filter(Credential.id == data.git_credential_id).first()
        if cred:
            if cred.username:
                git_username = cred.username
            git_password = cred.get_plain_secret()

    drone_server = data.drone_server
    drone_token = data.drone_token or ""

    if data.drone_credential_id:
        dcred = db.query(Credential).filter(Credential.id == data.drone_credential_id).first()
        if dcred:
            if dcred.server_url:
                drone_server = dcred.server_url
            drone_token = dcred.get_plain_secret()

    git_ok = False
    git_msg = ""
    branches: List[str] = []

    # 1. Test Git
    try:
        remote_heads = await fetch_remote_heads(
            git_url=data.git_url,
            username=git_username,
            password=git_password,
            timeout=15
        )
        git_ok = True
        branches = sorted(list(remote_heads.keys()))
        git_msg = f"Успешно! Найдено веток: {len(branches)}"
    except GitError as ge:
        git_ok = False
        git_msg = f"Ошибка Git: {str(ge)}"
    except Exception as e:
        git_ok = False
        git_msg = f"Непредвиденная ошибка Git: {mask_git_url(str(e))}"

    # 2. Test Drone CI
    drone_ok, drone_msg = await test_drone_connection(
        drone_server=drone_server,
        drone_repo=data.drone_repo,
        drone_token=drone_token
    )

    return TestConnectionResponse(
        git_ok=git_ok,
        git_message=git_msg,
        git_branches=branches[:30],
        drone_ok=drone_ok,
        drone_message=drone_msg
    )


@router.post("/repos/{repo_id}/check-now")
async def check_repo_now(repo_id: int, db: Session = Depends(get_db)):
    """Run an immediate poll for the specified repository."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    result = await poll_single_repository(repo.id, db, force=True)
    return result


@router.post("/repos/{repo_id}/toggle-active")
def toggle_repo_active(repo_id: int, db: Session = Depends(get_db)):
    """Toggle repository active status on/off."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    repo.is_active = not repo.is_active
    db.commit()
    db.refresh(repo)

    add_or_update_repo_job(repo.id, repo.poll_interval_minutes, repo.is_active)
    return {"id": repo.id, "is_active": repo.is_active}


@router.post("/repos/{repo_id}/branches/{branch_name:path}/trigger-build")
async def manual_trigger_branch_build(
    repo_id: int,
    branch_name: str,
    db: Session = Depends(get_db)
):
    """Manually trigger Drone CI build for a branch."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    state = db.query(BranchState).filter(
        BranchState.repo_id == repo.id,
        BranchState.branch_name == branch_name
    ).first()

    sha = state.last_sha if state else "HEAD"

    effective_server = repo.get_effective_drone_server()
    plain_token = repo.get_plain_drone_token()

    try:
        build_num, msg = await trigger_drone_build(
            effective_server,
            repo.drone_repo,
            plain_token,
            branch_name
        )

        now = utc_now()
        if state:
            state.last_build_number = int(build_num) if str(build_num).isdigit() else None
            state.last_build_status = "manual_triggered"
            state.updated_at = now

        db.add(PollLog(
            repo_id=repo.id,
            repo_name=repo.name,
            branch_name=branch_name,
            commit_sha=sha,
            build_number=int(build_num) if str(build_num).isdigit() else None,
            level="success",
            message=f"Ручной запуск сборки #{build_num} для ветки {branch_name}",
            created_at=now
        ))
        db.commit()

        from app.metrics import BUILDS_TRIGGERED_TOTAL
        BUILDS_TRIGGERED_TOTAL.labels(repo=repo.name, branch=branch_name).inc()

        try:
            from app.notifier import dispatch_event_notification
            import asyncio
            asyncio.create_task(dispatch_event_notification(
                event_type="build",
                title=f"Ручной запуск #{build_num}",
                message=f"Инициирован ручной запуск сборки #{build_num} для ветки {branch_name}.",
                repo_name=repo.name,
                branch=branch_name,
                commit_sha=sha,
                build_number=int(build_num) if str(build_num).isdigit() else None
            ))
        except Exception:
            pass

        return {"ok": True, "build_number": build_num, "message": msg}

    except DroneError as de:
        error_msg = f"Ошибка ручного запуска Drone CI: {str(de)}"
        db.add(PollLog(
            repo_id=repo.id,
            repo_name=repo.name,
            branch_name=branch_name,
            commit_sha=sha,
            level="error",
            message=error_msg,
            created_at=utc_now()
        ))
        db.commit()
        raise HTTPException(status_code=400, detail=error_msg)


@router.delete("/repos/{repo_id}")
def delete_repo(repo_id: int, db: Session = Depends(get_db)):
    """Delete repository, its branch states, logs and scheduler job."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    remove_repo_job(repo.id)
    remove_repo_lock(repo.id)
    db.delete(repo)
    db.commit()

    return {"ok": True, "message": f"Репозиторий {repo_id} удалён"}


@router.get("/logs")
def get_logs(
    repo_id: Optional[int] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db)
):
    """Retrieve event logs."""
    q = db.query(PollLog)
    if repo_id:
        q = q.filter(PollLog.repo_id == repo_id)
    if level:
        q = q.filter(PollLog.level == level)
    
    logs = q.order_by(PollLog.created_at.desc()).limit(limit).all()
    return logs


@router.get("/logs/stream", summary="Live Event Stream (SSE)")
async def stream_logs(
    request: Request,
    repo_id: Optional[int] = Query(None),
    level: Optional[str] = Query(None)
):
    """
    Server-Sent Events (SSE) endpoint streaming PollLog events in real-time.
    Supports filtering by repository ID and log level.
    """
    from app.broadcaster import log_broadcaster

    queue = log_broadcaster.subscribe()

    async def event_generator():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break

                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue

                event_type = msg.get("event", "log")
                data = msg.get("data", {})

                if event_type == "log":
                    if repo_id is not None and data.get("repo_id") != repo_id:
                        continue
                    if level and data.get("level") != level:
                        continue
                elif event_type == "clear":
                    cleared_repo = data.get("repo_id")
                    if repo_id is not None and cleared_repo is not None and cleared_repo != repo_id:
                        continue

                payload_str = json.dumps(data, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {payload_str}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            log_broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.delete("/logs")
def clear_logs(repo_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """Clear event logs (optionally filtered by repo_id)."""
    q = db.query(PollLog)
    if repo_id:
        q = q.filter(PollLog.repo_id == repo_id)
    deleted_count = q.delete(synchronize_session=False)
    db.commit()

    try:
        from app.broadcaster import log_broadcaster
        log_broadcaster.publish("clear", {"repo_id": repo_id})
    except Exception:
        pass

    return {
        "ok": True,
        "deleted_count": deleted_count,
        "message": f"Очищено {deleted_count} записей журнала"
    }


# --- Notifications API ---

@router.post("/notifications/test")
async def test_notification_channel(data: TestNotificationRequest, db: Session = Depends(get_db)):
    """Send a test message to the specified notification channel."""
    from app.notifier import send_telegram, send_vk, send_email
    from app.models import get_notification_settings

    cfg = get_notification_settings(db)
    channel = data.channel.lower()

    if channel == "telegram":
        token = data.telegram_bot_token.strip() if data.telegram_bot_token else cfg.get_plain_telegram_bot_token()
        chat_id = data.telegram_chat_id.strip() if data.telegram_chat_id else (cfg.telegram_chat_id or "")
        if not token or not chat_id:
            return {"ok": False, "message": "Укажите Telegram Bot Token и Chat ID"}
        ok, msg = await send_telegram(
            bot_token=token,
            chat_id=chat_id,
            message="🔔 <b>Drone CI Build Sync</b>\n\nТестовое уведомление успешно доставлено!"
        )
        return {"ok": ok, "message": msg}

    elif channel == "vk":
        token = data.vk_group_token.strip() if data.vk_group_token else cfg.get_plain_vk_group_token()
        peer_id = data.vk_peer_id.strip() if data.vk_peer_id else (cfg.vk_peer_id or "")
        if not token or not peer_id:
            return {"ok": False, "message": "Укажите VK Group Token и Peer ID"}
        ok, msg = await send_vk(
            group_token=token,
            peer_id=peer_id,
            message="🔔 Drone CI Build Sync\n\nТестовое уведомление ВК успешно доставлено!"
        )
        return {"ok": ok, "message": msg}

    elif channel == "email":
        host = data.smtp_host or cfg.smtp_host or ""
        port = data.smtp_port or cfg.smtp_port or 587
        username = data.smtp_username or cfg.smtp_username or ""
        password = data.smtp_password if (data.smtp_password is not None and data.smtp_password != "") else cfg.get_plain_smtp_password()
        use_tls = data.smtp_use_tls if data.smtp_use_tls is not None else cfg.smtp_use_tls
        from_addr = data.smtp_from or cfg.smtp_from or ""
        raw_to = data.smtp_to or cfg.smtp_to or ""
        to_addrs = [e.strip() for e in raw_to.replace("\n", ",").split(",") if e.strip()]

        if not host or not to_addrs:
            return {"ok": False, "message": "Укажите SMTP Host и хотя бы один Email получателя"}

        ok, msg = await send_email(
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject="[Drone Sync] Тестовое уведомление",
            body="Это тестовое сообщение от сервиса Drone CI Build Sync. Настройки SMTP работают корректно!"
        )
        return {"ok": ok, "message": msg}

    else:
        raise HTTPException(status_code=400, detail=f"Неизвестный канал: {channel}")


@router.post("/notifications/save")
def save_notification_settings(data: SaveNotificationSettingsRequest, db: Session = Depends(get_db)):
    """Save notification settings for a specific channel/section."""
    from app.models import get_notification_settings
    cfg = get_notification_settings(db)
    channel = data.channel.lower()

    if channel in ("events", "all"):
        if data.notify_on_build is not None:
            cfg.notify_on_build = data.notify_on_build
        if data.notify_on_error is not None:
            cfg.notify_on_error = data.notify_on_error

    if channel in ("telegram", "all"):
        if data.telegram_enabled is not None:
            cfg.telegram_enabled = data.telegram_enabled
        if data.telegram_chat_id is not None:
            cfg.telegram_chat_id = data.telegram_chat_id.strip() if data.telegram_chat_id.strip() else None
        if data.telegram_bot_token is not None and data.telegram_bot_token.strip():
            cfg.set_secrets(telegram_token=data.telegram_bot_token.strip())

    if channel in ("vk", "all"):
        if data.vk_enabled is not None:
            cfg.vk_enabled = data.vk_enabled
        if data.vk_peer_id is not None:
            cfg.vk_peer_id = data.vk_peer_id.strip() if data.vk_peer_id.strip() else None
        if data.vk_group_token is not None and data.vk_group_token.strip():
            cfg.set_secrets(vk_token=data.vk_group_token.strip())

    if channel in ("email", "all"):
        if data.email_enabled is not None:
            cfg.email_enabled = data.email_enabled
        if data.smtp_host is not None:
            cfg.smtp_host = data.smtp_host.strip() if data.smtp_host.strip() else None
        if data.smtp_port is not None:
            cfg.smtp_port = data.smtp_port or 587
        if data.smtp_username is not None:
            cfg.smtp_username = data.smtp_username.strip() if data.smtp_username.strip() else None
        if data.smtp_use_tls is not None:
            cfg.smtp_use_tls = data.smtp_use_tls
        if data.smtp_from is not None:
            cfg.smtp_from = data.smtp_from.strip() if data.smtp_from.strip() else None
        if data.smtp_to is not None:
            cfg.smtp_to = data.smtp_to.strip() if data.smtp_to.strip() else None
        if data.smtp_password is not None and data.smtp_password.strip():
            cfg.set_secrets(smtp_password=data.smtp_password.strip())

    cfg.updated_at = utc_now()
    db.commit()

    labels = {
        "events": "События уведомлений успешно сохранены",
        "telegram": "Настройки Telegram успешно сохранены",
        "vk": "Настройки ВКонтакте успешно сохранены",
        "email": "Настройки Email успешно сохранены",
        "all": "Настройки успешно сохранены",
    }
    return {"ok": True, "message": labels.get(channel, "Настройки успешно сохранены")}


# --- Worker Schedule Endpoints ---

@router.get("/schedule")
def get_global_schedule_api(db: Session = Depends(get_db)):
    """Retrieve global worker schedule configuration and current execution status."""
    cfg = get_worker_schedule_settings(db)
    sched = cfg.get_schedule()
    is_active_now, msg, info = check_schedule_window(sched, cfg.timezone)
    
    return {
        "enabled": cfg.enabled,
        "timezone": cfg.timezone,
        "is_active_now": is_active_now if cfg.enabled else True,
        "status_message": msg if cfg.enabled else "Расписание отключено (воркер работает 24/7)",
        "current_time_str": info["current_time"],
        "days": sched,
    }


@router.post("/schedule")
def update_global_schedule_api(data: WorkerScheduleUpdate, db: Session = Depends(get_db)):
    """Update global worker schedule configuration."""
    cfg = get_worker_schedule_settings(db)
    cfg.enabled = data.enabled
    cfg.timezone = data.timezone or "Europe/Moscow"
    normalized = normalize_schedule_dict({k: v.model_dump() for k, v in data.days.items()})
    cfg.set_schedule(normalized)
    cfg.updated_at = utc_now()
    db.commit()
    db.refresh(cfg)

    is_active_now, msg, info = check_schedule_window(normalized, cfg.timezone)
    return {
        "ok": True,
        "message": "Расписание воркера успешно сохранено",
        "enabled": cfg.enabled,
        "timezone": cfg.timezone,
        "is_active_now": is_active_now if cfg.enabled else True,
        "status_message": msg if cfg.enabled else "Расписание отключено (воркер работает 24/7)",
        "current_time_str": info["current_time"],
        "days": normalized,
    }


@router.get("/schedule/timezones")
def get_schedule_timezones():
    """List available timezone options."""
    return [{"value": tz[0], "label": tz[1]} for tz in TIMEZONE_CHOICES]
