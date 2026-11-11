from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = str(_BASE_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    PUBLIC_BASE_URL: str = "http://localhost:8080"
    ODOO_URL: str = "https://mycompany.odoo.com"
    ODOO_DB: str = "mycompany"
    DATABASE_URL: str = "sqlite:///./data/gateway.db"
    ENCRYPTION_KEY: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_WORKSPACE_DOMAINS: list[str] | str = []
    GOOGLE_REDIRECT_URI: str | None = None

    @property
    def google_redirect_uri(self) -> str:
        if self.GOOGLE_REDIRECT_URI:
            return self.GOOGLE_REDIRECT_URI
        return f"{self.PUBLIC_BASE_URL.rstrip('/')}/oauth/google/callback"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    ODOO_EMPLOYEE_TELEGRAM_ID_FIELD: str = ""
    TELEGRAM_WEB_CLIENT_ID: str = ""
    TELEGRAM_WEB_CLIENT_SECRET: str = ""
    TELEGRAM_WEB_BASE_URL: str = ""

    @property
    def telegram_web_configured(self) -> bool:
        return bool(
            self.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD
            and self.TELEGRAM_WEB_CLIENT_ID
            and self.TELEGRAM_WEB_CLIENT_SECRET
            and self.ENCRYPTION_KEY
        )

    # AI Agent Integration
    ENABLE_AI_AGENT_FORWARDING: bool = False
    AI_AGENT_BASE_URL: str = "http://localhost:8000"
    GATEWAY_TO_AGENT_SECRET: str = ""
    AGENT_TO_GATEWAY_SECRET: str = ""

    ENABLE_REPORTING: bool = False
    ENABLE_QUOTATIONS: bool = False
    ENABLE_BUSINESS_ACTIONS: bool = False
    ENABLE_QUOTATION_APPROVAL_FLOW: bool = False
    ENABLE_BUSINESS_CONTACTS: bool = False
    ENABLE_BUSINESS_PRODUCTS: bool = False
    ENABLE_BUSINESS_CRM: bool = False
    ENABLE_BUSINESS_SALES: bool = False
    ENABLE_BUSINESS_PURCHASES: bool = False
    ENABLE_BUSINESS_STOCK: bool = False
    ENABLE_BUSINESS_ACTIVITIES: bool = False
    ENABLE_BUSINESS_NOTES: bool = False
    ENABLE_BUSINESS_DELETE: bool = False
    ENABLE_BUSINESS_EMAIL: bool = False

    # Optional Cloudflare
    CLOUDFLARE_TUNNEL_TOKEN: str = ""

    # Default intervals & settings
    SESSION_LIFETIME_HOURS: int = 8
    LINK_TOKEN_LIFETIME_MINUTES: int = 10
    REFRESH_JOB_SECONDS: int = 60
    CALENDAR_JOB_SECONDS: int = 120
    REMINDER_JOB_SECONDS: int = 60
    DEFAULT_TIMEZONE: str = "Asia/Bangkok"

    @model_validator(mode="after")
    def validate_production_constraints(self) -> "Settings":
        if isinstance(self.GOOGLE_WORKSPACE_DOMAINS, str):
            domains = [d.strip() for d in self.GOOGLE_WORKSPACE_DOMAINS.split(",") if d.strip()]
            self.GOOGLE_WORKSPACE_DOMAINS = domains

        if self.ENABLE_AI_AGENT_FORWARDING:
            if not self.GATEWAY_TO_AGENT_SECRET or not self.AGENT_TO_GATEWAY_SECRET:
                raise ValueError(
                    "GATEWAY_TO_AGENT_SECRET and AGENT_TO_GATEWAY_SECRET are required when ENABLE_AI_AGENT_FORWARDING is True."
                )
            if len(self.GATEWAY_TO_AGENT_SECRET) < 32 or len(self.AGENT_TO_GATEWAY_SECRET) < 32:
                raise ValueError("Both service secrets must be at least 32 characters long.")
            if self.GATEWAY_TO_AGENT_SECRET == self.AGENT_TO_GATEWAY_SECRET:
                raise ValueError(
                    "GATEWAY_TO_AGENT_SECRET and AGENT_TO_GATEWAY_SECRET must be different/distinct."
                )

        if self.APP_ENV == "production":
            # 1. ENCRYPTION_KEY must be a valid Fernet key
            if not self.ENCRYPTION_KEY:
                raise ValueError("ENCRYPTION_KEY is required in production")
            try:
                Fernet(self.ENCRYPTION_KEY.encode())
            except Exception as e:
                raise ValueError(f"Invalid Fernet ENCRYPTION_KEY in production: {e}") from e

            # 2. PUBLIC_BASE_URL must be HTTPS
            if not self.PUBLIC_BASE_URL.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")

            # 3. ODOO_URL must be HTTPS
            if not self.ODOO_URL.startswith("https://"):
                raise ValueError("ODOO_URL must use HTTPS in production")

        return self
