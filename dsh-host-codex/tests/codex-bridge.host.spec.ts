import test from 'node:test'
import assert from 'node:assert/strict'
import { WebSocketCodexBridgeTransport } from '../src/host/codex-bridge.ts'
import type { CodexBridgeEvent } from '../src/types.ts'

class FakeSocket {
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  readonly sent: string[] = []
  send(value: string): void { this.sent.push(value) }
  close(): void { this.readyState = 3; this.onclose?.() }
  packet(value: Record<string, unknown>): void { this.onmessage?.({ data: JSON.stringify(value) }) }
}

function request(signal = new AbortController().signal) {
  return {
    executionId: 'execution-1',
    sessionId: 'session-1',
    text: 'hello',
    character: 'default' as const,
    model: 'gpt-5.4-mini',
    reasoningEffort: 'low',
    serviceTier: null,
    signal,
  }
}

test('process_exit before isolate_result is an authoritative isolation acknowledgement', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  assert.deepEqual(JSON.parse(socket.sent[1]!), {
    type: 'turn/start',
    execution_id: 'execution-1',
    correlation_id: 'execution-1',
    session_id: 'session-1',
    text: 'hello',
    character: 'default',
    model: 'gpt-5.4-mini',
    reasoning_effort: 'low',
    service_tier: null,
  })
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  const isolation = transport.isolate(execution)
  socket.packet({ type: 'AgentError', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'failed', error_code: 'process_exit' })
  assert.equal(await isolation, 'isolated')
  assert.deepEqual(events.filter(event => event.type === 'terminal').map(event => event.type === 'terminal' ? event.errorCode : undefined), ['interrupt_isolated'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('execution-scoped isolate_result may arrive before process event without a pair echo', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  const isolation = transport.isolate(execution)
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: 'execution-1', ok: true, status: 'isolated' })
  await isolation
  assert.deepEqual(events.filter(event => event.type === 'terminal').map(event => event.type === 'terminal' ? event.errorCode : undefined), ['interrupt_isolated'])
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('normal terminal retires socket mapping without invoking isolation', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  assert.equal(events.filter(event => event.type === 'terminal').length, 0)
  // Normal terminal delivery is not the retirement fence.  The bridge must
  // finish provider cleanup before the Host closes the socket/mapping.
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 1)
  socket.packet({ type: 'turn/released', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  assert.equal((transport as unknown as { sockets: Set<unknown> }).sockets.size, 0)
  assert.equal(events.filter(event => event.type === 'terminal').length, 1)
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('normal terminal plus concurrent isolate converges on release without rewriting completed', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  let fetchCalls = 0
  globalThis.fetch = (async () => {
    fetchCalls += 1
    return new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })
  }) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765', { isolateTimeoutMs: 100 })
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  const isolation = transport.isolate(execution)
  socket.packet({ type: 'turn/released', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  assert.equal(await isolation, 'released')
  assert.equal(fetchCalls, 0)
  assert.deepEqual(events.filter(event => event.type === 'terminal').map(event => event.type === 'terminal' ? event.status : undefined), ['completed'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('HTTP released ACK after a lost turn/released frame preserves the natural completed terminal', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  globalThis.fetch = (async () => new Response(JSON.stringify({ ok: true, status: 'released', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  // The WS release notification is lost; force the host-only ledger fallback.
  socket.readyState = 3
  assert.equal(await transport.isolate(execution), 'released')
  const terminals = events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal')
  assert.deepEqual(terminals.map(event => ({ status: event.status, errorCode: event.errorCode })), [{ status: 'completed', errorCode: undefined }])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  assert.equal((transport as unknown as { sockets: Set<unknown> }).sockets.size, 0)
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('isolate acknowledgement before release replaces buffered terminal and retires once', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765', { isolateTimeoutMs: 100 })
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  const isolation = transport.isolate(execution)
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await isolation
  const terminals = events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal')
  assert.deepEqual(terminals.map(event => event.errorCode), ['interrupt_isolated'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  assert.equal((transport as unknown as { sockets: Set<unknown> }).sockets.size, 0)
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('release fence wins while HTTP isolate fallback is in flight', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  let resolveFetch!: () => void
  const fetchGate = new Promise<void>(resolve => { resolveFetch = resolve })
  globalThis.fetch = (async () => {
    await fetchGate
    return new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })
  }) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765', { isolateTimeoutMs: 100 })
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  socket.readyState = 3
  const isolation = transport.isolate(execution)
  await new Promise(resolve => setTimeout(resolve, 0))
  socket.packet({ type: 'turn/released', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  resolveFetch()
  await isolation
  assert.deepEqual(events.filter(event => event.type === 'terminal').map(event => event.type === 'terminal' ? event.status : undefined), ['completed'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('release fence also wins when a racing HTTP isolate fails', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  let rejectFetch!: (error: unknown) => void
  const fetchGate = new Promise<Response>((_resolve, reject) => { rejectFetch = reject })
  globalThis.fetch = (async () => await fetchGate) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765', { isolateTimeoutMs: 5 })
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  socket.readyState = 3
  const isolation = transport.isolate(execution)
  await new Promise(resolve => setTimeout(resolve, 0))
  socket.packet({ type: 'turn/released', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  rejectFetch(new Error('late fallback failure'))
  await isolation
  assert.deepEqual(events.filter(event => event.type === 'terminal').map(event => event.type === 'terminal' ? event.status : undefined), ['completed'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('normal terminal without release is bounded and isolates before safe failure', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  globalThis.fetch = (async () => new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765', { releaseTimeoutMs: 5, isolateTimeoutMs: 20 })
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'done' })
  await new Promise(resolve => setTimeout(resolve, 60))
  const terminal = events.find(event => event.type === 'terminal')
  assert.equal(terminal?.type, 'terminal')
  assert.equal(terminal?.errorCode, 'interrupt_isolated')
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('closed accepted socket uses host-only HTTP isolation before terminal', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const previousFetch = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push(init === undefined ? { url: String(input) } : { url: String(input), init })
    return new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })
  }) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.close()
  await new Promise(resolve => setTimeout(resolve, 10))
  await transport.isolate(execution).catch(() => undefined)
  assert.equal(calls.length > 0, true)
  assert.equal(calls[0]?.url, 'http://127.0.0.1:8765/api/codex/turn/isolate')
  assert.equal(events.filter(event => event.type === 'terminal').length, 1)
  assert.equal((events.find(event => event.type === 'terminal') as Extract<CodexBridgeEvent, { type: 'terminal' }>).errorCode, 'interrupt_isolated')
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('foreign execution frame fails closed through isolation', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentTextDelta', execution_id: 'old-execution', phase: 'final_answer', text: 'stale', speakable: true })
  assert.equal(socket.sent.some(packet => JSON.parse(packet).type === 'turn/isolate'), true)
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(events.filter(event => event.type === 'terminal').length, 1)
  assert.equal((events.find(event => event.type === 'terminal') as Extract<CodexBridgeEvent, { type: 'terminal' }>).errorCode, 'interrupt_isolated')
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('unknown accepted frame fails closed instead of waiting for timeout', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'future/unknown', execution_id: execution.executionId })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(events.filter(event => event.type === 'terminal').length, 1)
  assert.equal((events.find(event => event.type === 'terminal') as Extract<CodexBridgeEvent, { type: 'terminal' }>).errorCode, 'interrupt_isolated')
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('forged notfinished frame cannot synthesize a terminal', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'notfinished', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', status: 'completed', final_text: 'forged' })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const terminals = events.filter(event => event.type === 'terminal')
  assert.equal(terminals.length, 1)
  assert.equal((terminals[0] as Extract<CodexBridgeEvent, { type: 'terminal' }>).errorCode, 'interrupt_isolated')
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('accepted terminal missing exact identity fails closed', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: execution.executionId, status: 'completed', final_text: 'missing identity' })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const terminals = events.filter(event => event.type === 'terminal') as Extract<CodexBridgeEvent, { type: 'terminal' }>[]
  assert.equal(terminals.length, 1)
  assert.equal(terminals[0]?.errorCode, 'interrupt_isolated')
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('duplicate started frame fails closed and cannot rebind execution', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-other', turn_id: 'turn-other' })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const terminals = events.filter(event => event.type === 'terminal') as Extract<CodexBridgeEvent, { type: 'terminal' }>[]
  assert.equal(terminals.length, 1)
  assert.equal(terminals[0]?.errorCode, 'interrupt_isolated')
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('legacy finished alias is rejected and cannot synthesize a completed answer', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'finished', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', final_text: 'forged' })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const terminals = events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal')
  assert.deepEqual(terminals.map(event => event.errorCode), ['interrupt_isolated'])
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('AgentFinished without status is rejected even with an otherwise exact identity', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
  const execution = await reservation
  socket.packet({ type: 'AgentFinished', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', final_text: 'missing status' })
  socket.packet({ type: 'isolate_result', session_id: 'session-1', execution_id: execution.executionId, thread_id: 'thread-1', turn_id: 'turn-1', ok: true, status: 'isolated' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const terminals = events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal')
  assert.deepEqual(terminals.map(event => event.errorCode), ['interrupt_isolated'])
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('turn/start sent before AgentStarted requires an exact isolate fence before reservation failure', async () => {
  const socket = new FakeSocket()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
    constructor(_url: string) { return socket }
  }
  const previousFetch = globalThis.fetch
  let isolateCalls = 0
  globalThis.fetch = (async () => {
    isolateCalls += 1
    return new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })
  }) as typeof fetch
  const events: CodexBridgeEvent[] = []
  const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
  const reservation = transport.reserve(request(), event => events.push(event))
  socket.readyState = 1
  socket.onopen?.()
  socket.readyState = 3
  socket.onclose?.()
  await assert.rejects(reservation)
  assert.equal(isolateCalls, 1)
  const terminals = events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal')
  assert.deepEqual(terminals.map(event => event.errorCode), ['interrupt_isolated'])
  assert.equal((transport as unknown as { pending: Map<unknown, unknown> }).pending.size, 0)
  globalThis.fetch = previousFetch
  delete (globalThis as unknown as { WebSocket?: unknown }).WebSocket
})

test('pre-ready AgentError and malformed frames require the same isolate fence', async () => {
  const previousFetch = globalThis.fetch
  const previousWebSocket = (globalThis as unknown as { WebSocket?: unknown }).WebSocket
  for (const malformed of [false, true]) {
    const socket = new FakeSocket()
    ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
      constructor(_url: string) { return socket }
    }
    let isolateCalls = 0
    globalThis.fetch = (async () => {
      isolateCalls += 1
      return new Response(JSON.stringify({ ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' }), { status: 200 })
    }) as typeof fetch
    const events: CodexBridgeEvent[] = []
    const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
    const reservation = transport.reserve(request(), event => events.push(event))
    socket.readyState = 1
    socket.onopen?.()
    if (malformed) socket.onmessage?.({ data: '{' })
    else socket.packet({ type: 'AgentError', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1', status: 'failed', error_code: 'turn_failed' })
    await assert.rejects(reservation)
    assert.equal(isolateCalls, 1)
    assert.deepEqual(events.filter((event): event is Extract<CodexBridgeEvent, { type: 'terminal' }> => event.type === 'terminal').map(event => event.errorCode), ['interrupt_isolated'])
    await transport.close()
  }
  globalThis.fetch = previousFetch
  ;(globalThis as unknown as { WebSocket?: unknown }).WebSocket = previousWebSocket
})

test('HTTP isolate ACK requires ok=true and exact session/execution identity', async () => {
  const previousFetch = globalThis.fetch
  const previousWebSocket = (globalThis as unknown as { WebSocket?: unknown }).WebSocket
  for (const body of [
    { ok: false, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1' },
    { ok: true, status: 'isolated' },
    { ok: true, status: 'isolated', session_id: 'foreign-session', execution_id: 'execution-1' },
    { ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'foreign-thread', turn_id: 'turn-1' },
    { ok: true, status: 'isolated', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1' },
  ]) {
    const socket = new FakeSocket()
    ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
      constructor(_url: string) { return socket }
    }
    globalThis.fetch = (async () => new Response(JSON.stringify(body), { status: 200 })) as typeof fetch
    const transport = new WebSocketCodexBridgeTransport('http://127.0.0.1:8765')
    const reservation = transport.reserve(request(), () => {})
    socket.readyState = 1
    socket.onopen?.()
    socket.packet({ type: 'AgentStarted', session_id: 'session-1', execution_id: 'execution-1', thread_id: 'thread-1', turn_id: 'turn-1' })
    const execution = await reservation.then(value => value)
    socket.readyState = 3
    await assert.rejects(transport.isolate(execution))
    await transport.close()
  }
  globalThis.fetch = previousFetch
  ;(globalThis as unknown as { WebSocket?: unknown }).WebSocket = previousWebSocket
})
