import test from 'node:test'
import assert from 'node:assert/strict'
import { acceptsQqInbound, acceptsQqOutbound } from '../src/client/voice/qq-gate.ts'
import { MAX_QQ_FRAME_BYTES, MAX_QQ_TEXT_CHARS, parseQqFrame, QqSessionOwner } from '../src/client/voice/qq-owner.ts'
import { QQ_PUSH_KEY, readQqPush, writeQqPush } from '../src/client/voice/qq-settings.ts'

test('QQ push preference is opt-in and storage failures stay off', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
  }
  assert.equal(readQqPush(storage), false)
  writeQqPush(true, storage)
  assert.equal(values.get(QQ_PUSH_KEY), '1')
  assert.equal(readQqPush(storage), true)
  writeQqPush(false, storage)
  assert.equal(readQqPush(storage), false)
  const broken = {
    getItem: () => { throw new Error('storage unavailable') },
    setItem: () => { throw new Error('storage unavailable') },
  }
  assert.equal(readQqPush(broken), false)
})

test('QQ inbound is native-only and requires a session identity', () => {
  assert.equal(acceptsQqInbound('dsh', 'session-a'), true)
  assert.equal(acceptsQqInbound('codex', 'session-a'), false)
  assert.equal(acceptsQqInbound('dsh', undefined), false)
  assert.equal(acceptsQqInbound('dsh', '  '), false)
})

test('QQ outbound is owner-scoped and disabled for Codex', () => {
  assert.equal(acceptsQqOutbound('dsh', 'session-a'), true)
  assert.equal(acceptsQqOutbound('codex', 'session-a'), false)
  assert.equal(acceptsQqOutbound('dsh', undefined), false)
})

test('QQ front-end rejects oversized frames/text before routing', () => {
  assert.equal(parseQqFrame(JSON.stringify({ type: 'qq_message', text: '长'.repeat(MAX_QQ_TEXT_CHARS + 1) })), null)
  assert.equal(parseQqFrame('x'.repeat(MAX_QQ_FRAME_BYTES + 1)), null)
  assert.deepEqual(parseQqFrame(JSON.stringify({ type: 'qq_message', text: '你好' })), { type: 'qq_message', text: '你好' })
})

test('one apply-level QQ owner routes inbound and outbound to the active session only', () => {
  const previous = globalThis.WebSocket
  class FakeSocket {
    static readonly OPEN = 1
    static readonly CONNECTING = 0
    static readonly instances: FakeSocket[] = []
    readonly readyState = FakeSocket.OPEN
    binaryType = ''
    onmessage: ((event: { data: unknown }) => void) | null = null
    onclose: (() => void) | null = null
    readonly sent: string[] = []
    constructor(_url: string) { FakeSocket.instances.push(this) }
    send(value: string): void { this.sent.push(value) }
    close(): void { this.onclose?.() }
  }
  globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
  try {
    const owner = new QqSessionOwner(() => 'ws://127.0.0.1:8765/api/qq/ws')
    owner.start()
    const routed: string[] = []
    const releaseA = owner.register('session-a', text => routed.push(`a:${text}`))
    const releaseB = owner.register('session-b', text => routed.push(`b:${text}`))
    const socket = FakeSocket.instances[0]
    assert.ok(socket)
    socket.onmessage?.({ data: JSON.stringify({ type: 'qq_message', text: '只给 B' }) })
    assert.deepEqual(routed, ['b:只给 B'])
    assert.equal(owner.sendReply('session-a', '旧 owner'), false)
    assert.equal(owner.sendReply('session-b', '当前 owner'), true)
    releaseB()
    socket.onmessage?.({ data: JSON.stringify({ type: 'qq_message', text: '切回 A' }) })
    assert.deepEqual(routed, ['b:只给 B', 'a:切回 A'])
    releaseA()
    owner.dispose()
  } finally {
    globalThis.WebSocket = previous
  }
})

test('QQ owner does not connect until explicitly enabled', () => {
  const previous = globalThis.WebSocket
  class FakeSocket {
    static readonly OPEN = 1
    static readonly instances: FakeSocket[] = []
    readonly readyState = FakeSocket.OPEN
    binaryType = ''
    onmessage: ((event: { data: unknown }) => void) | null = null
    onclose: (() => void) | null = null
    constructor(_url: string) { FakeSocket.instances.push(this) }
    close(): void { this.onclose?.() }
  }
  globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
  try {
    const owner = new QqSessionOwner(() => 'ws://127.0.0.1:8765/api/qq/ws')
    owner.register('session-a', () => {})
    assert.equal(FakeSocket.instances.length, 0)
    owner.start()
    assert.equal(FakeSocket.instances.length, 1)
    owner.stop()
    owner.dispose()
  } finally {
    globalThis.WebSocket = previous
  }
})
