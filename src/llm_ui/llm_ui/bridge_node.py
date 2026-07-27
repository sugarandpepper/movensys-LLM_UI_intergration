"""FastAPI + rclpy bridge.

- HTTP: serves map metadata/image read from an arbitrary directory (?dir=...),
  plus the latest known robot state (GET /api/robot_state, one-shot).
- WebSocket /ws/robot_state: pushes the robot state to the browser every time
  /robot_current_state updates, instead of the browser polling for it -- browsers
  throttle setInterval-based polling to ~1/s once a tab is backgrounded/unfocused,
  which made fast polling look choppy. A push isn't subject to that throttling.
- WebSocket /ws/debug: live terminal-style feed of every topic this node touches
  (chat_request/response, ctrl_response/command, robot_current_state), for the
  per-topic debug log panels in the UI. Sends {"topic": ..., "line": ...} JSON
  so the frontend can split traffic into one panel per topic. Push-only and
  unbuffered -- only shows what happens while a client is connected.
  robot_current_state is throttled to at most 5 lines/sec (it publishes at
  ~90Hz from a rosbag/simulation, which would otherwise flood the panel) --
  this only affects what's shown in the debug log, not the actual state store.
- WebSocket /ws/chat: bridges the browser to ROS2, over the channels defined
  by ui_interfaces (the only contract shared with the LLM-side package):

    publish:   /llm_ui/chat_request  (ChatRequest)   -- every user message
    subscribe: /llm_ui/chat_response (ChatResponse)  -- plain chat reply
    subscribe: /llm_ui/ctrl_response (CtrlResponse)  -- proposed action, needs Allow
    publish:   /llm_ui/ctrl_command  (CtrlCommand)    -- sent when the user clicks Allow

  ChatRequest.checked_pos_list is empty for plain chat and carries the map
  points the user attached (A, B, C, ...) when they marked one or more. The
  LLM side decides whether to answer on chat_response or ctrl_response;
  ctrl_response answers get an Allow button in the UI. ctrl_command is
  consumed by the simulation/execution side, not by the LLM side.

- Also subscribes to robot state from the simulation side (see robot_state.py):

    subscribe: /robot_current_state  (std_msgs/Float64MultiArray)
      data[0:3] = base pose   (x, y, yaw)
      data[3:9] = ee pose     (x, y, z, roll, pitch, yaw)

  Starts out as the dummy values in state/robot_state.json and is used as
  ChatRequest.base_pose / ChatRequest.manipulator_ee_pos on every request.
  If the simulation never publishes, the dummy values just stay put -- there
  is no separate "stale" tracking, only "whatever the latest value is."

- If --frontend-dist points at a built frontend (frontend/dist, from `npm run
  build`), it's served as static files -- index.html at "/", assets under it --
  so a single bridge_node process can serve the whole app with no separate
  vite dev server. This is the mode meant for exposing the UI beyond
  localhost/LAN (see ./run.sh --external); the vite dev server is a dev-only tool
  and was never meant to be reachable from untrusted networks.
- If both LLM_UI_AUTH_USER and LLM_UI_AUTH_PASS are set in the environment,
  every request (HTTP and WebSocket alike) requires HTTP Basic Auth with
  those credentials. Unset (the default/local-dev case) means no auth at all --
  only turn this on when actually exposing bridge_node beyond your own machine.

Run with: python3 -m llm_ui.bridge_node --default-map-dir <path>
"""
import argparse
import asyncio
import base64
import hmac
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import rclpy
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
from std_msgs.msg import Float64MultiArray

from ui_interfaces.msg import BasePose, ChatRequest, ChatResponse, CtrlCommand, CtrlResponse, EEPose, NamedPoint

from llm_ui.map_reader import MapNotFoundError, read_map
from llm_ui.robot_state import DEFAULT_STATE_FILE, RobotStateStore

CHAT_REQUEST_TOPIC = '/llm_ui/chat_request'
CHAT_RESPONSE_TOPIC = '/llm_ui/chat_response'
CTRL_RESPONSE_TOPIC = '/llm_ui/ctrl_response'
CTRL_COMMAND_TOPIC = '/llm_ui/ctrl_command'
ROBOT_CURRENT_STATE_TOPIC = '/robot_current_state'


def _find_map_root() -> Path:
    """Locate <repo>/src/llm_ui/map without assuming a fixed directory depth.

    Same reasoning as the old default-map-dir lookup: this file may run from
    the source tree (src/llm_ui/llm_ui/bridge_node.py) or the colcon-installed
    copy (install/llm_ui/lib/python3.X/site-packages/llm_ui/...), which sit at
    different depths. This is the root that ?dir= / --default-map-dir values
    get resolved against when they're not already absolute -- e.g. `demo` ->
    <repo>/src/llm_ui/map/demo -- so a relative value works the same
    regardless of the server process's own CWD.
    """
    for start in (Path(__file__).resolve(), Path.cwd()):
        cur = start
        for _ in range(10):
            for candidate in (cur / 'map', cur / 'src' / 'llm_ui' / 'map'):
                if candidate.is_dir():
                    return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return Path.cwd() / 'map'


def _find_default_frontend_dist() -> Path | None:
    """Locate <repo>/src/llm_ui/frontend/dist (the `npm run build` output), if built.

    Unlike the map dir / state file defaults, there's no reasonable fallback here --
    if it hasn't been built, static serving is simply skipped (the dev server,
    ./run.sh, is the normal way to use this package locally).
    """
    for start in (Path(__file__).resolve(), Path.cwd()):
        cur = start
        for _ in range(10):
            for candidate in (cur / 'frontend' / 'dist', cur / 'src' / 'llm_ui' / 'frontend' / 'dist'):
                if (candidate / 'index.html').is_file():
                    return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


MAP_ROOT = _find_map_root()
DEFAULT_MAP_DIR = MAP_ROOT / 'demo'
DEFAULT_FRONTEND_DIST = _find_default_frontend_dist()


def _format_chat_request(msg: ChatRequest) -> str:
    """Full raw contents of a published ChatRequest, for the debug log -- every
    field actually sent over the topic (not just a summary), since that's the
    whole point of the debug feed: showing what's really on the wire."""
    bp = msg.base_pose
    ee = msg.manipulator_ee_pos
    points = ', '.join(f'{p.label}({p.x:.3f}, {p.y:.3f}, {p.yaw:.3f})' for p in msg.checked_pos_list)
    return (
        f'id={msg.id} text={msg.text!r} '
        f'base_pose=(x={bp.x:.3f}, y={bp.y:.3f}, yaw={bp.yaw:.3f}) '
        f'manipulator_ee_pos=(x={ee.x:.3f}, y={ee.y:.3f}, z={ee.z:.3f}, '
        f'roll={ee.roll:.3f}, pitch={ee.pitch:.3f}, yaw={ee.yaw:.3f}) '
        f'checked_pos_list=[{points}]'
    )


class BridgeNode(Node):
    """Owns the ROS2 side: publishes chat requests / ctrl commands, dispatches responses."""

    def __init__(self, loop: asyncio.AbstractEventLoop, state: RobotStateStore):
        super().__init__('bridge_node')
        self._loop = loop
        self._pending: dict[str, asyncio.Future] = {}
        # ctrl_response id -> resolved_text, so Allow can publish the
        # coordinate-resolved version on ctrl_command instead of trusting
        # whatever label-form text the browser echoes back. Popped on use.
        self._ctrl_resolved_text: dict[str, str] = {}
        self._lock = threading.Lock()
        self.state = state
        self._robot_state_websockets: set[WebSocket] = set()
        self._debug_websockets: set[WebSocket] = set()
        self._last_robot_state_debug_ts = 0.0

        self._chat_req_pub = self.create_publisher(ChatRequest, CHAT_REQUEST_TOPIC, 10)
        self._chat_resp_sub = self.create_subscription(
            ChatResponse, CHAT_RESPONSE_TOPIC, self._make_resolver('chat', CHAT_RESPONSE_TOPIC), 10)
        self._ctrl_resp_sub = self.create_subscription(
            CtrlResponse, CTRL_RESPONSE_TOPIC, self._make_resolver('ctrl', CTRL_RESPONSE_TOPIC), 10)

        self._ctrl_cmd_pub = self.create_publisher(CtrlCommand, CTRL_COMMAND_TOPIC, 10)

        self._robot_state_sub = self.create_subscription(
            Float64MultiArray, ROBOT_CURRENT_STATE_TOPIC, self._on_robot_current_state, 10)

    def _on_robot_current_state(self, msg: Float64MultiArray) -> None:
        # data[0:3] = base pose (x, y, yaw), data[3:9] = ee pose (x, y, z, roll, pitch, yaw)
        d = msg.data
        if len(d) < 9:
            self.get_logger().warn(
                f'{ROBOT_CURRENT_STATE_TOPIC}: expected 9 values (base 3 + ee 6), got {len(d)} -- ignoring')
            return
        self.state.update_base_pose(BasePose(x=d[0], y=d[1], yaw=d[2]))
        self.state.update_manipulator_ee_pos(
            EEPose(x=d[3], y=d[4], z=d[5], roll=d[6], pitch=d[7], yaw=d[8]))
        # Push to any connected /ws/robot_state clients instead of making them
        # poll -- setInterval-based polling gets throttled to ~1/s by browsers
        # once a tab is backgrounded/unfocused, which made fast polling look
        # choppy. A push arrives immediately regardless of tab focus.
        self._loop.call_soon_threadsafe(self._broadcast_robot_state)

        # This topic publishes at ~90Hz -- only mirror it into the debug log
        # at most 5x/sec, or the panel would be an unreadable blur. The state
        # store update above always happens at full rate; this only throttles
        # what's shown in the debug feed.
        now = time.monotonic()
        if now - self._last_robot_state_debug_ts >= 0.2:
            self._last_robot_state_debug_ts = now
            self._debug(
                ROBOT_CURRENT_STATE_TOPIC,
                f'base=({d[0]:.3f}, {d[1]:.3f}, {d[2]:.3f}) '
                f'ee=({d[3]:.3f}, {d[4]:.3f}, {d[5]:.3f}, {d[6]:.3f}, {d[7]:.3f}, {d[8]:.3f})')

    def _broadcast_robot_state(self) -> None:
        if not self._robot_state_websockets:
            return
        data = self.state.to_dict()
        for ws in list(self._robot_state_websockets):
            self._loop.create_task(self._send_robot_state(ws, data))

    async def _send_robot_state(self, ws: WebSocket, data: dict) -> None:
        try:
            await ws.send_json(data)
        except Exception:
            self._robot_state_websockets.discard(ws)

    def _make_resolver(self, kind: str, topic: str):
        def _on_response(msg):
            self._debug(topic, f'id={msg.id} text={msg.text!r}')
            if kind == 'ctrl':
                with self._lock:
                    self._ctrl_resolved_text[msg.id] = getattr(msg, 'resolved_text', '') or msg.text

            with self._lock:
                future = self._pending.pop(msg.id, None)
            if future is None:
                return  # unknown/expired id

            def _resolve():
                if not future.done():
                    future.set_result({'id': msg.id, 'text': msg.text, 'kind': kind})

            self._loop.call_soon_threadsafe(_resolve)

        return _on_response

    def get_ctrl_resolved_text(self, ctrl_id: str) -> str | None:
        """One-shot lookup of the resolved_text stashed for a ctrl_response id."""
        with self._lock:
            return self._ctrl_resolved_text.pop(ctrl_id, None)

    def _debug(self, topic: str, text: str) -> None:
        """Push a line to the /ws/debug per-topic panels. Safe to call from any thread."""
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        line = f'[{ts}] {text}'
        self._loop.call_soon_threadsafe(self._broadcast_debug, topic, line)

    def _broadcast_debug(self, topic: str, line: str) -> None:
        payload = {'topic': topic, 'line': line}
        for ws in list(self._debug_websockets):
            self._loop.create_task(self._send_debug(ws, payload))

    async def _send_debug(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            self._debug_websockets.discard(ws)

    def send_chat(self, text: str, points: list[dict]) -> asyncio.Future:
        req_id = str(uuid.uuid4())
        future: asyncio.Future = self._loop.create_future()
        with self._lock:
            self._pending[req_id] = future

        msg = ChatRequest()
        msg.id = req_id
        msg.text = text
        # Latest known robot state -- dummy values from state/robot_state.json
        # until the simulation side actually publishes. See robot_state.py.
        msg.base_pose = self.state.base_pose
        msg.manipulator_ee_pos = self.state.manipulator_ee_pos
        msg.checked_pos_list = [
            NamedPoint(label=p['label'], x=float(p['x']), y=float(p['y']), yaw=float(p.get('yaw', 0.0)))
            for p in points
        ]
        self._chat_req_pub.publish(msg)
        self._debug(CHAT_REQUEST_TOPIC, _format_chat_request(msg))
        return future

    def send_ctrl_command(self, ctrl_id: str, text: str) -> None:
        """Fire-and-forget: publish approval for a CtrlResponse. No reply expected here."""
        msg = CtrlCommand()
        msg.id = ctrl_id
        msg.text = text
        self._ctrl_cmd_pub.publish(msg)
        self._debug(CTRL_COMMAND_TOPIC, f'id={ctrl_id} text={text!r}')


def _resolve_map_dir(dir_param: str | None, default_map_dir: Path) -> Path:
    """Resolve a ?dir=/--default-map-dir value to an absolute map directory.

    Absolute paths and paths starting with ~ are used as-is. A bare relative
    name (e.g. "demo", or "some/sub/dir") is resolved against MAP_ROOT
    (<repo>/src/llm_ui/map), so typing "demo" in the UI's map-directory field
    loads <repo>/src/llm_ui/map/demo regardless of where bridge_node's process
    happens to have its CWD.
    """
    if not dir_param:
        return default_map_dir
    path = Path(dir_param).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (MAP_ROOT / path).resolve()


class BasicAuthMiddleware:
    """Raw ASGI (not Starlette's BaseHTTPMiddleware, which can't see websockets)
    middleware gating both HTTP and WebSocket requests behind HTTP Basic Auth.

    Browsers cache Basic Auth credentials per-origin after the first prompt, so
    once the user authenticates loading the page, the same Authorization header
    is attached automatically to subsequent requests -- including the WebSocket
    upgrade requests -- with no extra login flow needed.
    """

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)

        headers = dict(scope.get('headers') or [])
        if self._check(headers.get(b'authorization')):
            return await self.app(scope, receive, send)

        if scope['type'] == 'websocket':
            # No standard way to send a 401 during a websocket handshake here;
            # just refuse the connection.
            await send({'type': 'websocket.close', 'code': 4401})
            return

        await send({
            'type': 'http.response.start',
            'status': 401,
            'headers': [
                (b'www-authenticate', b'Basic realm="llm_ui"'),
                (b'content-type', b'text/plain'),
            ],
        })
        await send({'type': 'http.response.body', 'body': b'Authentication required'})

    def _check(self, auth_header: bytes | None) -> bool:
        if not auth_header:
            return False
        try:
            scheme, _, creds = auth_header.decode().partition(' ')
            if scheme.lower() != 'basic':
                return False
            user, _, pw = base64.b64decode(creds).decode().partition(':')
        except Exception:
            return False
        return hmac.compare_digest(user, self.username) and hmac.compare_digest(pw, self.password)


def create_app(ros_node: BridgeNode, default_map_dir: Path, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    @app.get('/api/map')
    def get_map(dir: str | None = None):
        try:
            meta = read_map(_resolve_map_dir(dir, default_map_dir))
        except MapNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        meta = dict(meta)
        image_query = f'?dir={quote(dir)}' if dir else ''
        meta['image_url'] = f'/api/map/image{image_query}'
        del meta['yaml_path'], meta['image_path']
        return meta

    @app.get('/api/map/image')
    def get_map_image(dir: str | None = None):
        try:
            meta = read_map(_resolve_map_dir(dir, default_map_dir))
        except MapNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return FileResponse(meta['image_path'], media_type='image/png')

    @app.get('/api/robot_state')
    def get_robot_state():
        return ros_node.state.to_dict()

    @app.websocket('/ws/robot_state')
    async def robot_state_ws(ws: WebSocket):
        await ws.accept()
        ros_node._robot_state_websockets.add(ws)
        await ws.send_json(ros_node.state.to_dict())
        try:
            while True:
                await ws.receive_text()  # push-only channel; just wait for disconnect
        except WebSocketDisconnect:
            pass
        finally:
            ros_node._robot_state_websockets.discard(ws)

    @app.websocket('/ws/debug')
    async def debug_ws(ws: WebSocket):
        """Live feed of every topic this node touches, as {"topic": ..., "line": ...}
        JSON, for the per-topic debug log panels in the UI. Push-only; nothing is
        buffered before a client connects, so it only shows what happens while open."""
        await ws.accept()
        ros_node._debug_websockets.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ros_node._debug_websockets.discard(ws)

    @app.websocket('/ws/chat')
    async def chat_ws(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                payload = await ws.receive_json()
                msg_type = payload.get('type', 'chat')

                if msg_type == 'allow':
                    ctrl_id = payload.get('id', '')
                    # Prefer the server-side resolved_text (coordinates) stashed
                    # when the ctrl_response came in; fall back to whatever the
                    # browser echoed back if it's missing/expired (e.g. an LLM
                    # node that doesn't set resolved_text).
                    resolved = ros_node.get_ctrl_resolved_text(ctrl_id)
                    if resolved is None:
                        resolved = payload.get('text', '')
                    ros_node.send_ctrl_command(ctrl_id, resolved)
                    continue

                text = payload.get('text', '')
                points = payload.get('points') or []
                reply = await ros_node.send_chat(text, points)
                await ws.send_json(reply)
        except WebSocketDisconnect:
            pass

    if frontend_dist is not None:
        # Mounted last so it only catches what none of the routes above matched
        # (Starlette tries routes in registration order) -- serves index.html at
        # "/" and the built JS/CSS/PNG assets alongside it.
        app.mount('/', StaticFiles(directory=frontend_dist, html=True), name='frontend')

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--default-map-dir', default='')  # empty means "auto-detect <repo>/map/demo"
    parser.add_argument('--state-file', default='')  # empty means "auto-detect <repo>/state/robot_state.json"
    parser.add_argument('--frontend-dist', default='')  # empty means "auto-detect frontend/dist, if built"
    parser.add_argument('--no-frontend', action='store_true')  # force-disable static serving even if dist exists
    # ros2 launch/run always appends --ros-args (for remapping/parameters, unused
    # here) which argparse doesn't know about -- strip it before parsing.
    args = parser.parse_args(remove_ros_args(sys.argv)[1:])

    rclpy.init()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    state_file = Path(args.state_file) if args.state_file else DEFAULT_STATE_FILE
    state = RobotStateStore.load(state_file)
    ros_node = BridgeNode(loop, state)

    ros_thread = threading.Thread(target=rclpy.spin, args=(ros_node,), daemon=True)
    ros_thread.start()

    map_dir = _resolve_map_dir(args.default_map_dir, DEFAULT_MAP_DIR)
    if args.no_frontend:
        frontend_dist = None
    else:
        frontend_dist = Path(args.frontend_dist) if args.frontend_dist else DEFAULT_FRONTEND_DIST
    app = create_app(ros_node, map_dir, frontend_dist)

    auth_user = os.environ.get('LLM_UI_AUTH_USER')
    auth_pass = os.environ.get('LLM_UI_AUTH_PASS')
    if auth_user and auth_pass:
        app = BasicAuthMiddleware(app, auth_user, auth_pass)
    elif auth_user or auth_pass:
        raise SystemExit('Set both LLM_UI_AUTH_USER and LLM_UI_AUTH_PASS to enable auth, not just one.')
    config = uvicorn.Config(app, host=args.host, port=args.port, loop='asyncio')
    server = uvicorn.Server(config)

    try:
        loop.run_until_complete(server.serve())
    finally:
        rclpy.shutdown()


if __name__ == '__main__':
    main()
