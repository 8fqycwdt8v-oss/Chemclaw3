#!/usr/bin/env bash
# Chemclaw FastAPI front-door starter for Replit.
# Maps Replit AI Integration env vars → standard Anthropic SDK vars,
# and wires DATABASE_URL → CHEMCLAW_POSTGRES_DSN.
set -euo pipefail

# Bridge Replit AI Integration → Anthropic SDK convention
export ANTHROPIC_API_KEY="${AI_INTEGRATIONS_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
export ANTHROPIC_BASE_URL="${AI_INTEGRATIONS_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-}}"

# Wire Replit PostgreSQL → Chemclaw DSN
export CHEMCLAW_POSTGRES_DSN="${DATABASE_URL:-postgresql://localhost/chemclaw}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${CHEMCLAW_SERVICE_HOST:-0.0.0.0}"
PORT="${CHEMCLAW_SERVICE_PORT:-8080}"

echo "Starting Chemclaw service on ${HOST}:${PORT}"
VENV="$SCRIPT_DIR/.venv"
exec "$VENV/bin/python" -m uvicorn service.app:create_app \
  --factory \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info
