# llm_ui

UI-side ROS2 package: a FastAPI/rclpy bridge (`bridge_node`) that serves the map viewer +
chat frontend over HTTP/WebSocket, and translates browser actions into ROS2 topics defined
by [`ui_interfaces`](../ui_interfaces). This package owns the UI side only — the LLM
side (`mobile_llm_node`) and the simulation/execution side are separate packages that only
need to agree on the message contract below.

```
 browser (frontend/) <--WebSocket/HTTP--> bridge_node <--ROS2 topics--> LLM side / simulation side
```

## Topics

All message types live in `ui_interfaces`. `bridge_node` is the only node in this package
that touches ROS2 — it's both the sole publisher and sole subscriber on every topic below.

### LLM communication (UI <-> LLM side)

| Topic | Message | Direction | Purpose |
|---|---|---|---|
| `/llm_ui/chat_request` | `ChatRequest` | bridge_node publishes | every user message, chat or command |
| `/llm_ui/chat_response` | `ChatResponse` | bridge_node subscribes | plain chat reply |
| `/llm_ui/ctrl_response` | `CtrlResponse` | bridge_node subscribes | proposed action that needs human approval (renders an Allow button in the UI) |

**`ChatRequest`** — published once per user message. `checked_pos_list` is empty for plain
chat; when the user has marked one or more points on the map (A, B, C, ...), it's a command
and those points are attached in click order.

```
string id                        # unique id (uuid4), echoed back in the matching response
string text                      # user's message text

BasePose base_pose               # robot base pose at request time (placeholder, see Known placeholders)
EEPose manipulator_ee_pos        # manipulator end-effector pose at request time (placeholder, see Known placeholders)
NamedPoint[] checked_pos_list    # points marked on the map; empty for plain chat

# BasePose: mobile robot base, 2D
float64 x
float64 y
float64 yaw

# EEPose: manipulator end-effector, 6DOF as Euler angles
float64 x
float64 y
float64 z
float64 roll
float64 pitch
float64 yaw

# NamedPoint: a map point the user marked, with the heading they dragged out (0 for a plain click)
string label   # e.g. "A", "B", "C"
float64 x      # world x (meters)
float64 y      # world y (meters)
float64 yaw    # heading (radians)
```

**`ChatResponse`** — the LLM side's reply to plain chat. Shown as a normal chat bubble.

```
string id      # same id as the ChatRequest this answers
string text    # LLM's reply text
```

**`CtrlResponse`** — the LLM side sends this *instead of* `ChatResponse` when it's proposing
an action that needs human approval before it's carried out. The UI renders it with an Allow
button; clicking Allow publishes a matching `CtrlCommand` (see below). Which topic the LLM
side answers on is entirely its own decision — `bridge_node` just relays whichever comes back
first for a given request id.

```
string id      # same id as the ChatRequest this answers
string text    # human-readable description of the proposed action
```

### Simulation communication

**UI -> simulation/execution side**

| Topic | Message | Direction | Purpose |
|---|---|---|---|
| `/llm_ui/ctrl_command` | `CtrlCommand` | bridge_node publishes | fires when the user clicks Allow on a `CtrlResponse` |

**`CtrlCommand`** — fire-and-forget; no response is expected back on this channel. Consumed
by whatever executes the approved action (a simulation node, a real robot driver, etc.), not
by the LLM side.

```
string id      # same id as the CtrlResponse that was approved
string text    # the approved action's text, echoed back for convenience
```

**Simulation -> UI**

| Topic | Message | Direction | Purpose |
|---|---|---|---|
| `/robot_current_state` | `std_msgs/Float64MultiArray` | bridge_node subscribes | robot base + end-effector pose, feeds `ChatRequest.base_pose` / `ChatRequest.manipulator_ee_pos` and the map's robot marker |

`/robot_current_state.data` is a flat 9-value array: `data[0:3]` is the base pose
`(x, y, yaw)`, `data[3:9]` is the end-effector pose `(x, y, z, roll, pitch, yaw)`.
`bridge_node` unpacks it into `ui_interfaces/BasePose` and `ui_interfaces/EEPose` internally
(`_on_robot_current_state` in `llm_ui/bridge_node.py`) and keeps the latest value in memory
(`RobotStateStore`, see `llm_ui/robot_state.py`), stamping every outgoing `ChatRequest` with
whatever it currently holds. There's no staleness/timeout tracking — if the simulation never
publishes, or stops publishing, the store just keeps whatever it last had. Messages with fewer
than 9 values are logged and ignored.

### Known placeholders

`base_pose` and `manipulator_ee_pos` are seeded from [`state/robot_state.json`](state/robot_state.json)
(dummy values) at startup and updated in place by the subscriptions above. Nothing publishes
on those topics yet, so in practice the dummy seed is what ships in every `ChatRequest` and
what the map's robot marker shows, until a real simulation node exists.

## Map interaction (frontend)

- **Left click**: drop a point (A, B, C, ...) with yaw 0.
- **Left click + drag**: drop a point and set its yaw by dragging in the desired heading
  direction; released direction becomes the arrow you see. This is what fills in `NamedPoint.yaw`.
- **Right click + drag**: pan the view.
- **Scroll**: zoom.
- **Reset points** button: clears all marked points (in case an arrow was dragged wrong) without
  touching pan/zoom.

## Non-ROS2 surface (browser <-> bridge_node)

Not part of the ROS2 contract, but documented here since it's the other half of this package:

- `GET /api/map?dir=<path>` — map metadata (resolution, origin, size, negate, thresholds) read
  from `<dir>/*.yaml`; defaults to `map/demo` if `dir` is omitted.
- `GET /api/map/image?dir=<path>` — the map's PNG.
- `GET /api/robot_state` — the current `RobotStateStore` contents as JSON
  (`{"base_pose": {...}, "manipulator_ee_pos": {...}}`); the map view polls this every 1s to
  draw the robot marker.
- `WS /ws/chat` — one message in ⇄ one message out per exchange:
  - browser -> bridge: `{"type": "chat", "text": "...", "points": [{"label": "A", "x": 1.0, "y": 2.0}, ...]}`
  - browser -> bridge: `{"type": "allow", "id": "<ctrl_response id>", "text": "..."}`
  - bridge -> browser: `{"id": "...", "text": "...", "kind": "chat" | "ctrl"}`

## Requirements

- ROS2 Humble (sourced from `/opt/ros/humble/setup.bash`)
- Python 3.10 (matches Humble's rclpy build — see repo-level conda env `llm_ui`)
- Node.js/npm (for the frontend dev server)
- Python packages: see [`requirements.txt`](requirements.txt) (`pip install -r requirements.txt`)
- Frontend packages: `npm install` inside `frontend/`

## Running

From the repo root (`/home/jh/LLM_UI`), one-time setup:

```bash
source /opt/ros/humble/setup.bash
conda activate llm_ui              # or your equivalent Python 3.10 env
pip install -r src/llm_ui/requirements.txt
npm install --prefix src/llm_ui/frontend
colcon build
```

Then, in two terminals:

```bash
# terminal 1 (UI side: bridge_node + frontend dev server together)
./run.sh

# terminal 2 (the real LLM node)
./run_mobile_llm.sh
```

Open http://localhost:5173. Ctrl-C in terminal 1 stops `bridge_node` and the frontend dev
server together.

### Running bridge_node directly (without the launch file)

```bash
source install/setup.bash
ros2 run llm_ui bridge_node --port 8000 --default-map-dir /path/to/map/dir --state-file /path/to/robot_state.json
```

`--default-map-dir` defaults to auto-detecting `src/llm_ui/map/demo`; every request can still
override it with `?dir=`. `--state-file` defaults to auto-detecting
`src/llm_ui/state/robot_state.json` (the dummy seed values for `RobotStateStore`).

### Launch file arguments

```bash
ros2 launch llm_ui llm_ui.launch.py bridge_port:=8000 frontend_port:=5173 default_map_dir:=""
```
