import test from 'node:test'
import assert from 'node:assert/strict'
import { ReplyExecutionFence, ReplyTtsGeneration } from '../src/client/voice/reply-generation.ts'
import {
  MAX_REPLY_TTS_BYTES,
  MAX_REPLY_TTS_JOBS,
  MAX_REPLY_TTS_RETRIES,
  ReplyTtsJobLedger,
} from '../src/client/voice/reply-job-ledger.ts'

test('barge-in fences queued native and Codex TTS promises', async () => {
  const native = new ReplyTtsGeneration()
  const codex = new ReplyTtsGeneration()
  const nativeGeneration = native.current
  const codexGeneration = codex.current
  const nativeController = new AbortController()
  const codexController = new AbortController()
  native.track(nativeController)
  codex.track(codexController)
  const spoken: string[] = []

  let releaseNative: (() => void) | undefined
  let releaseCodex: (() => void) | undefined
  const nativePromise = new Promise<void>(resolve => { releaseNative = resolve }).then(() => {
    if (native.isCurrent(nativeGeneration) && !nativeController.signal.aborted) spoken.push('native')
  })
  const codexPromise = new Promise<void>(resolve => { releaseCodex = resolve }).then(() => {
    if (codex.isCurrent(codexGeneration) && !codexController.signal.aborted) spoken.push('codex')
  })

  native.advance()
  codex.advance()
  releaseNative?.()
  releaseCodex?.()
  await Promise.all([nativePromise, codexPromise])

  assert.equal(nativeController.signal.aborted, true)
  assert.equal(codexController.signal.aborted, true)
  assert.deepEqual(spoken, [])
})

test('mode and session edges fence both reply pipelines before A audio can reach B', () => {
  const native = new ReplyTtsGeneration()
  const codex = new ReplyTtsGeneration()
  const nativeA = native.current
  const codexA = codex.current
  const ownerA = { sessionId: 'session-a', mode: 'dsh' as const }

  // A→B is a synchronous owner edge: both generations advance before any
  // stale promise is allowed to inspect the new mode/session refs.
  native.advance()
  codex.advance()
  const ownerB = { sessionId: 'session-b', mode: 'codex' as const }
  assert.equal(native.isCurrent(nativeA), false)
  assert.equal(codex.isCurrent(codexA), false)
  assert.notDeepEqual(ownerA, ownerB)
  assert.equal(ownerB.mode, 'codex')
})

test('new Codex execution advances the shared-speaker fence and rejects old accepted audio', () => {
  const fence = new ReplyExecutionFence()
  const first = fence.begin('exec-a')
  assert.equal(first.changed, true)
  assert.equal(fence.isCurrent('exec-a', first.generation), true)
  const second = fence.begin('exec-b')
  assert.equal(second.changed, true)
  assert.equal(fence.isCurrent('exec-a', first.generation), false)
  assert.equal(fence.isCurrent('exec-b', second.generation), true)
  assert.equal(fence.begin('exec-b').changed, false)
  fence.reset()
  assert.equal(fence.isCurrent('exec-b', second.generation), false)
})

test('TTS throw or speaker rejection leaves one Codex job retryable exactly once', () => {
  const ledger = new ReplyTtsJobLedger()
  const job = ledger.enqueue('第一句。', 'exec-1', 0)
  assert.ok(job)
  ledger.markQueued(job)
  // Simulate a TTS throw: no spoken/accepted cursor is committed.
  ledger.markRetry(job)
  assert.equal(ledger.nextPending(), job)
  ledger.markQueued(job)
  // Simulate the later snapshot retry being accepted by the speaker.
  ledger.markAccepted(job)
  assert.equal(ledger.nextPending(), undefined)
  assert.equal(job.accepted, true)
  assert.equal(job.queued, false)
})

test('reply ledger has hard count/UTF-8 byte bounds and a finite transport retry budget', () => {
  const ledger = new ReplyTtsJobLedger()
  const oversized = ledger.enqueue('字'.repeat(MAX_REPLY_TTS_BYTES), 'too-large', 0)
  assert.equal(oversized, undefined)
  const first = ledger.enqueue('可重试', 'exec-1', 0)
  assert.ok(first)
  for (let attempt = 1; attempt <= MAX_REPLY_TTS_RETRIES; attempt += 1) {
    assert.equal(ledger.markRetry(first), attempt < MAX_REPLY_TTS_RETRIES)
  }
  assert.equal(first.failed, true)
  assert.equal(ledger.nextPending(), undefined)

  const backpressure = new ReplyTtsJobLedger()
  const queued = backpressure.enqueue('队列稍后重试', 'exec-backpressure', 0)
  assert.ok(queued)
  assert.equal(backpressure.markRetry(queued, false), true)
  assert.equal(queued.retries, 0)
  assert.equal(queued.failed, false)

  const bounded = new ReplyTtsJobLedger()
  for (let index = 0; index < MAX_REPLY_TTS_JOBS; index += 1) {
    assert.ok(bounded.enqueue(`句子-${index}`, `key-${index}`, 0))
  }
  assert.equal(bounded.enqueue('超出数量', 'overflow', 0), undefined)
  assert.ok(bounded.bytes <= MAX_REPLY_TTS_BYTES)
})

test('full accepted ledger prunes committed jobs before admitting the next answer', () => {
  const ledger = new ReplyTtsJobLedger()
  for (let index = 0; index < MAX_REPLY_TTS_JOBS; index += 1) {
    const job = ledger.enqueue(`已完成-${index}`, `history-${index}`, 0)
    assert.ok(job)
    ledger.markAccepted(job)
  }
  const next = ledger.enqueue('新的完整答案。', 'live-answer', 1)
  assert.ok(next)
  assert.equal(next.text, '新的完整答案。')
  assert.equal(next.text.length <= 16_000, true)
  assert.equal(ledger.size, MAX_REPLY_TTS_JOBS)
  assert.equal(ledger.nextPending(), next)
})

test('Codex 140-sentence terminal source drains past the bounded ledger without loss or replay', () => {
  const ledger = new ReplyTtsJobLedger()
  const source = Array.from({ length: 140 }, (_, index) => `Codex 句子 ${index + 1}。`)
  const deferred = [...source]
  const spoken: string[] = []

  // This models the listener's deferred source frontier: enqueue only the
  // oldest prefix that fits, accept one job, then retry the retained prefix
  // after committed ledger entries are pruned.
  while (deferred.length > 0 || ledger.nextPending() !== undefined) {
    const nextSource = deferred[0]
    if (nextSource !== undefined) {
      const admitted = ledger.enqueue(nextSource, 'codex-140', 1)
      if (admitted !== undefined) deferred.shift()
    }
    const pending = ledger.nextPending()
    if (pending === undefined) {
      assert.ok(deferred.length > 0, 'a non-empty source frontier must remain retryable')
      continue
    }
    ledger.markQueued(pending)
    ledger.markAccepted(pending)
    spoken.push(pending.text)
  }

  assert.deepEqual(spoken, source)
  assert.equal(new Set(spoken).size, 140)
  assert.deepEqual(deferred, [])
})
