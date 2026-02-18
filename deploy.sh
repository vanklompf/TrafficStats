#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: $ENV_FILE not found. Copy .env.example to .env and configure it." >&2
  exit 1
fi

echo "Building image..."
docker compose --env-file "$ENV_FILE" build

echo "Deploying with docker compose..."
docker compose --env-file "$ENV_FILE" up -d

echo "Done. TrafficStats is running (port 3896)."
