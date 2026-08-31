import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import type { CodexSafeErrorCode as HostSafeErrorCode, CodexSessionEventMap as HostEventMap } from '@deepseek-ai/dsh-api-remotes/client'
import type {
  ClientCodexSafeErrorCode,
  CodexSessionEventMap as ClientEventMap,
} from '../src/client/session-events.ts'

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  (<Value>() => Value extends Right ? 1 : 2)
    ? true
    : false
type Assert<T extends true> = T

// These aliases deliberately fail compilation when either side drifts.  The
// test has no runtime Host import: both maps are type-only source contracts.
type EventMapMustMatch = Assert<Equal<ClientEventMap, HostEventMap>>
type ErrorCodeMustMatch = Assert<Equal<ClientCodexSafeErrorCode, HostSafeErrorCode>>
const eventMapMustMatch: EventMapMustMatch = true
const errorCodeMustMatch: ErrorCodeMustMatch = true

test('browser and Host Codex durable event contracts stay exact', () => {
  assert.equal(eventMapMustMatch, true)
  assert.equal(errorCodeMustMatch, true)
})

test('delegation-start carries no duplicated user prompt text', () => {
  const start: ClientEventMap['codex/delegation-start'] = {
    executionId: 'execution-1',
    sessionId: 'session-1',
    character: 'default',
  }
  assert.deepEqual(start, {
    executionId: 'execution-1',
    sessionId: 'session-1',
    character: 'default',
  })
})

test('Host and Client persistence merges declare every Codex key directly', () => {
  const sources = [
    readFileSync(new URL('../src/client/session-events-augmentation.tsx', import.meta.url), 'utf8'),
  ]
  const eventNames = [
    'codex/user-start',
    'codex/delegation-start',
    'codex/text-delta',
    'codex/text-final',
    'codex/tool-status',
    'codex/approval-request',
    'codex/approval-decision',
    'codex/interrupt-intent',
    'codex/terminal',
  ]
  for (const source of sources) {
    assert.doesNotMatch(source, /interface\s+SessionEventMap\s+extends\s+/)
    for (const eventName of eventNames) {
      const escaped = eventName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      assert.match(source, new RegExp(`['"]${escaped}['"]\\s*:`), eventName)
    }
  }
})

test('browser activation waits for Codex Remote, composer input, and the model directory', () => {
  const source = readFileSync(new URL('../src/client/index.ts', import.meta.url), 'utf8')
  assert.match(
    source,
    /export const inject = \['slots', 'locale', 'sessions', 'remote', 'remote\.codex', 'conversation', 'conversationEvents', 'modelDirectories'\]/,
  )
})
