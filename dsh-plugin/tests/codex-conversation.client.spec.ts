import test from 'node:test'
import assert from 'node:assert/strict'
import type { ConversationMatch, ConversationNodeContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { SessionEvent, SessionEventMap } from '@deepseek-ai/dsh-session/types'
import type { CodexEventType } from '../src/client/session-events.ts'
import type {} from '../src/client/session-events.ts'
import type {} from '../src/client/session-events-augmentation.tsx'
import {
  codexConversationDefinition,
  type CodexAnswerChatData,
} from '../src/client/codex-conversation.ts'

type CodexSessionEvent<T extends CodexEventType> = Extract<SessionEvent, { type: T }>

function event<T extends CodexEventType>(type: T, data: SessionEventMap[T], seq: number): CodexSessionEvent<T> {
  return { type, data, seq, time: seq } as unknown as CodexSessionEvent<T>
}

function match(value: SessionEvent, role: 'start' | 'update'): ConversationMatch {
  return {
    event: value,
    view: undefined,
    role,
    location: { kind: 'unresolved' },
  }
}

function update(state: CodexAnswerChatData, value: SessionEvent): CodexAnswerChatData {
  return codexConversationDefinition.update(
    { state } as ConversationNodeContext<CodexAnswerChatData> & { readonly state: CodexAnswerChatData },
    match(value, 'update'),
  )
}

const noPreviousContext = { previous: () => undefined }

test('Codex conversation accepts exact typed events and fences sequence/terminal replays', () => {
  const start = event('codex/delegation-start', {
    executionId: 'exec-1',
    sessionId: 'session-a',
    character: 'default',
  }, 1)
  const startResult = codexConversationDefinition.match(start)
  assert.deepEqual(startResult, { id: 'exec-1', role: 'start' })
  const state = codexConversationDefinition.start(
    {} as ConversationNodeContext<CodexAnswerChatData>,
    match(start, 'start'),
    noPreviousContext,
  )

  const commentary = update(state, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'commentary', speakable: false, text: '工具处理中', sequence: 1,
  }, 2))
  assert.equal(commentary.text, '')
  assert.equal(commentary.speakable, false)

  const answer = update(commentary, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'final_answer', speakable: true, text: '答案', sequence: 2,
  }, 3))
  assert.equal(answer.text, '答案')
  assert.equal(answer.speakable, true)
  assert.equal(answer.phase, 'final_answer')

  // Host-shaped streams remain strictly 1-based even when commentary and
  // final-answer phases alternate within one execution.
  const laterCommentary = update(answer, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'commentary', speakable: false, text: '工具状态', sequence: 3,
  }, 3.5))
  assert.equal(laterCommentary.sequence, 3)
  assert.equal(laterCommentary.sequenceGap, false)
  const laterAnswer = update(laterCommentary, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'final_answer', speakable: true, text: '继续', sequence: 4,
  }, 3.75))
  assert.equal(laterAnswer.text, '答案继续')
  assert.equal(laterAnswer.sequence, 4)

  // Duplicate and out-of-order deltas are no-ops by sequence identity.
  assert.equal(update(answer, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'final_answer', speakable: true, text: '重复', sequence: 1,
  }, 4)), answer)
  assert.equal(update(answer, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'commentary', speakable: false, text: '旧', sequence: 0,
  }, 5)), answer)
  // A late commentary event must not make an already speakable final answer
  // non-speakable again.
  assert.equal(update(laterAnswer, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'commentary', speakable: false, text: '晚到', sequence: 2,
  }, 6)), laterAnswer)

  const completed = update(laterAnswer, event('codex/terminal', {
    executionId: 'exec-1', status: 'completed', reason: 'done',
  }, 7))
  assert.equal(completed.status, 'completed')
  assert.equal(update(completed, event('codex/terminal', {
    executionId: 'exec-1', status: 'completed', reason: 'duplicate',
  }, 8)), completed)
  assert.equal(update(completed, event('codex/text-delta', {
    executionId: 'exec-1', phase: 'final_answer', speakable: true, text: '幽灵', sequence: 99,
  }, 9)), completed)
})

test('Codex conversation ignores unknown and non-answer events without fail-open matching', () => {
  const unknown: SessionEvent<'turn/start'> = {
    type: 'turn/start',
    data: { turn: 1 },
    seq: 1,
    time: 1,
  }
  assert.equal(codexConversationDefinition.match(unknown), null)
  const approval = event('codex/approval-request', {
    executionId: 'exec-1', approvalId: 'a-1', kind: 'command', safeSummary: '需要确认',
  }, 2)
  const matched = codexConversationDefinition.match(approval)
  assert.deepEqual(matched, { id: 'exec-1', role: 'update' })
})

test('a sequence gap enters a non-speakable protocol state until authoritative final', () => {
  const start = event('codex/delegation-start', {
    executionId: 'exec-gap', sessionId: 'session-a', character: 'default',
  }, 1)
  const state = codexConversationDefinition.start(
    {} as ConversationNodeContext<CodexAnswerChatData>,
    match(start, 'start'),
    noPreviousContext,
  )
  const gap = update(state, event('codex/text-delta', {
    executionId: 'exec-gap', phase: 'final_answer', speakable: true, text: '跳号', sequence: 2,
  }, 2))
  assert.equal(gap.sequenceGap, true)
  assert.equal(gap.speakable, false)
  assert.equal(gap.text, '')

  const stillBlocked = update(gap, event('codex/text-delta', {
    executionId: 'exec-gap', phase: 'final_answer', speakable: true, text: '后续', sequence: 3,
  }, 3))
  assert.equal(stillBlocked.sequenceGap, true)
  assert.equal(stillBlocked.speakable, false)

  const final = update(stillBlocked, event('codex/text-final', {
    executionId: 'exec-gap', text: '权威答案',
  }, 4))
  assert.equal(final.sequenceGap, false)
  assert.equal(final.speakable, false)
  assert.equal(final.text, '权威答案')

  const completed = update(final, event('codex/terminal', {
    executionId: 'exec-gap', status: 'completed', reason: 'done',
  }, 5))
  assert.equal(completed.sequenceGap, false)
  assert.equal(completed.speakable, true)
})

test('text-final before an interrupted terminal never becomes speakable', () => {
  const start = event('codex/delegation-start', {
    executionId: 'exec-interrupted-final', sessionId: 'session-a', character: 'default',
  }, 1)
  const state = codexConversationDefinition.start(
    {} as ConversationNodeContext<CodexAnswerChatData>,
    match(start, 'start'),
    noPreviousContext,
  )
  const pending = update(state, event('codex/text-final', {
    executionId: 'exec-interrupted-final', text: '不应播报',
  }, 2))
  assert.equal(pending.speakable, false)
  const interrupted = update(pending, event('codex/terminal', {
    executionId: 'exec-interrupted-final', status: 'interrupted', reason: 'barge-in',
  }, 3))
  assert.equal(interrupted.speakable, false)
  assert.equal(interrupted.status, 'interrupted')
})

test('Host-shaped one-based stream advances high-water across commentary after a final', () => {
  const start = event('codex/delegation-start', {
    executionId: 'host-exec-1', sessionId: 'host-session', character: 'xiaoman',
  }, 10)
  const state = codexConversationDefinition.start(
    {} as ConversationNodeContext<CodexAnswerChatData>,
    match(start, 'start'),
    noPreviousContext,
  )
  const first = update(state, event('codex/text-delta', {
    executionId: 'host-exec-1', phase: 'final_answer', speakable: true, text: '首句', sequence: 1,
  }, 11))
  const commentary = update(first, event('codex/text-delta', {
    executionId: 'host-exec-1', phase: 'commentary', speakable: false, text: '工具进度', sequence: 2,
  }, 12))
  const final = update(commentary, event('codex/text-delta', {
    executionId: 'host-exec-1', phase: 'final_answer', speakable: true, text: '尾句', sequence: 3,
  }, 13))
  assert.equal(final.sequence, 3)
  assert.equal(final.sequenceGap, false)
  assert.equal(final.text, '首句尾句')
  assert.equal(final.speakable, true)
})
