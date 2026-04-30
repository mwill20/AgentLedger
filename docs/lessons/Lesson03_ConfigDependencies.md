# 🎓 Lesson 03: Mission Control — Configuration and Dependencies

> **Beginner frame:** Configuration is the control panel that tells AgentLedger which database, cache, keys, and runtime modes to use. Dependency injection is the wiring that hands those choices to the right code without hiding them in global state.

## 🛡️ Welcome Back, Systems Engineer!

How does AgentLedger know which database to connect to, what API keys to accept, and whether to load a 100MB ML model or use a fast hash fallback? 🔍 Today we're exploring **configuration and dependency injection** — the "mission control" that wires every component together.

**Goal:** Understand how settings flow from `.env` into the running app, and how FastAPI's dependency injection provides database sessions, Redis clients, and auth guards.  
**Time:** 45 minutes  
**Prerequisites:** Lessons 01-02  
**Why this matters:** Misconfigured dependencies cause 90% of "works on my machine" bugs. Understanding this layer prevents that.

---

## 🎯 Learning Objectives

- Trace a setting from `.env` through `pydantic-settings` into application code ✅
- Explain all configuration knobs and their defaults ✅
- Understand FastAPI dependency injection with `Depends()` ✅
- Describe the four auth surfaces (none, API key, admin API key, bearer VC) ✅
- Explain why `NullRedisClient` exists ✅
- Run the app with different `EMBEDDING_MODE` values ✅

---

## 🔍 How Configuration Flows

```
.env file (or OS env vars)
        |
        v
┌──────────────────────┐
│  api/config.py       │
│  Settings (pydantic)  │
│  - database_url      │
│  - redis_url         │
│  - api_keys          │
│  - embedding_mode    │
│  - ...               │
└──────────┬───────────┘
           |
     ┌─────┴──────┐
     v            v
┌─────────┐  ┌──────────┐
│ depend- │  │ ratelimit│
│ encies  │  │ .py      │
│ .py     │  │          │
│ (DB,    │  │ (reads   │
│  Redis, │  │  limits) │
│  Auth)  │  │          │
└─────────┘  └──────────┘
```

---

## 📝 Code Walkthrough: `api/config.py`

```python
# api/config.py — The entire file (37 lines)
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
    authorization_webhook_url: str = ""
    authorization_webhook_secret: str = ""
    authorization_webhook_timeout_seconds: float = 3.0

    # Embeddings
    embedding_mode: str = "model"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
```

🔍 **Line-by-Line:**

1. **Two database URLs** — `database_url` uses `asyncpg` (for FastAPI's async code), `database_url_sync` uses plain `postgresql://` (for Celery workers which are synchronous). Same database, different drivers.

2. **`api_keys: str = ""`** — Not a list, a comma-separated string. Parsed at runtime by `_configured_api_keys()`. Empty default means no keys accepted until you set `API_KEYS=dev-local-only` in the environment.

3. **`embedding_mode: str = "model"`** — Controls whether the 100MB sentence-transformers model is loaded (`"model"`) or a microsecond-fast hash-based fallback is used (`"hash"`). The `"hash"` mode exists for CI, load testing, and CPU-only environments.

4. **`model_config = {"env_file": ".env", "extra": "ignore"}`** — Tells pydantic-settings to read from a `.env` file AND ignore any extra env vars that don't match a field. This prevents crashes when unrelated env vars are present.

`★ Insight ─────────────────────────────────────`
**Why `extra = "ignore"` matters:** Without this, pydantic-settings raises `ValidationError` for any environment variable that doesn't match a `Settings` field. In Docker, there are dozens of system env vars (`PATH`, `HOME`, etc.) — `"ignore"` prevents them from crashing the app.
`─────────────────────────────────────────────────`

---

## 📝 Code Walkthrough: `api/dependencies.py`

This file creates the database engine, Redis client, and auth dependencies that FastAPI injects into route handlers.

### Database Engine Setup

```python
# api/dependencies.py, Lines 18-28
try:
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=20,        # 20 persistent connections
        max_overflow=30,     # 30 additional connections under load
    )
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
except ModuleNotFoundError:
    engine = None
    async_session_factory = None
```

🔍 **Key details:**
- `pool_size=20, max_overflow=30` — Under normal load, 20 connections are maintained. Under burst load, up to 50 total (20 + 30) can be created. These were tuned during load testing (Phase 5).
- `expire_on_commit=False` — After committing, SQLAlchemy normally expires all loaded attributes (forcing a re-query on next access). Disabling this avoids unnecessary round-trips when you read attributes after commit.
- `try/except ModuleNotFoundError` — The app starts even without `asyncpg` installed. This enables running unit tests without a database driver.

### Redis Client with Fallback

```python
# api/dependencies.py, Lines 30-42
class NullRedisClient:
    """No-op Redis client used when redis-py is unavailable."""
    async def aclose(self) -> None:
        """Match the redis client close contract."""

redis_client = (
    aioredis.from_url(settings.redis_url, decode_responses=True)
    if aioredis is not None
    else NullRedisClient()
)
```

The `NullRedisClient` exists so the app runs without Redis. In the rate limiter, a `NullRedisClient` causes rate limiting to be skipped (fail-open). This means:
- **With Redis:** Rate limiting is enforced, caching works
- **Without Redis:** No rate limiting, no caching, but the app still serves requests

### The Auth Dependency Chain

```python
# api/dependencies.py — Three auth levels

# Level 1: require_api_key() — checks settings.api_keys OR api_keys table
async def require_api_key(x_api_key: str | None = Header(...)) -> str:
    # Check config-based keys first (fast path)
    # Then check DB-backed keys (slow path)
    # Raise 401 if neither matches

# Level 2: require_admin_api_key() — checks settings.admin_api_keys
async def require_admin_api_key(x_api_key: str | None = Header(...)) -> str:
    # If ADMIN_API_KEYS is empty, falls back to require_api_key()
    # This keeps local dev simple — any valid key is also admin
```

`★ Insight ─────────────────────────────────────`
**The admin fallback pattern:** `require_admin_api_key()` falls back to `require_api_key()` when `ADMIN_API_KEYS` is empty. This is a deliberate dev-experience choice: in local development, you don't want to manage separate admin keys. In production, you set `ADMIN_API_KEYS` and the fallback is disabled. This avoids the "I can't test revocation locally" problem.
`─────────────────────────────────────────────────`

---

## 📝 Code Walkthrough: `api/main.py`

```python
# api/main.py — The entire app entry point (40 lines)
"""FastAPI app entry point for AgentLedger Layer 1."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from api.dependencies import engine, redis_client
from api.ratelimit import RateLimitMiddleware
from api.routers import health, identity, manifests, ontology, search, services, verify


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    yield
    if engine is not None:
        await engine.dispose()
    await redis_client.aclose()


app = FastAPI(
    title="AgentLedger",
    description="Manifest Registry -- Discovery & Distribution for the Agent Web",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)

# Mount all routers under /v1
app.include_router(health.router, prefix="/v1", tags=["health"])
app.include_router(ontology.router, prefix="/v1", tags=["ontology"])
app.include_router(manifests.router, prefix="/v1", tags=["manifests"])
app.include_router(services.router, prefix="/v1", tags=["services"])
app.include_router(search.router, prefix="/v1", tags=["search"])
app.include_router(verify.router, prefix="/v1", tags=["verification"])
app.include_router(identity.router, prefix="/v1", tags=["identity"])
```

🔍 **Key patterns:**
- **Lifespan context manager** — The `yield` separates startup from shutdown. The shutdown code (`engine.dispose()`, `redis_client.aclose()`) runs when the app is stopping. This properly closes all database connections and Redis connections.
- **All routers under `/v1`** — Versioned API prefix. When v2 is built, it can coexist at `/v2`.
- **Middleware added before routers** — `RateLimitMiddleware` wraps every request. It runs before any router handler is called.

---

## 🧪 Hands-On Exercises

### 🔬 Exercise 1: Check Current Settings

```python
# In a Python shell or test file
import os
os.environ["API_KEYS"] = "test-key"
from api.config import Settings
s = Settings()
print(f"DB: {s.database_url}")
print(f"Embedding mode: {s.embedding_mode}")
print(f"Rate limit: {s.ip_rate_limit} req/{s.ip_rate_window_seconds}s")
```

### 🔬 Exercise 2: Switch Embedding Modes

```powershell
# Start with hash mode (fast, no model download)
$env:EMBEDDING_MODE='hash'
docker compose up -d --build app

# Verify in logs
docker compose logs app | Select-String "EMBEDDING_MODE"
```

### 🔬 Exercise 3: Test Auth Rejection

```powershell
# No key
curl http://localhost:8000/v1/ontology
# Expected: {"detail":"missing X-API-Key header"}

# Wrong key
curl -H "X-API-Key: wrong-key" http://localhost:8000/v1/ontology
# Expected: {"detail":"invalid API key"}

# Correct key
curl -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
# Expected: 200 with ontology data
```

---

## 📚 Interview Prep

**Q: How does FastAPI dependency injection work in this project?**

**A:** Route handlers declare dependencies using `Depends()`. For example, `db: AsyncSession = Depends(get_db)` tells FastAPI to call `get_db()` before the handler runs and pass the yielded session as the `db` parameter. The session is automatically cleaned up after the handler returns (or raises). This is the same pattern for Redis (`Depends(get_redis)`) and auth (`Depends(require_api_key)`). Dependencies can be stacked — a router can have `dependencies=[Depends(require_api_key)]` to protect all its routes at once.

---

**Q: Why are there two database URL settings?**

**A:** FastAPI uses async SQLAlchemy with the `asyncpg` driver (`postgresql+asyncpg://`), while Celery workers are synchronous processes that use `psycopg2` (`postgresql://`). Same database, different Python drivers. The sync URL is also used by the ontology seed script and Alembic migrations.

---

## 🎯 Key Takeaways

- All configuration flows through `api/config.py` using `pydantic-settings`
- Environment variables override `.env` file values (standard 12-factor app pattern)
- `dependencies.py` creates DB engine, Redis client, and auth guards
- Four auth surfaces: none (health), API key (registry routes), admin API key (admin actions), bearer VC (agent session flows)
- `NullRedisClient` allows the app to run without Redis (fail-open)
- `EMBEDDING_MODE=hash` skips the 100MB model download for CI/testing

---

## 📋 Summary Reference Card

| Setting | Env Var | Default | Purpose |
|---------|---------|---------|---------|
| `database_url` | `DATABASE_URL` | `postgresql+asyncpg://...@db:5432/agentledger` | Async DB connection |
| `redis_url` | `REDIS_URL` | `redis://redis:6379/0` | Cache + rate limiting |
| `api_keys` | `API_KEYS` | `""` (empty) | Comma-separated accepted keys |
| `admin_api_keys` | `ADMIN_API_KEYS` | `""` (empty) | Admin-scoped keys |
| `ip_rate_limit` | `IP_RATE_LIMIT` | `100` | Per-IP req/window |
| `ip_rate_window_seconds` | `IP_RATE_WINDOW_SECONDS` | `60` | Window duration |
| `issuer_did` | `ISSUER_DID` | `"did:web:agentledger.io"` | VC issuer DID |
| `session_assertion_ttl_seconds` | `SESSION_ASSERTION_TTL_SECONDS` | `300` | Default session assertion lifetime |
| `authorization_webhook_url` | `AUTHORIZATION_WEBHOOK_URL` | `""` | Optional HITL webhook target |
| `embedding_mode` | `EMBEDDING_MODE` | `"model"` | `model` or `hash` |

---

## 🚀 Ready for Lesson 04?

Next up, we'll explore **The Blueprints** — the Pydantic data models that validate every request before it reaches business logic. Get ready to see how type safety prevents bad data from ever hitting the database! 📐

*Remember: Configuration is not glamorous, but every production outage starts with a misconfigured setting!* 🛡️
