/**
 * MicRecorder: mic capture with silence-based utterance endpointing.
 *
 * Reuses the embedded mic-capture AudioWorklet (16 kHz PCM16 chunks at ~40 ms
 * plus per-chunk RMS). The main thread accumulates chunks and, once speech has
 * started, ends the utterance after `minSilenceMs` of RMS below threshold
 * (or `maxUtteranceMs` hard cap), then hands the concatenated PCM16 to
 * `onUtterance`. One utterance per activation: the caller stops the recorder
 * after the callback fires (click-to-speak-one-turn semantics; continuous
 * listening is a T7 enhancement).
 */
import { MIC_CAPTURE_WORKLET_SOURCE } from '../worklets/mic-capture.ts'
import type { VadStream } from '../bridge.ts'

const DEFAULT_MIN_SILENCE_MS = 1800
export const MAX_UTTERANCE_MS = 30_000
/** 16 kHz mono PCM16 for one 30-second utterance (includes pre-roll). */
export const MAX_UTTERANCE_BYTES = MAX_UTTERANCE_MS / 1000 * 16_000 * 2
const DEFAULT_PRE_ROLL_MS = 1200
const PCM_BYTES_PER_SECOND = 16_000 * 2
/** Linear amplitude threshold (~ -40 dBFS). */
const DEFAULT_RMS_THRESHOLD = 0.01

export interface MicRecorderOptions {
  minSilenceMs?: number
  maxUtteranceMs?: number
  /** Fixed audio history retained before speech starts. */
  preRollMs?: number
  rmsThreshold?: number
  /** Streamed to the bridge's silero VAD while a reply is playing; its
   *  `speech_start` is the barge-in trigger. When absent, the recorder falls
   *  back to the RMS heuristics below (interruptThreshold / hold). */
  vad?: VadStream
  /** RMS threshold for barge-in detection during playback (fallback path). */
  interruptThreshold?: number
  /** Sustained above-threshold time (ms) before a barge-in fires (fallback). */
  interruptHoldMs?: number
  /** Noise gate threshold in dBFS (0 or undefined = disabled). Quiet ambient
   *  audio below this level is faded out of the SENT mic stream — mirrors the
   *  original project's worklet gate (the attack/hold/release envelope
   *  already lives in the embedded worklet; this just arms it). */
  noiseGateDb?: number
  /** Called once when speech is detected while a reply is playing (barge-in);
   *  the recorder then switches back to normal accumulation so the user's
   *  ongoing speech becomes the next utterance. */
  onSpeechInterrupt?: () => void
  /** Called once with the complete silence-endpointed utterance (PCM16). */
  onUtterance: (pcm16: ArrayBuffer) => void
}

/** Default barge-in level (~ -24 dBFS) and hold time.
 *  0.06 is well above TTS echo residue that slips past browser AEC, so a
 *  reply playing by itself rarely trips the interrupt; real speech is
 *  typically much louder than this. */
const DEFAULT_INTERRUPT_THRESHOLD = 0.06
const DEFAULT_INTERRUPT_HOLD_MS = 250
export class MicRecorder {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private node: AudioWorkletNode | null = null
  private chunks: ArrayBuffer[] = []
  private chunkBytes = 0
  private preRoll: ArrayBuffer[] = []
  private preRollBytes = 0
  private speaking = false
  private lastVoiceAt = 0
  private maxTimer: ReturnType<typeof setTimeout> | null = null
  private released = false
  private lifecycleGeneration = 0
  private paused = false
  private interruptMode = false
  private interruptArmed = false
  private interruptHoldStart = 0

  constructor(private readonly opts: MicRecorderOptions) {}

  get active(): boolean {
    return !this.released && this.ctx !== null
  }

  /**
   * Pause/resume capture. While paused, incoming chunks and levels are
   * dropped (nothing accumulates, no endpointing fires). Used sparingly —
   * during playback we use {@link setInterruptMode} instead so barge-in
   * detection keeps running.
   */
  setPaused(paused: boolean): void {
    if (this.paused === paused) return
    this.paused = paused
    if (paused) this.resetBuffers()
  }

  /**
   * Barge-in listening mode (during reply playback): chunks are streamed to
   * the bridge's silero VAD (never accumulated), which fires `speech_start`
   * only for a REAL human voice — TTS echo / music / ambient noise cannot
   * trip it. On `speech_start` the recorder leaves interrupt mode and the
   * user's ongoing speech accumulates normally.
   */
  setInterruptMode(enabled: boolean): void {
    if (this.interruptMode === enabled) return
    this.resetBuffers()
    this.interruptMode = enabled
    this.interruptArmed = false
    if (enabled) {
      this.opts.vad?.open(() => this.onVadSpeechStart())
    } else {
      this.opts.vad?.close()
    }
  }

  /** Bridge VAD heard a real voice: stop the reply and accumulate the user's
   *  ongoing speech. (silero's speech_start is already the confirmation — no
   *  RMS hold/confirm heuristics needed on this path.) */
  private onVadSpeechStart(): void {
    if (!this.interruptMode) return
    this.interruptMode = false
    this.interruptArmed = false
    this.opts.vad?.close()
    this.beginSpeech()
    this.opts.onSpeechInterrupt?.()
  }

  private resetBuffers(): void {
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    this.chunks = []
    this.chunkBytes = 0
    this.preRoll = []
    this.preRollBytes = 0
    this.speaking = false
    this.interruptArmed = false
  }

  /** Start an utterance and transfer only the bounded pre-roll ring. */
  private beginSpeech(): void {
    if (this.speaking) return
    this.speaking = true
    this.lastVoiceAt = performance.now()
    for (const chunk of this.preRoll) this.appendUtterance(chunk)
    const preRollMs = this.chunkBytes / PCM_BYTES_PER_SECOND * 1000
    this.preRoll = []
    this.preRollBytes = 0
    // The pre-roll is part of the same utterance budget.  Starting the
    // duration timer only after speech detection would otherwise permit a
    // 30-second speech segment on top of the retained history.
    if (this.speaking) this.armMaxTimer(preRollMs)
  }

  /** Keep the last fixed-duration audio without allowing pre-speech growth. */
  private appendPreRoll(buffer: ArrayBuffer): void {
    const configuredPreRollMs = this.opts.preRollMs ?? DEFAULT_PRE_ROLL_MS
    const maxBytes = Math.min(
      MAX_UTTERANCE_BYTES,
      Math.max(0, Number.isFinite(configuredPreRollMs)
        ? Math.floor(configuredPreRollMs / 1000 * PCM_BYTES_PER_SECOND)
        : DEFAULT_PRE_ROLL_MS / 1000 * PCM_BYTES_PER_SECOND),
    )
    if (maxBytes === 0) return
    const copy = buffer.byteLength > maxBytes ? buffer.slice(buffer.byteLength - maxBytes) : buffer.slice(0)
    this.preRoll.push(copy)
    this.preRollBytes += copy.byteLength
    while (this.preRoll.length > 0 && this.preRollBytes > maxBytes) {
      const first = this.preRoll.shift()!
      this.preRollBytes -= first.byteLength
    }
  }

  /** Append one chunk while enforcing the hard 960 kB utterance limit. */
  private appendUtterance(buffer: ArrayBuffer): void {
    const remaining = MAX_UTTERANCE_BYTES - this.chunkBytes
    if (remaining <= 0) {
      this.flush()
      return
    }
    const copy = buffer.byteLength > remaining ? buffer.slice(0, remaining) : buffer.slice(0)
    if (copy.byteLength === 0) return
    this.chunks.push(copy)
    this.chunkBytes += copy.byteLength
    if (this.chunkBytes >= MAX_UTTERANCE_BYTES) this.flush()
  }

  private ownsStart(generation: number, ctx: AudioContext): boolean {
    return this.lifecycleGeneration === generation && !this.released && this.ctx === ctx
  }

  private closeLateStart(ctx: AudioContext, stream: MediaStream | null, source: MediaStreamAudioSourceNode | null, node: AudioWorkletNode | null): void {
    node?.port.close()
    source?.disconnect()
    stream?.getTracks().forEach((track) => track.stop())
    void ctx.close().catch(() => {})
  }

  /** Acquire the mic and start the capture worklet. */
  async start(): Promise<void> {
    if (this.ctx !== null) this.stop()
    const generation = ++this.lifecycleGeneration
    this.released = false
    const ctx = new AudioContext({ latencyHint: 'interactive' })
    this.ctx = ctx
    let stream: MediaStream | null = null
    let source: MediaStreamAudioSourceNode | null = null
    let node: AudioWorkletNode | null = null
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      if (!this.ownsStart(generation, ctx)) {
        this.closeLateStart(ctx, stream, null, null)
        return
      }
      this.stream = stream
      const workletUrl = URL.createObjectURL(
        new Blob([MIC_CAPTURE_WORKLET_SOURCE], { type: 'text/javascript' }),
      )
      try {
        await ctx.audioWorklet.addModule(workletUrl)
      } finally {
        URL.revokeObjectURL(workletUrl)
      }
      if (!this.ownsStart(generation, ctx)) {
        this.closeLateStart(ctx, stream, null, null)
        return
      }
      source = ctx.createMediaStreamSource(stream)
      node = new AudioWorkletNode(ctx, 'mic-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: { chunkMs: 40 },
      })
      if (!this.ownsStart(generation, ctx)) {
        this.closeLateStart(ctx, stream, source, node)
        return
      }
      // Arm the noise gate (mirrors the original project): quiet ambient audio
      // below `noiseGateDb` is faded out of the SENT stream. The worklet already
      // carries the full attack/hold/release envelope — this message enables it.
      const gateDb = this.opts.noiseGateDb
      // dBFS values are NEGATIVE (e.g. -35); 0 or undefined means disabled —
      // a `> 0` guard would silently never arm the gate and ambient noise
      // would reach STT untouched (phantom messages).
      if (gateDb !== undefined && gateDb !== 0) {
        node.port.postMessage({ kind: 'gate', enabled: true, thresholdDb: gateDb })
      }
      node.port.onmessage = (e) => {
        if (this.released || this.lifecycleGeneration !== generation || this.node !== node) return
        const data = e.data
        if (data instanceof ArrayBuffer) this.onChunk(data)
        else if (data !== null && typeof data === 'object' && data.kind === 'level') this.onLevel(data.rms)
      }
      source.connect(node)
      this.source = source
      this.node = node
    } catch (error) {
      const current = this.ownsStart(generation, ctx)
      if (current) {
        this.released = true
        this.closeLateStart(ctx, stream, source, node)
        this.ctx = null
        this.stream = null
        this.source = null
        this.node = null
      } else {
        this.closeLateStart(ctx, stream, source, node)
      }
      if (current) throw error
    }
  }

  /** Stop capture and release the mic / AudioContext. */
  stop(): void {
    this.lifecycleGeneration += 1
    if (this.released && this.ctx === null && this.stream === null && this.node === null) return
    this.released = true
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    this.opts.vad?.close()
    this.node?.port.close()
    this.source?.disconnect()
    this.stream?.getTracks().forEach((track) => track.stop())
    void this.ctx?.close().catch(() => {})
    this.node = null
    this.source = null
    this.stream = null
    this.ctx = null
    this.chunks = []
    this.chunkBytes = 0
    this.preRoll = []
    this.preRollBytes = 0
    this.speaking = false
    this.interruptMode = false
    this.interruptArmed = false
  }

  private onLevel(rms: number): void {
    if (this.released || this.paused) return

    // Barge-in mode: stream to the bridge VAD (never accumulate). Without a
    // connected VadStream (bridge without the endpoint), fall back to RMS.
    if (this.interruptMode) {
      if (this.opts.vad === undefined || !this.opts.vad.available) {
        const threshold = this.opts.interruptThreshold ?? DEFAULT_INTERRUPT_THRESHOLD
        const now = performance.now()
        if (rms >= threshold) {
          if (!this.interruptArmed) {
            this.interruptArmed = true
            this.interruptHoldStart = now
          } else if (now - this.interruptHoldStart >= (this.opts.interruptHoldMs ?? DEFAULT_INTERRUPT_HOLD_MS)) {
            this.interruptMode = false
            this.beginSpeech()
            this.opts.onSpeechInterrupt?.()
            this.interruptArmed = false
          }
        } else {
          this.interruptArmed = false
        }
      }
      return
    }

    const threshold = this.opts.rmsThreshold ?? DEFAULT_RMS_THRESHOLD
    if (rms >= threshold) {
      this.lastVoiceAt = performance.now()
      if (!this.speaking) {
        this.beginSpeech()
      }
    }
  }

  private onChunk(buffer: ArrayBuffer): void {
    if (this.released || this.paused) return
    if (this.interruptMode) {
      // Barge-in mode: stream to the bridge VAD while retaining only the
      // fixed pre-roll ring. No unbounded pre-speech utterance can form.
      this.opts.vad?.send(buffer)
      this.appendPreRoll(buffer)
      return
    }
    if (!this.speaking) {
      this.appendPreRoll(buffer)
      return
    }
    this.appendUtterance(buffer)
    const silenceMs = performance.now() - this.lastVoiceAt
    if (silenceMs >= (this.opts.minSilenceMs ?? DEFAULT_MIN_SILENCE_MS)) this.flush()
  }

  private armMaxTimer(preRollMs = 0): void {
    if (this.maxTimer !== null) clearTimeout(this.maxTimer)
    const requestedMs = this.opts.maxUtteranceMs ?? MAX_UTTERANCE_MS
    const configuredMs = Number.isFinite(requestedMs) ? requestedMs : MAX_UTTERANCE_MS
    this.maxTimer = setTimeout(
      () => this.flush(),
      Math.max(0, Math.min(
        MAX_UTTERANCE_MS,
        configuredMs - preRollMs,
      )),
    )
  }

  private flush(): void {
    if (this.released) return
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    const pcm = this.chunkBytes > 0 ? this.concatChunks() : null
    this.chunks = []
    this.chunkBytes = 0
    this.preRoll = []
    this.preRollBytes = 0
    this.speaking = false
    if (pcm !== null) this.opts.onUtterance(pcm)
  }

  private concatChunks(): ArrayBuffer {
    const out = new Uint8Array(this.chunkBytes)
    let offset = 0
    for (const chunk of this.chunks) {
      out.set(new Uint8Array(chunk), offset)
      offset += chunk.byteLength
    }
    return out.buffer
  }
}
