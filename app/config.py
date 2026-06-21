from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    environment: str = "development"
    auto_create_tables: bool = True

    # Database
    database_url: str = "postgresql+asyncpg://email_agent:changeme@localhost:5432/email_agent"
    database_url_sync: str = "postgresql://email_agent:changeme@localhost:5432/email_agent"

    # Background jobs
    task_queue_backend: str = "inprocess"
    background_jobs_enabled: bool = True
    scheduler_startup_delay_seconds: int = 10
    redis_url: str = ""

    # Security
    secret_key: str = "change-this-secret-key-in-production"
    master_key: str = "change-this-master-key-32-bytes!"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    local_app_token: str = "local-email-agent-access"
    local_app_email: str = "owner@emailagent.local"

    # Device limits
    max_trusted_devices: int = 2

    # Gmail OAuth
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = "http://localhost:8000/api/v1/accounts/gmail/callback"

    # Outlook OAuth
    outlook_client_id: str = ""
    outlook_client_secret: str = ""
    outlook_redirect_uri: str = "http://localhost:8000/api/v1/accounts/outlook/callback"
    outlook_tenant: str = "common"

    # cPanel / Namecheap hosting email integration
    cpanel_base_url: str = ""
    cpanel_username: str = ""
    cpanel_api_token: str = ""
    cpanel_domains: str = "edumail.az,smartapply.az"
    cpanel_imap_host: str = ""
    cpanel_smtp_host: str = ""

    # Mobile APK self-update metadata
    mobile_apk_url: str = ""
    mobile_apk_version: str = "0.2.1"
    mobile_apk_version_code: int = 3
    mobile_release_label: str = "2026.06.21-02 APKUpdater"
    mobile_release_notes: str = "Mobile folders, accounts, AI draft assistant, hosting mailboxes, account deletion, in-app APK updater"
    mobile_update_required: bool = False

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "changeme"
    minio_secure: bool = False
    minio_bucket: str = "email-agent"

    # Sync
    sync_interval_minutes: int = 5
    imap_idle_timeout: int = 1740  # 29 minutes
    imap_skip_uids: str = ""
    imap_strict_clamp_accounts: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

    @model_validator(mode="after")
    def validate_production_settings(self):
        if self.environment.lower() not in {"prod", "production"}:
            return self

        unsafe = []
        secret_defaults = {
            "secret_key": "change-this-secret-key-in-production",
            "master_key": "change-this-master-key-32-bytes!",
            "minio_secret_key": "changeme",
        }
        for field_name, default_value in secret_defaults.items():
            value = getattr(self, field_name)
            if value == default_value or "changeme" in value.lower():
                unsafe.append(field_name.upper())

        if unsafe:
            raise ValueError("Unsafe production settings: " + ", ".join(unsafe))
        return self


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
