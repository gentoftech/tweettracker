"""Runtime configuration loaded strictly from environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tweettracker.crypto import resolve_secret


DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[2] / "data" / "tweettracker.db"
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"


class Settings(BaseSettings):
    """Application settings resolved from environment variables."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = Field(default=DEFAULT_DATABASE_URL, validation_alias="DATABASE_URL")
    auto_migrate: bool = Field(default=True, validation_alias="AUTO_MIGRATE")
    seed_fixtures: bool = Field(default=True, validation_alias="SEED_FIXTURES")
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="CORS_ORIGINS",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @staticmethod
    def admin_password() -> str:
        return os.environ.get("TWEETTRACKER_ADMIN_PASSWORD", "").strip()

    @staticmethod
    def webhook_secret() -> str:
        return resolve_secret("TWEETTRACKER_WEBHOOK_SECRET")

    @staticmethod
    def tweetapi_api_key() -> str:
        return resolve_secret("TWEETAPI_API_KEY")

    @staticmethod
    def google_ai_api_key() -> str:
        return resolve_secret("GOOGLE_AI_API_KEY")

    @staticmethod
    def xai_api_key() -> str:
        return resolve_secret("XAI_API_KEY")

    @staticmethod
    def github_token() -> str:
        return resolve_secret("GITHUB_TOKEN")

    @staticmethod
    def provider_env_status() -> dict[str, bool]:
        """Return boolean availability of provider credentials without exposing values."""
        return {
            "fixture": True,
            "tweetapi_com": bool(resolve_secret("TWEETAPI_API_KEY")),
            "socialdata": bool(os.environ.get("SOCIALDATA_API_KEY", "").strip()),
            "twitterapi_io": bool(os.environ.get("TWITTERAPI_IO_KEY", "").strip()),
            "official_x": bool(os.environ.get("X_BEARER_TOKEN", "").strip()),
            "google_gemini": bool(resolve_secret("GOOGLE_AI_API_KEY")),
            "xai_grok": bool(resolve_secret("XAI_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            "deepinfra": bool(os.environ.get("DEEPINFRA_API_KEY", "").strip()),
            "github": bool(resolve_secret("GITHUB_TOKEN")),
            "webhook_hmac": bool(resolve_secret("TWEETTRACKER_WEBHOOK_SECRET")),
            "admin_password": bool(os.environ.get("TWEETTRACKER_ADMIN_PASSWORD", "").strip()),
        }


def get_settings() -> Settings:
    return Settings()
