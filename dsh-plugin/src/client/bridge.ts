/**
 * Bridge HTTP client: talks to the local voice-bridge service
 * (http://127.0.0.1:8765 by default, overridable via localStorage
 * `s2s.voice.bridge`).
 */

import { latencyEvent, monotonicNow, newTraceId } from './latency.ts'
import type { CharacterId } from './character.ts'

const DEFAULT_BRIDGE = 'http://127.0.0.1:8765'
const ALLOWED_BRIDGE_ORIGINS = new Set([
  'http://127.0.0.1:8765',
  'http://[::1]:8765',
  'http://localhost:8765',
])
/** 16 kHz mono PCM16 for the recorder's 30-second utterance ceiling. */
export const MAX_STT_AUDIO_BYTES = 30 * 16_000 * 2
/** Keep malformed bridge JSON from becoming an unbounded client allocation. */
export const MAX_STT_RESPONSE_BYTES = 64 * 1024
/** Bound recognized text before it can enter a native prompt or Codex start. */
export const MAX_STT_TEXT_CHARS = 8_000
/** Hard upper bound for one synthesized WAV response. */
export const MAX_TTS_RESPONSE_BYTES = 4 * 1024 * 1024
/** The bridge accepts one bounded sentence per TTS request. */
export const MAX_TTS_TEXT_CHARS = 512

export interface BridgeRequestOptions {
  /** DSH product session correlation; never a credential. */
  sessionId?: string | undefined
  /** Explicit character namespace; default keeps the legacy path. */
  character?: CharacterId | undefined
  /** Stable logical reply identity used by the Avatar PCM timeline. */
  turnId?: string | undefined
  /** Monotonic cancellation generation for stale Avatar packet rejection. */
  generation?: number | undefined
  /** Marks the final packet of this complete sentence/utterance. */
  end?: boolean | undefined
  /** Cancels one utterance without affecting later capture. */
  signal?: AbortSignal | undefined
}

export interface TtsPcmChunk {
  readonly pcm: ArrayBuffer
  readonly sampleRate: number
  readonly channels: 1
}

export interface TtsStreamResult {
  readonly traceId: string
  readonly chunks: number
  readonly bytes: number
  readonly sampleRate: number
}

function sessionHeaders(options?: BridgeRequestOptions): Record<string, string> {
  const headers: Record<string, string> = {}
  if (options?.sessionId?.trim()) headers['X-DSH-Session-Id'] = options.sessionId.trim()
  if (options?.character !== undefined) headers['X-Voice-Character'] = options.character
  return headers
}

/** Accept only fixed-origin loopback bridge URLs. */
export function isAllowedBridgeBase(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 256) return false
  try {
    const url = new URL(value)
    return ALLOWED_BRIDGE_ORIGINS.has(url.origin)
      && (url.pathname === '' || url.pathname === '/')
      && url.search === ''
      && url.hash === ''
      && url.username === ''
      && url.password === ''
  } catch {
    return false
  }
}

/** Resolve the bridge base URL; persisted overrides cannot leave loopback. */
export function bridgeBase(): string {
  try {
    const configured = localStorage.getItem('s2s.voice.bridge')?.trim()
    if (configured !== undefined && isAllowedBridgeBase(configured)) return new URL(configured).origin
    return DEFAULT_BRIDGE
  } catch {
    return DEFAULT_BRIDGE
  }
}

async function readResponseBytes(response: Response, maxBytes: number): Promise<ArrayBuffer> {
  const declared = Number(response.headers.get('content-length'))
  if (Number.isFinite(declared) && declared > maxBytes) throw new Error('voice bridge response exceeds the size limit')
  if (response.body === null) {
    const value = await response.arrayBuffer()
    if (value.byteLength > maxBytes) throw new Error('voice bridge response exceeds the size limit')
    return value
  }
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      total += next.value.byteLength
      if (total > maxBytes) {
        await reader.cancel()
        throw new Error('voice bridge response exceeds the size limit')
      }
      chunks.push(next.value)
    }
  } finally {
    reader.releaseLock()
  }
  const output = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    output.set(chunk, offset)
    offset += chunk.byteLength
  }
  return output.buffer
}

async function readResponseText(response: Response, maxBytes: number): Promise<string> {
  return new TextDecoder().decode(await readResponseBytes(response, maxBytes))
}

/** Speech to text: raw 16 kHz mono PCM16 -> { text, language }. */
export async function stt(pcm16: ArrayBuffer, options?: BridgeRequestOptions): Promise<{ text: string; language?: string | undefined; traceId?: string | undefined }> {
  if (pcm16.byteLength > MAX_STT_AUDIO_BYTES) throw new Error('voice bridge /api/stt request exceeds the audio limit')
  const started = monotonicNow()
  const traceId = newTraceId()
  try {
    const request: RequestInit = {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Max-Audio-Sec': '30',
        'X-Voice-Trace-Id': traceId,
        ...sessionHeaders(options),
      },
      body: pcm16,
    }
    if (options?.signal !== undefined) request.signal = options.signal
    const resp = await fetch(`${bridgeBase()}/api/stt`, request)
    if (!resp.ok) {
      latencyEvent('stt.http', {
        trace_id: traceId,
        duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
        status: resp.status,
        audio_bytes: pcm16.byteLength,
      })
      throw new Error(`voice bridge /api/stt failed: ${resp.status}`)
    }
    const parsed: unknown = JSON.parse(await readResponseText(resp, MAX_STT_RESPONSE_BYTES))
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('voice bridge /api/stt response is invalid')
    const record = parsed as Record<string, unknown>
    if (typeof record['text'] !== 'string' || record['text'].length > MAX_STT_TEXT_CHARS) {
      throw new Error('voice bridge /api/stt response exceeds the text limit')
    }
    const result = {
      text: record['text'],
      language: typeof record['language'] === 'string' ? record['language'] : undefined,
      trace_id: typeof record['trace_id'] === 'string' ? record['trace_id'] : undefined,
    }
    latencyEvent('stt.http', {
      trace_id: result.trace_id || resp.headers.get('X-Voice-Trace-Id') || traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: resp.status,
      audio_bytes: pcm16.byteLength,
      session_id_present: options?.sessionId !== undefined,
    })
    return { text: result.text, language: result.language, traceId: result.trace_id || traceId }
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('voice bridge /api/stt failed:')) throw error
    latencyEvent('stt.http', {
      trace_id: traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: 'error',
      audio_bytes: pcm16.byteLength,
    })
    throw error
  }
}

/** Text to speech: { text } -> 16 kHz mono PCM16 WAV bytes. */
export async function tts(text: string, signal?: AbortSignal, parentTraceId?: string, options?: BridgeRequestOptions): Promise<ArrayBuffer> {
  if (text.trim() === '' || text.length > MAX_TTS_TEXT_CHARS) throw new Error('voice bridge /api/tts request exceeds the text limit')
  const started = monotonicNow()
  const traceId = parentTraceId || newTraceId()
  const init: RequestInit = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Voice-Trace-Id': traceId,
      ...sessionHeaders(options),
    },
    body: JSON.stringify({ text, character: options?.character ?? 'default', session_id: options?.sessionId }),
  }
  if (signal !== undefined) init.signal = signal
  try {
    const resp = await fetch(`${bridgeBase()}/api/tts`, init)
    if (!resp.ok) {
      latencyEvent('tts.http', {
        trace_id: traceId,
        duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
        status: resp.status,
        text_chars: text.length,
      })
      throw new Error(`voice bridge /api/tts failed: ${resp.status}`)
    }
    const result = await readResponseBytes(resp, MAX_TTS_RESPONSE_BYTES)
    latencyEvent('tts.http', {
      trace_id: resp.headers.get('X-Voice-Trace-Id') || traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: resp.status,
      text_chars: text.length,
      session_id_present: options?.sessionId !== undefined,
      audio_bytes: result.byteLength,
    })
    return result
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('voice bridge /api/tts failed:')) throw error
    latencyEvent('tts.http', {
      trace_id: traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: error instanceof Error && error.name === 'AbortError' ? 'aborted' : 'error',
      text_chars: text.length,
    })
    throw error
  }
}

/** Stream raw PCM chunks instead of waiting for a complete WAV response. */
export async function ttsStream(
  text: string,
  onChunk: (chunk: TtsPcmChunk) => boolean,
  signal?: AbortSignal,
  parentTraceId?: string,
  options?: BridgeRequestOptions,
): Promise<TtsStreamResult> {
  if (text.trim() === '' || text.length > MAX_TTS_TEXT_CHARS) throw new Error('voice bridge /api/tts/stream request exceeds the text limit')
  const started = monotonicNow()
  const traceId = parentTraceId || newTraceId()
  const init: RequestInit = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Voice-Trace-Id': traceId,
      ...sessionHeaders(options),
    },
    body: JSON.stringify({
      text,
      character: options?.character ?? 'default',
      session_id: options?.sessionId,
      turn_id: options?.turnId,
      generation: options?.generation ?? 0,
      // Every ttsStream call owns one complete bounded utterance.  Defaulting
      // this to false leaves LiveTalking's continuity turn active forever
      // when callers omit an explicit logical-reply terminator, causing the
      // Avatar to insert silence indefinitely after playback drains.  A
      // specialised caller that deliberately spans multiple HTTP requests
      // can still opt out with end:false.
      end: options?.end ?? true,
    }),
  }
  if (signal !== undefined) init.signal = signal
  try {
    const response = await fetch(`${bridgeBase()}/api/tts/stream`, init)
    if (!response.ok) throw new Error(`voice bridge /api/tts/stream failed: ${response.status}`)
    if (response.body === null) throw new Error('voice bridge /api/tts/stream response has no body')
    const format = response.headers.get('X-Voice-Audio-Format')
    const sampleRate = Number(response.headers.get('X-Voice-Sample-Rate'))
    const channels = Number(response.headers.get('X-Voice-Channels'))
    if (format !== 'pcm_s16le' || !Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000 || channels !== 1) {
      throw new Error('voice bridge /api/tts/stream response format is invalid')
    }
    const reader = response.body.getReader()
    let carry: number | undefined
    let bytes = 0
    let chunks = 0
    let first = true
    try {
      while (true) {
        const next = await reader.read()
        if (next.done) break
        bytes += next.value.byteLength
        if (bytes > MAX_TTS_RESPONSE_BYTES) {
          await reader.cancel()
          throw new Error('voice bridge response exceeds the size limit')
        }
        let value = next.value
        if (carry !== undefined) {
          const joined = new Uint8Array(value.byteLength + 1)
          joined[0] = carry
          joined.set(value, 1)
          value = joined
          carry = undefined
        }
        if (value.byteLength % 2 !== 0) {
          carry = value[value.byteLength - 1]
          value = value.subarray(0, value.byteLength - 1)
        }
        if (value.byteLength === 0) continue
        const pcm = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)
        if (first) {
          first = false
          latencyEvent('tts.first_pcm', {
            trace_id: response.headers.get('X-Voice-Trace-Id') || traceId,
            duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
            audio_bytes: value.byteLength,
          })
        }
        if (!onChunk({ pcm, sampleRate, channels: 1 })) {
          await reader.cancel()
          throw new Error('voice speaker rejected a streaming PCM chunk')
        }
        chunks += 1
      }
    } finally {
      reader.releaseLock()
    }
    if (carry !== undefined) throw new Error('voice bridge /api/tts/stream returned truncated PCM')
    if (chunks === 0) throw new Error('voice bridge /api/tts/stream returned no audio')
    const result = { traceId: response.headers.get('X-Voice-Trace-Id') || traceId, chunks, bytes, sampleRate }
    latencyEvent('tts.http', {
      trace_id: result.traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: response.status,
      text_chars: text.length,
      session_id_present: options?.sessionId !== undefined,
      audio_bytes: bytes,
      chunks,
      streaming: true,
    })
    return result
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('voice bridge /api/tts/stream failed:')) throw error
    latencyEvent('tts.http', {
      trace_id: traceId,
      duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
      status: error instanceof Error && error.name === 'AbortError' ? 'aborted' : 'error',
      text_chars: text.length,
      streaming: true,
    })
    throw error
  }
}

/**
 * Streaming silero VAD client for barge-in detection (the server-side VAD of
 * the original speech-to-speech project). While a reply is playing the mic
 * recorder pushes PCM16 chunks here; the bridge replies
 * `{ event: 'speech_start' }` only when a REAL human voice is detected — TTS
 * echo / music / ambient noise never trip it.
 */
export class VadStream {
  private ws: WebSocket | null = null
  private buffered: ArrayBuffer[] = []
  private closed = false
  private connected = false
  private generation = 0

  /** Whether the VAD socket is actually connected. The recorder falls back to
   *  RMS heuristics while this is false (e.g. an old bridge without /api/vad). */
  get available(): boolean {
    return this.connected
  }

  /**
   * @param onSpeechStart - fired once when silero VAD hears speech.
   */
  open(onSpeechStart: () => void): void {
    if (this.ws !== null) return
    this.closed = false
    const generation = ++this.generation
    const proto = bridgeBase().startsWith('https:') ? 'wss:' : 'ws:'
    const url = `${proto}//${bridgeBase().replace(/^https?:\/\//, '')}/api/vad`
    const ws = new WebSocket(url)
    this.ws = ws
    ws.binaryType = 'arraybuffer'
    ws.onopen = () => {
      if (this.ws !== ws || this.generation !== generation || this.closed) return
      this.connected = true
      for (const chunk of this.buffered.splice(0)) {
        ws.send(chunk)
      }
    }
    ws.onmessage = (event) => {
      if (this.ws !== ws || this.generation !== generation || this.closed) return
      try {
        const msg = JSON.parse(String(event.data)) as { event?: string }
        if (msg.event === 'speech_start') onSpeechStart()
      } catch {
        // ignore malformed frames
      }
    }
    ws.onclose = () => {
      // An old socket may close after a rapid close/reopen. Its callback is
      // never allowed to mutate the new socket's connection state.
      if (this.ws !== ws || this.generation !== generation) return
      this.ws = null
      this.connected = false
    }
  }

  /** Push one 16 kHz PCM16 chunk (no-op while the socket is down). */
  send(pcm16: ArrayBuffer): void {
    const ws = this.ws
    if (ws === null || this.closed) return
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(pcm16)
    } else if (ws.readyState === WebSocket.CONNECTING) {
      this.buffered.push(pcm16)
      if (this.buffered.length > 64) this.buffered.shift()
    }
  }

  close(): void {
    this.closed = true
    this.generation += 1
    this.buffered = []
    const ws = this.ws
    this.ws = null
    if (ws !== null) {
      try { ws.close() } catch { /* already closed */ }
    }
  }
}
