from datetime import datetime, timezone
import json
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.crypto import encrypt_secret, decrypt_secret


def utc_now():
    return datetime.now(timezone.utc)


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)           # Friendly name, e.g. "GitHub Personal Token"
    cred_type = Column(String(50), nullable=False)       # "git" or "drone"
    username = Column(String(255), nullable=True)        # For git credentials
    secret = Column(Text, nullable=False)                # Encrypted password or token
    server_url = Column(String(512), nullable=True)      # Server URL (optional for Drone or custom git)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def get_plain_secret(self) -> str:
        return decrypt_secret(self.secret) or ""

    def set_secret(self, val: str):
        self.secret = encrypt_secret(val.strip()) if val else ""


class Repository(Base):
    __tablename__ = "repositories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    git_url = Column(String(1024), nullable=False)
    git_username = Column(String(255), nullable=True)
    git_password = Column(String(1024), nullable=True)
    git_credential_id = Column(Integer, ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True, index=True)
    track_all_branches = Column(Boolean, default=False, nullable=False)
    tracked_branches = Column(String(1024), default="main", nullable=False)
    
    drone_server = Column(String(512), nullable=False)
    drone_repo = Column(String(255), nullable=False)
    drone_token = Column(String(1024), default="", nullable=False)
    drone_credential_id = Column(Integer, ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True, index=True)
    
    poll_interval_minutes = Column(Integer, default=2, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    # Schedule settings
    schedule_mode = Column(String(20), default="global", nullable=False)  # "global", "always", "custom"
    custom_schedule_timezone = Column(String(50), default="Europe/Moscow", nullable=False)
    custom_schedule_json = Column(Text, nullable=True)
    
    last_polled_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), default="idle", nullable=False)  # idle, success, error, checking
    last_error = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    branches = relationship("BranchState", back_populates="repository", cascade="all, delete-orphan")
    logs = relationship("PollLog", back_populates="repository", cascade="all, delete-orphan")
    git_credential = relationship("Credential", foreign_keys=[git_credential_id])
    drone_credential = relationship("Credential", foreign_keys=[drone_credential_id])

    def get_tracked_branch_list(self) -> list[str]:
        if not self.tracked_branches:
            return []
        cleaned = self.tracked_branches.replace("\n", ",").replace("\r", ",")
        return [b.strip() for b in cleaned.split(",") if b.strip()]

    def get_effective_git_username(self) -> str | None:
        if self.git_credential and self.git_credential.username:
            return self.git_credential.username
        return self.git_username

    def get_plain_git_password(self) -> str | None:
        if self.git_credential:
            return self.git_credential.get_plain_secret()
        return decrypt_secret(self.git_password)

    def get_effective_drone_server(self) -> str:
        if self.drone_credential and self.drone_credential.server_url:
            return self.drone_credential.server_url
        return self.drone_server

    def get_plain_drone_token(self) -> str:
        if self.drone_credential:
            return self.drone_credential.get_plain_secret()
        return decrypt_secret(self.drone_token) or ""

    def set_credentials(self, git_password: str | None = None, drone_token: str | None = None):
        if git_password is not None:
            self.git_password = encrypt_secret(git_password.strip()) if git_password.strip() else None
        if drone_token is not None:
            self.drone_token = encrypt_secret(drone_token.strip()) if drone_token.strip() else ""
        elif not hasattr(self, "drone_token") or self.drone_token is None:
            self.drone_token = ""

    def get_custom_schedule(self) -> dict:
        if self.custom_schedule_json:
            try:
                return json.loads(self.custom_schedule_json)
            except Exception:
                pass
        from app.schedule import get_default_schedule
        return get_default_schedule()

    @property
    def custom_schedule(self) -> dict:
        return self.get_custom_schedule()

    def set_custom_schedule(self, schedule_dict: dict):
        from app.schedule import normalize_schedule_dict
        self.custom_schedule_json = json.dumps(normalize_schedule_dict(schedule_dict))


class BranchState(Base):
    __tablename__ = "branch_states"

    id = Column(Integer, primary_key=True, index=True)
    repo_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_name = Column(String(255), nullable=False, index=True)
    last_sha = Column(String(64), nullable=False)
    last_build_number = Column(Integer, nullable=True)
    last_build_status = Column(String(50), nullable=True)
    is_stale = Column(Boolean, default=False, nullable=False)  # True if deleted on remote
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    repository = relationship("Repository", back_populates="branches")


class PollLog(Base):
    __tablename__ = "poll_logs"

    id = Column(Integer, primary_key=True, index=True)
    repo_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True, index=True)
    repo_name = Column(String(255), nullable=False)
    branch_name = Column(String(255), nullable=True)
    commit_sha = Column(String(64), nullable=True)
    build_number = Column(Integer, nullable=True)
    level = Column(String(20), default="info", nullable=False)  # info, success, warning, error
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)

    repository = relationship("Repository", back_populates="logs")


class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True, default=1)

    # Global event toggles
    notify_on_build = Column(Boolean, default=True, nullable=False)
    notify_on_error = Column(Boolean, default=True, nullable=False)

    # Telegram
    telegram_enabled = Column(Boolean, default=False, nullable=False)
    telegram_bot_token = Column(String(512), nullable=True)  # Fernet encrypted
    telegram_chat_id = Column(String(255), nullable=True)

    # VK Group
    vk_enabled = Column(Boolean, default=False, nullable=False)
    vk_group_token = Column(Text, nullable=True)  # Fernet encrypted
    vk_peer_id = Column(String(255), nullable=True)

    # Email (SMTP)
    email_enabled = Column(Boolean, default=False, nullable=False)
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, default=587, nullable=True)
    smtp_username = Column(String(255), nullable=True)
    smtp_password = Column(Text, nullable=True)  # Fernet encrypted
    smtp_use_tls = Column(Boolean, default=True, nullable=False)
    smtp_from = Column(String(255), nullable=True)
    smtp_to = Column(String(1024), nullable=True)

    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def get_plain_telegram_bot_token(self) -> str:
        return decrypt_secret(self.telegram_bot_token) or ""

    def get_plain_vk_group_token(self) -> str:
        return decrypt_secret(self.vk_group_token) or ""

    def get_plain_smtp_password(self) -> str:
        return decrypt_secret(self.smtp_password) or ""

    def set_secrets(self, telegram_token: str | None = None, vk_token: str | None = None, smtp_password: str | None = None):
        if telegram_token is not None:
            self.telegram_bot_token = encrypt_secret(telegram_token.strip()) if telegram_token.strip() else None
        if vk_token is not None:
            self.vk_group_token = encrypt_secret(vk_token.strip()) if vk_token.strip() else None
        if smtp_password is not None:
            self.smtp_password = encrypt_secret(smtp_password.strip()) if smtp_password.strip() else None


def get_notification_settings(db) -> NotificationSettings:
    """Retrieve or initialize singleton notification settings row."""
    cfg = db.query(NotificationSettings).filter(NotificationSettings.id == 1).first()
    if not cfg:
        cfg = NotificationSettings(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


class WorkerScheduleSettings(Base):
    __tablename__ = "worker_schedule_settings"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, default=False, nullable=False)  # False = 24/7
    timezone = Column(String(50), default="Europe/Moscow", nullable=False)
    schedule_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def get_schedule(self) -> dict:
        if self.schedule_json:
            try:
                return json.loads(self.schedule_json)
            except Exception:
                pass
        from app.schedule import get_default_schedule
        return get_default_schedule()

    def set_schedule(self, schedule_dict: dict):
        from app.schedule import normalize_schedule_dict
        self.schedule_json = json.dumps(normalize_schedule_dict(schedule_dict))


def get_worker_schedule_settings(db) -> WorkerScheduleSettings:
    """Retrieve or initialize singleton worker schedule settings row."""
    cfg = db.query(WorkerScheduleSettings).filter(WorkerScheduleSettings.id == 1).first()
    if not cfg:
        from app.schedule import get_default_schedule
        cfg = WorkerScheduleSettings(
            id=1,
            enabled=False,
            timezone="Europe/Moscow",
            schedule_json=json.dumps(get_default_schedule())
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg

