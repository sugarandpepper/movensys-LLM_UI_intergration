#!/usr/bin/env bash
# Single entry point for the UI side.
#
# Default: builds (if needed) and launches bridge_node + the frontend dev
# server together. Open http://localhost:5173 once it's up. Ctrl-C stops both.
# Fine for localhost/your own LAN; not hardened for untrusted networks (no
# auth, dev-only server).
#
#   ./run.sh --external
#
# exposes the UI to any network (school, mobile data, etc.) with no router
# configuration needed: builds the frontend once, runs bridge_node alone
# serving the built static files + API + WebSockets from a single port behind
# HTTP Basic Auth (credentials from .env.external -- copy
# .env.external.example and fill it in first), and opens a Cloudflare quick
# tunnel to it, printing the public URL once it's ready. Quick tunnels need no
# Cloudflare account, but the URL changes every restart and Cloudflare gives
# no uptime guarantee -- fine for demos/testing, not for anything that needs
# to stay at a fixed address.
#
# The LLM side runs separately, on purpose (see run_llm_mock.sh or your own
# LLM node package) -- that boundary is what ui_interfaces is for.
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Uses the system Python 3.10 directly (matches ROS2 Humble's rclpy build) --
# no conda env. If you do use a conda/venv env for this, activate it before
# running this script instead.
source /opt/ros/humble/setup.bash
source "$REPO_ROOT/install/setup.bash"

# npm (for the frontend dev server / build) lives in nvm, not the system PATH.
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

if [ "${1:-}" = "--external" ]; then
  shift

  if [ ! -f "$REPO_ROOT/.env.external" ]; then
    echo "Missing $REPO_ROOT/.env.external -- copy .env.external.example and set real credentials first." >&2
    exit 1
  fi
  set -a
  source "$REPO_ROOT/.env.external"
  set +a
  if [ -z "$LLM_UI_AUTH_USER" ] || [ -z "$LLM_UI_AUTH_PASS" ]; then
    echo "LLM_UI_AUTH_USER / LLM_UI_AUTH_PASS must both be set in .env.external -- refusing to run unauthenticated and externally-reachable." >&2
    exit 1
  fi

  echo "Building frontend (npm run build)..."
  npm install --prefix "$REPO_ROOT/src/llm_ui/frontend"
  npm run build --prefix "$REPO_ROOT/src/llm_ui/frontend"

  # Standalone binary, no root/package manager needed -- downloaded once to
  # ~/.local/bin and reused on every later run.
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared not found -- downloading standalone binary to ~/.local/bin ..."
    mkdir -p "$HOME/.local/bin"
    curl -sL -o "$HOME/.local/bin/cloudflared" \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x "$HOME/.local/bin/cloudflared"
    export PATH="$HOME/.local/bin:$PATH"
  fi

  BRIDGE_PORT="${LLM_UI_EXTERNAL_PORT:-8080}"
  CLOUDFLARED_LOG="$(mktemp)"

  cleanup() {
    echo "Stopping bridge_node + cloudflared tunnel..."
    [ -n "${BRIDGE_PID:-}" ] && kill "$BRIDGE_PID" 2>/dev/null
    [ -n "${CLOUDFLARED_PID:-}" ] && kill "$CLOUDFLARED_PID" 2>/dev/null
    rm -f "$CLOUDFLARED_LOG"
  }
  trap cleanup EXIT

  echo "Starting bridge_node on port $BRIDGE_PORT (built frontend + API, Basic Auth required)..."
  # Invoked as a module (not `ros2 run`) so relative import resolution matches
  # the other scripts here -- no conda env involved on this machine, so the
  # system python3 already picked up via install/setup.bash is fine directly.
  python3 -m llm_ui.bridge_node --port "$BRIDGE_PORT" "$@" &
  BRIDGE_PID=$!

  echo "Starting Cloudflare quick tunnel..."
  cloudflared tunnel --url "http://localhost:$BRIDGE_PORT" > "$CLOUDFLARED_LOG" 2>&1 &
  CLOUDFLARED_PID=$!

  URL=""
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$CLOUDFLARED_LOG" | head -n1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done

  echo
  echo "=================================================================="
  if [ -n "$URL" ]; then
    echo " External URL:  $URL"
    echo " Login:         $LLM_UI_AUTH_USER / (password from .env.external)"
  else
    echo " Tunnel URL not detected after 30s -- check $CLOUDFLARED_LOG"
  fi
  echo " Quick tunnel: no account needed, but the URL changes on every"
  echo " restart of this script, and Cloudflare gives no uptime guarantee."
  echo "=================================================================="
  echo

  wait "$BRIDGE_PID" "$CLOUDFLARED_PID"
else
  exec ros2 launch llm_ui llm_ui.launch.py "$@"
fi
