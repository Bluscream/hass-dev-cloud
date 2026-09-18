#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$ROOT_DIR/custom_components/dev_cloud"
DEST_DIR="/var/mnt/hass/custom_components/dev_cloud"
VENV_DIR="$ROOT_DIR/.venv"

ACTION="${1:-all}"

echo "=== [dev_cloud build & dev tool] (Action: $ACTION) ==="

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
  fi
}

run_lint() {
  echo "--> Running linters and type/syntax checks..."
  if [ -x "$VENV_DIR/bin/ruff" ]; then
    "$VENV_DIR/bin/ruff" check "$SRC_DIR"
    "$VENV_DIR/bin/ruff" format --check "$SRC_DIR"
  else
    ruff check "$SRC_DIR"
    ruff format --check "$SRC_DIR"
  fi
  python3 -m py_compile "$SRC_DIR"/*.py "$SRC_DIR"/providers/*.py
  echo "Lint and compile passed!"
}

run_format() {
  echo "--> Formatting code..."
  if [ -x "$VENV_DIR/bin/ruff" ]; then
    "$VENV_DIR/bin/ruff" check --fix "$SRC_DIR"
    "$VENV_DIR/bin/ruff" format "$SRC_DIR"
  else
    ruff check --fix "$SRC_DIR"
    ruff format "$SRC_DIR"
  fi
}

deploy_live() {
  echo "--> Deploying files to Home Assistant SMB share ($DEST_DIR)..."
  mkdir -p "$DEST_DIR"
  cp -r "$SRC_DIR"/* "$DEST_DIR"/
  echo "Files deployed!"
}

reload_ha() {
  local entry_id="${2:-}"
  echo "--> Reloading dev_cloud in Home Assistant..."
  source ~/.config/environment.d/20-hass.conf 2>/dev/null || true
  
  if [ -z "${HASS_TOKEN:-}" ]; then
    echo "Warning: HASS_TOKEN not set, skipping API reload"
    return 0
  fi

  local base_url="${HASS_SERVER:-http://192.168.2.4:8123}"
  base_url="${base_url%/}"

  if [ -n "$entry_id" ]; then
    echo "Reloading single entry: $entry_id"
    curl -s -X POST \
      -H "Authorization: Bearer $HASS_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"entry_id\": \"$entry_id\"}" \
      "$base_url/api/services/homeassistant/reload_config_entry" | jq -c . || true
  else
    # Find all dev_cloud entries
    if [ -f "/var/mnt/hass/.storage/core.config_entries" ]; then
      local entries
      entries=$(python3 -c "
import json
with open('/var/mnt/hass/.storage/core.config_entries') as f:
    d = json.load(f)
for e in d.get('data', {}).get('entries', []):
    if e.get('domain') == 'dev_cloud':
        print(e.get('entry_id'))
")
      for eid in $entries; do
        echo "Reloading entry $eid..."
        curl -s -X POST \
          -H "Authorization: Bearer $HASS_TOKEN" \
          -H "Content-Type: application/json" \
          -d "{\"entry_id\": \"$eid\"}" \
          "$base_url/api/services/homeassistant/reload_config_entry" >/dev/null 2>&1 || true
      done
      echo "All dev_cloud entries reloaded!"
    fi
  fi
}

case "$ACTION" in
  lint)
    run_lint
    ;;
  fmt|format)
    run_format
    ;;
  deploy)
    deploy_live
    ;;
  reload)
    reload_ha "${2:-}"
    ;;
  all|"")
    run_format
    run_lint
    deploy_live
    reload_ha "${2:-}"
    echo "=== Completed successfully ==="
    ;;
  *)
    echo "Usage: $0 {lint|format|deploy|reload|all} [config_entry_id]"
    exit 1
    ;;
esac
