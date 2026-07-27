#!/usr/bin/env bash
# Stops everything started by run.sh / run_mobile_llm.sh: the ros2 launch wrapper,
# bridge_node, the frontend vite dev server, and mobile_llm_node.
PATTERNS=(
  "ros2 launch llm_ui llm_ui.launch.py"
  "ros2 run mobile_llm_node mobile_llm_node"
  "lib/llm_ui/bridge_node"
  "lib/mobile_llm_node/mobile_llm_node"
  "llm_ui.bridge_node"
  "cloudflared tunnel"
  "vite --port"
)

found=0
for pattern in "${PATTERNS[@]}"; do
  pids=$(pgrep -f -- "$pattern" || true)
  if [ -n "$pids" ]; then
    echo "killing '$pattern': $pids"
    kill -9 $pids 2>/dev/null
    found=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "nothing running."
fi
