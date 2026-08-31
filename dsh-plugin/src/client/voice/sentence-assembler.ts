/**
 * Streaming, framework-independent sentence assembler for final-answer TTS.
 *
 * It accepts deltas, emits terminal-punctuation chunks immediately, bounds a
 * long or stalled fragment, and flushes an unfinished final at turn end.
 * Markdown is spoken as readable text; fenced code is silent by default.
 */

export type SentenceFlushReason = 'punctuation' | 'newline' | 'max_chars' | 'timeout' | 'final'

export interface SentenceChunk {
  readonly text: string
  readonly speakable: boolean
  readonly reason: SentenceFlushReason
}

export interface SentenceAssemblerOptions {
  /** Maximum spoken characters in one request (the bridge boundary is 512). */
  maxChars?: number
  /** Flush a stalled unfinished fragment (default 900 ms). */
  maxWaitMs?: number
  /** Speak fenced code only when explicitly enabled (default false). */
  speakCode?: boolean
  /** Injectable clock for deterministic tests. */
  now?: () => number
  /** Optional sink called for every emitted chunk, including timer flushes. */
  onChunk?: (chunk: SentenceChunk) => void
  /** Injectable timer hooks for host/test environments. */
  setTimer?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void
}

const STRONG_TERMINATORS = new Set(['。', '！', '？', '!', '?', '；', ';', '…'])
const SOFT_BREAKS = new Set(['，', '、', ',', '：', ':', ' '])

function isUsefulText(value: string): boolean {
  return /[\p{L}\p{N}\u4e00-\u9fff]/u.test(value)
}

/** Remove display-oriented markdown without reading syntax aloud. */
export function cleanMarkdownForSpeech(value: string): string {
  return value
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+|>\s+)/gm, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/[*_~]{1,3}/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * Assemble final-answer text without depending on React, timers or a DSH
 * runtime. `push()` is synchronous; a timer only invokes the optional sink.
 */
export class SentenceAssembler {
  private readonly maxChars: number
  private readonly maxWaitMs: number
  private readonly speakCode: boolean
  private readonly now: () => number
  private readonly onChunk: ((chunk: SentenceChunk) => void) | undefined
  private readonly setTimer: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>
  private readonly clearTimer: (timer: ReturnType<typeof setTimeout>) => void
  private buffer = ''
  /** Raw stream received so final snapshots can be de-duplicated after splits. */
  private receivedText = ''
  private codeFence = false
  private fenceCandidate = ''
  private bufferedAt: number | null = null
  private timer: ReturnType<typeof setTimeout> | null = null

  constructor(options: SentenceAssemblerOptions = {}) {
    this.maxChars = Math.min(512, Math.max(16, Math.floor(options.maxChars ?? 512)))
    this.maxWaitMs = Math.max(0, Math.floor(options.maxWaitMs ?? 900))
    this.speakCode = options.speakCode === true
    this.now = options.now ?? (() => Date.now())
    this.onChunk = options.onChunk
    this.setTimer = options.setTimer ?? ((callback, delayMs) => setTimeout(callback, delayMs))
    this.clearTimer = options.clearTimer ?? ((timer) => clearTimeout(timer))
  }

  get pendingText(): string {
    return cleanMarkdownForSpeech(this.buffer)
  }

  get inCodeFence(): boolean {
    return this.codeFence
  }

  /** Append a stream delta and return all synchronously completed chunks. */
  push(delta: string, atMs = this.now()): SentenceChunk[] {
    if (delta === '') return []
    this.receivedText += delta
    const emitted: SentenceChunk[] = []
    for (const character of delta) {
      this.consumeCharacter(character, emitted)
    }
    if (this.buffer !== '' && this.bufferedAt === null) {
      this.bufferedAt = atMs
      this.armTimer()
    }
    this.flushLongFragments(emitted)
    return emitted
  }

  /** Flush a stalled fragment when the caller has a deterministic clock. */
  flushExpired(atMs = this.now()): SentenceChunk[] {
    if (this.buffer === '' || this.bufferedAt === null || this.maxWaitMs <= 0) return []
    if (atMs - this.bufferedAt < this.maxWaitMs) return []
    return this.emitBuffer('timeout')
  }

  /**
   * Flush the final unfinished answer. If a complete final snapshot is
   * supplied, only text not already seen as deltas is appended.
   */
  finish(finalText?: string, atMs = this.now()): SentenceChunk[] {
    const emitted: SentenceChunk[] = []
    if (finalText !== undefined) {
      const seen = this.receivedText
      if (finalText.startsWith(seen)) {
        this.push(finalText.slice(seen.length), atMs).forEach((chunk) => emitted.push(chunk))
      } else if (!seen.startsWith(finalText)) {
        this.reset()
        this.push(finalText, atMs).forEach((chunk) => emitted.push(chunk))
      }
    }
    this.flushLongFragments(emitted)
    emitted.push(...this.emitBuffer('final'))
    this.cancelTimer()
    this.fenceCandidate = ''
    if (this.codeFence && !this.speakCode) {
      this.codeFence = false
    }
    return emitted
  }

  reset(): void {
    this.buffer = ''
    this.receivedText = ''
    this.codeFence = false
    this.fenceCandidate = ''
    this.bufferedAt = null
    this.cancelTimer()
  }

  private consumeCharacter(character: string, emitted: SentenceChunk[]): void {
    // A triple backtick is recognized across delta boundaries. A lone inline
    // backtick is retained until we can clean it as markdown below.
    if (character === '`') {
      this.fenceCandidate += character
      if (this.fenceCandidate.length === 3) {
        this.fenceCandidate = ''
        if (this.codeFence) {
          this.codeFence = false
        } else {
          if (!this.speakCode) this.emitBuffer('punctuation', emitted)
          this.codeFence = true
        }
      }
      return
    }
    if (this.fenceCandidate !== '') {
      if (!this.codeFence || this.speakCode) this.buffer += this.fenceCandidate
      this.fenceCandidate = ''
    }
    if (this.codeFence && !this.speakCode) return

    this.buffer += character
    // Bound a long sentence before a later terminal punctuation mark can
    // flush the entire raw buffer as one oversized TTS request.
    this.flushLongFragments(emitted)
    if (STRONG_TERMINATORS.has(character)) {
      this.emitBuffer('punctuation', emitted)
    } else if (character === '\n' && this.looksLikeMarkdownLine()) {
      this.emitBuffer('newline', emitted)
    }
  }

  private looksLikeMarkdownLine(): boolean {
    return /(?:^|\n)\s*(?:[-*+]\s+|\d+[.)]\s+|>\s+)/.test(this.buffer)
  }

  private flushLongFragments(emitted: SentenceChunk[]): void {
    while (cleanMarkdownForSpeech(this.buffer).length > this.maxChars) {
      const cleaned = cleanMarkdownForSpeech(this.buffer)
      let cut = this.maxChars
      for (let index = this.maxChars; index > Math.floor(this.maxChars / 2); index--) {
        if (SOFT_BREAKS.has(cleaned[index - 1] ?? '')) {
          cut = index
          break
        }
      }
      const rawCut = Math.max(1, Math.min(this.buffer.length, cut))
      const raw = this.buffer.slice(0, rawCut)
      this.buffer = this.buffer.slice(rawCut)
      const text = cleanMarkdownForSpeech(raw)
      if (isUsefulText(text)) emitted.push(this.publish({ text, speakable: true, reason: 'max_chars' }))
    }
  }

  private emitBuffer(reason: SentenceFlushReason, emitted?: SentenceChunk[]): SentenceChunk[] {
    const result: SentenceChunk[] = []
    if (this.fenceCandidate !== '') {
      if (!this.codeFence || this.speakCode) this.buffer += this.fenceCandidate
      this.fenceCandidate = ''
    }
    const text = cleanMarkdownForSpeech(this.buffer)
    this.buffer = ''
    this.bufferedAt = null
    this.cancelTimer()
    if (isUsefulText(text)) {
      const chunk = this.publish({ text, speakable: true, reason })
      result.push(chunk)
      emitted?.push(chunk)
    }
    return result
  }

  private publish(chunk: SentenceChunk): SentenceChunk {
    this.onChunk?.(chunk)
    return chunk
  }

  private armTimer(): void {
    if (this.timer !== null || this.maxWaitMs <= 0) return
    this.timer = this.setTimer(() => {
      this.timer = null
      this.flushExpired()
    }, this.maxWaitMs)
  }

  private cancelTimer(): void {
    if (this.timer !== null) {
      this.clearTimer(this.timer)
      this.timer = null
    }
  }
}
