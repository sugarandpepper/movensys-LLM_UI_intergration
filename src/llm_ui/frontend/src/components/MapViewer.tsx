import { useCallback, useEffect, useRef, useState } from 'react'
import type { ClickPoint, MapMetadata, RobotState, Section } from '../types'
import { pixelToWorld, pointLabel, worldArrowTip, worldToPixel } from '../mapMath'
import { findSectionAt, genSectionId, loadSections, saveSections } from '../sections'

interface ViewState {
  scale: number
  offsetX: number
  offsetY: number
}

const MIN_SCALE = 0.05
const MAX_SCALE = 32
const DRAG_THRESHOLD_PX = 4
const MIN_SECTION_SCREEN_SIZE_PX = 16
// Robot icon is sized from a real-world footprint (meters), not a fixed screen-pixel
// size, so it shrinks/grows in proportion to the map as the user zooms -- same as
// every other marker's position (though not their fixed-px radii/labels).
const ROBOT_SIZE_M = 1.0 / 2
const ROBOT_ICON_MIN_PX = 16
const ROBOT_ICON_MAX_PX = 200
// mobile_robot.png is drawn nose-down (front pointing toward the bottom of the image).
// "Down" in screen space is yaw = -90 deg (= 270 deg) in our world convention (see
// renderArrow / worldToPixel), so the extra CSS rotation needed to point the icon's
// front at a given yaw is (270deg - yaw), independent of that inherent artwork orientation.
const ROBOT_ICON_UP_YAW_DEG = 270
const FIT_MARGIN = 0.95

// Every topic bridge_node touches, matching the constants in bridge_node.py --
// one debug panel per entry, in this order.
const DEBUG_TOPICS = [
  '/llm_ui/chat_request',
  '/llm_ui/chat_response',
  '/llm_ui/ctrl_response',
  '/llm_ui/ctrl_command',
  '/robot_current_state',
]

function fitView(meta: MapMetadata, container: HTMLElement): ViewState {
  const rect = container.getBoundingClientRect()
  const scale = Math.min(rect.width / meta.width, rect.height / meta.height) * FIT_MARGIN
  return {
    scale,
    offsetX: (rect.width - meta.width * scale) / 2,
    offsetY: (rect.height - meta.height * scale) / 2,
  }
}

interface MapViewerProps {
  clicks: ClickPoint[]
  setClicks: React.Dispatch<React.SetStateAction<ClickPoint[]>>
}

interface PendingPoint {
  px: number
  py: number
  x: number
  y: number
  yaw: number
}

interface PendingRect {
  x1: number
  y1: number
  x2: number
  y2: number
}

type DragState =
  | { mode: 'pan'; startClientX: number; startClientY: number; origOffsetX: number; origOffsetY: number; moved: boolean }
  | { mode: 'point'; startPx: number; startPy: number; startX: number; startY: number; startClientX: number; startClientY: number; moved: boolean }
  | { mode: 'section-rect'; startX: number; startY: number; startClientX: number; startClientY: number; moved: boolean }
  | { mode: 'section-target'; startPx: number; startPy: number; startX: number; startY: number; startClientX: number; startClientY: number; moved: boolean }

export default function MapViewer({ clicks, setClicks }: MapViewerProps) {
  const [mapDir, setMapDir] = useState('')
  const [meta, setMeta] = useState<MapMetadata | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<ViewState>({ scale: 1, offsetX: 0, offsetY: 0 })
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 })
  const [robotState, setRobotState] = useState<RobotState | null>(null)
  const [pendingPoint, setPendingPoint] = useState<PendingPoint | null>(null)

  const [sectionMode, setSectionMode] = useState(false)
  const [sections, setSections] = useState<Section[]>([])
  const [pendingRect, setPendingRect] = useState<PendingRect | null>(null)
  const [pendingTarget, setPendingTarget] = useState<PendingPoint | null>(null)
  const [popupSection, setPopupSection] = useState<Section | null>(null)
  const [renamingSection, setRenamingSection] = useState(false)
  const [renameValue, setRenameValue] = useState('')

  const [debugOpen, setDebugOpen] = useState(false)
  const [debugByTopic, setDebugByTopic] = useState<Record<string, string[]>>({})

  const containerRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const metaRef = useRef<MapMetadata | null>(null)
  const clickCountRef = useRef(0)
  const dragState = useRef<DragState | null>(null)
  const mapDirRef = useRef('')
  const debugLogRef = useRef<HTMLDivElement>(null)

  const loadMap = useCallback((dir: string) => {
    const qs = dir ? `?dir=${encodeURIComponent(dir)}` : ''
    fetch(`/api/map${qs}`)
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
        return res.json() as Promise<MapMetadata>
      })
      .then((data) => {
        metaRef.current = data
        setMeta(data)
        setError(null)
        setClicks([])
        clickCountRef.current = 0
        mapDirRef.current = dir
        setSections(loadSections(dir))
        setPendingRect(null)
        setPendingTarget(null)
        setPopupSection(null)
        if (containerRef.current) setView(fitView(data, containerRef.current))
      })
      .catch((e) => setError(String(e.message ?? e)))
  }, [])

  useEffect(() => {
    loadMap(mapDirRef.current)
    // Load only on mount (with whatever dir was loaded before, or the
    // default demo map on first ever load) and otherwise only when the user
    // clicks Load -- no auto-load as the mapDir input changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Persist sections whenever they change (per map directory).
  useEffect(() => {
    saveSections(mapDirRef.current, sections)
  }, [sections])

  // Live robot state via WebSocket push (server sends one on connect, then one
  // per /robot_current_state update). Not setInterval-based polling on purpose:
  // browsers throttle timers to ~1/s in backgrounded/unfocused tabs, which made
  // fast polling look choppy; a pushed message isn't subject to that.
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/robot_state`)
    ws.onmessage = (event) => {
      setRobotState(JSON.parse(event.data) as RobotState)
    }
    return () => ws.close()
  }, [])

  // ROS topic debug feed: only connects while the panel is expanded, so it's
  // not just sitting there consuming the connection/CPU when nobody's looking.
  // Server sends {"topic": ..., "line": ...} JSON; sort into one buffer per
  // topic so each gets its own panel below.
  useEffect(() => {
    if (!debugOpen) return
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/debug`)
    ws.onmessage = (event) => {
      const { topic, line } = JSON.parse(event.data) as { topic: string; line: string }
      setDebugByTopic((prev) => ({
        ...prev,
        [topic]: [...(prev[topic] ?? []), line].slice(-300),
      }))
    }
    return () => ws.close()
  }, [debugOpen])

  // Auto-scroll every open per-topic terminal to its newest line.
  useEffect(() => {
    const container = debugLogRef.current
    if (!container) return
    container.querySelectorAll<HTMLDivElement>('.debug-log-terminal').forEach((el) => {
      el.scrollTop = el.scrollHeight
    })
  }, [debugByTopic])

  // Re-fit when the pane is resized (e.g. browser window resize).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver(() => {
      const rect = el.getBoundingClientRect()
      setContainerSize({ width: rect.width, height: rect.height })
      if (metaRef.current) setView(fitView(metaRef.current, el))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // Wheel zoom (needs a non-passive listener to allow preventDefault).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const cursorX = e.clientX - rect.left
      const cursorY = e.clientY - rect.top
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15

      // Single atomic update: nesting a setOffset(...) call inside a setScale
      // updater breaks under StrictMode, which invokes updaters twice to
      // check purity and would double-apply the offset side effect.
      setView((prev) => {
        const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.scale * factor))
        return {
          scale: nextScale,
          offsetX: cursorX - ((cursorX - prev.offsetX) / prev.scale) * nextScale,
          offsetY: cursorY - ((cursorY - prev.offsetY) / prev.scale) * nextScale,
        }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const toggleSectionMode = () => {
    dragState.current = null
    setPendingRect(null)
    setPendingTarget(null)
    setPopupSection(null)
    setRenamingSection(false)
    setSectionMode((prev) => !prev)
  }

  // Left button: drag to place a point + heading arrow (click with no drag -> yaw 0).
  // Right button: drag to pan. In section mode, left-button behaves differently (see below).
  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button === 2) {
      dragState.current = {
        mode: 'pan',
        startClientX: e.clientX,
        startClientY: e.clientY,
        origOffsetX: view.offsetX,
        origOffsetY: view.offsetY,
        moved: false,
      }
      return
    }
    if (e.button !== 0 || !meta || !imgRef.current) return
    const rect = imgRef.current.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * meta.width
    const py = ((e.clientY - rect.top) / rect.height) * meta.height
    if (px < 0 || py < 0 || px > meta.width || py > meta.height) return // outside image
    const world = pixelToWorld(meta, px, py)

    if (sectionMode) {
      if (!pendingRect) {
        const existing = findSectionAt(sections, world.x, world.y)
        if (existing) {
          setPopupSection(existing)
          setRenamingSection(false)
          return
        }
        dragState.current = {
          mode: 'section-rect',
          startX: world.x,
          startY: world.y,
          startClientX: e.clientX,
          startClientY: e.clientY,
          moved: false,
        }
        setPendingRect({ x1: world.x, y1: world.y, x2: world.x, y2: world.y })
      } else {
        // Rectangle already drawn -- this click/drag sets its mapped target point.
        dragState.current = {
          mode: 'section-target',
          startPx: px,
          startPy: py,
          startX: world.x,
          startY: world.y,
          startClientX: e.clientX,
          startClientY: e.clientY,
          moved: false,
        }
        setPendingTarget({ px, py, x: world.x, y: world.y, yaw: 0 })
      }
      return
    }

    dragState.current = {
      mode: 'point',
      startPx: px,
      startPy: py,
      startX: world.x,
      startY: world.y,
      startClientX: e.clientX,
      startClientY: e.clientY,
      moved: false,
    }
    setPendingPoint({ px, py, x: world.x, y: world.y, yaw: 0 })
  }

  const onMouseMove = (e: React.MouseEvent) => {
    const drag = dragState.current
    if (!drag) return

    const dxClient = e.clientX - drag.startClientX
    const dyClient = e.clientY - drag.startClientY
    if (Math.hypot(dxClient, dyClient) > DRAG_THRESHOLD_PX) drag.moved = true

    if (drag.mode === 'pan') {
      if (drag.moved) {
        const { origOffsetX, origOffsetY } = drag
        setView((prev) => ({ ...prev, offsetX: origOffsetX + dxClient, offsetY: origOffsetY + dyClient }))
      }
      return
    }

    if (!meta || !imgRef.current) return
    const rect = imgRef.current.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * meta.width
    const py = ((e.clientY - rect.top) / rect.height) * meta.height
    const world = pixelToWorld(meta, px, py)

    if (drag.mode === 'section-rect') {
      setPendingRect({ x1: drag.startX, y1: drag.startY, x2: world.x, y2: world.y })
      return
    }

    // 'point' or 'section-target': recompute yaw from start -> current world position.
    const yaw = drag.moved ? Math.atan2(world.y - drag.startY, world.x - drag.startX) : 0
    const preview = { px: drag.startPx, py: drag.startPy, x: drag.startX, y: drag.startY, yaw }
    if (drag.mode === 'section-target') {
      setPendingTarget(preview)
    } else {
      setPendingPoint(preview)
    }
  }

  const onMouseUp = () => {
    const drag = dragState.current
    dragState.current = null

    if (drag?.mode === 'section-rect') {
      // pendingRect already holds the final rectangle from the last move. If it's
      // too small on screen (a stray click/near-click rather than an intentional
      // drag), discard it instead of treating it as a finished zone -- otherwise
      // the very next click would be misread as setting that tiny zone's target.
      if (meta && pendingRect) {
        const px1 = worldToPixel(meta, pendingRect.x1, pendingRect.y1)
        const px2 = worldToPixel(meta, pendingRect.x2, pendingRect.y2)
        const p1 = toScreen(px1.px, px1.py)
        const p2 = toScreen(px2.px, px2.py)
        const wPx = Math.abs(p2.x - p1.x)
        const hPx = Math.abs(p2.y - p1.y)
        if (wPx < MIN_SECTION_SCREEN_SIZE_PX || hPx < MIN_SECTION_SCREEN_SIZE_PX) {
          setPendingRect(null)
        }
      }
      return
    }

    if (drag?.mode === 'section-target' && pendingTarget && pendingRect) {
      const section: Section = {
        id: genSectionId(),
        name: `S${sections.length + 1}`,
        x1: pendingRect.x1,
        y1: pendingRect.y1,
        x2: pendingRect.x2,
        y2: pendingRect.y2,
        target: { x: pendingTarget.x, y: pendingTarget.y, yaw: pendingTarget.yaw },
      }
      setSections((prev) => [...prev, section])
      setPendingRect(null)
      setPendingTarget(null)
      return
    }

    if (drag?.mode === 'point' && pendingPoint) {
      const label = pointLabel(clickCountRef.current++)
      const section = findSectionAt(sections, pendingPoint.x, pendingPoint.y)
      const resolved = section
        ? { px: worldToPixel(meta!, section.target.x, section.target.y).px, py: worldToPixel(meta!, section.target.x, section.target.y).py, x: section.target.x, y: section.target.y, yaw: section.target.yaw }
        : pendingPoint
      setClicks((prev) => [{ label, ...resolved }, ...prev].slice(0, 50))
    }
    setPendingPoint(null)
  }

  const cancelDrag = () => {
    dragState.current = null
    setPendingPoint(null)
    setPendingTarget(null)
  }

  const toScreen = (px: number, py: number) => ({
    x: view.offsetX + px * view.scale,
    y: view.offsetY + py * view.scale,
  })

  const resetPoints = () => {
    setClicks([])
    clickCountRef.current = 0
  }

  const deleteSection = (id: string) => {
    setSections((prev) => prev.filter((s) => s.id !== id))
    setPopupSection(null)
    setRenamingSection(false)
  }

  const startRenamingSection = () => {
    if (!popupSection) return
    setRenameValue(popupSection.name)
    setRenamingSection(true)
  }

  const saveSectionRename = () => {
    if (!popupSection) return
    const trimmed = renameValue.trim()
    const name = trimmed || popupSection.name
    setSections((prev) => prev.map((s) => (s.id === popupSection.id ? { ...s, name } : s)))
    setPopupSection(null)
    setRenamingSection(false)
  }

  // Black halo behind the colored line so the arrow reads against white, black,
  // and gray map cells alike, plus an arrowhead marker at the tip.
  const renderArrow = (x: number, y: number, yaw: number, color: string, markerId: string, lengthFactor = 0.08) => {
    if (!meta) return null
    const headLenM = Math.min(meta.width, meta.height) * meta.resolution * lengthFactor
    const p = worldToPixel(meta, x, y)
    const tip = worldArrowTip(meta, x, y, yaw, headLenM)
    const s = toScreen(p.px, p.py)
    const h = toScreen(tip.px, tip.py)
    return (
      <>
        <line x1={s.x} y1={s.y} x2={h.x} y2={h.y} stroke="#000" strokeWidth={5} strokeLinecap="round" opacity={0.6} />
        <line x1={s.x} y1={s.y} x2={h.x} y2={h.y} stroke={color} strokeWidth={2.5} strokeLinecap="round" markerEnd={`url(#${markerId})`} />
      </>
    )
  }

  const renderSectionRect = (rect: PendingRect, screenKey: string, finished: boolean) => {
    if (!meta) return null
    const corners = [
      [rect.x1, rect.y1],
      [rect.x2, rect.y1],
      [rect.x2, rect.y2],
      [rect.x1, rect.y2],
    ].map(([x, y]) => {
      const p = worldToPixel(meta, x, y)
      const s = toScreen(p.px, p.py)
      return `${s.x},${s.y}`
    }).join(' ')
    return (
      <polygon
        key={screenKey}
        points={corners}
        fill={finished ? 'rgba(255,51,51,0.18)' : 'rgba(255,136,0,0.15)'}
        stroke={finished ? '#f33' : '#f80'}
        strokeWidth={2}
        strokeDasharray={finished ? undefined : '6 4'}
      />
    )
  }

  return (
    <div className="map-viewer">
      <div className="map-toolbar">
        <input
          value={mapDir}
          onChange={(e) => setMapDir(e.target.value)}
          placeholder="map dir name under src/llm_ui/map (e.g. demo), or absolute path -- blank = default demo map"
        />
        <button onClick={() => loadMap(mapDir)}>Load</button>
        <button onClick={resetPoints}>Reset points</button>
        <button
          className={sectionMode ? 'section-mode-btn active' : 'section-mode-btn'}
          onClick={toggleSectionMode}
        >
          {sectionMode ? 'Section mode: ON' : 'Section mode: OFF'}
        </button>
      </div>

      {sectionMode && (
        <div className="map-hint">
          {!pendingRect
            ? 'Drag to draw a no-go zone, or click an existing zone to delete it.'
            : 'Now click (or drag for a heading) to set the point this zone maps to.'}
        </div>
      )}

      {error && <div className="map-error">{error}</div>}

      <div
        className="map-canvas"
        ref={containerRef}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={cancelDrag}
        onContextMenu={(e) => e.preventDefault()}
      >
        {meta && (
          <img
            ref={imgRef}
            src={meta.image_url}
            alt="map"
            draggable={false}
            style={{
              position: 'absolute',
              left: view.offsetX,
              top: view.offsetY,
              width: meta.width * view.scale,
              height: meta.height * view.scale,
              imageRendering: 'pixelated',
            }}
          />
        )}

        {meta && containerSize.width > 0 && (
          <svg
            className="map-overlay"
            width={containerSize.width}
            height={containerSize.height}
            style={{ position: 'absolute', left: 0, top: 0, pointerEvents: 'none' }}
          >
            <defs>
              <marker id="arrow-x" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="#e33" />
              </marker>
              <marker id="arrow-y" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="#3c3" />
              </marker>
              <marker id="arrowhead-yellow" markerWidth="10" markerHeight="10" refX="7" refY="3.5" orient="auto">
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#ff0" stroke="#000" strokeWidth={0.6} />
              </marker>
              <marker id="arrowhead-blue" markerWidth="10" markerHeight="10" refX="7" refY="3.5" orient="auto">
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#0af" stroke="#000" strokeWidth={0.6} />
              </marker>
              <marker id="arrowhead-pending" markerWidth="10" markerHeight="10" refX="7" refY="3.5" orient="auto">
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#ff8800" stroke="#000" strokeWidth={0.6} />
              </marker>
              <marker id="arrowhead-magenta" markerWidth="10" markerHeight="10" refX="7" refY="3.5" orient="auto">
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#f0f" stroke="#000" strokeWidth={0.6} />
              </marker>
              <marker id="arrowhead-robot" markerWidth="12" markerHeight="12" refX="8" refY="4" orient="auto">
                <path d="M0,0 L8,4 L0,8 Z" fill="#0f0" stroke="#000" strokeWidth={0.6} />
              </marker>
            </defs>

            {(() => {
              // World (0, 0) -- NOT meta.origin, which is the yaml `origin` field
              // (the world pose of the image's bottom-left pixel, not the world zero point).
              const axisLenM = Math.min(meta.width, meta.height) * meta.resolution * 0.1
              const origin = worldToPixel(meta, 0, 0)
              const xTip = worldToPixel(meta, axisLenM, 0)
              const yTip = worldToPixel(meta, 0, axisLenM)
              const o = toScreen(origin.px, origin.py)
              const xt = toScreen(xTip.px, xTip.py)
              const yt = toScreen(yTip.px, yTip.py)
              return (
                <>
                  <line x1={o.x} y1={o.y} x2={xt.x} y2={xt.y} stroke="#e33" strokeWidth={2} markerEnd="url(#arrow-x)" />
                  <line x1={o.x} y1={o.y} x2={yt.x} y2={yt.y} stroke="#3c3" strokeWidth={2} markerEnd="url(#arrow-y)" />
                  <text x={xt.x + 6} y={xt.y} fill="#e33" fontSize={12} fontFamily="monospace">X</text>
                  <text x={yt.x} y={yt.y - 6} fill="#3c3" fontSize={12} fontFamily="monospace">Y</text>
                  <circle cx={o.x} cy={o.y} r={4} fill="#fff" stroke="#000" strokeWidth={1.5} />
                  <text x={o.x + 8} y={o.y + 14} fill="#fff" stroke="#000" strokeWidth={0.3} fontSize={11} fontFamily="monospace">
                    origin (0, 0)
                  </text>
                </>
              )
            })()}

            {sections.map((s) => (
              <g key={s.id}>
                {renderSectionRect(s, `rect-${s.id}`, true)}
                {(() => {
                  const rp = worldToPixel(meta, (s.x1 + s.x2) / 2, Math.max(s.y1, s.y2))
                  const labelPos = toScreen(rp.px, rp.py)
                  return (
                    <>
                      {sectionMode && (() => {
                        const p = worldToPixel(meta, s.target.x, s.target.y)
                        const sc = toScreen(p.px, p.py)
                        return (
                          <>
                            <circle cx={sc.x} cy={sc.y} r={4.5} fill="#f0f" stroke="#000" strokeWidth={1} />
                            {renderArrow(s.target.x, s.target.y, s.target.yaw, '#f0f', 'arrowhead-magenta')}
                          </>
                        )
                      })()}
                      <text x={labelPos.x} y={labelPos.y - 6} fill="#f33" stroke="#000" strokeWidth={0.4} fontSize={12} fontFamily="monospace" textAnchor="middle">
                        {s.name}
                      </text>
                    </>
                  )
                })()}
              </g>
            ))}

            {pendingRect && renderSectionRect(pendingRect, 'pending-rect', false)}

            {pendingTarget && (
              <g>
                <circle cx={toScreen(pendingTarget.px, pendingTarget.py).x} cy={toScreen(pendingTarget.px, pendingTarget.py).y} r={5} fill="#f0f" stroke="#000" strokeWidth={1} />
                {renderArrow(pendingTarget.x, pendingTarget.y, pendingTarget.yaw, '#f0f', 'arrowhead-magenta', 0.12)}
              </g>
            )}

            {clicks.map((c, i) => {
              const s = toScreen(c.px, c.py)
              const isLatest = i === 0
              const color = isLatest ? '#ff0' : '#0af'
              return (
                <g key={c.label}>
                  <circle cx={s.x} cy={s.y} r={isLatest ? 5 : 3.5} fill={color} stroke="#000" strokeWidth={1} />
                  {renderArrow(c.x, c.y, c.yaw, color, isLatest ? 'arrowhead-yellow' : 'arrowhead-blue')}
                  <text x={s.x + 8} y={s.y - 8} fill={color} stroke="#000" strokeWidth={0.3} fontSize={11} fontFamily="monospace">
                    {c.label} ({c.x.toFixed(2)}, {c.y.toFixed(2)}, {c.yaw.toFixed(2)})
                  </text>
                </g>
              )
            })}

            {pendingPoint && (
              <g>
                <circle cx={toScreen(pendingPoint.px, pendingPoint.py).x} cy={toScreen(pendingPoint.px, pendingPoint.py).y} r={5} fill="#ff8800" stroke="#000" strokeWidth={1} />
                {renderArrow(pendingPoint.x, pendingPoint.y, pendingPoint.yaw, '#ff8800', 'arrowhead-pending', 0.12)}
              </g>
            )}

            {/* Explicit center point + heading arrow for the robot -- the icon's
                rotation shows facing direction too, but this makes the exact
                position and "front" unambiguous at a glance. */}
            {robotState && (() => {
              const p = worldToPixel(meta, robotState.base_pose.x, robotState.base_pose.y)
              const sc = toScreen(p.px, p.py)
              return (
                <>
                  <circle cx={sc.x} cy={sc.y} r={4.5} fill="#0f0" stroke="#000" strokeWidth={1} />
                  {renderArrow(robotState.base_pose.x, robotState.base_pose.y, robotState.base_pose.yaw, '#0f0', 'arrowhead-robot', 0.07)}
                </>
              )
            })()}
          </svg>
        )}

        {robotState && meta && (() => {
          const { x, y, yaw } = robotState.base_pose
          const p = worldToPixel(meta, x, y)
          const s = toScreen(p.px, p.py)
          const rotateDeg = ROBOT_ICON_UP_YAW_DEG - (yaw * 180) / Math.PI
          // Screen px per world meter = view.scale (image px per world meter, via
          // meta.resolution) times the current zoom -- same conversion toScreen/
          // worldToPixel use, so the icon scales with the map instead of staying a
          // fixed screen size regardless of zoom.
          const iconPx = Math.min(
            ROBOT_ICON_MAX_PX,
            Math.max(ROBOT_ICON_MIN_PX, (ROBOT_SIZE_M / meta.resolution) * view.scale),
          )
          return (
            <div style={{ position: 'absolute', left: s.x, top: s.y, pointerEvents: 'none' }}>
              <img
                src="/mobile_robot.png"
                alt="robot"
                style={{
                  position: 'absolute',
                  left: 0,
                  top: 0,
                  width: iconPx,
                  height: iconPx,
                  transform: `translate(-50%, -50%) rotate(${rotateDeg}deg)`,
                }}
              />
              <span
                style={{
                  position: 'absolute',
                  left: iconPx / 2 + 6,
                  top: -6,
                  color: '#000',
                  fontSize: 12,
                  fontWeight: 'bold',
                  fontFamily: 'monospace',
                  whiteSpace: 'nowrap',
                  textShadow: '1px 1px 0 #fff, -1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff',
                }}
              >
                robot ({x.toFixed(2)}, {y.toFixed(2)})
              </span>
            </div>
          )
        })()}

        {popupSection && meta && (() => {
          const cx = (popupSection.x1 + popupSection.x2) / 2
          const cy = (popupSection.y1 + popupSection.y2) / 2
          const p = worldToPixel(meta, cx, cy)
          const s = toScreen(p.px, p.py)
          const containerRect = containerRef.current?.getBoundingClientRect()
          const left = (containerRect?.left ?? 0) + s.x
          const top = (containerRect?.top ?? 0) + s.y
          return (
            <div className="section-popup" style={{ left, top }}>
              {renamingSection ? (
                <>
                  <div className="section-popup-title">
                    <input
                      autoFocus
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveSectionRename()
                        if (e.key === 'Escape') setRenamingSection(false)
                      }}
                    />
                  </div>
                  <div className="section-popup-actions">
                    <button onClick={saveSectionRename}>Save</button>
                    <button onClick={() => setRenamingSection(false)}>Cancel</button>
                  </div>
                </>
              ) : (
                <>
                  <div className="section-popup-title">Zone: {popupSection.name}</div>
                  <div className="section-popup-actions">
                    <button onClick={startRenamingSection}>Rename</button>
                    <button onClick={() => deleteSection(popupSection.id)}>Delete</button>
                    <button onClick={() => setPopupSection(null)}>Cancel</button>
                  </div>
                </>
              )}
            </div>
          )
        })()}
      </div>

      {meta && (
        <div className="map-info">
          <div>resolution: {meta.resolution} m/px, size: {meta.width}x{meta.height}px, origin: [{meta.origin.join(', ')}]</div>
        </div>
      )}

      <details className="debug-log" open={debugOpen} onToggle={(e) => setDebugOpen(e.currentTarget.open)}>
        <summary>Debugging {debugOpen && '(live)'}</summary>
        <div className="debug-panels" ref={debugLogRef}>
          {DEBUG_TOPICS.map((topic) => {
            const lines = debugByTopic[topic] ?? []
            return (
              <details key={topic} className="debug-topic-panel">
                <summary>
                  {topic} <span className="debug-topic-count">({lines.length})</span>
                </summary>
                <div className="debug-log-terminal">
                  {lines.length === 0 ? (
                    <div className="debug-log-empty">waiting for traffic...</div>
                  ) : (
                    lines.map((line, i) => <div key={i}>{line}</div>)
                  )}
                </div>
              </details>
            )
          })}
        </div>
      </details>
    </div>
  )
}
