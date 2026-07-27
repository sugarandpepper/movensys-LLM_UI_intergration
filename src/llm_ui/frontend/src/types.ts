export interface MapMetadata {
  image_name: string
  resolution: number
  origin: [number, number, number]
  negate: number
  occupied_thresh: number
  free_thresh: number
  width: number
  height: number
  image_url: string
}

export interface WorldPoint {
  x: number
  y: number
}

export interface PixelPoint {
  px: number
  py: number
}

/** A point the user marked on the map (map click log: A, B, C, ...). yaw is set by drag. */
export interface ClickPoint {
  label: string
  px: number
  py: number
  x: number
  y: number
  yaw: number
}

export interface BasePose {
  x: number
  y: number
  yaw: number
}

export interface EEPose {
  x: number
  y: number
  z: number
  roll: number
  pitch: number
  yaw: number
}

/** Latest known robot state, polled from GET /api/robot_state. */
export interface RobotState {
  base_pose: BasePose
  manipulator_ee_pos: EEPose
}

/**
 * A no-go zone the robot can't physically reach (e.g. the middle of a table).
 * Clicking inside x1..x2 / y1..y2 in normal mode is redirected to `target`
 * instead of using the raw click position. Set up via "Section mode" in the
 * map toolbar. Persisted client-side only (localStorage), per map directory.
 */
export interface Section {
  id: string
  name: string
  x1: number
  y1: number
  x2: number
  y2: number
  target: { x: number; y: number; yaw: number }
}
