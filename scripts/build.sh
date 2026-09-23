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
# Tools are invoked as `python -m <tool>` rather than through their console scripts.
# Those scripts carry an absolute shebang baked in at creation, so renaming the project
# directory left every one of them pointing at a path that no longer exists -- and the gate
# then died with "bad interpreter" partway through instead of reporting on the code.
VENV_PY="$VENV_DIR/bin/python"

# A missing tool fails the gate rather than skipping it. A gate that quietly checks less
# than it claims to reports green for work nobody type-checked.
run_tool() {
  local tool="$1"
  shift
  if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: no interpreter at $VENV_PY - run '$0 setup' or recreate the venv" >&2
    exit 1
  fi
  if ! "$VENV_PY" -c "import $tool" 2>/dev/null; then
    echo "ERROR: $tool is not installed in $VENV_DIR" >&2
    exit 1
  fi
  "$VENV_PY" -m "$tool" "$@"
}

run_lint() {
  echo "--> Running linters and type/syntax checks..."
  run_tool ruff check "$SRC_DIR"
  run_tool ruff format --check "$SRC_DIR"
  # find, not a glob: `providers/*.py` silently stopped covering the tree the moment a
  # provider became a package, and a compile step that quietly checks less than it used to
  # is worse than none.
  find "$SRC_DIR" -name '*.py' -not -path '*/__pycache__/*' -exec python3 -m py_compile {} +
  echo "Lint and compile passed!"

  echo "--> Type checking (mypy --strict)..."
  run_tool mypy "$SRC_DIR"

  run_tests
}

run_tests() {
  echo "--> Running tests..."
  run_tool pytest "$ROOT_DIR/tests" -q
}

run_format() {
  echo "--> Formatting code..."
  run_tool ruff check --fix "$SRC_DIR"
  run_tool ruff format "$SRC_DIR"
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
