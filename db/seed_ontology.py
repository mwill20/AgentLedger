"""Seed the ontology_tags table from ontology/v0.1.json.

Usage:
    python db/seed_ontology.py

Idempotent — safe to re-run. Uses upsert (INSERT ... ON CONFLICT UPDATE).
"""

import json
import os
import sys

import psycopg2


def get_database_url() -> str:
    """Get sync database URL from environment."""
    url = os.getenv("DATABASE_URL_SYNC")
    if not url:
        url = os.getenv("DATABASE_URL", "")
        # Convert asyncpg URL to psycopg2 format if needed
        url = url.replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        print("ERROR: DATABASE_URL_SYNC or DATABASE_URL must be set")
        sys.exit(1)
    return url


def seed_ontology(ontology_path: str = None) -> int:
    """Load ontology tags from JSON and upsert into database.

    Returns the number of tags upserted.
    """
    if ontology_path is None:
        # Resolve relative to project root
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ontology_path = os.path.join(project_root, "ontology", "v0.1.json")

    with open(ontology_path, "r") as f:
        data = json.load(f)

    tags = data["tags"]
    db_url = get_database_url()

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    upsert_sql = """
        INSERT INTO ontology_tags (tag, domain, function, label, description, sensitivity_tier)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (tag) DO UPDATE SET
            domain = EXCLUDED.domain,
            function = EXCLUDED.function,
            label = EXCLUDED.label,
            description = EXCLUDED.description,
            sensitivity_tier = EXCLUDED.sensitivity_tier;
    """

    count = 0
    for t in tags:
        cur.execute(upsert_sql, (
            t["tag"],
            t["domain"],
            t["function"],
            t["label"],
            t["description"],
            t["sensitivity_tier"],
        ))
        count += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"Seeded {count} ontology tags from {ontology_path}")
    return count


if __name__ == "__main__":
    seed_ontology()
