"""Configuration settings for drone-ci-build-sync."""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "Drone CI Build Sync"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    data_dir: Path = BASE_DIR / "data"
    database_url: str = f"sqlite:///{BASE_DIR}/data/drone_sync.db"
    
    # Default fallback values for Drone CI
    default_drone_server: str = "https://drone.uberserver.ru"
    default_drone_token: str = ""
    
    # Encryption secret key (optional, auto-generated if omitted)
    secret_key: str = ""

    # Git timeout in seconds
    git_timeout: int = 20

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
