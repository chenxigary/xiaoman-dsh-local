import test from 'node:test'
import assert from 'node:assert/strict'
import { VadStream } from '../src/client/bridge.ts'
import { MAX_UTTERANCE_BYTES, MAX_UTTERANCE_MS, MicRecorder } from '../src/client/voice/recorder.ts'

type RecorderInternals = {
  onChunk(buffer: ArrayBuffer): void
  onLevel(rms: number): void
  flush(): void
}

class TestVadStream extends VadStream {
  speechStart: (() => void) | undefined

  override get available(): boolean { return true }
  override open(callback: () => void): void { this.speechStart = callback }
  override close(): void { this.speechStart = undefined }
  override send(_pcm16: ArrayBuffer): void {}
}

function internals(recorder: MicRecorder): RecorderInternals {
  return recorder as unknown as RecorderInternals
}

function chunk(value: number, bytes = 1_000): ArrayBuffer {
  const buffer = new Uint8Array(bytes)
  buffer.fill(value)
  return buffer.buffer
}

test('normal speech uses a fixed pre-roll ring and drops older audio', () => {
  const utterances: ArrayBuffer[] = []
  const recorder = new MicRecorder({
    preRollMs: 100,
    onUtterance: pcm => utterances.push(pcm),
  })
  const capture = internals(recorder)
  capture.onChunk(chunk(1))
  capture.onChunk(chunk(2))
  capture.onChunk(chunk(3))
  capture.onChunk(chunk(4))
  capture.onLevel(0.5)
  capture.flush()

  assert.equal(utterances.length, 1)
  assert.equal(utterances[0]?.byteLength, 3_000)
  assert.equal(new Uint8Array(utterances[0] ?? [])[0], 2)
  assert.equal(new Uint8Array(utterances[0] ?? [])[2_999], 4)
})

test('interrupt/VAD capture preserves the bounded ring and hard-caps an utterance', () => {
  const vad = new TestVadStream()
  let interrupted = 0
  const utterances: ArrayBuffer[] = []
  const recorder = new MicRecorder({
    preRollMs: 100,
    vad,
    onSpeechInterrupt: () => { interrupted += 1 },
    onUtterance: pcm => utterances.push(pcm),
  })
  const capture = internals(recorder)
  recorder.setInterruptMode(true)
  capture.onChunk(chunk(7))
  capture.onChunk(chunk(8))
  capture.onChunk(chunk(9))
  capture.onChunk(chunk(10))
  vad.speechStart?.()
  assert.equal(interrupted, 1)

  capture.onChunk(new ArrayBuffer(1_045_576))
  assert.equal(utterances.length, 1)
  assert.equal(utterances[0]?.byteLength, MAX_UTTERANCE_BYTES)
  assert.equal(recorder.active, false)
})

test('pre-roll counts toward the unified byte and duration budget', async () => {
  const utterances: ArrayBuffer[] = []
  const recorder = new MicRecorder({
    // Keep a 120 ms ring (1,920 bytes at 16 kHz PCM16) before speech.
    preRollMs: 60,
    maxUtteranceMs: MAX_UTTERANCE_MS,
    onUtterance: pcm => utterances.push(pcm),
  })
  const capture = internals(recorder)
  capture.onChunk(new ArrayBuffer(1_920))
  capture.onLevel(0.5)
  capture.onChunk(new ArrayBuffer(MAX_UTTERANCE_BYTES))
  assert.equal(utterances.length, 1)
  assert.equal(utterances[0]?.byteLength, MAX_UTTERANCE_BYTES)
  assert.equal(utterances[0]?.byteLength <= MAX_UTTERANCE_BYTES, true)
  await new Promise(resolve => setTimeout(resolve, 1))
})

test('duration endpoint flushes a spoken capture even without a silence chunk', async () => {
  const utterances: ArrayBuffer[] = []
  const recorder = new MicRecorder({
    preRollMs: 0,
    maxUtteranceMs: 1,
    onUtterance: pcm => utterances.push(pcm),
  })
  const capture = internals(recorder)
  capture.onLevel(0.5)
  capture.onChunk(chunk(11, 100))
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(utterances.length, 1)
  assert.equal(utterances[0]?.byteLength, 100)
})

class DeferredTrack {
  stopped = false
  stop(): void { this.stopped = true }
}

class DeferredStream {
  readonly track = new DeferredTrack()
  getTracks(): DeferredTrack[] { return [this.track] }
}

class DeferredPort {
  onmessage: ((event: { data: unknown }) => void) | null = null
  closed = false
  close(): void { this.closed = true }
  postMessage(_value: unknown): void {}
}

class DeferredNode {
  readonly port = new DeferredPort()
  static created = 0
  constructor(_ctx: unknown, _name: string, _options: unknown) { DeferredNode.created += 1 }
}

class DeferredSource {
  disconnected = false
  connect(_node: unknown): void {}
  disconnect(): void { this.disconnected = true }
}

class DeferredContext {
  static instances: DeferredContext[] = []
  readonly state = 'running'
  readonly sources: DeferredSource[] = []
  readonly audioWorklet = {
    addModule: (_url: string) => {
      this.addModuleCalls += 1
      return new Promise<void>(resolve => { this.resolveAddModule = resolve })
    },
  }
  addModuleCalls = 0
  resolveAddModule: (() => void) | undefined
  closed = false
  constructor(_options: unknown) { DeferredContext.instances.push(this) }
  createMediaStreamSource(_stream: unknown): DeferredSource {
    const source = new DeferredSource()
    this.sources.push(source)
    return source
  }
  async close(): Promise<void> { this.closed = true }
}

async function withDeferredAudio<T>(getUserMedia: () => Promise<DeferredStream>, run: () => Promise<T>): Promise<T> {
  const previousAudioContext = globalThis.AudioContext
  const previousWorkletNode = globalThis.AudioWorkletNode
  const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const previousUrl = globalThis.URL
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { mediaDevices: { getUserMedia } },
  })
  globalThis.AudioContext = DeferredContext as unknown as typeof AudioContext
  globalThis.AudioWorkletNode = DeferredNode as unknown as typeof AudioWorkletNode
  globalThis.URL = {
    createObjectURL: () => 'blob:mic',
    revokeObjectURL: () => {},
  } as unknown as typeof URL
  try {
    return await run()
  } finally {
    globalThis.AudioContext = previousAudioContext
    globalThis.AudioWorkletNode = previousWorkletNode
    globalThis.URL = previousUrl
    if (previousNavigator === undefined) delete (globalThis as { navigator?: unknown }).navigator
    else Object.defineProperty(globalThis, 'navigator', previousNavigator)
    DeferredContext.instances.length = 0
    DeferredNode.created = 0
  }
}

test('stop during getUserMedia fences late stream and context without creating a node', async () => {
  let resolveMedia: ((stream: DeferredStream) => void) | undefined
  const media = new Promise<DeferredStream>(resolve => { resolveMedia = resolve })
  await withDeferredAudio(() => media, async () => {
    const recorder = new MicRecorder({ onUtterance: () => {} })
    const starting = recorder.start()
    const context = DeferredContext.instances[0]
    assert.ok(context)
    recorder.stop()
    const stream = new DeferredStream()
    resolveMedia?.(stream)
    await starting
    assert.equal(stream.track.stopped, true)
    assert.equal(context.closed, true)
    assert.equal(context.addModuleCalls, 0)
    assert.equal(DeferredNode.created, 0)
    assert.equal(recorder.active, false)
  })
})

test('stop during addModule fences late worklet setup and releases the stream', async () => {
  const stream = new DeferredStream()
  await withDeferredAudio(async () => stream, async () => {
    const recorder = new MicRecorder({ onUtterance: () => {} })
    const starting = recorder.start()
    const context = DeferredContext.instances[0]
    assert.ok(context)
    for (let index = 0; index < 4 && context.addModuleCalls === 0; index += 1) await Promise.resolve()
    assert.equal(context.addModuleCalls, 1)
    recorder.stop()
    context.resolveAddModule?.()
    await starting
    assert.equal(stream.track.stopped, true)
    assert.equal(context.closed, true)
    assert.equal(DeferredNode.created, 0)
    assert.equal(recorder.active, false)
  })
})
