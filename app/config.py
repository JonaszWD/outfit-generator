from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    database_url: str
    test_database_url: str | None = None
    dev_user_email: str = "dev@example.com"
    media_root: Path = Path("media")
    # Where photos live. "local" is a folder (Docker, dev); "supabase" and "s3" are for hosts
    # without a persistent disk, like Vercel.
    storage_backend: Literal["local", "supabase", "s3"] = "local"
    supabase_url: str | None = None  # https://<project>.supabase.co
    supabase_service_key: SecretStr | None = None  # server-side only: it bypasses all policies
    supabase_bucket: str = "wardrobe"  # a private bucket
    s3_endpoint: str | None = None  # host[:port] of any S3-compatible service (R2, B2, S3)
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str | None = None
    s3_secure: bool = True
    max_upload_mb: int = 10
    max_batch_files: int = 10

    # Tagging (Phase 2). "fake" never sends anything over the network.
    tagger_backend: Literal["claude", "gemini", "fake"] = "fake"
    tagger_model: str = "claude-opus-5-5"
    anthropic_api_key: SecretStr | None = None
    tagger_max_retries: int = 4  # SDK-level retries with exponential backoff
    tag_confidence_threshold: float = 0.7
    max_concurrent_pipelines: int = 2
    rembg_model: str = "u2net"
    # "none" skips cutouts and shows the original photo (needed on Vercel, where rembg does not
    # fit). "rembg" needs `uv sync --extra cutout`.
    cutout_backend: Literal["rembg", "none"] = "rembg"
    # Seconds to wait for an AI vendor. Keep it under the host's function time limit.
    ai_timeout_seconds: float = 90.0

    # Style references. "fake" is offline; "claude" sends the (EXIF-stripped) reference photo to
    # the Anthropic API; "ollama" keeps it on this machine (a local vision model).
    # "gemini" and "openai" use an OpenAI-compatible chat API ("openai" is any such host).
    style_backend: Literal["claude", "ollama", "gemini", "openai", "fake"] = "fake"
    style_model: str = "claude-opus-5-5"  # used by the claude and openai backends
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    style_base_url: str | None = None  # openai backend: e.g. https://api.openai.com/v1
    style_api_key: SecretStr | None = None  # openai backend
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"
    ollama_timeout_seconds: float = 180.0

    # Link analysis (Phase 4)
    analyses_per_hour: int = 10
    candidate_cache_hours: int = 24
    max_concurrent_analyses: int = 2
    candidate_retention_days: int = 30
    fetch_connect_timeout: float = 5.0
    fetch_total_timeout: float = 15.0
    fetch_max_redirects: int = 5
    fetch_max_html_kb: int = 2048
    fetch_user_agent: str = (
        "WardrobeBot/0.1 (personal outfit analysis; +https://example.invalid/bot)"
    )
    fetch_host_delay_seconds: float = 1.0
    # Dev/test only: allow private and loopback addresses (and any port) so a local fixture
    # server can be fetched. Never enable this in production.
    fetch_allow_private_hosts: bool = False
    scraper_use_browser: bool = False

    # Serverless hosting (Vercel)
    # Run tagging inside a request the browser triggers per item, not in a background task that
    # a serverless function may freeze after responding.
    process_in_request: bool = False
    # Fresh connection per request (use the provider's pooled connection string).
    db_serverless: bool = False
    # Take the client address from X-Forwarded-For (only behind a proxy that sets it).
    trust_proxy_headers: bool = False

    # Accounts (Phase 5)
    # Signs CSRF tokens. Must be set to something private when ENV=production.
    secret_key: SecretStr = SecretStr("dev-only-secret-change-me")
    cookie_secure: bool | None = None  # None: secure when ENV=production
    session_days: int = 14
    min_password_length: int = 10
    # Can new people register? Unset: yes in development; in production only when INVITE_CODE
    # is set, and then registering needs that code (a few people can sign up, it is not public).
    registration_open: bool | None = None
    invite_code: SecretStr | None = None
    app_base_url: str = "http://localhost:8000"
    email_backend: Literal["console", "smtp"] = "console"
    email_from: str = "Wardrobe <no-reply@localhost>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True
    login_attempts_per_account: int = 5  # failed attempts per window
    login_attempts_per_ip: int = 20
    login_window_minutes: int = 15
    reset_requests_per_account: int = 3  # per hour
    reset_requests_per_ip: int = 10
    register_per_ip: int = 10

    # Weather (Phase 5): Open-Meteo needs no key. Only rounded coordinates are ever sent.
    weather_provider: Literal["open-meteo", "none"] = "open-meteo"
    weather_cache_minutes: int = 60
    weather_timeout_seconds: float = 5.0

    @property
    def secure_cookies(self) -> bool:
        return self.env == "production" if self.cookie_secure is None else self.cookie_secure

    @property
    def registration_allowed(self) -> bool:
        """Explicit REGISTRATION_OPEN wins. Unset: open in development; in production only with
        an INVITE_CODE (otherwise closed)."""
        if self.registration_open is not None:
            return self.registration_open
        return self.env != "production" or self.invite_code is not None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
