import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.models import Repository
from app.poller import poll_single_repository

logger = logging.getLogger("drone_sync.scheduler")

scheduler = AsyncIOScheduler()


def job_id_for_repo(repo_id: int) -> str:
    return f"poll_repo_{repo_id}"


async def _run_poll_job(repo_id: int):
    """Worker task executed by APScheduler."""
    logger.info("Executing scheduled poll for repo_id=%d", repo_id)
    db = SessionLocal()
    try:
        await poll_single_repository(repo_id, db)
    except Exception as e:
        logger.exception("Unexpected exception in poll job for repo_id=%d: %s", repo_id, e)
    finally:
        db.close()


def add_or_update_repo_job(repo_id: int, interval_minutes: int, is_active: bool = True):
    """Add or reconfigure polling job for a repository."""
    jid = job_id_for_repo(repo_id)
    
    # Remove existing job if present
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)

    if not is_active:
        logger.info("Repo %d is inactive, no scheduler job registered.", repo_id)
        return

    interval = max(1, interval_minutes)
    scheduler.add_job(
        _run_poll_job,
        trigger=IntervalTrigger(minutes=interval),
        id=jid,
        name=f"Poll Repo {repo_id}",
        args=[repo_id],
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )
    logger.info("Registered poll job for repo %d every %d minute(s).", repo_id, interval)


def remove_repo_job(repo_id: int):
    """Remove polling job when a repository is deleted."""
    jid = job_id_for_repo(repo_id)
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
        logger.info("Removed poll job for repo %d.", repo_id)


async def _run_cleanup_job():
    """Periodic log retention task."""
    logger.info("Executing periodic log retention cleanup...")
    db = SessionLocal()
    try:
        from app.poller import cleanup_old_logs
        cleanup_old_logs(db, max_age_days=14)
    except Exception as e:
        logger.exception("Error during log cleanup: %s", e)
    finally:
        db.close()


def init_scheduler():
    """Start scheduler and load all active repositories from DB."""
    if not scheduler.running:
        scheduler.start()
        logger.info("AsyncIOScheduler started.")

    # Register log cleanup job (runs every 12 hours)
    scheduler.add_job(
        _run_cleanup_job,
        trigger=IntervalTrigger(hours=12),
        id="cleanup_old_logs",
        name="Log Retention Cleanup",
        replace_existing=True,
        coalesce=True
    )

    db = SessionLocal()
    try:
        repos = db.query(Repository).all()
        for repo in repos:
            add_or_update_repo_job(repo.id, repo.poll_interval_minutes, repo.is_active)
        logger.info("Loaded %d repositories into scheduler.", len(repos))
    except Exception as e:
        logger.exception("Failed to initialize scheduler jobs: %s", e)
    finally:
        db.close()


def shutdown_scheduler():
    """Graceful shutdown of scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("AsyncIOScheduler stopped.")
