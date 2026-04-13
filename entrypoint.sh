#!/bin/bash
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Seeding ontology..."
python db/seed_ontology.py

echo "Starting AgentLedger API..."
exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}
