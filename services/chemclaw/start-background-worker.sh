#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Bridge Anthropic env vars (same as start.sh)
export ANTHROPIC_API_KEY="${AI_INTEGRATIONS_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
export ANTHROPIC_BASE_URL="${AI_INTEGRATIONS_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-}}"
export CHEMCLAW_POSTGRES_DSN="${DATABASE_URL:-postgresql://localhost/chemclaw}"

echo "Starting Chemclaw3 background worker (ELN sync, BO campaigns, approvals)..."
echo "  Temporal: ${CHEMCLAW_TEMPORAL_ADDRESS:-localhost:7233}"
echo "  ELN dir : ${CHEMCLAW_ELN_EXPORT_DIR:-eln/exports}"

exec "$SCRIPT_DIR/.venv/bin/python" -m workers.background_worker
