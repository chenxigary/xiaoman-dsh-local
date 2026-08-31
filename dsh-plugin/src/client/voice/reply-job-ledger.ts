/** Bounded TTS source ledger: accepted jobs commit, failures never mark spoken. */
export const MAX_REPLY_TTS_JOBS = 128
export const MAX_REPLY_TTS_BYTES = 512 * 1024
export const MAX_REPLY_TTS_RETRIES = 3

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).byteLength
}

export interface ReplyTtsJob {
  readonly id: number
  readonly text: string
  readonly bytes: number
  readonly key: string
  readonly generation: number
  queued: boolean
  accepted: boolean
  failed: boolean
  retries: number
}

export class ReplyTtsJobLedger {
  private nextId = 0
  private readonly jobs: ReplyTtsJob[] = []

  enqueue(text: string, key: string, generation: number): ReplyTtsJob | undefined {
    if (text.trim() === '') return undefined
    const bytes = utf8Bytes(text)
    if (bytes > MAX_REPLY_TTS_BYTES) return undefined
    // Accepted jobs are a replay fence, not an unbounded archive. Retire the
    // oldest committed entries first so a new answer can still enter a full
    // ledger; never evict a pending/retryable job.
    while (this.jobs.length >= MAX_REPLY_TTS_JOBS || this.totalBytes + bytes > MAX_REPLY_TTS_BYTES) {
      const oldestAccepted = this.jobs.findIndex(job => job.accepted)
      if (oldestAccepted < 0) return undefined
      this.jobs.splice(oldestAccepted, 1)
    }
    const job: ReplyTtsJob = {
      id: ++this.nextId,
      text,
      bytes,
      key,
      generation,
      queued: false,
      accepted: false,
      failed: false,
      retries: 0,
    }
    this.jobs.push(job)
    return job
  }

  nextPending(): ReplyTtsJob | undefined {
    return this.jobs.find(job => !job.accepted && !job.failed && !job.queued)
  }

  markQueued(job: ReplyTtsJob): void {
    if (this.jobs.includes(job)) job.queued = true
  }

  markAccepted(job: ReplyTtsJob): void {
    if (this.jobs.includes(job)) {
      job.queued = false
      job.accepted = true
    }
  }

  /**
   * Returns false after the bounded retry budget is consumed. Backpressure
   * retries can pass `false` so a full speaker queue waits for its drain event
   * without spending the transport-failure budget.
   */
  markRetry(job: ReplyTtsJob, consumeRetry = true): boolean {
    if (this.jobs.includes(job)) {
      job.queued = false
      job.accepted = false
      if (consumeRetry) job.retries += 1
      if (job.retries >= MAX_REPLY_TTS_RETRIES) {
        job.failed = true
        return false
      }
    }
    return this.jobs.includes(job) && !job.failed
  }

  prune(maxJobs = MAX_REPLY_TTS_JOBS, maxBytes = MAX_REPLY_TTS_BYTES): void {
    const limit = Math.min(MAX_REPLY_TTS_JOBS, Math.max(1, Math.floor(maxJobs)))
    const byteLimit = Math.min(MAX_REPLY_TTS_BYTES, Math.max(1, Math.floor(maxBytes)))
    while (this.jobs.length > limit || this.totalBytes > byteLimit) {
      const oldestAccepted = this.jobs.findIndex(job => job.accepted)
      if (oldestAccepted < 0) return
      this.jobs.splice(oldestAccepted, 1)
    }
  }

  clear(): void {
    this.jobs.length = 0
  }

  get size(): number {
    return this.jobs.length
  }

  get bytes(): number {
    return this.totalBytes
  }

  get pendingCount(): number {
    return this.jobs.filter(job => !job.accepted && !job.failed).length
  }

  get failedCount(): number {
    return this.jobs.filter(job => job.failed).length
  }

  private get totalBytes(): number {
    return this.jobs.reduce((total, job) => total + job.bytes, 0)
  }
}
