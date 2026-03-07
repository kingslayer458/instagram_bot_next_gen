"""
Configuration management using Pydantic Settings.
Validates all env vars at startup with clear error messages.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

# Resolve .env relative to this package directory (not cwd)
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class AIProvider(str, enum.Enum):
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class CaptionVariety(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Settings(BaseSettings):
    """All configuration loaded from environment variables or .env file."""

    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8", "extra": "ignore"}

    # ── Instagram ────────────────────────────────────────────────────────
    instagram_access_token: str = Field(..., description="Meta Graph API long-lived token")
    instagram_page_id: str = Field(..., description="Instagram Business Account ID")

    # ── Steam ────────────────────────────────────────────────────────────
    steam_user_ids: list[str] = Field(default_factory=list, description="Comma-separated Steam64 IDs")
    max_screenshots_per_user: int = Field(100, ge=1)
    batch_size: int = Field(10, ge=1, le=50)
    max_retries: int = Field(3, ge=1, le=10)

    # ── Scheduling ───────────────────────────────────────────────────────
    posting_schedule: str = Field("0 12 * * *", description="Cron expression for posting")
    port: int = Field(3000, ge=1, le=65535)

    # ── AI – Primary ────────────────────────────────────────────────────
    enable_ai_captions: bool = True
    enable_vision_analysis: bool = True
    ai_provider: AIProvider = AIProvider.GEMINI
    ai_model: str = "gemini-2.5-flash"
    caption_variety: CaptionVariety = CaptionVariety.HIGH
    fallback_to_static: bool = True

    # ── AI – Multi-modal enrichment (NEW) ────────────────────────────
    enable_mood_detection: bool = Field(True, description="Detect mood/atmosphere via vision")
    enable_smart_hashtags: bool = Field(True, description="AI-generated contextual hashtags")
    enable_caption_scoring: bool = Field(True, description="Score & rank caption candidates")
    caption_candidates: int = Field(3, ge=1, le=5, description="Number of caption variants to generate")
    max_caption_length: int = Field(2200, description="Instagram caption char limit")

    # ── AI Keys ──────────────────────────────────────────────────────────
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

    # ── Image Hosting ────────────────────────────────────────────────────
    imgbb_api_key: Optional[str] = None

    # ── Persistence ──────────────────────────────────────────────────────
    database_url: Optional[str] = None

    # ── Rate-limit tuning ────────────────────────────────────────────────
    steam_page_delay: float = Field(15.0, ge=1.0, description="Seconds between Steam page fetches")
    steam_detail_delay: float = Field(8.0, ge=1.0, description="Seconds between screenshot detail fetches")
    steam_user_delay: float = Field(30.0, ge=5.0, description="Seconds between different Steam users")
    parallel_workers: int = Field(5, ge=1, le=20, description="Number of parallel workers for detail fetching")

    @field_validator("steam_user_ids", mode="before")
    @classmethod
    def parse_steam_ids(cls, v):
        if isinstance(v, (int, float)):
            return [str(int(v))]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def get_active_ai_key(self) -> Optional[str]:
        """Return the API key for the currently selected provider."""
        return {
            AIProvider.GEMINI: self.gemini_api_key,
            AIProvider.OPENAI: self.openai_api_key,
            AIProvider.ANTHROPIC: self.anthropic_api_key,
        }.get(self.ai_provider)

    def validate_ai_config(self) -> list[str]:
        """Return warning messages about AI configuration."""
        warnings: list[str] = []
        if self.enable_vision_analysis and not self.gemini_api_key:
            warnings.append("Vision analysis enabled but GEMINI_API_KEY not set – will fall back to text captions.")
        if self.enable_ai_captions and not any([self.gemini_api_key, self.openai_api_key, self.anthropic_api_key]):
            warnings.append("AI captions enabled but no API keys set – will fall back to static captions.")
        return warnings
