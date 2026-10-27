from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
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
    get_default_schedule
)
from app.scheduler import add_or_update_repo_job, remove_repo_job

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False)


def extract_days_from_form(form_data: Dict[str, Any], prefix: str = "day_") -> Dict[str, Any]:
    """Helper to parse week schedule day entries from form data."""
    days = {}
    for day in DAY_KEYS:
        enabled = form_data.get(f"{prefix}{day}_enabled") in ("on", "true", "1", True)
        start = str(form_data.get(f"{prefix}{day}_start") or "09:00").strip()
        end = str(form_data.get(f"{prefix}{day}_end") or "18:00").strip()
        days[day] = {
            "enabled": enabled,
            "start": start,
            "end": end,
        }
    return normalize_schedule_dict(days)


@router.get("/", response_class=HTMLResponse)
def index_view(request: Request, db: Session = Depends(get_db)):
    repos = db.query(Repository).order_by(Repository.name.asc()).all()
    recent_logs = db.query(PollLog).order_by(PollLog.created_at.desc()).limit(8).all()
    
    # Calculate schedule status for each repo
    repo_schedule_statuses = {}
    for r in repos:
        in_win, reason = is_repo_in_schedule_window(r, db)
        repo_schedule_statuses[r.id] = {
            "in_window": in_win,
            "reason": reason,
            "mode": getattr(r, "schedule_mode", "global") or "global"
        }

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "repos": repos,
            "recent_logs": recent_logs,
            "repo_schedules": repo_schedule_statuses,
            "title": "Главная — Управление репозиториями"
        }
    )


# --- Repositories Views ---

@router.get("/repos/new", response_class=HTMLResponse)
def new_repo_view(request: Request, db: Session = Depends(get_db)):
    git_credentials = db.query(Credential).filter(Credential.cred_type == "git").order_by(Credential.name.asc()).all()
    drone_credentials = db.query(Credential).filter(Credential.cred_type == "drone").order_by(Credential.name.asc()).all()
    global_schedule_cfg = get_worker_schedule_settings(db)

    return templates.TemplateResponse(
        request=request,
        name="repo_form.html",
        context={
            "repo": None,
            "git_credentials": git_credentials,
            "drone_credentials": drone_credentials,
            "default_drone_server": settings.default_drone_server,
            "default_drone_token": settings.default_drone_token,
            "global_schedule_cfg": global_schedule_cfg,
            "timezones": TIMEZONE_CHOICES,
            "day_keys": DAY_KEYS,
            "day_names": DAY_NAMES_RU,
            "day_shorts": DAY_SHORT_RU,
            "title": "Добавить репозиторий"
        }
    )


@router.post("/repos/new")
async def create_repo(
    request: Request,
    name: str = Form(...),
    git_url: str = Form(...),
    git_credential_id: Optional[str] = Form(None),
    git_username: Optional[str] = Form(None),
    git_password: Optional[str] = Form(None),
    track_all_branches: Optional[str] = Form(None),
    tracked_branches: str = Form("main"),
    drone_server: str = Form(...),
    drone_repo: str = Form(...),
    drone_credential_id: Optional[str] = Form(None),
    drone_token: Optional[str] = Form(None),
    poll_interval_minutes: int = Form(2),
    is_active: Optional[str] = Form(None),
    schedule_mode: str = Form("global"),
    custom_schedule_timezone: str = Form("Europe/Moscow"),
    db: Session = Depends(get_db)
):
    clean_git_cred = int(git_credential_id) if git_credential_id and git_credential_id.strip().isdigit() and int(git_credential_id) > 0 else None
    clean_drone_cred = int(drone_credential_id) if drone_credential_id and drone_credential_id.strip().isdigit() and int(drone_credential_id) > 0 else None

    form_data = await request.form()
    custom_sched = extract_days_from_form(dict(form_data), prefix="repo_day_")

    repo = Repository(
        name=name.strip(),
        git_url=git_url.strip(),
        git_credential_id=clean_git_cred,
        git_username=git_username.strip() if git_username else None,
        track_all_branches=(track_all_branches == "on" or track_all_branches == "true"),
        tracked_branches=tracked_branches.strip(),
        drone_server=drone_server.strip(),
        drone_repo=drone_repo.strip(),
        drone_credential_id=clean_drone_cred,
        drone_token="",
        poll_interval_minutes=max(1, poll_interval_minutes),
        is_active=(is_active == "on" or is_active == "true"),
        schedule_mode=schedule_mode if schedule_mode in ("global", "always", "custom") else "global",
        custom_schedule_timezone=custom_schedule_timezone or "Europe/Moscow",
        last_status="idle"
    )
    repo.set_credentials(git_password=git_password, drone_token=drone_token)
    repo.set_custom_schedule(custom_sched)
    db.add(repo)
    db.commit()
    db.refresh(repo)

    # Schedule background job
    add_or_update_repo_job(repo.id, repo.poll_interval_minutes, repo.is_active)

    return RedirectResponse(url=f"/repos/{repo.id}", status_code=303)


@router.get("/repos/{repo_id}/edit", response_class=HTMLResponse)
def edit_repo_view(repo_id: int, request: Request, db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    git_credentials = db.query(Credential).filter(Credential.cred_type == "git").order_by(Credential.name.asc()).all()
    drone_credentials = db.query(Credential).filter(Credential.cred_type == "drone").order_by(Credential.name.asc()).all()
    global_schedule_cfg = get_worker_schedule_settings(db)

    return templates.TemplateResponse(
        request=request,
        name="repo_form.html",
        context={
            "repo": repo,
            "git_credentials": git_credentials,
            "drone_credentials": drone_credentials,
            "default_drone_server": settings.default_drone_server,
            "default_drone_token": settings.default_drone_token,
            "global_schedule_cfg": global_schedule_cfg,
            "timezones": TIMEZONE_CHOICES,
            "day_keys": DAY_KEYS,
            "day_names": DAY_NAMES_RU,
            "day_shorts": DAY_SHORT_RU,
            "title": f"Редактирование: {repo.name}"
        }
    )


@router.post("/repos/{repo_id}/edit")
async def update_repo(
    repo_id: int,
    request: Request,
    name: str = Form(...),
    git_url: str = Form(...),
    git_credential_id: Optional[str] = Form(None),
    git_username: Optional[str] = Form(None),
    git_password: Optional[str] = Form(None),
    track_all_branches: Optional[str] = Form(None),
    tracked_branches: str = Form("main"),
    drone_server: str = Form(...),
    drone_repo: str = Form(...),
    drone_credential_id: Optional[str] = Form(None),
    drone_token: Optional[str] = Form(None),
    poll_interval_minutes: int = Form(2),
    is_active: Optional[str] = Form(None),
    schedule_mode: str = Form("global"),
    custom_schedule_timezone: str = Form("Europe/Moscow"),
    db: Session = Depends(get_db)
):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    clean_git_cred = int(git_credential_id) if git_credential_id and git_credential_id.strip().isdigit() and int(git_credential_id) > 0 else None
    clean_drone_cred = int(drone_credential_id) if drone_credential_id and drone_credential_id.strip().isdigit() and int(drone_credential_id) > 0 else None

    form_data = await request.form()
    custom_sched = extract_days_from_form(dict(form_data), prefix="repo_day_")

    repo.name = name.strip()
    repo.git_url = git_url.strip()
    repo.git_credential_id = clean_git_cred
    repo.git_username = git_username.strip() if git_username else None
    
    # Update password if entered, otherwise preserve previous
    plain_pw = git_password.strip() if git_password and git_password.strip() else None
    plain_tk = drone_token.strip() if drone_token and drone_token.strip() else None
    repo.set_credentials(git_password=plain_pw, drone_token=plain_tk)

    repo.track_all_branches = (track_all_branches == "on" or track_all_branches == "true")
    repo.tracked_branches = tracked_branches.strip()
    repo.drone_server = drone_server.strip()
    repo.drone_repo = drone_repo.strip()
    repo.drone_credential_id = clean_drone_cred

    repo.poll_interval_minutes = max(1, poll_interval_minutes)
    repo.is_active = (is_active == "on" or is_active == "true")
    repo.schedule_mode = schedule_mode if schedule_mode in ("global", "always", "custom") else "global"
    repo.custom_schedule_timezone = custom_schedule_timezone or "Europe/Moscow"
    repo.set_custom_schedule(custom_sched)
    repo.updated_at = utc_now()

    db.commit()
    db.refresh(repo)

    # Reschedule in APScheduler
    add_or_update_repo_job(repo.id, repo.poll_interval_minutes, repo.is_active)

    return RedirectResponse(url=f"/repos/{repo.id}", status_code=303)


@router.get("/repos/{repo_id}", response_class=HTMLResponse)
def repo_detail_view(repo_id: int, request: Request, db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    in_window, schedule_reason = is_repo_in_schedule_window(repo, db)
    branches = db.query(BranchState).filter(BranchState.repo_id == repo.id).order_by(BranchState.branch_name.asc()).all()
    logs = db.query(PollLog).filter(PollLog.repo_id == repo.id).order_by(PollLog.created_at.desc()).limit(50).all()

    return templates.TemplateResponse(
        request=request,
        name="repo_detail.html",
        context={
            "repo": repo,
            "in_window": in_window,
            "schedule_reason": schedule_reason,
            "branches": branches,
            "logs": logs,
            "title": f"Репозиторий: {repo.name}"
        }
    )


# --- Credentials Registry Views ---

@router.get("/credentials", response_class=HTMLResponse)
def credentials_list_view(request: Request, db: Session = Depends(get_db)):
    credentials = db.query(Credential).order_by(Credential.name.asc()).all()
    
    # Calculate usage count for each credential
    items = []
    for c in credentials:
        usage = db.query(Repository).filter(
            (Repository.git_credential_id == c.id) | (Repository.drone_credential_id == c.id)
        ).count()
        items.append({
            "cred": c,
            "usage_count": usage
        })

    return templates.TemplateResponse(
        request=request,
        name="credentials.html",
        context={
            "items": items,
            "title": "Реестр креденшелов"
        }
    )


@router.get("/credentials/new", response_class=HTMLResponse)
def new_credential_view(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="credential_form.html",
        context={
            "cred": None,
            "default_drone_server": settings.default_drone_server,
            "title": "Новый креденшел"
        }
    )


@router.post("/credentials/new")
def create_credential_form(
    name: str = Form(...),
    cred_type: str = Form(...),
    secret: str = Form(...),
    username: Optional[str] = Form(None),
    server_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    cred = Credential(
        name=name.strip(),
        cred_type=cred_type.strip(),
        username=username.strip() if username else None,
        server_url=server_url.strip() if server_url else None,
        description=description.strip() if description else None
    )
    cred.set_secret(secret)
    db.add(cred)
    db.commit()
    return RedirectResponse(url="/credentials", status_code=303)


@router.get("/credentials/{cred_id}/edit", response_class=HTMLResponse)
def edit_credential_view(cred_id: int, request: Request, db: Session = Depends(get_db)):
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")

    return templates.TemplateResponse(
        request=request,
        name="credential_form.html",
        context={
            "cred": cred,
            "default_drone_server": settings.default_drone_server,
            "title": f"Редактировать креденшел: {cred.name}"
        }
    )


@router.post("/credentials/{cred_id}/edit")
def update_credential_form(
    cred_id: int,
    name: str = Form(...),
    cred_type: str = Form(...),
    secret: Optional[str] = Form(None),
    username: Optional[str] = Form(None),
    server_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")

    cred.name = name.strip()
    cred.cred_type = cred_type.strip()
    cred.username = username.strip() if username else None
    cred.server_url = server_url.strip() if server_url else None
    cred.description = description.strip() if description else None
    
    if secret and secret.strip():
        cred.set_secret(secret)

    cred.updated_at = utc_now()
    db.commit()
    return RedirectResponse(url="/credentials", status_code=303)


@router.post("/credentials/{cred_id}/delete")
def delete_credential_form(cred_id: int, db: Session = Depends(get_db)):
    cred = db.query(Credential).filter(Credential.id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Креденшел не найден")

    # Clear references in repositories
    db.query(Repository).filter(Repository.git_credential_id == cred.id).update(
        {"git_credential_id": None}
    )
    db.query(Repository).filter(Repository.drone_credential_id == cred.id).update(
        {"drone_credential_id": None}
    )

    db.delete(cred)
    db.commit()
    return RedirectResponse(url="/credentials", status_code=303)


# --- Logs Views ---

@router.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    repo_id: Optional[int] = None,
    level: Optional[str] = None,
    db: Session = Depends(get_db)
):
    repos = db.query(Repository).order_by(Repository.name.asc()).all()
    q = db.query(PollLog)
    if repo_id:
        q = q.filter(PollLog.repo_id == repo_id)
    if level:
        q = q.filter(PollLog.level == level)

    logs = q.order_by(PollLog.created_at.desc()).limit(150).all()

    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "logs": logs,
            "repos": repos,
            "selected_repo_id": repo_id,
            "selected_level": level,
            "title": "Журнал событий"
        }
    )


@router.post("/logs/clear")
def clear_logs_view(
    repo_id: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    q = db.query(PollLog)
    clean_repo_id = int(repo_id) if repo_id and repo_id.strip().isdigit() else None
    if clean_repo_id:
        q = q.filter(PollLog.repo_id == clean_repo_id)
    q.delete(synchronize_session=False)
    db.commit()
    try:
        from app.broadcaster import log_broadcaster
        log_broadcaster.publish("clear", {"repo_id": clean_repo_id})
    except Exception:
        pass
    redirect_url = f"/logs?repo_id={clean_repo_id}" if clean_repo_id else "/logs"
    return RedirectResponse(url=redirect_url, status_code=303)


# --- Notification Settings Views ---

@router.get("/notifications", response_class=HTMLResponse)
def notifications_view(request: Request, db: Session = Depends(get_db)):
    from app.models import get_notification_settings
    cfg = get_notification_settings(db)
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={
            "cfg": cfg,
            "title": "Настройки нотификаций"
        }
    )


@router.post("/notifications")
def update_notifications_view(
    channel: Optional[str] = Form(None),
    notify_on_build: Optional[str] = Form(None),
    notify_on_error: Optional[str] = Form(None),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: Optional[str] = Form(None),
    telegram_chat_id: Optional[str] = Form(None),
    vk_enabled: Optional[str] = Form(None),
    vk_group_token: Optional[str] = Form(None),
    vk_peer_id: Optional[str] = Form(None),
    email_enabled: Optional[str] = Form(None),
    smtp_host: Optional[str] = Form(None),
    smtp_port: Optional[int] = Form(587),
    smtp_username: Optional[str] = Form(None),
    smtp_password: Optional[str] = Form(None),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_from: Optional[str] = Form(None),
    smtp_to: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    from app.models import get_notification_settings
    cfg = get_notification_settings(db)
    ch = channel.lower() if channel else None

    if ch in ("events", None):
        cfg.notify_on_build = (notify_on_build == "on" or notify_on_build == "true")
        cfg.notify_on_error = (notify_on_error == "on" or notify_on_error == "true")

    if ch in ("telegram", None):
        cfg.telegram_enabled = (telegram_enabled == "on" or telegram_enabled == "true")
        if telegram_chat_id is not None:
            cfg.telegram_chat_id = telegram_chat_id.strip() if telegram_chat_id.strip() else None
        if telegram_bot_token and telegram_bot_token.strip():
            cfg.set_secrets(telegram_token=telegram_bot_token.strip())

    if ch in ("vk", None):
        cfg.vk_enabled = (vk_enabled == "on" or vk_enabled == "true")
        if vk_peer_id is not None:
            cfg.vk_peer_id = vk_peer_id.strip() if vk_peer_id.strip() else None
        if vk_group_token and vk_group_token.strip():
            cfg.set_secrets(vk_token=vk_group_token.strip())

    if ch in ("email", None):
        cfg.email_enabled = (email_enabled == "on" or email_enabled == "true")
        if smtp_host is not None:
            cfg.smtp_host = smtp_host.strip() if smtp_host.strip() else None
        if smtp_port is not None:
            cfg.smtp_port = smtp_port or 587
        if smtp_username is not None:
            cfg.smtp_username = smtp_username.strip() if smtp_username.strip() else None
        cfg.smtp_use_tls = (smtp_use_tls == "on" or smtp_use_tls == "true")
        if smtp_from is not None:
            cfg.smtp_from = smtp_from.strip() if smtp_from.strip() else None
        if smtp_to is not None:
            cfg.smtp_to = smtp_to.strip() if smtp_to.strip() else None
        if smtp_password and smtp_password.strip():
            cfg.set_secrets(smtp_password=smtp_password.strip())

    cfg.updated_at = utc_now()
    db.commit()
    redirect_url = f"/notifications?saved={ch}" if ch else "/notifications?saved=1"
    return RedirectResponse(url=redirect_url, status_code=303)


# --- Worker Schedule Views ---

@router.get("/schedule", response_class=HTMLResponse)
def schedule_view(request: Request, saved: Optional[str] = None, db: Session = Depends(get_db)):
    cfg = get_worker_schedule_settings(db)
    sched = cfg.get_schedule()
    is_active_now, msg, info = check_schedule_window(sched, cfg.timezone)

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "cfg": cfg,
            "schedule": sched,
            "is_active_now": is_active_now if cfg.enabled else True,
            "status_message": msg,
            "current_time_str": info["current_time"],
            "timezones": TIMEZONE_CHOICES,
            "day_keys": DAY_KEYS,
            "day_names": DAY_NAMES_RU,
            "day_shorts": DAY_SHORT_RU,
            "saved": bool(saved),
            "title": "Расписание работы воркера"
        }
    )


@router.post("/schedule")
async def update_schedule_view(request: Request, db: Session = Depends(get_db)):
    form_data = await request.form()
    cfg = get_worker_schedule_settings(db)

    enabled_val = form_data.get("enabled")
    cfg.enabled = (enabled_val in ("on", "true", "1", True))

    tz_val = form_data.get("timezone", "Europe/Moscow")
    cfg.timezone = str(tz_val).strip() if tz_val else "Europe/Moscow"

    days_dict = extract_days_from_form(dict(form_data), prefix="day_")
    cfg.set_schedule(days_dict)
    cfg.updated_at = utc_now()

    db.commit()
    db.refresh(cfg)
    return RedirectResponse(url="/schedule?saved=1", status_code=303)


# --- Help & Guide View ---

@router.get("/guide", response_class=HTMLResponse)
@router.get("/help", response_class=HTMLResponse)
def guide_view(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="guide.html",
        context={"title": "Инструкция и руководство"}
    )
