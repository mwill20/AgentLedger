"""GET /ontology endpoint — returns the full capability taxonomy."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db

router = APIRouter()


@router.get("/ontology")
async def get_ontology(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("""
            SELECT tag, domain, function, label, description, sensitivity_tier
            FROM ontology_tags
            ORDER BY domain, function, tag
        """)
    )
    rows = result.fetchall()

    tags = [
        {
            "tag": row.tag,
            "domain": row.domain,
            "function": row.function,
            "label": row.label,
            "description": row.description,
            "sensitivity_tier": row.sensitivity_tier,
        }
        for row in rows
    ]

    # Group by domain for structured response
    domains = {}
    for tag in tags:
        domain = tag["domain"]
        if domain not in domains:
            domains[domain] = []
        domains[domain].append(tag)

    return {
        "ontology_version": "0.1",
        "total_tags": len(tags),
        "domains": list(domains.keys()),
        "tags": tags,
        "by_domain": domains,
    }
