#!/bin/bash
# Start OpenCode-Go System 1 Intelligent Router Proxy
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Source user environment if present
[ -f "$HOME/.env" ] && export $(grep -v '^#' "$HOME/.env" | xargs)
[ -f "$HOME/.hermes/.env" ] && export $(grep -v '^#' "$HOME/.hermes/.env" | xargs)

PORT=${PORT:-8787}
HOST=${HOST:-127.0.0.1}

echo "Starting System 1 Router on http://${HOST}:${PORT}..."
exec python3 -m uvicorn server:app --host "$HOST" --port "$PORT" --log-level info
