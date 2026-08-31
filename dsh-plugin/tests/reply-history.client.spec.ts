import test from 'node:test'
import assert from 'node:assert/strict'
import {
  collectReplySpeechJobs,
  commitSettledReply,
  recordReplySentenceAccepted,
  establishReplyHistoryBaseline,
  isCodexLiveReplyNode,
  rollbackReplySpeechJob,
  shouldRunCodexSpeechProjection,
  type ReplySpeechNode,
} from '../src/client/voice/reply-history.ts'

function node(
  key: string,
  anchorSeq: number,
  status: ReplySpeechNode['status'],
  sentences: readonly string[],
  partial: string | null = null,
): ReplySpeechNode {
  return { key, anchorSeq, status, sentences, partial }
}

test('loading empty then open with settled history establishes a no-replay fence', () => {
  let baseline = establishReplyHistoryBaseline('loading', [], null)
  assert.equal(baseline, null)

  baseline = establishReplyHistoryBaseline('open', [
    { anchorSeq: 4, status: 'settled' },
    { anchorSeq: 9, status: 'settled' },
  ], baseline)
  assert.equal(baseline, 9)
  assert.deepEqual(
    collectReplySpeechJobs([node('old', 9, 'settled', ['旧回复。'])], baseline, 0, new Map()),
    [],
  )
})

test('open blank snapshot still speaks the first running reply', () => {
  const baseline = establishReplyHistoryBaseline('open', [
    { anchorSeq: 1, status: 'running' },
  ], null)
  assert.equal(baseline, 0)
  assert.deepEqual(
    collectReplySpeechJobs([node('first', 1, 'running', ['第一句。'])], baseline, 0, new Map()),
    [{ anchor: 1, key: 'first', index: 0, sentence: '第一句。' }],
  )
})

test('anchor zero remains eligible when no barge-in fence is active', () => {
  assert.deepEqual(
    collectReplySpeechJobs([node('first', 0, 'running', ['第一句。'])], Number.NEGATIVE_INFINITY, 0, new Map()),
    [{ anchor: 0, key: 'first', index: 0, sentence: '第一句。' }],
  )
})

test('settling a first reply flushes only its unfinished tail once', () => {
  const baseline = establishReplyHistoryBaseline('open', [], null)
  assert.equal(baseline, 0)
  const spoken = new Map<string, number>()
  assert.deepEqual(
    collectReplySpeechJobs([node('first', 1, 'running', ['完整句。'], '未完')], baseline, 0, spoken),
    [{ anchor: 1, key: 'first', index: 0, sentence: '完整句。' }],
  )
  assert.deepEqual(
    collectReplySpeechJobs([node('first', 1, 'settled', ['完整句。'], '未完')], baseline, 0, spoken),
    [{ anchor: 1, key: 'first', index: 1, sentence: '未完' }],
  )
  assert.deepEqual(
    collectReplySpeechJobs([node('first', 1, 'settled', ['完整句。'], '未完')], baseline, 0, spoken),
    [],
  )
})

test('reopening a session hydrates old history without replaying it', () => {
  let baseline = establishReplyHistoryBaseline('loading', [], null)
  baseline = establishReplyHistoryBaseline('open', [
    { anchorSeq: 22, status: 'settled' },
  ], baseline)
  assert.equal(baseline, 22)
  const oldHistory = [node('old', 22, 'settled', ['历史答案。'])]
  assert.deepEqual(collectReplySpeechJobs(oldHistory, baseline, 0, new Map()), [])
  assert.equal(establishReplyHistoryBaseline('open', oldHistory, baseline), 22)
})

test('native reply jobs also obey the 512-character TTS boundary', () => {
  const jobs = collectReplySpeechJobs([
    node('long', 1, 'settled', ['长'.repeat(700)]),
  ], 0, 0, new Map())
  assert.ok(jobs.length >= 2)
  assert.equal(jobs.every(job => job.sentence.length <= 512), true)
})

test('large snapshots reserve a bounded prefix by count and UTF-8 bytes', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  }
  const jobs = collectReplySpeechJobs(
    Array.from({ length: 512 }, (_, index) =>
      node(`flood-${index}`, index + 1, 'settled', ['字'.repeat(512)])),
    0,
    0,
    spoken,
    settledHighWater,
  )
  const bytes = jobs.reduce((total, job) => total + new TextEncoder().encode(job.sentence).byteLength, 0)
  assert.equal(jobs.length <= 128, true)
  assert.equal(bytes <= 256 * 1024, true)
  assert.equal((settledHighWater.openKeys?.size ?? 0) <= 128, true)
  assert.equal(jobs[0]?.anchor, 1)
})

test('a single 140-sentence terminal snapshot drains its source frontier without loss or replay', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  }
  const source = node(
    'one-terminal-answer',
    1,
    'settled',
    Array.from({ length: 140 }, (_, index) => `句子 ${index + 1}。`),
  )
  const drained: number[] = []
  for (;;) {
    const batch = collectReplySpeechJobs([source], 0, 0, spoken, settledHighWater)
    if (batch.length === 0) break
    drained.push(...batch.map(job => job.index))
    // Simulate speaker acceptance of the batch. This is the same source
    // cursor used by the listener's drain wake, not a new snapshot.
    for (const job of batch) {
      recordReplySentenceAccepted(
        settledHighWater,
        source.key,
        source.anchorSeq,
        job.index,
        140,
        source.status,
      )
    }
  }
  assert.deepEqual(drained, Array.from({ length: 140 }, (_, index) => index))
  assert.equal(new Set(drained).size, 140)
  assert.equal(settledHighWater.value, 1)
  assert.deepEqual(collectReplySpeechJobs([source], 0, 0, spoken, settledHighWater), [])
})

test('settled high-water survives 128-node cursor pruning without replaying old history', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  }
  const history = Array.from({ length: 129 }, (_, index) =>
    node(`reply-${index + 1}`, index + 1, 'settled', [`答案 ${index + 1}。`]))
  const first = collectReplySpeechJobs(history, 0, 0, spoken, settledHighWater)
  assert.equal(first.length, 128)
  assert.equal(first.every(job => job.index >= 0), true)
  assert.equal(settledHighWater.value, 0)
  for (const job of first) commitSettledReply(settledHighWater, job.key, job.anchor)
  const tail = collectReplySpeechJobs(history, 0, 0, spoken, settledHighWater)
  assert.deepEqual(tail, [{ anchor: 129, key: 'reply-129', index: 0, sentence: '答案 129。' }])
  commitSettledReply(settledHighWater, 'reply-129', 129)
  assert.equal(settledHighWater.value, 129)
  assert.equal(spoken.size, 128)

  // The oldest cursor was pruned, but its settled anchor remains fenced.
  assert.deepEqual(
    collectReplySpeechJobs([history[0]!], 0, 0, spoken, settledHighWater),
    [],
  )
  // A retained cursor is also idempotent.
  assert.deepEqual(
    collectReplySpeechJobs([history[128]!], 0, 0, spoken, settledHighWater),
    [],
  )
})

test('a closed settled row never reopens after its accepted cursor is retained', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  }
  const first = node('closed-1', 1, 'settled', ['首条。'])
  assert.deepEqual(collectReplySpeechJobs([first], 0, 0, spoken, settledHighWater), [
    { anchor: 1, key: 'closed-1', index: 0, sentence: '首条。' },
  ])
  commitSettledReply(settledHighWater, first.key, first.anchorSeq)
  assert.equal(settledHighWater.value, 1)
  assert.equal(settledHighWater.openKeys?.size, 0)

  // The same snapshot still carries the old spoken cursor. It is closed, not
  // a newly pending node, so it must not be re-added to openKeys.
  assert.deepEqual(collectReplySpeechJobs([first], 0, 0, spoken, settledHighWater), [])
  assert.equal(settledHighWater.openKeys?.size, 0)

  const later = Array.from({ length: 129 }, (_, index) =>
    node(`closed-later-${index + 2}`, index + 2, 'settled', [`后续 ${index + 2}。`]))
  const jobs = collectReplySpeechJobs(later, 0, 0, spoken, settledHighWater)
  for (const job of jobs) commitSettledReply(settledHighWater, job.key, job.anchor)
  const laterTail = collectReplySpeechJobs(later, 0, 0, spoken, settledHighWater)
  for (const job of laterTail) commitSettledReply(settledHighWater, job.key, job.anchor)
  assert.equal(settledHighWater.value, 130)
  assert.deepEqual(collectReplySpeechJobs([first], 0, 0, spoken, settledHighWater), [])
})

test('oldest settled job rejected under a full ledger remains eligible for one retry', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
  }
  const history = Array.from({ length: 129 }, (_, index) =>
    node(`retry-${index + 1}`, index + 1, 'settled', [`重试 ${index + 1}。`]))
  const jobs = collectReplySpeechJobs(history, 0, 0, spoken, settledHighWater)
  const oldest = jobs[0]!
  rollbackReplySpeechJob(spoken, oldest)
  assert.deepEqual(
    collectReplySpeechJobs([history[0]!], 0, 0, spoken, settledHighWater),
    [oldest],
  )
  commitSettledReply(settledHighWater, oldest.key, oldest.anchor)
  assert.equal(settledHighWater.value, 1)
})

test('settled high-water does not skip a lower running reply that settles later', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = { value: 0, openKeys: new Map<string, number>(), acceptedAnchors: new Map<string, number>(), acceptedCounts: new Map<string, number>() }
  assert.deepEqual(
    collectReplySpeechJobs([
      node('running-low', 2, 'running', ['低答案。']),
      node('settled-high', 9, 'settled', ['高答案。']),
    ], 0, 0, spoken, settledHighWater),
    [{ anchor: 2, key: 'running-low', index: 0, sentence: '低答案。' }, { anchor: 9, key: 'settled-high', index: 0, sentence: '高答案。' }],
  )
  assert.equal(settledHighWater.value, 0)
  commitSettledReply(settledHighWater, 'settled-high', 9)
  assert.equal(settledHighWater.value, 0)
  assert.deepEqual(
    collectReplySpeechJobs([
      node('running-low', 2, 'settled', ['低答案。'], '尾部'),
      node('settled-high', 9, 'settled', ['高答案。']),
    ], 0, 0, spoken, settledHighWater),
    [{ anchor: 2, key: 'running-low', index: 1, sentence: '尾部' }],
  )
  commitSettledReply(settledHighWater, 'running-low', 2)
  assert.equal(settledHighWater.value, 9)
})

test('running text accepted before settlement closes without a duplicate job', () => {
  const spoken = new Map<string, number>()
  const settledHighWater = {
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  }
  const running = node('running-complete', 4, 'running', ['已经完整。'])
  const jobs = collectReplySpeechJobs([running], 0, 0, spoken, settledHighWater)
  assert.deepEqual(jobs, [{ anchor: 4, key: 'running-complete', index: 0, sentence: '已经完整。' }])
  recordReplySentenceAccepted(settledHighWater, running.key, running.anchorSeq, 0, 1, 'running')
  assert.equal(settledHighWater.value, 0)

  const settled = node('running-complete', 4, 'settled', ['已经完整。'])
  assert.deepEqual(collectReplySpeechJobs([settled], 0, 0, spoken, settledHighWater), [])
  assert.equal(settledHighWater.value, 4)
  assert.equal(settledHighWater.openKeys?.size, 0)
  assert.deepEqual(collectReplySpeechJobs([settled], 0, 0, spoken, settledHighWater), [])
})

test('Codex hydrated settled history stays silent without a live start intent', () => {
  assert.equal(isCodexLiveReplyNode({
    liveIntent: false,
    startHighWater: 12,
    executionId: null,
  }, { anchorSeq: 12, executionId: 'old' }), false)
})

test('Codex live start speaks a first snapshot that is already settled', () => {
  assert.equal(isCodexLiveReplyNode({
    liveIntent: true,
    startHighWater: 12,
    executionId: 'live-1',
  }, { anchorSeq: 13, executionId: 'live-1' }), true)
})

test('Codex running then settled remains one live execution', () => {
  const fence = { liveIntent: true, startHighWater: 4, executionId: 'live-2' }
  assert.equal(isCodexLiveReplyNode(fence, { anchorSeq: 5, executionId: 'live-2' }), true)
  // Once the intent is acknowledged, the same node is no longer considered a
  // new first snapshot; its per-node spoken cursor handles the final tail.
  assert.equal(isCodexLiveReplyNode({ ...fence, liveIntent: false }, { anchorSeq: 5, executionId: 'live-2' }), false)
})

test('Codex remount does not replay the old settled first answer', () => {
  assert.equal(isCodexLiveReplyNode({
    liveIntent: false,
    startHighWater: 5,
    executionId: null,
  }, { anchorSeq: 5, executionId: 'live-2' }), false)
})

test('arm-before-hydration stays silent until exact execution binding, then speaks the live settled row once', () => {
  const unbound = {
    liveIntent: true,
    startHighWater: 0,
    executionId: null,
  } as const
  // Old settled history is visible before the Remote start result.  A null
  // execution id must never claim it.
  assert.equal(isCodexLiveReplyNode(unbound, { anchorSeq: 8, executionId: 'old-execution' }), false)

  const bound = { ...unbound, executionId: 'live-execution' } as const
  assert.equal(isCodexLiveReplyNode(bound, { anchorSeq: 8, executionId: 'old-execution' }), false)
  assert.equal(isCodexLiveReplyNode(bound, { anchorSeq: 9, executionId: 'live-execution' }), true)
  const spoken = new Map<string, number>()
  const jobs = collectReplySpeechJobs([
    node('live-answer', 9, 'settled', ['新答案。']),
  ], 0, 0, spoken)
  assert.deepEqual(jobs, [{ anchor: 9, key: 'live-answer', index: 0, sentence: '新答案。' }])
  assert.deepEqual(collectReplySpeechJobs([
    node('live-answer', 9, 'settled', ['新答案。']),
  ], 0, 0, spoken), [])
})

test('Codex speech projection is disabled when voice is off', () => {
  assert.equal(shouldRunCodexSpeechProjection('codex', false, 'session-1', 'open'), false)
  assert.equal(shouldRunCodexSpeechProjection('codex', true, 'session-1', 'open'), true)
})
