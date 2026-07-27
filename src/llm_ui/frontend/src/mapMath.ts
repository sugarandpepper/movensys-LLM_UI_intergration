import type { MapMetadata, PixelPoint, WorldPoint } from './types'

/**
 * Pixel -> world conversion following the ROS map_server / Isaac Sim occupancy-map
 * convention: `origin` is the world pose (x, y, yaw) of the BOTTOM-LEFT pixel of the
 * image, and image pixel (0,0) is the TOP-LEFT corner, so the y axis is flipped.
 */
export function pixelToWorld(meta: MapMetadata, px: number, py: number): WorldPoint {
  const [originX, originY, theta] = meta.origin
  const lx = px * meta.resolution
  const ly = (meta.height - py) * meta.resolution

  const cos = Math.cos(theta)
  const sin = Math.sin(theta)

  return {
    x: originX + lx * cos - ly * sin,
    y: originY + lx * sin + ly * cos,
  }
}

/** Inverse of {@link pixelToWorld}: world (meters) -> image pixel coordinates. */
export function worldToPixel(meta: MapMetadata, x: number, y: number): PixelPoint {
  const [originX, originY, theta] = meta.origin
  const dx = x - originX
  const dy = y - originY

  const cos = Math.cos(theta)
  const sin = Math.sin(theta)

  const lx = dx * cos + dy * sin
  const ly = -dx * sin + dy * cos

  return {
    px: lx / meta.resolution,
    py: meta.height - ly / meta.resolution,
  }
}

/** Pixel coordinates of the tip of a heading arrow rooted at world (x, y) pointing along yaw. */
export function worldArrowTip(meta: MapMetadata, x: number, y: number, yaw: number, lengthM: number): PixelPoint {
  return worldToPixel(meta, x + lengthM * Math.cos(yaw), y + lengthM * Math.sin(yaw))
}

/** Spreadsheet-style labels: 0->A, 1->B, ..., 25->Z, 26->AA, ... */
export function pointLabel(n: number): string {
  let s = ''
  let i = n
  do {
    s = String.fromCharCode(65 + (i % 26)) + s
    i = Math.floor(i / 26) - 1
  } while (i >= 0)
  return s
}
