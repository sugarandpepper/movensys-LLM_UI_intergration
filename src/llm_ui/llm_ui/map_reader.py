"""Reads ROS map_server-style yaml + image pairs from an arbitrary directory.

YAML fields follow the standard ROS map_server / Isaac Sim occupancy-map convention:
  image: <filename>
  resolution: meters per pixel
  origin: [x, y, yaw] -- world pose of the BOTTOM-LEFT pixel of the image
  negate, occupied_thresh, free_thresh
"""
import struct
from pathlib import Path

import yaml


class MapNotFoundError(FileNotFoundError):
    pass


def _png_size(path: Path) -> tuple[int, int]:
    with open(path, 'rb') as f:
        header = f.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'{path} is not a PNG file')
    width, height = struct.unpack('>II', header[16:24])
    return width, height


def find_yaml(map_dir: Path) -> Path:
    yaml_files = sorted(map_dir.glob('*.yaml')) + sorted(map_dir.glob('*.yml'))
    if not yaml_files:
        raise MapNotFoundError(f'no .yaml file found in {map_dir}')
    return yaml_files[0]


def read_map(map_dir: str | Path) -> dict:
    map_dir = Path(map_dir).expanduser().resolve()
    if not map_dir.is_dir():
        raise MapNotFoundError(f'{map_dir} is not a directory')

    yaml_path = find_yaml(map_dir)
    with open(yaml_path, 'r') as f:
        meta = yaml.safe_load(f)

    image_path = (yaml_path.parent / meta['image']).resolve()
    if not image_path.is_file():
        raise MapNotFoundError(f'image {image_path} referenced by {yaml_path} not found')

    width, height = _png_size(image_path)
    origin = meta.get('origin', [0.0, 0.0, 0.0])

    return {
        'yaml_path': str(yaml_path),
        'image_path': str(image_path),
        'image_name': image_path.name,
        'resolution': float(meta['resolution']),
        'origin': [float(origin[0]), float(origin[1]), float(origin[2])],
        'negate': int(meta.get('negate', 0)),
        'occupied_thresh': float(meta.get('occupied_thresh', 0.65)),
        'free_thresh': float(meta.get('free_thresh', 0.196)),
        'width': width,
        'height': height,
    }
