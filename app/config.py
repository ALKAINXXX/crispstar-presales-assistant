# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: added DeepSeek, local embedding and separate presales database settings.
# Source and license: see README.md.

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Preferred DeepSeek names and legacy GROQ_* inputs share each source's
    # canonical field key; source precedence still outranks alias preference.
    deepseek_api_key: str = Field(
        default="", repr=False,
        validation_alias=AliasChoices("deepseek_api_key", "groq_api_key"),
    )
    deepseek_model: str = Field(
        default="deepseek-flash",
        validation_alias=AliasChoices("deepseek_model", "groq_model"),
    )
    deepseek_max_context_tokens: int = Field(
        default=90_000,
        validation_alias=AliasChoices("deepseek_max_context_tokens", "groq_max_context_tokens"),
    )
    deepseek_default_max_completion_tokens: int = Field(
        default=2_048,
        validation_alias=AliasChoices("deepseek_default_max_completion_tokens", "groq_default_max_completion_tokens"),
    )
    # Explicit deployment choice; there is no automatic provider fallback.
    deepseek_tool_output_mode: Literal["standard", "deepseek_strict_beta"] = "standard"

    # Independent local Embedding configuration; never uses a chat API key.
    embedding_cache_dir: str = "data/embedding_models"
    embedding_threads: int = Field(default=2, ge=1, le=8)

    # Presales conversations and saved drafts use the configured local database.
    saas_database_url: str = "sqlite:///data/saas_assistant.db"

    app_env: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
