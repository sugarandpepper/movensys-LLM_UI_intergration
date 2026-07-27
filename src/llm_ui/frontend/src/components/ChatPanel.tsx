import { useEffect, useRef, useState } from 'react'
import type { ClickPoint } from '../types'

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  kind: 'chat' | 'command' | 'ctrl'
  points?: ClickPoint[]
  id?: string
  allowed?: boolean
}

// LLM replies often pack a numbered list onto one line (e.g. "... 1. Foo 2.
// Bar 3. Baz"), with no newline before each marker. Insert one before every
// "N." that starts a list item (not decimals like "3.14") so .chat-message-text's
// white-space: pre-wrap actually breaks it into separate lines.
function formatMessageText(text: string): string {
  return text.replace(/\s*(?<!\d)(\d{1,2}\.)\s+(?=\S)/g, (_match, marker, offset) =>
    offset === 0 ? `${marker} ` : `\n${marker} `
  )
}

interface ChatPanelProps {
  clicks: ClickPoint[]
}

export default function ChatPanel({ clicks }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/chat`)
    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: data.text, kind: data.kind ?? 'chat', id: data.id },
      ])
    }
    wsRef.current = ws
    return () => ws.close()
  }, [])

  const send = () => {
    const text = input.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return

    // clicks is stored newest-first; send in the order the points were
    // marked (A, B, C, ...) so the LLM sees them chronologically.
    const points = [...clicks].reverse()
    const kind: ChatMessage['kind'] = points.length > 0 ? 'command' : 'chat'

    setMessages((prev) => [...prev, { role: 'user', text, kind, points }])
    wsRef.current.send(JSON.stringify({
      type: 'chat',
      text,
      points: points.map((p) => ({ label: p.label, x: p.x, y: p.y, yaw: p.yaw })),
    }))
    setInput('')
  }

  const allow = (index: number) => {
    const msg = messages[index]
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !msg.id) return
    wsRef.current.send(JSON.stringify({ type: 'allow', id: msg.id, text: msg.text }))
    setMessages((prev) => prev.map((m, i) => (i === index ? { ...m, allowed: true } : m)))
  }

  return (
    <div className="chat-panel">
      <div className="chat-status">{connected ? 'connected' : 'disconnected'} (mock LLM node)</div>
      <div className="chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`chat-message chat-message-${m.role}${m.kind === 'ctrl' ? ' chat-message-ctrl' : ''}`}>
            {m.kind === 'command' && (
              <div className="chat-message-tag">
                command{m.points && m.points.length > 0 ? ` • ${m.points.map((p) => p.label).join(', ')}` : ''}
              </div>
            )}
            {m.kind === 'ctrl' && <div className="chat-message-tag">needs approval</div>}
            <div className="chat-message-body">
              <span className="chat-message-text">{m.kind === 'ctrl' ? formatMessageText(m.text) : m.text}</span>
              {m.kind === 'ctrl' && (
                <button
                  className="chat-allow-btn"
                  disabled={m.allowed}
                  onClick={() => allow(i)}
                >
                  {m.allowed ? 'Allowed ✓' : 'Allow'}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="chat-input-row">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder={clicks.length > 0 ? `Command (points: ${clicks.map((p) => p.label).reverse().join(', ')})...` : 'Type a message...'}
        />
        <button onClick={send}>Send</button>
      </div>
    </div>
  )
}
