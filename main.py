import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import uvicorn

from app.config import settings
from app.database import Base, engine, SessionLocal, get_db
from app.models import Repository, BranchState
from app.scheduler import init_scheduler, shutdown_scheduler
from app.routes import web_router, api_router
from app.metrics import update_db_gauges, generate_latest, CONTENT_TYPE_LATEST

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("drone_sync")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.poller import close_http_client

    import asyncio
    from app.broadcaster import log_broadcaster, register_broadcaster_listeners
    log_broadcaster.set_loop(asyncio.get_running_loop())
    register_broadcaster_listeners()

    # Startup: create tables, start background scheduler
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    from app.database import auto_migrate_sqlite
    auto_migrate_sqlite(engine)

    logger.info("Starting background scheduler...")
    init_scheduler()

    yield

    # Shutdown
    logger.info("Shutting down background scheduler...")
    shutdown_scheduler()
    await close_http_client()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None
)

# Mount static assets
static_dir = Path(__file__).resolve().parent / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include routes
app.include_router(api_router)
app.include_router(web_router)


@app.get("/metrics", tags=["monitoring"], summary="Prometheus Metrics")
def metrics_endpoint(db: Session = Depends(get_db)):
    """Expose Prometheus metrics for scraping."""
    update_db_gauges(db)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False
    )
