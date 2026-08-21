#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBAPP_DIR="$ROOT_DIR/webapp"
STATIC_INDEX="$ROOT_DIR/src/aimusic/server/static/index.html"
BUILD_MODE="auto"
DVC_MODE="auto"
SERVER_URL="http://localhost:8000"
APP_URL="$SERVER_URL/app/"

usage() {
  cat <<USAGE
Usage: scripts/dev-server.sh [--force-build | --skip-build] [--skip-dvc]

Options:
  --force-build   Always rebuild the Web UI before starting the server.
  --skip-build    Do not attempt to build the Web UI (assumes assets already exist).
  --skip-dvc      Do not run 'dvc pull' before starting (assumes score/data
                  artifacts already exist).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-build)
      BUILD_MODE="force"
      shift
      ;;
    --skip-build)
      BUILD_MODE="skip"
      shift
      ;;
    --skip-dvc)
      DVC_MODE="skip"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# The `live` extra pulls in python-rtmidi, the backend mido needs to see
# real MIDI ports (e.g. a Yamaha over USB/Core MIDI). Without it,
# GET /api/midi/devices silently returns empty input/output lists instead
# of erroring, which looks like a hardware/driver problem but isn't.
echo "[dev-server] Syncing Python deps (dev + live + audio extras)..."
cd "$ROOT_DIR"
uv sync --extra dev --extra live --extra audio

if [[ "$DVC_MODE" == "skip" ]]; then
  echo "[dev-server] Skipping dvc pull as requested."
else
  echo "[dev-server] Pulling DVC-managed score/data artifacts..."
  if ! (cd "$ROOT_DIR" && uv run dvc pull); then
    echo "[dev-server] Warning: 'dvc pull' did not complete cleanly (DVC remote may not be available)." >&2
    echo "[dev-server] Continuing with existing local artifacts..." >&2
  fi
fi

maybe_install_deps() {
  if [[ ! -d "$WEBAPP_DIR/node_modules" ]]; then
    echo "[dev-server] Installing webapp dependencies..."
    (cd "$WEBAPP_DIR" && npm install)
  fi
}

needs_build() {
  if [[ ! -f "$STATIC_INDEX" ]]; then
    return 0
  fi
  if find "$WEBAPP_DIR/src" -type f -newer "$STATIC_INDEX" | grep -q .; then
    return 0
  fi
  return 1
}

build_webapp() {
  maybe_install_deps
  echo "[dev-server] Building webapp..."
  (cd "$WEBAPP_DIR" && npm run build)
}

case "$BUILD_MODE" in
  force)
    build_webapp
    ;;
  skip)
    echo "[dev-server] Skipping webapp build as requested."
    ;;
  auto)
    maybe_install_deps
    if needs_build; then
      build_webapp
    else
      echo "[dev-server] Webapp assets are up to date."
    fi
    ;;
esac

echo "[dev-server] Starting FastAPI server..."
uv run python -m aimusic.server.app &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "[dev-server] Waiting for the server to become ready..."
ready=0
for _ in {1..60}; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[dev-server] Server process exited before becoming ready; see log above." >&2
    wait "$SERVER_PID"
    exit $?
  fi
  if curl -sf -o /dev/null "$SERVER_URL/"; then
    ready=1
    break
  fi
  sleep 0.5
done

if [[ "$ready" -eq 1 ]]; then
  echo "[dev-server] Ready at $APP_URL"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    open "$APP_URL" || true
  else
    echo "[dev-server] Open $APP_URL in your browser."
  fi
else
  echo "[dev-server] Server did not report ready within 30s; check the log above." >&2
  echo "[dev-server] It may still come up -- open $APP_URL manually once it does." >&2
fi

# Keep running in the foreground so Ctrl-C stops the server, same as before.
wait "$SERVER_PID"
