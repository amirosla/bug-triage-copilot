"""Application configuration via environment variables."""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+psycopg://triage:triage@localhost:5432/bug_triage",
        description="PostgreSQL connection URL (psycopg3 dialect).",
    )

    # ── Redis / Queue ───────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")
    worker_queue_name: str = Field(default="triage")

    # ── GitHub App ──────────────────────────────────────────────────────────────
    github_app_id: str = Field(default="")
    github_private_key_base64: str = Field(
        default="",
        description="Base64-encoded PEM private key for the GitHub App.",
    )
    github_webhook_secret: str = Field(default="changeme")

    # ── LLM provider ────────────────────────────────────────────────────────────
    llm_provider: Literal["mock", "openai"] = Field(
        default="mock",
        description="LLM backend: 'mock' for dev/testing, 'openai' for real calls.",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="Base URL for OpenAI-compatible endpoint (leave None for api.openai.com).",
    )
    llm_api_key: str = Field(default="sk-mock")
    llm_model: str = Field(default="gpt-4o-mini")

    # ── Embeddings ───────────────────────────────────────────────────────────────
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_dimensions: int = Field(default=1536)

    # ── App ─────────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    environment: str = Field(default="development")
    bot_comment_marker: str = Field(default="<!-- triage-copilot -->")
    bot_github_login: str = Field(
        default="bug-triage-copilot[bot]",
        description="GitHub login used to identify bot comments.",
    )

    # ── Derived ──────────────────────────────────────────────────────────────────
    @property
    def github_private_key(self) -> str:
        """Decode the base64 PEM private key."""
        if not self.github_private_key_base64:
            return ""
        try:
            return base64.b64decode(self.github_private_key_base64).decode()
        except Exception:
            return self.github_private_key_base64  # Already plain text (dev mode)

    @model_validator(mode="after")
    def _validate_github_config(self) -> Settings:
        if self.environment == "production":
            if not self.github_app_id:
                raise ValueError("GITHUB_APP_ID is required in production")
            if not self.github_private_key_base64:
                raise ValueError("GITHUB_PRIVATE_KEY_BASE64 is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
