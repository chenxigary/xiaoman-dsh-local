/** Apply-level QQ transport owner and bounded frame helpers. */

import { bridgeBase } from '../bridge.ts'

export const MAX_QQ_FRAME_BYTES = 64 * 1024
export const MAX_QQ_TEXT_CHARS = 8_000

export interface QqMessageFrame {
  readonly type: 'qq_message'
  readonly text: string
}

function defaultQqWsUrl(): string {
  const base = bridgeBase()
  const proto = base.startsWith('https:') ? 'wss:' : 'ws:'
  return `${proto}//${base.replace(/^https?:\/\//, '')}/api/qq/ws`
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength
}

/** Parse only bounded text frames; binary/blob frames are deliberately ignored. */
export function parseQqFrame(value: unknown): QqMessageFrame | null {
  let raw: string
  if (typeof value === 'string') raw = value
  else if (value instanceof ArrayBuffer) {
    if (value.byteLength > MAX_QQ_FRAME_BYTES) return null
    raw = new TextDecoder().decode(value)
  } else return null
  if (byteLength(raw) > MAX_QQ_FRAME_BYTES) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const record = parsed as Record<string, unknown>
    if (record['type'] !== 'qq_message' || typeof record['text'] !== 'string') return null
    const text = record['text'].trim()
    return text !== '' && text.length <= MAX_QQ_TEXT_CHARS ? { type: 'qq_message', text } : null
  } catch {
    return null
  }
}

function encodeQqReply(text: string): string | null {
  const trimmed = text.trim()
  if (trimmed === '' || trimmed.length > MAX_QQ_TEXT_CHARS) return null
  const frame = JSON.stringify({ type: 'reply', text: trimmed })
  return byteLength(frame) <= MAX_QQ_FRAME_BYTES ? frame : null
}

interface QqRegistration {
  readonly token: number
  readonly sessionId: string
  readonly onText: (text: string) => void
}

/** One apply-level QQ socket with an explicit active session owner. */
export class QqSessionOwner {
  private ws: WebSocket | null = null
  // Opt-in by construction: registering a session must not open a browser
  // socket until the user explicitly enables QQ push.
  private closed = true
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private nextToken = 1
  private generation = 0
  private readonly registrations = new Map<number, QqRegistration>()

  constructor(private readonly url: () => string = defaultQqWsUrl) {}

  start(): void {
    this.closed = false
    this.connect()
  }

  /** Stop the socket without dropping session registrations. */
  stop(): void {
    this.closed = true
    this.generation += 1
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    const ws = this.ws
    this.ws = null
    if (ws !== null) {
      try { ws.close() } catch { /* already closed */ }
    }
  }

  register(sessionId: string | undefined, onText: (text: string) => void): () => void {
    const id = sessionId?.trim() ?? ''
    if (id === '') return () => {}
    const token = this.nextToken++
    this.registrations.set(token, { token, sessionId: id, onText })
    this.connect()
    return () => { this.registrations.delete(token) }
  }

  sendReply(sessionId: string, text: string): boolean {
    const active = this.active()
    const frame = encodeQqReply(text)
    if (active === undefined || active.sessionId !== sessionId.trim() || frame === null) return false
    const ws = this.ws
    if (ws === null || ws.readyState !== WebSocket.OPEN) return false
    ws.send(frame)
    return true
  }

  dispose(): void {
    this.stop()
    this.registrations.clear()
  }

  private active(): QqRegistration | undefined {
    let current: QqRegistration | undefined
    for (const registration of this.registrations.values()) {
      if (current === undefined || registration.token > current.token) current = registration
    }
    return current
  }

  private connect(): void {
    if (this.closed || this.ws !== null || typeof WebSocket === 'undefined') return
    let ws: WebSocket
    try { ws = new WebSocket(this.url()) } catch { return }
    const generation = ++this.generation
    this.ws = ws
    ws.binaryType = 'arraybuffer'
    ws.onmessage = (event) => {
      if (this.ws !== ws || this.generation !== generation || this.closed) return
      const message = parseQqFrame(event.data)
      if (message !== null) this.active()?.onText(message.text)
    }
    ws.onclose = () => {
      if (this.ws !== ws || this.generation !== generation) return
      this.ws = null
      if (!this.closed && this.registrations.size > 0) {
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null
          this.connect()
        }, 3_000)
      }
    }
  }
}
