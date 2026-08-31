import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createComposerModeSnapshot,
  createVoiceStore,
  pruneComposerModeSnapshot,
  selectCodexComposerForSnapshot,
  updateComposerModeSnapshot,
} from '../src/client/agent-mode.ts'
import { shouldInterruptCodex } from '../src/client/voice/codex-gate.ts'
import type { ComposerChainProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import { UtteranceQueue } from '../src/client/voice/utterance-queue.ts'
import { UtteranceGeneration } from '../src/client/voice/turn-generation.ts'
import { SessionOwnerMap } from '../src/client/voice/session-owner.ts'
import { SessionOperationOwner } from '../src/client/voice/operation-owner.ts'
import { codexStatusLabel } from '../src/client/voice/status-label.ts'

function owner(sessionId: string): ComposerChainProps {
  return {
    interactions: [],
    session: { sessionId } as unknown as ComposerChainProps['session'],
  }
}

test('one voice handle keeps two session instances isolated', () => {
  const handle = createVoiceStore()
  const first = handle.create('session-a')
  const second = handle.create('session-b')

  first.actions.setMode('codex')
  first.actions.setDraft('仅属于 A')
  first.actions.setCharacter('xiaoman')
  first.actions.setCompanion(false)
  first.actions.setVoice(false)
  first.actions.bumpInterruptEpoch()

  assert.deepEqual(second.getSnapshot(), {
    mode: 'dsh',
    draft: '',
    character: 'xiaoman',
    companion: true,
    voice: true,
    interruptEpoch: 0,
    ttsEpoch: 0,
    codexStartIntent: 0,
    codexStartIntentConsumed: 0,
    codexStartHighWater: 0,
    codexStartExecutionId: null,
    codexHistoryHighWater: 0,
    codexHistoryHydrated: false,
  })
  assert.equal(first.getSnapshot().mode, 'codex')
  assert.equal(first.getSnapshot().draft, '仅属于 A')
  assert.equal(first.getSnapshot().character, 'xiaoman')
})

test('Codex start intent captures per-session history high-water before Remote start', () => {
  const handle = createVoiceStore()
  const cold = handle.create('session-before-hydration')
  cold.actions.markCodexStartIntent()
  assert.equal(cold.getSnapshot().codexStartIntent, 0)
  const session = handle.create('session-intent')
  session.actions.markCodexHistoryHydrated(17)
  assert.equal(session.getSnapshot().codexHistoryHydrated, true)
  session.actions.markCodexStartIntent()
  assert.equal(session.getSnapshot().codexStartIntent, 1)
  assert.equal(session.getSnapshot().codexStartHighWater, 17)
  session.actions.bindCodexStartIntent('execution-live')
  assert.equal(session.getSnapshot().codexStartExecutionId, 'execution-live')
  session.actions.acknowledgeCodexStartIntent(1)
  assert.equal(session.getSnapshot().codexStartIntentConsumed, 1)
  assert.equal(session.getSnapshot().codexStartExecutionId, null)
  session.actions.markCodexStartIntent()
  assert.equal(session.getSnapshot().codexStartHighWater, 17)
  session.actions.cancelCodexStartIntent()
  assert.equal(session.getSnapshot().codexStartIntentConsumed, 2)
})

test('native DSH mic never enters the Codex release/auth path', () => {
  assert.equal(shouldInterruptCodex('dsh', false), false)
  assert.equal(shouldInterruptCodex('dsh', true), false)
  assert.equal(shouldInterruptCodex('codex', false), false)
  assert.equal(shouldInterruptCodex('codex', true), true)
})

test('composer selector is owner-only, immutable, and bounded', () => {
  let snapshot = createComposerModeSnapshot()
  snapshot = updateComposerModeSnapshot(snapshot, 'session-a', 'codex')
  snapshot = updateComposerModeSnapshot(snapshot, 'session-b', 'dsh')
  const captured = snapshot

  const firstMatch = selectCodexComposerForSnapshot(owner('session-a'), captured)
  assert.deepEqual(firstMatch, { mode: 'codex' })
  assert.equal(selectCodexComposerForSnapshot(owner('session-a'), captured), firstMatch)
  assert.equal(Object.isFrozen(firstMatch), true)
  assert.equal(selectCodexComposerForSnapshot(owner('session-b'), captured), null)

  const switched = updateComposerModeSnapshot(captured, 'session-a', 'dsh')
  assert.equal(selectCodexComposerForSnapshot(owner('session-a'), captured)?.mode, 'codex')
  assert.equal(selectCodexComposerForSnapshot(owner('session-a'), switched), null)

  let bounded = createComposerModeSnapshot()
  for (let index = 0; index < 40; index += 1) {
    bounded = updateComposerModeSnapshot(bounded, `session-${index}`, 'codex', 32)
  }
  assert.equal(bounded.order.length, 32)
  assert.equal(bounded.order[0], 'session-8')
  assert.equal(bounded.modes['session-7'], undefined)
  assert.equal(bounded.modes['session-39'], 'codex')

  const pruned = pruneComposerModeSnapshot(bounded, 'session-20')
  assert.equal(pruned.modes['session-20'], undefined)
  assert.equal(pruned.order.includes('session-20'), false)
  assert.equal(bounded.modes['session-20'], 'codex')

  const reloaded = createVoiceStore().create('session-a')
  assert.equal(reloaded.getSnapshot().mode, 'dsh')
})

test('composer LRU refuses to evict protected live Codex owners', () => {
  let snapshot = createComposerModeSnapshot()
  snapshot = updateComposerModeSnapshot(snapshot, 'session-a', 'codex', 2, ['session-a'])
  snapshot = updateComposerModeSnapshot(snapshot, 'session-b', 'codex', 2, ['session-a', 'session-b'])
  const saturated = updateComposerModeSnapshot(snapshot, 'session-c', 'codex', 2, ['session-a', 'session-b', 'session-c'])
  assert.equal(saturated, snapshot)
  assert.equal(saturated.modes['session-a'], 'codex')
  assert.equal(saturated.modes['session-b'], 'codex')
  assert.equal(saturated.modes['session-c'], undefined)
})

test('utterance queue enforces count and byte backpressure and clears', () => {
  const queue = new UtteranceQueue(2, 5)
  assert.equal(queue.enqueue(new Uint8Array(3).buffer).accepted, true)
  assert.equal(queue.enqueue(new Uint8Array(2).buffer).accepted, true)
  assert.equal(queue.enqueue(new Uint8Array(1).buffer).accepted, false)
  assert.equal(queue.count, 2)
  assert.equal(queue.bytes, 5)
  assert.equal(queue.dequeue()?.byteLength, 3)
  assert.equal(queue.enqueue(new Uint8Array(1).buffer).accepted, true)
  queue.clear()
  assert.equal(queue.count, 0)
  assert.equal(queue.bytes, 0)
})

test('session operation owners keep speaker, companion, and abort state isolated', () => {
  const disposed: string[] = []
  const owners = new SessionOwnerMap((owner) => ({ owner, disposed: false }))
  const first = owners.get('session-a')
  const second = owners.get('session-b')
  assert.notEqual(first, second)
  assert.equal(owners.get('session-a'), first)
  first.disposed = true
  assert.equal(second.disposed, false)
  owners.clear(owner => { disposed.push(owner.owner) })
  assert.deepEqual(disposed.sort(), ['session-a', 'session-b'])
  assert.equal(owners.size, 0)
})

test('session owner leases dispose only after the final renderer unmount', () => {
  const disposed: string[] = []
  const owners = new SessionOwnerMap(
    owner => ({ owner }),
    value => { disposed.push(value.owner) },
  )
  const first = owners.acquire('session-a')
  const second = owners.acquire('session-a')
  assert.equal(owners.mountCount('session-a'), 2)
  first.release()
  assert.equal(owners.mountCount('session-a'), 1)
  assert.deepEqual(disposed, [])
  second.release()
  assert.equal(owners.mountCount('session-a'), 0)
  assert.deepEqual(disposed, ['session-a'])
  assert.equal(owners.size, 0)
})

test('mode switch, stop, and unmount cancellation share an abort generation fence', () => {
  for (const cancelReason of ['mode-switch', 'stop', 'unmount'] as const) {
    const fence = new UtteranceGeneration()
    const generation = fence.current
    const stt = new AbortController()
    const start = new AbortController()
    fence.track(stt)
    fence.track(start)
    assert.equal(fence.isCurrent(generation), true)

    fence.cancel()

    let ghostStart = 0
    if (fence.isCurrent(generation) && !stt.signal.aborted) {
      // This is the post-STT branch that would otherwise call codex.start.
      ghostStart += 1
    }

    assert.equal(stt.signal.aborted, true, cancelReason)
    assert.equal(start.signal.aborted, true, cancelReason)
    assert.equal(fence.isCurrent(generation), false, cancelReason)
    assert.equal(ghostStart, 0, cancelReason)
  }
})

test('tokenized overlay renderers cannot clear each other cancellation owners', () => {
  const operations = new SessionOperationOwner()
  const firstToken = Symbol('fallback')
  const secondToken = Symbol('codex-overlay')
  const firstController = new AbortController()
  const secondController = new AbortController()
  let firstCancel = 0
  let secondCancel = 0
  operations.registerTts(firstToken, firstController)
  operations.registerTts(secondToken, secondController)
  operations.registerTurnCancel(firstToken, () => { firstCancel += 1 })
  operations.registerTurnCancel(secondToken, () => { secondCancel += 1 })

  // The fallback unmount removes only its own token. The overlay remains
  // cancellable and both are still included in an apply-level mode switch.
  operations.registerTts(firstToken, null)
  operations.registerTurnCancel(firstToken, null)
  assert.equal(firstController.signal.aborted, true)
  assert.equal(operations.counts.tts, 1)
  operations.cancelTurns()
  operations.abortTts()
  assert.equal(firstCancel, 0)
  assert.equal(secondCancel, 1)
  assert.equal(secondController.signal.aborted, true)
})

test('overlay composition keeps fallback and Codex siblings under one session owner', () => {
  const operations = new SessionOperationOwner()
  const fallback = Symbol('fallback-input-bar')
  const elected = Symbol('elected-codex-composer')
  let cancellations = 0
  operations.registerTurnCancel(fallback, () => { cancellations += 1 })
  operations.registerTurnCancel(elected, () => { cancellations += 1 })

  // renderSlotChain may retain both siblings for one frame. Unmounting the
  // fallback must remove only its token; the apply-level mode edge still
  // cancels the elected Codex owner exactly once.
  operations.registerTurnCancel(fallback, null)
  operations.cancelTurns()
  assert.equal(cancellations, 1)
  assert.equal(operations.counts.turnCancels, 1)
  operations.registerTurnCancel(elected, null)
  assert.equal(operations.counts.turnCancels, 0)
})

test('completed Codex history is not rendered as actively speaking', () => {
  assert.equal(codexStatusLabel('running'), '思考中')
  assert.equal(codexStatusLabel('completed'), '已完成')
  assert.equal(codexStatusLabel('interrupted'), '已中断')
  assert.equal(codexStatusLabel('failed'), '已中断')
})
