from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOTLOAD_", env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("data")
    file_ttl_seconds: int = 3600
    job_ttl_seconds: int = 7200
    max_duration_seconds: int = 1200
    max_filesize_bytes: int = 2 * 1024**3
    rate_limit_per_minute: int = 20
    require_age_gate: bool = True
    proxy_url: str | None = None
    blocked_hosts: list[str] = []
    allowed_hosts: list[str] = []  # empty = any public host
    extract_timeout_seconds: int = 30
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (set behind a reverse proxy)
    ffmpeg_location: str | None = None

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "out"


@lru_cache
def get_settings() -> Settings:
    return Settings()
