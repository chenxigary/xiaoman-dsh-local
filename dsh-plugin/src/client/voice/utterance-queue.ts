/** Bounded utterance queue used by the continuous microphone control. */

/** Result of an enqueue attempt. */
export interface UtteranceQueueResult {
  readonly accepted: boolean
  readonly count: number
  readonly bytes: number
}

/**
 * FIFO queue with independent item-count and byte budgets. Rejecting the new
 * item is deliberate backpressure: an already captured utterance is never
 * silently evicted while its STT request is in flight.
 */
export class UtteranceQueue {
  private readonly items: ArrayBuffer[] = []
  private totalBytes = 0

  constructor(
    private readonly maxCount = 4,
    private readonly maxBytes = 4 * 1024 * 1024,
  ) {}

  get count(): number {
    return this.items.length
  }

  get bytes(): number {
    return this.totalBytes
  }

  enqueue(item: ArrayBuffer): UtteranceQueueResult {
    const accepted = item.byteLength > 0
      && this.items.length < this.maxCount
      && this.totalBytes + item.byteLength <= this.maxBytes
    if (accepted) {
      this.items.push(item)
      this.totalBytes += item.byteLength
    }
    return { accepted, count: this.count, bytes: this.bytes }
  }

  dequeue(): ArrayBuffer | undefined {
    const item = this.items.shift()
    if (item !== undefined) this.totalBytes -= item.byteLength
    return item
  }

  clear(): void {
    this.items.length = 0
    this.totalBytes = 0
  }
}
