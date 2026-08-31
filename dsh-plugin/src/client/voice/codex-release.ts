/** Typed client-side wait for the Host's authoritative Codex terminal state. */
export interface CodexReleaseExecution {
  readonly executionId: string
  /** Host lifecycle states; terminal is visible before maintenance release. */
  readonly state: 'starting' | 'running' | 'settling' | 'terminal' | 'blocked'
  /** Host release ACK; a terminal record can remain owned during cleanup. */
  readonly released?: boolean
}

export interface CodexReleasePort {
  status(sessionId: string, signal?: AbortSignal): Promise<{ readonly executions: readonly CodexReleaseExecution[] }>
  interrupt(sessionId: string, executionId: string, signal?: AbortSignal): Promise<void>
}

export interface CodexReleaseOptions {
  readonly pollMs?: number
  readonly maxPolls?: number
  readonly signal?: AbortSignal
}

/** Terminal is not a release boundary: the owner must disappear or ACK release. */
export function codexExecutionStillOwned(execution: CodexReleaseExecution): boolean {
  return execution.released !== true
}

/** Interrupt exact executions, then wait until every one is terminal. */
export async function interruptAndAwaitCodexTerminal(
  api: CodexReleasePort,
  sessionId: string,
  options: CodexReleaseOptions = {},
): Promise<void> {
  const pollMs = options.pollMs ?? 100
  const maxPolls = options.maxPolls ?? 50
  for (let poll = 0; poll <= maxPolls; poll += 1) {
    if (options.signal?.aborted) throw abortError()
    const status = await api.status(sessionId, options.signal)
    const active = status.executions.filter(codexExecutionStillOwned)
    if (active.length === 0) return
    if (poll === 0) {
      for (const execution of active) {
        await api.interrupt(sessionId, execution.executionId, options.signal)
      }
    }
    if (poll === maxPolls) break
    await delay(pollMs, options.signal)
  }
  throw new Error('Codex 执行仍未结束，暂不能切换模式')
}

/** Same fence for one exact execution returned by a late start response. */
export async function interruptExactAndAwaitCodexTerminal(
  api: CodexReleasePort,
  sessionId: string,
  executionId: string,
  options: CodexReleaseOptions = {},
): Promise<void> {
  const pollMs = options.pollMs ?? 100
  const maxPolls = options.maxPolls ?? 50
  for (let poll = 0; poll <= maxPolls; poll += 1) {
    if (options.signal?.aborted) throw abortError()
    // A late start response may arrive before the execution is visible in a
    // status snapshot.  The exact owner is still authoritative: send the
    // interrupt once before polling so that the host cannot resurrect it
    // between the response and the first status read.
    if (poll === 0) await api.interrupt(sessionId, executionId, options.signal)
    const status = await api.status(sessionId, options.signal)
    const execution = status.executions.find(item => item.executionId === executionId)
    if (execution === undefined || !codexExecutionStillOwned(execution)) return
    if (poll === maxPolls) break
    await delay(pollMs, options.signal)
  }
  throw new Error('Codex 执行仍未结束，暂不能切换模式')
}

function abortError(): Error {
  const error = new Error('Codex 操作已取消')
  error.name = 'AbortError'
  return error
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const finish = () => {
      if (settled) return
      settled = true
      if (timer !== null) globalThis.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }
    const onAbort = () => {
      if (settled) return
      settled = true
      if (timer !== null) globalThis.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      reject(abortError())
    }
    timer = globalThis.setTimeout(finish, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
    if (signal?.aborted) onAbort()
  })
}
