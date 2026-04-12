"""GET /health endpoint."""

from datetime import datetime, timezone

from fastapi import APIRouter

from api.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "version": settings.api_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
