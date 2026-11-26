#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker was not found. Install/start Docker Desktop, then run this script again." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required (the 'docker compose' command)." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "The Docker engine is not running. Start Docker Desktop, then retry." >&2
  exit 1
fi

generate_token() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  elif command -v python >/dev/null 2>&1; then
    python -c "import secrets; print(secrets.token_urlsafe(32))"
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    echo "Install Python or OpenSSL to generate the API token." >&2
    return 1
  fi
}

if [[ ! -f "$ENV_FILE" ]]; then
  token="${ECONOMIC_API_TOKEN:-}"
  if [[ -z "$token" ]]; then
    token="$(generate_token)"
  fi
  if [[ ${#token} -lt 32 ]]; then
    echo "ECONOMIC_API_TOKEN must contain at least 32 characters." >&2
    exit 1
  fi
  (
    umask 077
    printf 'ECONOMIC_API_TOKEN=%s\n' "$token" > "$ENV_FILE"
    printf 'ECONOMIC_ALLOWED_ORIGINS=%s\n' \
      "${ECONOMIC_ALLOWED_ORIGINS:-https://econcal.forexprostools.com}" >> "$ENV_FILE"
  )
  echo "Created a private .env file and generated/configured the API token."
else
  token="$(sed -n 's/^ECONOMIC_API_TOKEN=//p' "$ENV_FILE" | tail -n 1)"
  if [[ ${#token} -lt 32 ]]; then
    echo "The .env file must contain ECONOMIC_API_TOKEN with at least 32 characters." >&2
    exit 1
  fi
  if ! grep -q '^ECONOMIC_ALLOWED_ORIGINS=' "$ENV_FILE"; then
    printf 'ECONOMIC_ALLOWED_ORIGINS=https://econcal.forexprostools.com\n' >> "$ENV_FILE"
  fi
fi

chmod 600 "$ENV_FILE"

# Use the protected .env values rather than inherited shell variables.
unset ECONOMIC_API_TOKEN ECONOMIC_ALLOWED_ORIGINS

docker compose \
  --project-directory "$SCRIPT_DIR" \
  --env-file "$ENV_FILE" \
  -f "$COMPOSE_FILE" \
  up -d --build

echo "Server started."
echo "API (LAN): http://192.168.1.174:443/api/updates"
echo "API (same computer, DevTools): http://localhost:443/api/updates"
echo "Dashboard: http://192.168.1.174:8080/"
echo "The API token is stored in .env; copy it into your private DevTools script."
