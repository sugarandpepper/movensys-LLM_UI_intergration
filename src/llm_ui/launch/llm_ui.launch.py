"""Single entry point for the UI side: bridge_node (API/map/chat) + frontend dev server.

    ros2 launch llm_ui llm_ui.launch.py

Then open http://localhost:5173 in a browser. Ctrl-C stops both together.

The LLM side (mobile_llm_node) is a separate package and runs in its own
terminal on purpose -- that boundary is the whole point of going through
ui_interfaces instead of bundling the two together:

    ros2 run mobile_llm_node mobile_llm_node

Launch arguments:
    bridge_port      (default: 8000)  -- bridge_node's FastAPI/WebSocket port
    frontend_port     (default: 5173) -- vite dev server port
    default_map_dir   (default: "")   -- forwarded to bridge_node --default-map-dir; empty means "let bridge_node find <repo>/src/llm_ui/map/demo"
"""
import os
import sys
from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node


def _bridge_node_python() -> str:
    """Python interpreter to run bridge_node with.

    `ros2 launch` itself always runs under the system interpreter
    (/usr/bin/python3, per its own shebang), so `sys.executable` here is
    NOT the active conda env -- it's whatever `ros2` was installed against.
    bridge_node needs the conda env's interpreter (that's where uvicorn/
    fastapi/etc. from requirements.txt are installed), so look it up via
    CONDA_PREFIX, which survives from `conda activate` into this process's
    environment even though sys.executable doesn't reflect it.
    """
    conda_prefix = os.environ.get('CONDA_PREFIX')
    if conda_prefix:
        candidate = Path(conda_prefix) / 'bin' / 'python3'
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _find_frontend_dir() -> str:
    """Locate <repo>/src/llm_ui/frontend without assuming a fixed directory depth.

    This launch file is installed by colcon into install/llm_ui/share/..., a
    different depth than its location in the source tree
    (src/llm_ui/launch/...), so a fixed parents[N] index isn't reliable here
    either (same issue as bridge_node's default map dir lookup).
    """
    for start in (Path(__file__).resolve(), Path.cwd()):
        cur = start
        for _ in range(10):
            for candidate in (cur / 'frontend', cur / 'src' / 'llm_ui' / 'frontend'):
                if (candidate / 'package.json').is_file():
                    return str(candidate)
            if cur.parent == cur:
                break
            cur = cur.parent
    raise FileNotFoundError('could not locate the frontend/ directory (pass --default-map-dir manually if needed)')


def generate_launch_description():
    frontend_dir = _find_frontend_dir()

    bridge_port = LaunchConfiguration('bridge_port')
    frontend_port = LaunchConfiguration('frontend_port')
    default_map_dir = LaunchConfiguration('default_map_dir')

    return LaunchDescription([
        DeclareLaunchArgument('bridge_port', default_value='8000'),
        DeclareLaunchArgument('frontend_port', default_value='5173'),
        DeclareLaunchArgument('default_map_dir', default_value=''),

        Node(
            package='llm_ui',
            executable='bridge_node',
            output='screen',
            # Force the active conda env's Python interpreter instead of the
            # installed script's own #!/usr/bin/python3 shebang, which
            # otherwise resolves to the system interpreter and misses
            # packages (uvicorn, fastapi, ...) installed only in the conda env.
            prefix=_bridge_node_python(),
            arguments=[
                '--port', bridge_port,
                '--default-map-dir', default_map_dir,
            ],
        ),
        ExecuteProcess(
            cmd=['npm', 'run', 'dev', '--', '--port', frontend_port],
            cwd=frontend_dir,
            output='screen',
        ),
    ])
