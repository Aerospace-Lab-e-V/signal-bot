import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _split_env(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    signal_sender_number: str = os.getenv("SIGNAL_SENDER_NUMBER", "")
    signal_cli_path: str = os.getenv("SIGNAL_CLI_PATH", "signal-cli")
    signal_cli_data_dir: str = os.getenv("SIGNAL_CLI_DATA_DIR", "/signal-cli-config")
    signal_cli_timeout_seconds: int = _int_env("SIGNAL_CLI_TIMEOUT_SECONDS", 120)
    signal_receive_timeout_seconds: int = _int_env("SIGNAL_RECEIVE_TIMEOUT_SECONDS", 5)
    signal_receive_interval_seconds: int = _int_env("SIGNAL_RECEIVE_INTERVAL_SECONDS", 300)
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app-data/app.db")
    session_secret: str = os.getenv("SESSION_SECRET", "change-me-in-production")
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")
    oidc_allowed_group: str = os.getenv("OIDC_ALLOWED_GROUP", "")
    oidc_allowed_groups: str = os.getenv("OIDC_ALLOWED_GROUPS", "")
    oidc_allowed_role: str = os.getenv("OIDC_ALLOWED_ROLE", "")
    oidc_debug_claims: bool = _bool_env("OIDC_DEBUG_CLAIMS")
    app_base_url: str = os.getenv("APP_BASE_URL", "")
    logout_redirect_url: str = os.getenv("LOGOUT_REDIRECT_URL", "")
    app_timezone: str = os.getenv("APP_TIMEZONE", "Europe/Berlin")
    auth_bypass_for_development: bool = _bool_env("AUTH_BYPASS_FOR_DEVELOPMENT")
    retry_interval_seconds: int = _int_env("MESSAGE_RETRY_INTERVAL_SECONDS", 300)
    retry_window_hours: int = _int_env("MESSAGE_RETRY_WINDOW_HOURS", 24)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def oidc_metadata_url(self) -> str:
        return f"{self.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"

    @property
    def allowed_oidc_groups(self) -> set[str]:
        return {
            *(_split_env(self.oidc_allowed_group) if self.oidc_allowed_group else ()),
            *(_split_env(self.oidc_allowed_groups) if self.oidc_allowed_groups else ()),
        }


settings = Settings()
