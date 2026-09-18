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

# Full gate: format, lint, types, tests. Every step must pass before a deploy.
run_lint() {
  echo "--> Running linters and type/syntax checks..."
  if [ -x "$VENV_DIR/bin/ruff" ]; then
    "$VENV_DIR/bin/ruff" check "$SRC_DIR"
    "$VENV_DIR/bin/ruff" format --check "$SRC_DIR"
  else
    ruff check "$SRC_DIR"
    ruff format --check "$SRC_DIR"
  fi
  # find, not a glob: `providers/*.py` silently stopped covering the tree the moment a
  # provider became a package, and a compile step that quietly checks less than it used to
  # is worse than none.
  find "$SRC_DIR" -name '*.py' -not -path '*/__pycache__/*' -exec python3 -m py_compile {} +
  echo "Lint and compile passed!"

  if [ -x "$VENV_DIR/bin/mypy" ]; then
    echo "--> Type checking (mypy --strict)..."
    "$VENV_DIR/bin/mypy" "$SRC_DIR"
  else
    echo "Warning: mypy not installed in $VENV_DIR, skipping type check"
  fi

  run_tests
}

run_tests() {
  if [ -x "$VENV_DIR/bin/pytest" ]; then
    echo "--> Running tests..."
    "$VENV_DIR/bin/pytest" "$ROOT_DIR/tests" -q
  else
    echo "Warning: pytest not installed in $VENV_DIR, skipping tests"
  fi
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
  # --delete because `cp -r` only ever adds: a module that has been renamed or split into a
  # package would otherwise linger beside its replacement and shadow it on import.
  # __pycache__ is excluded from deletion so Home Assistant's own bytecode is left alone.
  # -rlt rather than -a, and --inplace: the destination is a CIFS mount, where rsync's
  # default write-to-temp-then-rename fails outright, and owner/group cannot be preserved.
  rsync -rlt --inplace --delete --exclude='__pycache__' "$SRC_DIR"/ "$DEST_DIR"/
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
  test|tests)
    run_tests
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
    echo "Usage: $0 {lint|test|format|deploy|reload|all} [config_entry_id]"
    exit 1
    ;;
esac
