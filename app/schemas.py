from typing import Optional, List, Literal, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class CredentialBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Friendly credential name")
    cred_type: Literal["git", "drone"] = Field(..., description="Type of credential: 'git' or 'drone'")
    username: Optional[str] = Field(None, max_length=255, description="Username (primarily for Git)")
    server_url: Optional[str] = Field(None, max_length=512, description="Server URL (optional, e.g. for Drone CI)")
    description: Optional[str] = Field(None, description="Optional notes or description")


class CredentialCreate(CredentialBase):
    secret: str = Field(..., min_length=1, description="Password, PAT or API token")


class CredentialUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    cred_type: Optional[Literal["git", "drone"]] = None
    username: Optional[str] = None
    secret: Optional[str] = Field(None, description="Provide new secret to update, or omit to keep current")
    server_url: Optional[str] = None
    description: Optional[str] = None


class CredentialOut(CredentialBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    secret_masked: str = "••••••••"
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime


class RepositoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Repository friendly name")
    git_url: str = Field(..., min_length=1, max_length=1024, description="Git clone/fetch URL")
    git_credential_id: Optional[int] = Field(None, description="Linked Credential ID for Git access")
    git_username: Optional[str] = Field(None, max_length=255, description="Git username (if not using linked credential)")
    git_password: Optional[str] = Field(None, max_length=255, description="Git password or PAT (if not using linked credential)")
    track_all_branches: bool = Field(False, description="Whether to track all remote branches")
    tracked_branches: str = Field("main", description="Comma-separated branch names if track_all_branches is false")
    
    drone_server: str = Field("https://drone.uberserver.ru", max_length=512, description="Drone CI server URL")
    drone_repo: str = Field(..., min_length=1, max_length=255, description="Drone repository in format owner/repo")
    drone_credential_id: Optional[int] = Field(None, description="Linked Credential ID for Drone CI access")
    drone_token: Optional[str] = Field("", max_length=512, description="Drone CI user access token (if not using linked credential)")
    
    poll_interval_minutes: int = Field(2, ge=1, le=1440, description="Polling interval in minutes")
    is_active: bool = Field(True, description="Whether polling is enabled")
    schedule_mode: Literal["global", "always", "custom"] = Field("global", description="Schedule mode: global, always, custom")
    custom_schedule_timezone: Optional[str] = Field("Europe/Moscow", description="Custom timezone for repository")
    custom_schedule_json: Optional[str] = Field(None, description="Custom schedule JSON for repository")


class RepositoryCreate(RepositoryBase):
    pass


class RepositoryUpdate(RepositoryBase):
    pass


class BranchStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_name: str
    last_sha: str
    last_build_number: Optional[int] = None
    last_build_status: Optional[str] = None
    is_stale: bool = False
    updated_at: datetime


class RepositoryOut(RepositoryBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    last_polled_at: Optional[datetime] = None
    last_status: str
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    branches: List[BranchStateOut] = []


class TestConnectionRequest(BaseModel):
    git_url: str
    git_credential_id: Optional[int] = None
    git_username: Optional[str] = None
    git_password: Optional[str] = None
    drone_server: str
    drone_repo: str
    drone_credential_id: Optional[int] = None
    drone_token: Optional[str] = ""


class TestConnectionResponse(BaseModel):
    git_ok: bool
    git_message: str
    git_branches: List[str] = []
    drone_ok: bool
    drone_message: str


class TestNotificationRequest(BaseModel):
    channel: str  # "telegram", "vk", "email"
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    vk_group_token: Optional[str] = None
    vk_peer_id: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = True
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None


class SaveNotificationSettingsRequest(BaseModel):
    channel: str  # "telegram", "vk", "email", "events", or "all"
    notify_on_build: Optional[bool] = None
    notify_on_error: Optional[bool] = None
    telegram_enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    vk_enabled: Optional[bool] = None
    vk_group_token: Optional[str] = None
    vk_peer_id: Optional[str] = None
    email_enabled: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = True
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None


class DayScheduleItem(BaseModel):
    enabled: bool = True
    start: str = "09:00"
    end: str = "18:00"


class WorkerScheduleUpdate(BaseModel):
    enabled: bool = False
    timezone: str = "Europe/Moscow"
    days: Dict[str, DayScheduleItem]


class WorkerScheduleOut(BaseModel):
    enabled: bool
    timezone: str
    current_time_str: str
    is_active_now: bool
    status_message: str
    days: Dict[str, DayScheduleItem]
