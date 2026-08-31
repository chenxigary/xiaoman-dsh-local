import { latencyEvent, monotonicNow } from '../latency.ts'

/** Renderer-only speaker face; playback ownership remains in the apply fiber. */
export interface ReplySpeakerPort {
  readonly speaking: boolean
  speak(wav: ArrayBuffer): SpeakerEnqueueResult
  speakPcm(pcm: ArrayBuffer, sampleRate: number, muted?: boolean): SpeakerEnqueueResult
  stop(): void
  onSpeakingChange(listener: () => void): () => void
}

export const MAX_SPEAKER_QUEUE_COUNT = 64
export const MAX_SPEAKER_QUEUE_BYTES = 8 * 1024 * 1024

export interface SpeakerEnqueueResult {
  readonly accepted: boolean
  readonly count: number
  readonly bytes: number
  readonly reason?: 'accepted' | 'empty' | 'disposed' | 'count_limit' | 'byte_limit'
  /** Silence introduced because this PCM chunk arrived after the scheduled tail. */
  readonly scheduleGapMs?: number
  /** Buffered PCM remaining on the AudioContext clock after this enqueue. */
  readonly scheduledAheadMs?: number
}

/**
 * ReplySpeaker: plays synthesized reply WAVs through an AudioContext as a
 * FIFO queue (sentence streaming). `speak()` enqueues; the drain plays one
 * clip at a time and `onended` advances to the next, so sentence N+1 starts
 * the moment N finishes (gaps between clips are tiny). `speaking` stays true
 * while anything is queued or playing, so the companion window and the mic
 * echo-guard follow the whole reply, not individual clips.
 *
 * Interruption: `stop()` halts the current clip and clears the queue; a
 * generation counter discards clips that were queued but not yet started.
 */
export class ReplySpeaker implements ReplySpeakerPort {
  private ctx: AudioContext | null = null
  private queue: Array<{ readonly wav: ArrayBuffer; readonly generation: number }> = []
  private currentItem: { readonly wav: ArrayBuffer; readonly generation: number } | null = null
  private playing: AudioBufferSourceNode | null = null
  private drainRunning = false
  private pcmSources = new Map<AudioBufferSourceNode, { readonly bytes: number; readonly generation: number }>()
  private nextPcmStartTime = 0
  private gen = 0
  private disposed = false
  private listeners = new Set<() => void>()

  /** True while a reply is being read (playing or waiting in the queue). */
  get speaking(): boolean {
    return this.playing !== null || this.currentItem !== null || this.queue.length > 0 || this.pcmSources.size > 0
  }

  /** Register a renderer listener; returns the disposer. */
  onSpeakingChange(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private emit(): void {
    for (const listener of this.listeners) listener()
  }

  /** Enqueue one clip; rejection is explicit backpressure, never a silent drop. */
  speak(wav: ArrayBuffer): SpeakerEnqueueResult {
    const currentCount = this.currentItem === null ? 0 : 1
    const queuedBytes = this.queue.reduce((total, item) => total + item.wav.byteLength, 0)
    const currentBytes = this.currentItem?.wav.byteLength ?? 0
    const count = currentCount + this.queue.length
    const bytes = currentBytes + queuedBytes
    if (wav.byteLength === 0) return { accepted: false, count, bytes, reason: 'empty' }
    if (this.disposed) return { accepted: false, count, bytes, reason: 'disposed' }
    if (count >= MAX_SPEAKER_QUEUE_COUNT) {
      return { accepted: false, count, bytes, reason: 'count_limit' }
    }
    if (bytes + wav.byteLength > MAX_SPEAKER_QUEUE_BYTES) {
      return { accepted: false, count, bytes, reason: 'byte_limit' }
    }
    this.queue.push({ wav, generation: this.gen })
    latencyEvent('speaker.enqueue', {
      audio_bytes: wav.byteLength,
      queue_depth: this.queue.length,
    })
    this.emit()
    void this.drain()
    return { accepted: true, count: count + 1, bytes: bytes + wav.byteLength, reason: 'accepted' }
  }

  /**
   * Schedule one PCM16 chunk on the AudioContext clock.  Every subsequent
   * chunk starts at the prior chunk's exact end time, including chunks from
   * the next sentence request, so transport jitter cannot insert silence.
   * `muted` keeps the clock/state projection alive while WebRTC owns sound.
   */
  speakPcm(pcm: ArrayBuffer, sampleRate: number, muted = false): SpeakerEnqueueResult {
    const wavCount = (this.currentItem === null ? 0 : 1) + this.queue.length
    const wavBytes = (this.currentItem?.wav.byteLength ?? 0)
      + this.queue.reduce((total, item) => total + item.wav.byteLength, 0)
    const pcmBytes = [...this.pcmSources.values()].reduce((total, item) => total + item.bytes, 0)
    const count = wavCount + this.pcmSources.size
    const bytes = wavBytes + pcmBytes
    if (pcm.byteLength === 0 || pcm.byteLength % 2 !== 0 || !Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000) {
      return { accepted: false, count, bytes, reason: 'empty' }
    }
    if (this.disposed) return { accepted: false, count, bytes, reason: 'disposed' }
    if (count >= MAX_SPEAKER_QUEUE_COUNT) return { accepted: false, count, bytes, reason: 'count_limit' }
    if (bytes + pcm.byteLength > MAX_SPEAKER_QUEUE_BYTES) return { accepted: false, count, bytes, reason: 'byte_limit' }

    const ctx = this.ctx ?? (this.ctx = new AudioContext())
    if (ctx.state === 'suspended') void ctx.resume().catch(() => {})
    const samples = new Int16Array(pcm)
    const buffer = ctx.createBuffer(1, samples.length, sampleRate)
    const channel = buffer.getChannelData(0)
    for (let index = 0; index < samples.length; index += 1) channel[index] = (samples[index] ?? 0) / 32768
    const source = ctx.createBufferSource()
    source.buffer = buffer
    if (!muted) source.connect(ctx.destination)
    const generation = this.gen
    const previousEnd = this.nextPcmStartTime
    const startAt = Math.max(ctx.currentTime + 0.025, previousEnd)
    const scheduleGapMs = previousEnd > 0
      ? Math.round(Math.max(0, startAt - previousEnd) * 100_000) / 100
      : 0
    this.nextPcmStartTime = startAt + buffer.duration
    const scheduledAheadMs = Math.round(Math.max(0, this.nextPcmStartTime - ctx.currentTime) * 1000)
    this.pcmSources.set(source, { bytes: pcm.byteLength, generation })
    source.onended = () => {
      const owned = this.pcmSources.get(source)
      this.pcmSources.delete(source)
      if (this.pcmSources.size === 0) this.nextPcmStartTime = 0
      latencyEvent('speaker.pcm_playback', {
        audio_bytes: pcm.byteLength,
        audio_seconds: Math.round(buffer.duration * 1000) / 1000,
        status: owned?.generation === this.gen ? 'ok' : 'stopped',
        remote_audio: muted,
      })
      this.emit()
    }
    try {
      source.start(startAt)
    } catch {
      this.pcmSources.delete(source)
      if (this.pcmSources.size === 0) this.nextPcmStartTime = 0
      return { accepted: false, count, bytes, reason: 'disposed' }
    }
    latencyEvent('speaker.pcm_enqueue', {
      audio_bytes: pcm.byteLength,
      queue_depth: this.pcmSources.size,
      scheduled_ahead_ms: scheduledAheadMs,
      schedule_gap_ms: scheduleGapMs,
      remote_audio: muted,
    })
    this.emit()
    return {
      accepted: true,
      count: count + 1,
      bytes: bytes + pcm.byteLength,
      reason: 'accepted',
      scheduleGapMs,
      scheduledAheadMs,
    }
  }

  private async drain(): Promise<void> {
    if (this.drainRunning) return
    this.drainRunning = true
    try {
      const ctx = this.ctx ?? (this.ctx = new AudioContext())
      if (ctx.state === 'suspended') {
        try {
          await ctx.resume()
        } catch {
          // best-effort
        }
      }
      while (this.queue.length > 0 && !this.disposed) {
        const item = this.queue.shift()!
        this.currentItem = item
        await this.playOne(ctx, item)
        this.currentItem = null
      }
    } finally {
      this.drainRunning = false
      this.emit()
    }
  }

  private playOne(ctx: AudioContext, item: { readonly wav: ArrayBuffer; readonly generation: number }): Promise<void> {
    const gen = item.generation
    const started = monotonicNow()
    return new Promise((resolve) => {
      void ctx.decodeAudioData(item.wav)
        .then((buffer) => {
          if (this.disposed || gen !== this.gen || item.generation !== this.gen) {
            resolve()
            return
          }
          const source = ctx.createBufferSource()
          source.buffer = buffer
          source.connect(ctx.destination)
          this.playing = source
          this.emit()
          source.onended = () => {
            latencyEvent('speaker.playback', {
              duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
              audio_seconds: Math.round(buffer.duration * 1000) / 1000,
              status: 'ok',
            })
            if (this.playing === source) {
              this.playing = null
              this.emit()
            }
            resolve()
          }
          source.start()
        })
        .catch(() => {
          latencyEvent('speaker.playback', {
            duration_ms: Math.round((monotonicNow() - started) * 1000) / 1000,
            status: 'decode_error',
          })
          resolve()
        })
    })
  }

  /** Stop the current clip immediately and clear the queued ones. */
  stop(): void {
    this.gen += 1
    if (this.playing !== null) {
      try {
        this.playing.stop()
      } catch {
        // already stopped
      }
      this.playing = null
    }
    this.queue = []
    this.currentItem = null
    const pcmSources = [...this.pcmSources.keys()]
    this.pcmSources.clear()
    this.nextPcmStartTime = 0
    for (const source of pcmSources) {
      try {
        source.stop()
      } catch {
        // already stopped
      }
    }
    this.emit()
  }

  /** Release the AudioContext (plugin teardown). */
  dispose(): void {
    this.disposed = true
    this.stop()
    if (this.ctx !== null) {
      void this.ctx.close().catch(() => {})
      this.ctx = null
    }
    this.listeners.clear()
  }
}
