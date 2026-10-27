"""Prometheus metrics exporter for Drone CI Build Sync."""
import logging
from typing import Optional
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
    REGISTRY
)
from app.config import settings

logger = logging.getLogger("drone_sync.metrics")

# --- Prometheus Metrics Definitions ---

POLL_TOTAL = Counter(
    "drone_sync_poll_total",
    "Total repository poll cycles executed",
    ["repo", "status"]
)

POLL_DURATION_SECONDS = Histogram(
    "drone_sync_poll_duration_seconds",
    "Duration of repository git polling operations in seconds",
    ["repo"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0]
)

POLL_ERRORS_TOTAL = Counter(
    "drone_sync_poll_errors_total",
    "Total poll errors categorized by error type",
    ["repo", "error_type"]
)

BUILDS_TRIGGERED_TOTAL = Counter(
    "drone_sync_builds_triggered_total",
    "Total Drone CI builds triggered by commit detection or manual action",
    ["repo", "branch"]
)

NOTIFICATIONS_SENT_TOTAL = Counter(
    "drone_sync_notifications_sent_total",
    "Total notifications dispatched across all notification channels",
    ["channel", "status"]
)

REPOSITORIES_TOTAL = Gauge(
    "drone_sync_repositories_total",
    "Number of configured repositories by status",
    ["status"]
)

BRANCHES_TRACKED_TOTAL = Gauge(
    "drone_sync_branches_tracked_total",
    "Total number of active (non-stale) tracked branches in the database"
)

SERVICE_INFO = Gauge(
    "drone_sync_service_info",
    "Application metadata information",
    ["version", "app_name"]
)
SERVICE_INFO.labels(version="1.0.0", app_name=settings.app_name).set(1)


def update_db_gauges(db) -> None:
    """Refresh database-backed gauge metrics right before scrape."""
    from app.models import Repository, BranchState
    try:
        active_count = db.query(Repository).filter(Repository.is_active == True).count()
        paused_count = db.query(Repository).filter(Repository.is_active == False).count()
        branches_count = db.query(BranchState).filter(BranchState.is_stale == False).count()

        REPOSITORIES_TOTAL.labels(status="active").set(active_count)
        REPOSITORIES_TOTAL.labels(status="paused").set(paused_count)
        BRANCHES_TRACKED_TOTAL.set(branches_count)
    except Exception as e:
        logger.warning("Could not refresh DB gauges for Prometheus: %s", e)
