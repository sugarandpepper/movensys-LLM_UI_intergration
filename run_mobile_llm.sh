#!/usr/bin/env bash
# Runs the real LLM node (Mobile_LLM's chat_v2 model) in its own terminal,
# separate from run.sh (the UI side). This replaces run_llm_mock.sh once the
# real model is ready -- both only need to agree on ui_interfaces.
#
# Uses the system Python 3.10 directly (matches ROS2 Humble's rclpy build) --
# no conda env. This node needs torch/transformers/peft/bitsandbytes (see
# ../Mobile_LLM/requirements.txt) to load Qwen2.5-7B-Instruct + the
# qwen-robot-lora-v2 adapter. If you do use a conda/venv env for this,
# activate it before running this script instead. Requires:
#   - `colcon build` already run once (needs empy/lark/catkin_pkg/
#     colcon-common-extensions) so mobile_llm_node and ui_interfaces are
#     present under install/
#   - a local Ollama server running with `ollama pull qwen2.5:7b` (used for
#     chat/command classification and plain chat replies)
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source /opt/ros/humble/setup.bash
source "$REPO_ROOT/install/setup.bash"

exec ros2 run mobile_llm_node mobile_llm_node
