"""FastAPI app entry point for AgentLedger Layer 1."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.dependencies import engine, redis_client
from api.ratelimit import RateLimitMiddleware
from api.routers import (
    attestation,
    audit,
    chain,
    context,
    federation,
    health,
    identity,
    liability,
    manifests,
    ontology,
    search,
    services,
    verify,
    workflows,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Startup: verify connections
    yield
    # Shutdown: close connections
    if engine is not None:
        await engine.dispose()
    await redis_client.aclose()


app = FastAPI(
    title="AgentLedger",
    description="Manifest Registry — Discovery & Distribution for the Agent Web",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting middleware
app.add_middleware(RateLimitMiddleware)

# Mount all routers under /v1
app.include_router(health.router, prefix="/v1", tags=["health"])
app.include_router(ontology.router, prefix="/v1", tags=["ontology"])
app.include_router(manifests.router, prefix="/v1", tags=["manifests"])
app.include_router(services.router, prefix="/v1", tags=["services"])
app.include_router(search.router, prefix="/v1", tags=["search"])
app.include_router(verify.router, prefix="/v1", tags=["verification"])
app.include_router(identity.router, prefix="/v1", tags=["identity"])
app.include_router(attestation.router, prefix="/v1", tags=["attestation"])
app.include_router(audit.router, prefix="/v1", tags=["audit"])
app.include_router(federation.router, prefix="/v1", tags=["federation"])
app.include_router(chain.router, prefix="/v1", tags=["chain"])
app.include_router(context.router, prefix="/v1", tags=["context"])
app.include_router(workflows.router, prefix="/v1", tags=["workflows"])
app.include_router(liability.router, prefix="/v1", tags=["liability"])
