import test from 'node:test'
import assert from 'node:assert/strict'
import {
  isAllowedBridgeBase,
  MAX_STT_AUDIO_BYTES,
  MAX_STT_RESPONSE_BYTES,
  MAX_STT_TEXT_CHARS,
  MAX_TTS_TEXT_CHARS,
  MAX_TTS_RESPONSE_BYTES,
  VadStream,
  stt,
  tts,
  ttsStream,
} from '../src/client/bridge.ts'

class FakeVadSocket {
  static readonly OPEN = 1
  static readonly CONNECTING = 0
  static instances: FakeVadSocket[] = []
  readyState = FakeVadSocket.CONNECTING
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  closed = false
  constructor(_url: string) { FakeVadSocket.instances.push(this) }
  send(_value: unknown): void {}
  close(): void { this.closed = true }
}

test('bridge base accepts only fixed loopback origins', () => {
  assert.equal(isAllowedBridgeBase('http://127.0.0.1:8765'), true)
  assert.equal(isAllowedBridgeBase('http://localhost:8765/'), true)
  assert.equal(isAllowedBridgeBase('http://[::1]:8765'), true)
  assert.equal(isAllowedBridgeBase('https://127.0.0.1:8765'), false)
  assert.equal(isAllowedBridgeBase('http://127.0.0.1:8765/?remote=1'), false)
  assert.equal(isAllowedBridgeBase('http://example.com:8765'), false)
})

test('STT rejects audio over the 30-second byte ceiling before fetch', async () => {
  const original = globalThis.fetch
  let called = false
  globalThis.fetch = (async () => {
    called = true
    return new Response('{}')
  }) as typeof fetch
  try {
    await assert.rejects(
      () => stt(new Uint8Array(MAX_STT_AUDIO_BYTES + 1).buffer),
      /audio limit/,
    )
    assert.equal(called, false)
  } finally {
    globalThis.fetch = original
  }
})

test('STT JSON and TTS bytes have bounded response bodies without exposing payloads', async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = (async () => new Response('x'.repeat(MAX_STT_RESPONSE_BYTES + 1))) as typeof fetch
    await assert.rejects(
      () => stt(new Uint8Array(16).buffer),
      error => error instanceof Error && !error.message.includes('x'.repeat(32)) && error.message.includes('size limit'),
    )

    globalThis.fetch = (async () => new Response(new Uint8Array(MAX_TTS_RESPONSE_BYTES + 1))) as typeof fetch
    await assert.rejects(
      () => tts('你好'),
      error => error instanceof Error && error.message.includes('size limit'),
    )

    globalThis.fetch = (async () => new Response('bridge secret', { status: 500 })) as typeof fetch
    await assert.rejects(
      () => stt(new Uint8Array(16).buffer),
      error => error instanceof Error && !error.message.includes('bridge secret'),
    )

    globalThis.fetch = (async () => new Response(JSON.stringify({ text: '长'.repeat(MAX_STT_TEXT_CHARS + 1) }))) as typeof fetch
    await assert.rejects(() => stt(new Uint8Array(16).buffer), /text limit/)
  } finally {
    globalThis.fetch = original
  }
})

test('TTS rejects a sentence beyond the server character boundary before fetch', async () => {
  const original = globalThis.fetch
  let called = false
  globalThis.fetch = (async () => {
    called = true
    return new Response(new Uint8Array())
  }) as typeof fetch
  try {
    await assert.rejects(() => tts('长'.repeat(MAX_TTS_TEXT_CHARS + 1)), /text limit/)
    assert.equal(called, false)
  } finally {
    globalThis.fetch = original
  }
})

test('streaming TTS forwards aligned PCM before the response completes', async () => {
  const original = globalThis.fetch
  let posted: Record<string, unknown> | undefined
  globalThis.fetch = (async (_input, init) => {
    posted = JSON.parse(String(init?.body)) as Record<string, unknown>
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(Uint8Array.from([1, 2, 3]))
        controller.enqueue(Uint8Array.from([4, 5, 6]))
        controller.close()
      },
    })
    return new Response(body, {
      headers: {
        'X-Voice-Audio-Format': 'pcm_s16le',
        'X-Voice-Sample-Rate': '16000',
        'X-Voice-Channels': '1',
        'X-Voice-Trace-Id': 'server-trace',
      },
    })
  }) as typeof fetch
  try {
    const chunks: number[][] = []
    const result = await ttsStream('你好', ({ pcm }) => {
      chunks.push([...new Uint8Array(pcm)])
      return true
    }, undefined, 'parent-trace', {
      sessionId: 'session-1',
      character: 'xiaoman',
      turnId: 'turn-1',
      generation: 7,
    })
    assert.deepEqual(chunks, [[1, 2], [3, 4, 5, 6]])
    assert.equal(result.traceId, 'server-trace')
    assert.equal(result.bytes, 6)
    assert.equal(result.chunks, 2)
    assert.equal(posted?.['turn_id'], 'turn-1')
    assert.equal(posted?.['generation'], 7)
    assert.equal(posted?.['end'], true)
  } finally {
    globalThis.fetch = original
  }
})

test('VAD ignores late frames and close callbacks from an old socket generation', () => {
  const previous = globalThis.WebSocket
  FakeVadSocket.instances.length = 0
  globalThis.WebSocket = FakeVadSocket as unknown as typeof WebSocket
  try {
    const vad = new VadStream()
    let speechStarts = 0
    vad.open(() => { speechStarts += 1 })
    const first = FakeVadSocket.instances[0]!
    first.readyState = FakeVadSocket.OPEN
    first.onopen?.()
    vad.close()
    vad.open(() => { speechStarts += 1 })
    const second = FakeVadSocket.instances[1]!
    second.readyState = FakeVadSocket.OPEN
    second.onopen?.()
    first.onmessage?.({ data: JSON.stringify({ event: 'speech_start' }) })
    first.onclose?.()
    assert.equal(vad.available, true)
    assert.equal(speechStarts, 0)
    second.onmessage?.({ data: JSON.stringify({ event: 'speech_start' }) })
    assert.equal(speechStarts, 1)
    vad.close()
  } finally {
    globalThis.WebSocket = previous
  }
})
