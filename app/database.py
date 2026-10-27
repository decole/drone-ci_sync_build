from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

# SQLite specific connect_args to allow multithreading
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False
)

# Enable WAL (Write-Ahead Logging) and busy_timeout for concurrent SQLite access
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def auto_migrate_sqlite(bind_engine):
    """Safely add missing columns to existing SQLite tables without data loss."""
    from sqlalchemy import inspect, text
    try:
        inspector = inspect(bind_engine)
        existing_tables = inspector.get_table_names()

        if "repositories" in existing_tables:
            existing_cols = {col["name"] for col in inspector.get_columns("repositories")}
            with bind_engine.connect() as conn:
                if "git_credential_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE repositories ADD COLUMN git_credential_id INTEGER REFERENCES credentials(id) ON DELETE SET NULL"))
                if "drone_credential_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE repositories ADD COLUMN drone_credential_id INTEGER REFERENCES credentials(id) ON DELETE SET NULL"))
                if "schedule_mode" not in existing_cols:
                    conn.execute(text("ALTER TABLE repositories ADD COLUMN schedule_mode VARCHAR(20) DEFAULT 'global' NOT NULL"))
                if "custom_schedule_timezone" not in existing_cols:
                    conn.execute(text("ALTER TABLE repositories ADD COLUMN custom_schedule_timezone VARCHAR(50) DEFAULT 'Europe/Moscow' NOT NULL"))
                if "custom_schedule_json" not in existing_cols:
                    conn.execute(text("ALTER TABLE repositories ADD COLUMN custom_schedule_json TEXT"))
                conn.commit()

        if "branch_states" in existing_tables:
            existing_cols = {col["name"] for col in inspector.get_columns("branch_states")}
            with bind_engine.connect() as conn:
                if "is_stale" not in existing_cols:
                    conn.execute(text("ALTER TABLE branch_states ADD COLUMN is_stale BOOLEAN DEFAULT 0 NOT NULL"))
                conn.commit()
    except Exception as e:
        # Avoid crashing if inspector or alter table encounters an issue
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
