import test from 'node:test'
import assert from 'node:assert/strict'
import { ReplySpeaker } from '../src/client/voice/speaker.ts'

class DeferredAudioContext {
  static instances: DeferredAudioContext[] = []
  readonly state = 'running'
  readonly destination = {}
  currentTime = 1
  readonly decodes: Array<(value: AudioBuffer) => void> = []
  readonly sources: FakeSource[] = []

  constructor() { DeferredAudioContext.instances.push(this) }
  async resume(): Promise<void> {}
  decodeAudioData(_wav: ArrayBuffer): Promise<AudioBuffer> {
    return new Promise(resolve => { this.decodes.push(resolve) })
  }
  createBufferSource(): AudioBufferSourceNode {
    const source = new FakeSource()
    this.sources.push(source)
    return source as unknown as AudioBufferSourceNode
  }
  createBuffer(_channels: number, length: number, sampleRate: number): AudioBuffer {
    const channel = new Float32Array(length)
    return {
      duration: length / sampleRate,
      getChannelData: () => channel,
    } as unknown as AudioBuffer
  }
  async close(): Promise<void> {}
}

class FakeSource {
  buffer: AudioBuffer | null = null
  onended: (() => void) | null = null
  connected = false
  startAt: number | undefined
  connect(_destination: unknown): void { this.connected = true }
  start(at?: number): void { this.startAt = at }
  stop(): void { this.onended?.() }
}

test('PCM chunks share one AudioContext timeline without sentence gaps', () => {
  const previous = globalThis.AudioContext
  globalThis.AudioContext = DeferredAudioContext as unknown as typeof AudioContext
  try {
    const speaker = new ReplySpeaker()
    const first = speaker.speakPcm(new Int16Array(1600).buffer, 16000)
    const second = speaker.speakPcm(new Int16Array(800).buffer, 16000, true)
    const context = DeferredAudioContext.instances.at(-1)
    assert.ok(context)
    assert.equal(first.accepted, true)
    assert.equal(second.accepted, true)
    assert.equal(first.scheduleGapMs, 0)
    assert.equal(second.scheduleGapMs, 0)
    assert.equal(context.sources[0]?.startAt, 1.025)
    assert.equal(context.sources[1]?.startAt, 1.125)
    assert.equal(context.sources[0]?.connected, true)
    assert.equal(context.sources[1]?.connected, false)
    assert.equal(speaker.speaking, true)
    speaker.stop()
    assert.equal(speaker.speaking, false)
    speaker.dispose()
  } finally {
    globalThis.AudioContext = previous
    DeferredAudioContext.instances.length = 0
  }
})

test('late PCM chunk exposes the exact audible scheduling gap', () => {
  const previous = globalThis.AudioContext
  globalThis.AudioContext = DeferredAudioContext as unknown as typeof AudioContext
  try {
    const speaker = new ReplySpeaker()
    const first = speaker.speakPcm(new Int16Array(1600).buffer, 16000)
    const context = DeferredAudioContext.instances.at(-1)
    assert.ok(context)
    assert.equal(first.scheduleGapMs, 0)
    // First chunk ends at 1.125. Arrival at 1.200 plus the safety lead makes
    // the next start 1.225: exactly 100 ms of user-audible silence.
    context.currentTime = 1.2
    const late = speaker.speakPcm(new Int16Array(1600).buffer, 16000)
    assert.equal(context.sources[1]?.startAt, 1.2249999999999999)
    assert.equal(late.scheduleGapMs, 100)
    assert.equal(late.scheduledAheadMs, 125)
    speaker.dispose()
  } finally {
    globalThis.AudioContext = previous
    DeferredAudioContext.instances.length = 0
  }
})

test('stop fences a pending decode so stale audio cannot resume playback', async () => {
  const previous = globalThis.AudioContext
  globalThis.AudioContext = DeferredAudioContext as unknown as typeof AudioContext
  try {
    const speaker = new ReplySpeaker()
    speaker.speak(new ArrayBuffer(4))
    speaker.speak(new ArrayBuffer(4))
    const context = DeferredAudioContext.instances.at(-1)
    assert.ok(context)
    assert.equal(context.decodes.length, 1)
    speaker.stop()
    context.decodes[0]?.({ duration: 0.1 } as AudioBuffer)
    await Promise.resolve()
    await Promise.resolve()
    assert.equal(speaker.speaking, false)
    assert.equal(context.sources.length, 0)
    speaker.dispose()
  } finally {
    globalThis.AudioContext = previous
    DeferredAudioContext.instances.length = 0
  }
})

test('speaker reports bounded queue backpressure instead of marking a job spoken', async () => {
  const previous = globalThis.AudioContext
  globalThis.AudioContext = DeferredAudioContext as unknown as typeof AudioContext
  try {
    const speaker = new ReplySpeaker()
    const first = speaker.speak(new ArrayBuffer(4))
    assert.equal(first.accepted, true)
    let rejected = first
    for (let index = 0; index < 100; index += 1) {
      rejected = speaker.speak(new ArrayBuffer(4))
    }
    assert.equal(rejected.accepted, false)
    assert.equal(rejected.reason, 'count_limit')
    assert.equal(rejected.count <= 64, true)
    assert.equal(rejected.bytes <= 8 * 1024 * 1024, true)
    speaker.stop()
    speaker.dispose()
  } finally {
    globalThis.AudioContext = previous
    DeferredAudioContext.instances.length = 0
  }
})
