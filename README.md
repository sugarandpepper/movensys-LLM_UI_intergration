# LLM_UI

A chat UI for commanding a robot: a map viewer (click to mark points, drag to give them a
heading) on one side, an LLM chat on the other, all wired together over ROS2. See
[`src/llm_ui/README.md`](src/llm_ui/README.md) for the full topic-by-topic breakdown; this
file is just "clone it and get it running."

```
 browser (map + chat) <--WebSocket/HTTP--> bridge_node <--ROS2 topics--> LLM node / simulation
```

- [`src/llm_ui`](src/llm_ui) — UI package: `bridge_node` (FastAPI/rclpy) + the React frontend
- [`src/ui_interfaces`](src/ui_interfaces) — shared ROS2 message contract
- [`src/mobile_llm_node`](src/mobile_llm_node) — real LLM node (Qwen2.5-7B-Instruct +
  qwen-robot-lora-v2-modify, both pulled from HuggingFace at runtime), implementing the same
  `ui_interfaces` contract

## Prerequisites

- Ubuntu 22.04 with **ROS2 Humble** installed at `/opt/ros/humble`
- System **Python 3.10** (matches ROS2 Humble's `rclpy` build) -- no conda/venv needed; these
  scripts source `/opt/ros/humble/setup.bash` directly
- **Node.js / npm** (frontend dev server)
- **[Ollama](https://ollama.com)** running locally, with `qwen2.5:7b` pulled (used by
  `mobile_llm_node` for chat/command classification and plain chat replies)
- For `mobile_llm_node`: a CUDA GPU + torch/transformers/peft/bitsandbytes (see
  `src/mobile_llm_node/requirements.txt`)

## One-time setup

```bash
git clone git@github.com:sugarandpepper/movensys-LLM_UI_intergration.git LLM_UI
cd LLM_UI

source /opt/ros/humble/setup.bash

# Build tooling colcon/rosidl need to generate ui_interfaces' custom messages
pip install empy==3.3.4 lark catkin_pkg colcon-common-extensions numpy

# Runtime deps for bridge_node (FastAPI/WebSocket/map yaml parsing)
pip install -r src/llm_ui/requirements.txt

# Runtime deps for mobile_llm_node (torch/transformers/peft/bitsandbytes/ollama)
pip install -r src/mobile_llm_node/requirements.txt

# Frontend deps
npm install --prefix src/llm_ui/frontend

# Model mobile_llm_node uses for classification/plain chat (LLM commands themselves
# go through Qwen2.5-7B-Instruct + the qwen-robot-lora-v2-modify LoRA, not Ollama)
ollama pull qwen2.5:7b

# Build all ROS2 packages (ui_interfaces, llm_ui, mobile_llm_node)
colcon build
```

Re-run `colcon build` any time you pull changes that touch Python/message files.

## Running

Two terminals, from the repo root:

```bash
# terminal 1 -- UI side: bridge_node + frontend dev server together
./run.sh
```

Open **http://localhost:5173** once it says `Uvicorn running` and `VITE ... ready`.

```bash
# terminal 2 -- the real LLM node
./run_mobile_llm.sh
```

Both scripts source ROS2/the workspace themselves -- no manual `source` needed. Ctrl-C stops
each terminal's own process tree.

To stop everything from a third terminal (e.g. if a port is stuck from a previous run):

```bash
./stop.sh
```

## Exposing it beyond your own machine

`./run.sh` runs the frontend as a **vite dev server** -- fine on localhost or your own LAN, but
not meant to be reachable from untrusted networks (no auth, dev-only hardening). To expose the
UI externally -- any network, no router configuration needed -- use `./run.sh --external` instead:

```bash
cp .env.external.example .env.external
# edit .env.external, set LLM_UI_AUTH_USER / LLM_UI_AUTH_PASS to real credentials

./run.sh --external
```

One command does everything:
1. Builds the frontend (`npm run build`).
2. Starts `bridge_node` alone, serving the built static files + API + WebSockets from one port
   (8080 by default, override with `LLM_UI_EXTERNAL_PORT`), gated behind HTTP Basic Auth using
   the credentials from `.env.external`. Refuses to start if that file is missing or either
   variable is unset.
3. Downloads `cloudflared` (a standalone binary, no root/package manager needed) the first time,
   then opens a Cloudflare quick tunnel to that port and prints the public URL once it's ready:

   ```
   ==================================================================
    External URL:  https://some-random-words.trycloudflare.com
    Login:         movensys / (password from .env.external)
   ==================================================================
   ```

Open that URL from anywhere -- the browser prompts for the Basic Auth username/password once per
session; there's no separate login page. Ctrl-C stops both bridge_node and the tunnel together.

Quick tunnels need no Cloudflare account, but the URL changes every time the script restarts, and
Cloudflare gives no uptime guarantee for them -- fine for demos/testing, not for anything that
needs to stay at a fixed address. `.env.external` is gitignored -- never commit real credentials.

## Verify it's actually talking over ROS2

With both terminals from **Running** still up, in a third terminal:

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 topic list
```

Expected output:

```
/llm_ui/chat_request
/llm_ui/chat_response
/llm_ui/ctrl_command
/llm_ui/ctrl_response
/parameter_events
/robot_current_state
/rosout
```

All of these topics come from `bridge_node` alone (it's both the publisher and subscriber on
each one), so this list looks the same whether or not `run_mobile_llm.sh` is up. If the `/llm_ui/*`
and `/robot_current_state` topics are missing entirely, `run.sh` itself isn't up. To confirm the
LLM node specifically is alive, check the node list instead:

```bash
ros2 node list
```

```
/bridge_node
/mobile_llm_node
```

If `/mobile_llm_node` is missing, `run_mobile_llm.sh` isn't running (or crashed -- check that
terminal's output).
