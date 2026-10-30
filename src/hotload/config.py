from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOTLOAD_", env_file=".env", extra="ignore")

    database_url: str | None = None  # default: sqlite in data_dir. Use postgresql+asyncpg://... for the hosted service
    secret_key: str = ""  # signs session cookies; set a long random value (empty = random per process)
    public_url: str | None = None  # e.g. https://hotload.example (OAuth callbacks, same-origin check)
    cookie_secure: bool | None = None  # None = secure whenever public_url is https (recommended); set explicitly to override
    registration_open: bool = True
    quotas_enabled: bool = False  # self-host default: unlimited. The hosted service turns this on.
    allowed_origins: list[str] = []  # extra Origins allowed to make state-changing requests (e.g. extensions)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    web_dir: Path | None = None  # built frontend (frontend/build); default: bundled fallback page
    # --- billing (all optional; a provider is enabled when its credentials are set) ---
    prices: dict[str, dict[str, int]] = {}  # {"KES": {"premium_30": 129900}} in minor units; merged over the USD defaults
    paystack_secret_key: str | None = None
    paystack_base_url: str = "https://api.paystack.co"
    btcpay_url: str | None = None  # e.g. https://btcpay.example.com
    btcpay_store_id: str | None = None
    btcpay_api_key: str | None = None
    btcpay_webhook_secret: str | None = None
    api_rate_limit_per_minute: int = 120  # per API key
    webhook_timeout_seconds: int = 10
    webhook_max_attempts: int = 5
    # --- storage: "local" (shared disk, default) or "s3" (AWS S3, Cloudflare R2, MinIO...) ---
    storage_backend: str = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # R2: https://<account-id>.r2.cloudflarestorage.com
    s3_region: str = "auto"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_prefix: str = "hotload/"
    s3_presign_seconds: int = 900
    worker_queue: str | None = None  # arq queue this worker serves; see docs/SCALING.md
    # Fernet key(s) for saved cookies, comma separated (first encrypts, all decrypt: rotation). Unset = feature off.
    # Generate one with: python -m hotload.admin cookie-key
    cookie_key: str | None = None
    max_live_minutes: int = 60  # longest live recording a job may request (a worker slot is held that long)
    # --- Telegram bot (needs public_url too: replies carry download links) ---
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None  # Telegram echoes it in X-Telegram-Bot-Api-Secret-Token
    telegram_allowed_chats: list[int] = []  # empty = anyone may use the bot
    telegram_api_base: str = "https://api.telegram.org"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("data")
    file_ttl_seconds: int = 3600
    job_ttl_seconds: int = 7200
    max_duration_seconds: int = 1200
    max_filesize_bytes: int = 2 * 1024**3
    rate_limit_per_minute: int = 20
    require_age_gate: bool = True
    proxy_url: str | None = None  # single default proxy (shorthand for proxy_urls=[...])
    proxy_urls: list[str] = []
    # host suffix -> proxy URLs to use for it; "direct" means no proxy. e.g. {"youtube.com": ["http://p1:8080"]}
    proxy_routes: dict[str, list[str]] = {}
    proxy_failure_threshold: int = 3
    proxy_cooldown_seconds: int = 300
    max_attempts: int = 3
    max_batch_size: int = 20
    max_playlist_entries: int = 50
    egress_filtered: bool = False  # operator confirms workers sit behind the egress filter (docs/LAUNCH.md)
    extract_deadline_seconds: int = 60  # wall-clock cap for fetching a page's metadata
    extract_max_bytes: int = 20 * 1024 * 1024  # most bytes read while extracting metadata (pages, manifests)
    max_concurrent_extractions: int = 8  # the API runs at most this many metadata fetches at once
    extract_queue_seconds: float = 5.0  # how long a request waits for a free slot before getting 503
    allow_private_targets: bool = False  # tests/dev only: disables the outbound SSRF guard
    canary_enabled: bool = True
    canary_file: Path | None = None  # JSON file replacing the built-in canary list (e.g. adult sites)
    canary_degraded_after: int = 2  # consecutive failures before a site is shown as degraded
    metrics_token: str | None = None  # if set, /metrics requires "Authorization: Bearer <token>"
    blocked_hosts: list[str] = []
    allowed_hosts: list[str] = []  # empty = any public host
    extract_timeout_seconds: int = 30
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (set behind a reverse proxy)
    trusted_proxy_hops: int = 1  # reverse proxies in front of the app; the client IP is the Nth entry from the RIGHT
    ffmpeg_location: str | None = None

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return (self.public_url or "").lower().startswith("https://")

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir.resolve() / 'hotload.db'}"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "out"


@lru_cache
def get_settings() -> Settings:
    return Settings()
