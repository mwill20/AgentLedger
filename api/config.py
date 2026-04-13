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
    api_keys: str = ""  # comma-separated; set via API_KEYS env var
    admin_api_keys: str = ""  # comma-separated admin keys for identity revocation
    ip_rate_limit: int = 100
    ip_rate_window_seconds: int = 60

    # Identity / Layer 2
    issuer_did: str = "did:web:agentledger.io"
    issuer_private_jwk: str = ""
    credential_ttl_seconds: int = 31536000
    proof_nonce_ttl_seconds: int = 60
    session_assertion_ttl_seconds: int = 300
    approved_session_ttl_seconds: int = 900
    authorization_request_ttl_seconds: int = 300
    revocation_cache_ttl_seconds: int = 300
    did_web_cache_ttl_seconds: int = 600

    # Embeddings: "model" = sentence-transformers (GPU/prod), "hash" = fast fallback (CPU/CI/load-test)
    embedding_mode: str = "model"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
