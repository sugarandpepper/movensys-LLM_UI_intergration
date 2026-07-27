import { useState } from 'react'
import MapViewer from './components/MapViewer'
import ChatPanel from './components/ChatPanel'
import type { ClickPoint } from './types'
import './App.css'

function App() {
  const [clicks, setClicks] = useState<ClickPoint[]>([])

  return (
    <div className="app-layout">
      <MapViewer clicks={clicks} setClicks={setClicks} />
      <ChatPanel clicks={clicks} />
    </div>
  )
}

export default App
