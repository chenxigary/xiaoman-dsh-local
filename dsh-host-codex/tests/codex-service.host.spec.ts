import test from 'node:test'
import assert from 'node:assert/strict'
import { CodexService } from '../src/host/codex-service.ts'

test('read-only capability checks auth before coordinator reservation', async () => {
  let fetchCalls = 0
  let processFactoryCalls = 0
  const service = {
    authStatus: async () => {
      fetchCalls += 1
      return { loggedIn: true, account: { type: 'chatgpt', planType: 'pro' }, capability: 'read-only' }
    },
    coordinator: {
      start: async () => {
        processFactoryCalls += 1
        return { executionId: 'exec-1' }
      },
    },
  }

  const value = await CodexService.prototype.start.call(
      service as never,
      {} as never,
      { text: 'start safely' },
      new AbortController().signal,
    )
  assert.deepEqual(value, { executionId: 'exec-1' })
  assert.equal(fetchCalls, 1)
  assert.equal(processFactoryCalls, 1)
})

test('a pending login owner is never evicted by age/capacity and terminal cleanup is exact', async () => {
  const calls: string[] = []
  const requestBodies: string[] = []
  const responses = [
    { status: 'pending', login_id: 'login-old', auth_url: 'https://auth.openai.com/oauth/authorize?state=s' },
    { status: 'pending' },
    { status: 'canceled' },
    { status: 'pending', login_id: 'login-new', auth_url: 'https://auth.openai.com/oauth/authorize?state=t' },
  ]
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginFetch: async (path: string, init?: RequestInit) => {
      calls.push(path)
      const response = responses.shift()
      if (response === undefined) throw new Error('unexpected login request')
      if (path.endsWith('/start')) {
        requestBodies.push(String(init?.body ?? ''))
        return { ...response, operation_id: JSON.parse(String(init?.body ?? '{}')).operation_id }
      }
      return response
    },
  }
  const agentA = { session: { id: 'session-a' } }
  const agentB = { session: { id: 'session-b' } }

  const first = await CodexService.prototype.loginStart.call(
    service as never, agentA as never, new AbortController().signal,
  )
  assert.equal(first.loginId, 'login-old')

  // Simulate a clock well beyond the old five-minute eviction window. The
  // owner remains pollable because only a backend terminal clears it.
  const originalNow = Date.now
  Date.now = () => originalNow() + 6 * 60_000
  try {
    await assert.rejects(
      CodexService.prototype.loginStart.call(
        service as never, agentB as never, new AbortController().signal,
      ),
      error => (error as { code?: string }).code === 'turn_in_progress',
    )
    const pending = await CodexService.prototype.loginStatus.call(
      service as never, agentA as never, 'login-old', new AbortController().signal,
    )
    assert.equal(pending.status, 'pending')
  } finally {
    Date.now = originalNow
  }
  const canceled = await CodexService.prototype.loginCancel.call(
    service as never, agentA as never, 'login-old', new AbortController().signal,
  )
  assert.equal(canceled.status, 'canceled')

  const second = await CodexService.prototype.loginStart.call(
    service as never, agentB as never, new AbortController().signal,
  )
  assert.equal(second.loginId, 'login-new')
  assert.deepEqual(calls, [
    '/api/codex/auth/login/start',
    '/api/codex/auth/login/login-old',
    '/api/codex/auth/login/login-old/cancel',
    '/api/codex/auth/login/start',
  ])
  assert.equal(requestBodies.length, 2)
  assert.equal(requestBodies.every(body => {
    const parsed = JSON.parse(body) as Record<string, unknown>
    return Object.keys(parsed).length === 1
      && typeof parsed.operation_id === 'string'
      && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(parsed.operation_id)
  }), true)
  assert.notEqual(JSON.parse(requestBodies[0]!).operation_id, JSON.parse(requestBodies[1]!).operation_id)
})

test('login cancellation cleanup uses the captured owner after polling abort', async () => {
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginFetch: async (path: string, init?: RequestInit) => {
      if (path.endsWith('/start')) return {
        status: 'pending',
        login_id: 'login-owned',
        auth_url: 'https://auth.openai.com/oauth/authorize?state=s',
        operation_id: JSON.parse(String(init?.body ?? '{}')).operation_id,
      }
      if (path.endsWith('/cancel')) return { status: 'canceled' }
      throw new Error('unexpected')
    },
  }
  const owner = { session: { id: 'session-owned' } }
  const started = await CodexService.prototype.loginStart.call(
    service as never, owner as never, new AbortController().signal,
  )
  assert.equal(started.loginId, 'login-owned')
  const canceled = await CodexService.prototype.loginCancel.call(
    service as never, owner as never, started.loginId, new AbortController().signal,
  )
  assert.equal(canceled.success, false)
  assert.equal(service.loginOwners.size, 0)
})

test('completion winning the cancel race clears ownership without a false canceled result', async () => {
  const service = {
    loginOwners: new Map([['login-race', { sessionId: 'session-race' }]]),
    loginFetch: async () => ({ status: 'completed', success: true }),
  }
  const result = await CodexService.prototype.loginCancel.call(
    service as never,
    { session: { id: 'session-race' } } as never,
    'login-race',
    new AbortController().signal,
  )
  assert.equal(result.status, 'completed')
  assert.equal(result.success, false)
  assert.equal(service.loginOwners.size, 0)
})

test('service disposal cancels the exact pending owner before a new service can start', async () => {
  const ownerAgent = { session: { id: 'session-dispose' } }
  const loginOwners = new Map([[
    'login-dispose',
    { sessionId: 'session-dispose', agent: ownerAgent },
  ]])
  const calls: string[] = []
  let coordinatorClosed = false
  const service = {
    loginOwners,
    loginStartInFlight: false,
    loginStartUncertain: false,
    closing: false,
    recoveryTimers: new Map(),
    closePromise: undefined,
    coordinator: { close: async () => { coordinatorClosed = true } },
    loginFetch: async (path: string) => {
      calls.push(path)
      if (path.endsWith('/cancel')) return { status: 'canceled' }
      throw new Error('unexpected login request')
    },
  }
  const closeResources = (CodexService.prototype as unknown as {
    closeResources: (this: unknown) => Promise<void>
  }).closeResources
  await closeResources.call(service)
  assert.deepEqual(calls, ['/api/codex/auth/login/login-dispose/cancel'])
  assert.equal(loginOwners.size, 0)
  assert.equal(coordinatorClosed, true)

  // The next service instance sees the same process registry/map after the
  // terminal cancel and is allowed to create a fresh exact operation.
  const nextService = {
    loginOwners,
    loginStartInFlight: false,
    loginStartUncertain: false,
    loginFetch: async (_path: string, init?: RequestInit) => ({
      status: 'pending',
      login_id: 'login-new-after-dispose',
      auth_url: 'https://auth.openai.com/oauth/authorize?state=new',
      operation_id: JSON.parse(String(init?.body ?? '{}')).operation_id,
    }),
  }
  const started = await CodexService.prototype.loginStart.call(
    nextService as never,
    { session: { id: 'session-dispose-next' } } as never,
    new AbortController().signal,
  )
  assert.equal(started.loginId, 'login-new-after-dispose')
})

test('disposal waits for a late accepted login response and preserves an ambiguous owner for reconciliation', async () => {
  const ownerAgent = { session: { id: 'session-late' } }
  const loginOwners = new Map<string, { sessionId: string; agent?: typeof ownerAgent }>()
  const calls: string[] = []
  let releaseStart!: (value: Record<string, unknown>) => void
  const response = new Promise<Record<string, unknown>>(resolve => { releaseStart = resolve })
  let coordinatorClosed = false
  const service = {
    loginOwners,
    loginStartInFlight: false,
    loginStartUncertain: false,
    closing: false,
    recoveryTimers: new Map(),
    closePromise: undefined,
    coordinator: { close: async () => { coordinatorClosed = true } },
    loginFetch: async (path: string, init?: RequestInit) => {
      calls.push(path)
      if (path.endsWith('/start')) return {
        ...(await response),
        operation_id: JSON.parse(String(init?.body ?? '{}')).operation_id,
      }
      if (path.endsWith('/cancel')) return { status: 'canceled' }
      throw new Error('unexpected login request')
    },
  }
  const start = CodexService.prototype.loginStart.call(
    service as never,
    ownerAgent as never,
    new AbortController().signal,
  )
  await Promise.resolve()
  const closeResources = (CodexService.prototype as unknown as {
    closeResources: (this: unknown) => Promise<void>
  }).closeResources
  const closing = closeResources.call(service)
  await Promise.resolve()
  releaseStart({
    status: 'pending',
    login_id: 'login-late',
    auth_url: 'https://auth.openai.com/oauth/authorize?state=late',
  })
  const started = await start
  await closing
  assert.equal(started.loginId, 'login-late')
  assert.deepEqual(calls, [
    '/api/codex/auth/login/start',
    '/api/codex/auth/login/login-late/cancel',
  ])
  assert.equal(loginOwners.size, 0)
  assert.equal(coordinatorClosed, true)

  // If exact cancel is ambiguous, disposal must leave the owner in the
  // shared registry. A later service can use its current Agent/session to
  // reconcile it instead of opening another flow.
  const retainedOwners = new Map<string, { sessionId: string; agent?: typeof ownerAgent }>([
    ['login-ambiguous', { sessionId: 'session-late', agent: ownerAgent }],
  ])
  const ambiguousService = {
    loginOwners: retainedOwners,
    loginStartInFlight: false,
    loginStartUncertain: false,
    closing: false,
    recoveryTimers: new Map(),
    closePromise: undefined,
    coordinator: { close: async () => {} },
    loginFetch: async () => { throw Object.assign(new Error('lost response'), { code: 'bridge_unavailable', authoritative: false }) },
  }
  await closeResources.call(ambiguousService)
  assert.equal(retainedOwners.has('login-ambiguous'), true)
  assert.equal(ambiguousService.loginStartUncertain, true)

  const reconciler = {
    loginOwners: retainedOwners,
    loginFetch: async () => ({ status: 'canceled' }),
  }
  await CodexService.prototype.loginCancel.call(
    reconciler as never,
    ownerAgent as never,
    'login-ambiguous',
    new AbortController().signal,
  )
  assert.equal(retainedOwners.size, 0)
})

test('same-session pending lookup recovers an owner after a browser reload without cross-session leakage', async () => {
  const owners = new Map([
    ['login-reload', { sessionId: 'session-reload' }],
  ])
  let requests = 0
  const service = {
    loginOwners: owners,
    loginFetch: async () => {
      requests += 1
      return { status: 'pending' }
    },
  }
  const pending = await CodexService.prototype.loginPending.call(
    service as never,
    { session: { id: 'session-reload' } } as never,
    new AbortController().signal,
  )
  assert.deepEqual(pending, { loginId: 'login-reload', status: 'pending' })
  assert.equal(requests, 1)
  const foreign = await CodexService.prototype.loginPending.call(
    service as never,
    { session: { id: 'session-other' } } as never,
    new AbortController().signal,
  )
  assert.equal(foreign, null)
  assert.equal(requests, 1)
})

test('caller abort after login POST acceptance cancels the exact owner with a fresh Host signal', async () => {
  let releaseStart!: (value: { status: string; login_id: string; auth_url: string }) => void
  const startResponse = new Promise<{ status: string; login_id: string; auth_url: string }>(resolve => { releaseStart = resolve })
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginStartUncertain: false,
    loginFetch: async (path: string, init?: RequestInit) => path.endsWith('/start')
      ? { ...(await startResponse), operation_id: JSON.parse(String(init?.body ?? '{}')).operation_id }
      : { status: 'canceled' },
  }
  const controller = new AbortController()
  const pending = CodexService.prototype.loginStart.call(
    service as never,
    { session: { id: 'session-aborted' } } as never,
    controller.signal,
  )
  await Promise.resolve()
  controller.abort()
  releaseStart({ status: 'pending', login_id: 'login-aborted', auth_url: 'https://auth.openai.com/oauth/authorize?state=a' })
  await assert.rejects(pending, error => (error as { name?: string }).name === 'AbortError')
  assert.equal(service.loginOwners.size, 0)
  assert.equal(service.loginStartInFlight, false)
  assert.equal(service.loginStartUncertain, false)
})

test('unknown login-start timeout keeps the Host blocked instead of opening a second flow', async () => {
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginStartUncertain: false,
    loginFetch: async () => { throw Object.assign(new Error('timeout'), { code: 'bridge_unavailable' }) },
  }
  await assert.rejects(
    CodexService.prototype.loginStart.call(
      service as never,
      { session: { id: 'session-timeout' } } as never,
      new AbortController().signal,
    ),
    error => (error as { code?: string }).code === 'bridge_unavailable',
  )
  assert.equal(service.loginStartUncertain, true)
  assert.equal(service.loginStartInFlight, true)
  await assert.rejects(
    CodexService.prototype.loginStart.call(
      service as never,
      { session: { id: 'session-timeout' } } as never,
      new AbortController().signal,
    ),
    error => (error as { code?: string }).code === 'turn_in_progress',
  )
})

test('authoritative HTTP login responses do not retry or leave an unknown-owner latch', async () => {
  const cases = [
    { name: 'bad request', code: 'invalid_request', status: 400 },
    { name: 'pending owner conflict', code: 'turn_in_progress', status: 409 },
    { name: 'backend unavailable response', code: 'bridge_unavailable', status: 503 },
  ] as const
  for (const item of cases) {
    let calls = 0
    const service = {
      loginOwners: new Map<string, { sessionId: string }>(),
      loginStartInFlight: false,
      loginStartUncertain: false,
      loginFetch: async () => {
        calls += 1
        throw Object.assign(new Error(item.name), { code: item.code, authoritative: true })
      },
    }
    await assert.rejects(
      CodexService.prototype.loginStart.call(
        service as never,
        { session: { id: `session-${item.status}` } } as never,
        new AbortController().signal,
      ),
      error => (error as { code?: string }).code === item.code,
    )
    assert.equal(calls, 1, `${item.name} must not be retried`)
    assert.equal(service.loginStartUncertain, false, `${item.name} is authoritative`)
    assert.equal(service.loginStartInFlight, false, `${item.name} must release its latch`)
  }
})

test('loginFetch marks received non-2xx responses authoritative', async () => {
  const cases = [
    { status: 400, code: 'invalid_request' },
    { status: 409, code: 'turn_in_progress' },
    { status: 503, code: 'bridge_unavailable' },
  ] as const
  const loginFetch = (CodexService.prototype as unknown as {
    loginFetch(this: object, path: string, init: RequestInit): Promise<Record<string, unknown>>
  }).loginFetch
  for (const item of cases) {
    const service = {
      fetchControl: async () => new Response('', { status: item.status }),
    }
    await assert.rejects(
      loginFetch.call(service, '/api/codex/auth/login/start', { method: 'POST' }),
      error => {
        const candidate = error as { code?: string; authoritative?: unknown }
        return candidate.code === item.code && candidate.authoritative === true
      },
    )
  }
})

test('ambiguous login-start network failure retries the same operation, then succeeds', async () => {
  const requestBodies: string[] = []
  let calls = 0
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginStartUncertain: false,
    loginFetch: async (_path: string, init?: RequestInit) => {
      calls += 1
      const body = String(init?.body ?? '')
      requestBodies.push(body)
      if (calls === 1) throw Object.assign(new Error('connection dropped'), { code: 'bridge_unavailable' })
      return {
        operation_id: JSON.parse(body).operation_id,
        login_id: 'network-reconciled',
        status: 'pending',
        auth_url: 'https://auth.openai.com/oauth/authorize?state=reconciled',
      }
    },
  }
  const result = await CodexService.prototype.loginStart.call(
    service as never,
    { session: { id: 'session-network-reconciled' } } as never,
    new AbortController().signal,
  )
  assert.equal(result.loginId, 'network-reconciled')
  assert.equal(calls, 2)
  assert.equal(requestBodies[0], requestBodies[1])
  assert.ok(new TextEncoder().encode(requestBodies[0] ?? '').byteLength <= 256)
  assert.equal(service.loginStartUncertain, false)
  assert.equal(service.loginStartInFlight, false)
})

test('two ambiguous login-start network failures retain the same operation and quarantine', async () => {
  const requestBodies: string[] = []
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginStartInFlight: false,
    loginStartUncertain: false,
    loginFetch: async (_path: string, init?: RequestInit) => {
      requestBodies.push(String(init?.body ?? ''))
      throw Object.assign(new Error('connection dropped'), { code: 'bridge_unavailable' })
    },
  }
  await assert.rejects(
    CodexService.prototype.loginStart.call(
      service as never,
      { session: { id: 'session-network-unknown' } } as never,
      new AbortController().signal,
    ),
    error => (error as { code?: string }).code === 'bridge_unavailable',
  )
  assert.equal(requestBodies.length, 2)
  assert.equal(requestBodies[0], requestBodies[1])
  assert.equal(service.loginStartUncertain, true)
  assert.equal(service.loginStartInFlight, true)
})

test('login owner parser accepts the backend 256-character bound and rejects 257', async () => {
  const service = {
    loginOwners: new Map<string, { sessionId: string }>(),
    loginFetch: async () => ({ status: 'pending' }),
  }
  for (const length of [128, 129, 256]) {
    const loginId = 'a'.repeat(length)
    service.loginOwners.set(loginId, { sessionId: 'session-long-login' })
    const result = await CodexService.prototype.loginStatus.call(
      service as never,
      { session: { id: 'session-long-login' } } as never,
      loginId,
      new AbortController().signal,
    )
    assert.equal(result.loginId, loginId)
  }
  const tooLong = 'a'.repeat(257)
  await assert.rejects(
    CodexService.prototype.loginStatus.call(
      service as never,
      { session: { id: 'session-long-login' } } as never,
      tooLong,
      new AbortController().signal,
    ),
    error => (error as { code?: string }).code === 'invalid_request',
  )
})
