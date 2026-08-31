/**
 * Pure history/reply projection helpers used by ReplySpeakerMount.
 *
 * DSH opens a session in two observable phases: the snapshot can contain no
 * history while it is loading, and then becomes `open` once the durable
 * history has been hydrated.  The first `open` snapshot is the only point at
 * which it is safe to establish a replay baseline.  In particular, running
 * assistant nodes must not contribute to that baseline: they are live work
 * and must be eligible for sentence playback in a brand-new session.
 */
import { boundSpeechText } from './sentences.ts'

export type ReplyHistoryNodeStatus = 'running' | 'settled' | 'interrupted'

export interface ReplyHistoryAnchor {
  readonly anchorSeq: number
  readonly status: ReplyHistoryNodeStatus
}

/**
 * Establish the durable-history replay fence exactly once.
 *
 * `0` is intentional: it records that hydration has happened even when the
 * conversation has no settled assistant rows yet, allowing a newly-running
 * first reply to be processed by the same snapshot effect.
 */
export function establishReplyHistoryBaseline(
  openState: string,
  nodes: Iterable<ReplyHistoryAnchor>,
  previous: number | null,
): number | null {
  if (previous !== null || openState !== 'open') return previous

  let maxSettledAnchor = 0
  for (const node of nodes) {
    if (node.status !== 'settled') continue
    if (node.anchorSeq > maxSettledAnchor) maxSettledAnchor = node.anchorSeq
  }
  return maxSettledAnchor
}

/**
 * Volatile per-session fence for a newly submitted Codex turn.  Durable
 * history is hydrated silently, but a start whose first visible snapshot is
 * already settled is still live work and must be eligible exactly once.
 */
export interface CodexReplyHistoryFence {
  readonly liveIntent: boolean
  readonly startHighWater: number
  readonly executionId: string | null
}

export interface CodexReplyHistoryNode {
  readonly anchorSeq: number
  readonly executionId: string
}

/**
 * Decide whether a Codex node belongs to the live intent rather than the
 * hydrated settled history.  The caller acknowledges the intent after the
 * first matching node is selected; this pure predicate is intentionally free
 * of store/runtime dependencies so race cases can be tested in isolation.
 */
export function isCodexLiveReplyNode(
  fence: CodexReplyHistoryFence,
  node: CodexReplyHistoryNode,
): boolean {
  if (!fence.liveIntent || node.anchorSeq <= fence.startHighWater) return false
  // Before the Host start result binds the exact execution, no node is
  // attributable to the live intent. The durable snapshot remains available
  // and is reconsidered once the exact id is bound.
  return fence.executionId !== null && fence.executionId === node.executionId
}

/** Codex projection must be inert while voice is disabled. */
export function shouldRunCodexSpeechProjection(
  mode: string,
  voice: boolean,
  sessionId: string | number | undefined,
  openState: string,
): boolean {
  return mode === 'codex' && voice && sessionId !== undefined && openState === 'open'
}

export interface ReplySpeechNode {
  readonly key: string
  readonly anchorSeq: number
  readonly status: ReplyHistoryNodeStatus
  /** Complete sentences already split from the node text. */
  readonly sentences: readonly string[]
  /** The trailing unfinished sentence, only eligible after settlement. */
  readonly partial: string | null
}

export interface ReplySpeechJob {
  readonly anchor: number
  readonly key: string
  readonly index: number
  readonly sentence: string
}

/** Monotonic settled-anchor fence that survives bounded per-node pruning. */
export interface ReplySettledHighWater {
  value: number
  /** Key -> anchor for nodes whose final sentence is still pending. */
  openKeys?: Map<string, number>
  /** Settled nodes whose final sentence has actually reached the speaker. */
  acceptedAnchors?: Map<string, number>
  /** Number of sentences accepted by the speaker for each node. */
  acceptedCounts?: Map<string, number>
}

/** Record speaker acceptance without treating a running node as settled. */
export function recordReplySentenceAccepted(
  fence: ReplySettledHighWater,
  key: string,
  anchorSeq: number,
  index: number,
  total: number,
  status: ReplyHistoryNodeStatus,
): void {
  if (fence.acceptedCounts !== undefined) {
    const accepted = Math.max(fence.acceptedCounts.get(key) ?? 0, index + 1)
    fence.acceptedCounts.set(key, accepted)
    if (status !== 'settled' || accepted < total) return
  } else if (status !== 'settled' || index + 1 < total) {
    return
  }
  commitSettledReply(fence, key, anchorSeq)
}

/** Commit a settled node only after its final sentence was accepted by TTS. */
export function commitSettledReply(
  fence: ReplySettledHighWater,
  key: string,
  anchorSeq: number,
): void {
  if (fence.acceptedAnchors === undefined) return
  fence.acceptedAnchors.set(key, anchorSeq)
  fence.openKeys?.delete(key)
  const earliestOpen = fence.openKeys === undefined
    ? undefined
    : [...fence.openKeys.values()].reduce<number | undefined>(
      (earliest, anchor) => earliest === undefined ? anchor : Math.min(earliest, anchor),
      undefined,
    )
  let frontier = fence.value
  for (const anchor of fence.acceptedAnchors.values()) {
    if (earliestOpen === undefined || anchor < earliestOpen) frontier = Math.max(frontier, anchor)
  }
  if (frontier <= fence.value) return
  fence.value = frontier
  for (const [acceptedKey, anchor] of fence.acceptedAnchors) {
    if (anchor <= frontier) fence.acceptedAnchors.delete(acceptedKey)
  }
  fence.acceptedCounts?.forEach((_count, acceptedKey) => {
    if (!fence.openKeys?.has(acceptedKey) && !fence.acceptedAnchors?.has(acceptedKey)) {
      fence.acceptedCounts?.delete(acceptedKey)
    }
  })
}

/** Roll a scheduled job back when the renderer speaker applies backpressure. */
export function rollbackReplySpeechJob(spoken: Map<string, number>, job: ReplySpeechJob): void {
  const current = spoken.get(job.key)
  if (current === undefined || current <= job.index) return
  if (job.index === 0) spoken.delete(job.key)
  else spoken.set(job.key, job.index)
}

/** Maximum number of node fences retained by one mounted reply listener. */
export const MAX_REPLY_HISTORY = 128
/** Maximum number of newly reserved sentence jobs in one snapshot pass. */
export const MAX_REPLY_SPEECH_JOBS = 128
/** Maximum UTF-8 bytes reserved by one snapshot pass. */
export const MAX_REPLY_SPEECH_BYTES = 256 * 1024

/**
 * Return newly speakable sentence jobs and advance the per-node spoken fence.
 * The caller owns the map so this remains allocation-free in the hot snapshot
 * path.  `skipUntil` is the durable history fence; `skipAnchor` is the exact
 * reply swallowed by barge-in.
 */
export function collectReplySpeechJobs(
  nodes: Iterable<ReplySpeechNode>,
  skipUntil: number,
  skipAnchor: number,
  spoken: Map<string, number>,
  settledHighWater?: ReplySettledHighWater,
): ReplySpeechJob[] {
  const jobs: ReplySpeechJob[] = []
  let jobBytes = 0
  // Preserve the oldest-to-newest prefix when a hostile/large snapshot is
  // presented. Anything beyond the budget remains unreserved for the next
  // snapshot; it is never dropped or allowed to advance the frontier.
  const snapshotNodes = [...nodes].sort((left, right) =>
    (left.anchorSeq - right.anchorSeq) || left.key.localeCompare(right.key))
  for (const node of snapshotNodes) {
    if (node.anchorSeq <= skipUntil || (skipAnchor > 0 && node.anchorSeq === skipAnchor)) continue
    if (node.status === 'interrupted') {
      settledHighWater?.openKeys?.delete(node.key)
      continue
    }

    const speakable = node.status === 'settled' && node.partial !== null
      ? [...node.sentences, ...boundSpeechText(node.partial)]
      : node.sentences.flatMap(sentence => boundSpeechText(sentence))
    const alreadySpoken = spoken.get(node.key) ?? 0

    // An anchor at or below the committed frontier is closed forever. In
    // particular, do not recreate an open fence merely because its bounded
    // spoken cursor still happens to be retained after TTS accepted it.
    const isClosed = node.status === 'settled'
      && settledHighWater !== undefined
      && node.anchorSeq <= settledHighWater.value
      && !settledHighWater.openKeys?.has(node.key)
      && !settledHighWater.acceptedAnchors?.has(node.key)
    if (isClosed && !spoken.has(node.key)) continue
    if (speakable.length === 0) {
      settledHighWater?.openKeys?.delete(node.key)
      continue
    }

    const keyIsPending = settledHighWater?.openKeys?.has(node.key) === true
    const keyIsAccepted = settledHighWater?.acceptedAnchors?.has(node.key) === true
    const acceptedCount = settledHighWater?.acceptedCounts?.get(node.key) ?? 0
    const settledAlreadyAccepted = node.status === 'settled'
      && acceptedCount >= speakable.length
      && alreadySpoken >= speakable.length
    const needsWork = speakable.length > alreadySpoken || settledAlreadyAccepted
    if (settledHighWater !== undefined
      && needsWork
      && !keyIsPending
      && !keyIsAccepted
      && node.anchorSeq > settledHighWater.value) {
      const trackedKeys = new Set([
        ...(settledHighWater.openKeys?.keys() ?? []),
        ...(settledHighWater.acceptedAnchors?.keys() ?? []),
      ])
      if (trackedKeys.size >= MAX_REPLY_HISTORY) break
      settledHighWater.openKeys?.set(node.key, node.anchorSeq)
    } else if (node.status === 'running' && !keyIsPending && !keyIsAccepted) {
      // Without a high-water object this remains the legacy per-node cursor;
      // with one, only non-empty work consumes a bounded pending key.
      if (settledHighWater !== undefined) {
        const trackedKeys = new Set([
          ...(settledHighWater.openKeys?.keys() ?? []),
          ...(settledHighWater.acceptedAnchors?.keys() ?? []),
        ])
        if (trackedKeys.size >= MAX_REPLY_HISTORY) break
        settledHighWater.openKeys?.set(node.key, node.anchorSeq)
      }
    }

    const wasOpen = settledHighWater?.openKeys?.has(node.key) ?? false
    // Once a settled anchor has been observed, an evicted node fence must not
    // make an old history row speak again.  A node that still has a cursor is
    // allowed through so its settled tail can flush exactly once.
    if (node.status === 'settled'
      && settledHighWater !== undefined
      && node.anchorSeq <= settledHighWater.value
      && !spoken.has(node.key)
      && !wasOpen) continue
    // A complete running reply may have had every sentence accepted before
    // its terminal snapshot arrived. Close that node on the first settled
    // snapshot even though there is no new TTS job to schedule.
    if (node.status === 'settled'
      && acceptedCount >= speakable.length
      && alreadySpoken >= speakable.length) {
      if (settledHighWater !== undefined) commitSettledReply(settledHighWater, node.key, node.anchorSeq)
      continue
    }
    // A settled node remains open until its final queued sentence is accepted
    // by the speaker. High-water is committed from `commitSettledReply`, not
    // while merely reserving jobs here, so backpressure/rollback cannot lose
    // the only retry opportunity.
    if (speakable.length <= alreadySpoken) continue

    let reserveUntil = alreadySpoken
    while (reserveUntil < speakable.length && jobs.length < MAX_REPLY_SPEECH_JOBS) {
      const sentence = speakable[reserveUntil]
      if (sentence === undefined) break
      const sentenceBytes = new TextEncoder().encode(sentence).byteLength
      if (jobBytes + sentenceBytes > MAX_REPLY_SPEECH_BYTES) break
      jobs.push({ anchor: node.anchorSeq, key: node.key, index: reserveUntil, sentence })
      jobBytes += sentenceBytes
      reserveUntil += 1
    }
    spoken.set(node.key, reserveUntil)
    if (reserveUntil < speakable.length || jobs.length >= MAX_REPLY_SPEECH_JOBS) break
  }

  jobs.sort((a, b) => (a.anchor - b.anchor) || (a.index - b.index))
  // The snapshot can retain a long-lived conversation. Keep the spoken fence
  // bounded while preserving the newest node identities for replay safety.
  while (spoken.size > MAX_REPLY_HISTORY) {
    const oldest = [...spoken.keys()].find(key =>
      !settledHighWater?.openKeys?.has(key)
      && !settledHighWater?.acceptedAnchors?.has(key)
      && !settledHighWater?.acceptedCounts?.has(key),
    )
    if (oldest === undefined) break
    spoken.delete(oldest)
  }
  return jobs
}
