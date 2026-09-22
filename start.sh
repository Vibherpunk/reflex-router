#!/bin/bash
# Start OpenCode-Go System 1 Intelligent Router Proxy
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

PORT=${PORT:-8787}
HOST=${HOST:-127.0.0.1}

echo "Starting System 1 Router on http://${HOST}:${PORT}..."
exec python3 -m uvicorn server:app --host "$HOST" --port "$PORT" --log-level info
