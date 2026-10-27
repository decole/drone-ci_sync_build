import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TEST_DB_FILE = Path(__file__).resolve().parent / "test_drone_sync.db"
TEST_DB_URL = f"sqlite:///{TEST_DB_FILE}"

from app.database import Base, get_db, SessionLocal
from main import app

test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False}
)

from sqlalchemy import event

@event.listens_for(test_engine, "connect")
def set_test_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    # Setup test DB tables once per session
    SessionLocal.configure(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    for ext in ["", "-wal", "-shm"]:
        p = Path(f"{TEST_DB_FILE}{ext}")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def override_db_dependency():
    def _override():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()
