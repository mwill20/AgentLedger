"""FastAPI app entry point for AgentLedger Layer 1."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.dependencies import engine, redis_client
from api.routers import health, ontology


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Startup: verify connections
    yield
    # Shutdown: close connections
    await engine.dispose()
    await redis_client.aclose()


app = FastAPI(
    title="AgentLedger",
    description="Manifest Registry — Discovery & Distribution for the Agent Web",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount all routers under /v1
app.include_router(health.router, prefix="/v1", tags=["health"])
app.include_router(ontology.router, prefix="/v1", tags=["ontology"])
