"""Application settings via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://agentledger:agentledger@db:5432/agentledger"
    database_url_sync: str = "postgresql://agentledger:agentledger@db:5432/agentledger"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # API
    api_version: str = "0.1.0"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_keys: str = "dev-api-key"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
