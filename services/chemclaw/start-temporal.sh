#!/usr/bin/env bash
# Temporal dev server (all-in-one: frontend + history + matching).
# Binds the gRPC frontend on 7233 (matches CHEMCLAW_TEMPORAL_ADDRESS=localhost:7233).
# The built-in web UI is served on 8233.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPORAL="$SCRIPT_DIR/.bin/temporal"

echo "Starting Temporal dev server (frontend=7233, ui=8233)..."
exec "$TEMPORAL" server start-dev \
  --port 7233 \
  --ui-port 8233 \
  --namespace default
