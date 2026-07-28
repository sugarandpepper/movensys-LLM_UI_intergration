import { useState } from 'react'
import MapViewer from './components/MapViewer'
import ChatPanel from './components/ChatPanel'
import type { ClickPoint, Section } from './types'
import './App.css'

function App() {
  const [clicks, setClicks] = useState<ClickPoint[]>([])
  const [sections, setSections] = useState<Section[]>([])

  return (
    <div className="app-layout">
      <MapViewer clicks={clicks} setClicks={setClicks} sections={sections} setSections={setSections} />
      <ChatPanel clicks={clicks} sections={sections} />
    </div>
  )
}

export default App
