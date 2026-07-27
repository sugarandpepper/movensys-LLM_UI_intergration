import type { Section } from './types'

export function genSectionId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export function pointInSection(s: Section, x: number, y: number): boolean {
  const minX = Math.min(s.x1, s.x2)
  const maxX = Math.max(s.x1, s.x2)
  const minY = Math.min(s.y1, s.y2)
  const maxY = Math.max(s.y1, s.y2)
  return x >= minX && x <= maxX && y >= minY && y <= maxY
}

/** Last-defined section wins when zones overlap. */
export function findSectionAt(sections: Section[], x: number, y: number): Section | null {
  for (let i = sections.length - 1; i >= 0; i--) {
    if (pointInSection(sections[i], x, y)) return sections[i]
  }
  return null
}

function storageKey(mapDir: string): string {
  return `llm_ui_sections:${mapDir || '__default__'}`
}

export function loadSections(mapDir: string): Section[] {
  try {
    const raw = localStorage.getItem(storageKey(mapDir))
    if (!raw) return []
    const parsed = JSON.parse(raw) as Section[]
    // Backfill names for sections saved before renaming existed.
    return parsed.map((s, i) => (s.name ? s : { ...s, name: `S${i + 1}` }))
  } catch {
    return []
  }
}

export function saveSections(mapDir: string, sections: Section[]): void {
  try {
    localStorage.setItem(storageKey(mapDir), JSON.stringify(sections))
  } catch {
    // localStorage unavailable (private mode, quota, etc.) -- sections just won't persist.
  }
}
