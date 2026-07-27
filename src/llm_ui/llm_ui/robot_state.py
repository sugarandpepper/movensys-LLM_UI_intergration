"""Latest known robot state (base pose + manipulator end-effector pose).

Seeded from a dummy JSON file until the simulation side actually publishes on
/robot_current_state (std_msgs/Float64MultiArray, see bridge_node's
_on_robot_current_state). bridge_node unpacks that message and calls
update_base_pose()/update_manipulator_ee_pos() on every message; if nothing
ever arrives, the dummy seed value from the file just stays put and is used
as-is -- there's no separate "no data" state, only "whatever the latest value is."
"""
import json
import threading
from pathlib import Path

from ui_interfaces.msg import BasePose, EEPose


def _find_default_state_file() -> Path:
    """Locate <repo>/src/llm_ui/state/robot_state.json without assuming a fixed depth.

    Same reasoning as bridge_node's _find_default_map_dir: this file may run
    from the source tree or the colcon-installed copy, which sit at different
    depths.
    """
    for start in (Path(__file__).resolve(), Path.cwd()):
        cur = start
        for _ in range(10):
            for candidate in (cur / 'state' / 'robot_state.json', cur / 'src' / 'llm_ui' / 'state' / 'robot_state.json'):
                if candidate.is_file():
                    return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return Path.cwd() / 'state' / 'robot_state.json'


DEFAULT_STATE_FILE = _find_default_state_file()


def _base_pose_to_dict(pose: BasePose) -> dict:
    return {'x': pose.x, 'y': pose.y, 'yaw': pose.yaw}


def _dict_to_base_pose(data: dict) -> BasePose:
    return BasePose(x=float(data.get('x', 0.0)), y=float(data.get('y', 0.0)), yaw=float(data.get('yaw', 0.0)))


def _ee_pose_to_dict(pose: EEPose) -> dict:
    return {
        'x': pose.x, 'y': pose.y, 'z': pose.z,
        'roll': pose.roll, 'pitch': pose.pitch, 'yaw': pose.yaw,
    }


def _dict_to_ee_pose(data: dict) -> EEPose:
    return EEPose(
        x=float(data.get('x', 0.0)), y=float(data.get('y', 0.0)), z=float(data.get('z', 0.0)),
        roll=float(data.get('roll', 0.0)), pitch=float(data.get('pitch', 0.0)), yaw=float(data.get('yaw', 0.0)),
    )


class RobotStateStore:
    """Thread-safe holder for the latest base_pose / manipulator_ee_pos."""

    def __init__(self, base_pose: BasePose, manipulator_ee_pos: EEPose):
        self._lock = threading.Lock()
        self._base_pose = base_pose
        self._manipulator_ee_pos = manipulator_ee_pos

    @classmethod
    def load(cls, path: Path) -> 'RobotStateStore':
        with open(path) as f:
            data = json.load(f)
        return cls(
            base_pose=_dict_to_base_pose(data['base_pose']),
            manipulator_ee_pos=_dict_to_ee_pose(data['manipulator_ee_pos']),
        )

    @property
    def base_pose(self) -> BasePose:
        with self._lock:
            return self._base_pose

    @property
    def manipulator_ee_pos(self) -> EEPose:
        with self._lock:
            return self._manipulator_ee_pos

    def update_base_pose(self, pose: BasePose) -> None:
        with self._lock:
            self._base_pose = pose

    def update_manipulator_ee_pos(self, pose: EEPose) -> None:
        with self._lock:
            self._manipulator_ee_pos = pose

    def to_dict(self) -> dict:
        with self._lock:
            return {
                'base_pose': _base_pose_to_dict(self._base_pose),
                'manipulator_ee_pos': _ee_pose_to_dict(self._manipulator_ee_pos),
            }
