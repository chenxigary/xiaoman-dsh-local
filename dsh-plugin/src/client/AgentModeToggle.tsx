/** Explicit DSH/Codex mode and character selector. */
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { AgentMode } from './agent-mode.ts'
import type { VoiceControlProps } from './contract.ts'
import {
  cancelCodexLoginBestEffort,
  forgetCodexLoginOwner,
  getCodexLoginOwner,
  isAllowedCodexAuthUrl,
  rememberCodexLoginOwner,
  unavailableCodexStatus,
  type CodexAuthStatus,
  type CodexLoginOwner,
} from './codex-remote-client.ts'
import { canSelectCodex, canShowCodexLogin } from './codex-auth-gate.ts'
import css from './AgentModeToggle.module.css'

export type AgentModeToggleProps = VoiceControlProps

function waitWithAbort(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    let settled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const finish = () => {
      if (settled) return
      settled = true
      if (timer !== null) globalThis.clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      resolve()
    }
    const onAbort = () => finish()
    timer = globalThis.setTimeout(finish, delayMs)
    signal.addEventListener('abort', onAbort, { once: true })
    if (signal.aborted) finish()
  })
}

export const AgentModeToggle = memo(function AgentModeToggle({ useStore, actions, codexStatus, codexLoginStart, codexLoginPending, codexLoginStatus, codexLoginCancel, sessionId, switchMode, syncComposerRoute }: AgentModeToggleProps) {
  const mode = useStore(state => state.mode)
  const character = useStore(state => state.character)
  const codexHistoryHydrated = useStore(state => state.codexHistoryHydrated)
  const [busy, setBusy] = useState(false)
  const [auth, setAuth] = useState<CodexAuthStatus | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)
  const [operationError, setOperationError] = useState<string | null>(null)
  const loginAbortRef = useRef<AbortController | null>(null)
  const loginEpochRef = useRef(0)
  const reconciledLoginSessionRef = useRef<string | undefined>(undefined)
  const sessionRef = useRef(sessionId)
  sessionRef.current = sessionId

  useEffect(() => {
    syncComposerRoute({ character, codexHistoryHydrated })
  }, [character, codexHistoryHydrated, syncComposerRoute])

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false
    void codexStatus(sessionId, controller.signal).then((status) => {
      if (!cancelled) setAuth(status)
    }).catch(error => {
      if (!cancelled && !(error instanceof Error && error.name === 'AbortError')) setAuth(unavailableCodexStatus())
    })
    return () => { cancelled = true; controller.abort() }
  }, [codexStatus, sessionId])

  useEffect(() => {
    if (sessionId === undefined) return
    const ownerSessionId = String(sessionId)
    // A remounted toggle gets one fresh chance to reconcile a timed-out
    // cleanup. The operation itself is deduplicated in the client owner
    // registry, so rerenders cannot issue parallel cancel requests.
    if (reconciledLoginSessionRef.current === ownerSessionId) return
    reconciledLoginSessionRef.current = ownerSessionId
    void (async () => {
      let owner = getCodexLoginOwner(ownerSessionId)
      if (codexLoginPending !== undefined) {
        try {
          const pending = await codexLoginPending(ownerSessionId)
          if (pending === null) {
            if (owner !== undefined) forgetCodexLoginOwner(owner)
            return
          }
          const recovered: CodexLoginOwner = { sessionId: ownerSessionId, loginId: pending.loginId }
          if (pending.status !== 'pending') {
            forgetCodexLoginOwner(recovered)
            if (owner !== undefined) forgetCodexLoginOwner(owner)
            return
          }
          owner = recovered
          rememberCodexLoginOwner(owner)
        } catch {
          // Keep a locally retained owner for the exact cancel retry. A
          // temporary lookup failure must not turn it into a new flow.
        }
      }
      if (owner !== undefined) void cancelCodexLoginBestEffort(codexLoginCancel, owner)
    })()
  }, [codexLoginCancel, codexLoginPending, sessionId])

  useEffect(() => {
    // A login belongs to the exact session that opened its popup. Session
    // reuse must abort it before a late completion can update the new owner.
    loginEpochRef.current += 1
    loginAbortRef.current?.abort()
    loginAbortRef.current = null
    setLoggingIn(false)
    setAuth(null)
  }, [sessionId])

  useEffect(() => () => {
    loginEpochRef.current += 1
    loginAbortRef.current?.abort()
    loginAbortRef.current = null
  }, [])

  const choose = useCallback(async (next: AgentMode) => {
    if (next === mode || busy) return
    setBusy(true)
    setOperationError(null)
    try {
      if (next === 'codex') {
        const status = await codexStatus(sessionId)
        if (!canSelectCodex(status)) {
          setAuth(status)
          return
        }
        setAuth(status)
      }
      await switchMode(next)
    } catch (error) {
      if (!(error instanceof Error && error.name === 'AbortError')) {
        setOperationError('Codex 执行未确认结束，无法切换模式')
      }
    } finally {
      setBusy(false)
    }
  }, [busy, codexStatus, mode, sessionId, switchMode])

  const openLogin = useCallback(async () => {
    if (loggingIn || sessionId === undefined || !canShowCodexLogin(auth)) return
    const ownerSessionId = String(sessionId)
    const controller = new AbortController()
    const epoch = ++loginEpochRef.current
    loginAbortRef.current?.abort()
    loginAbortRef.current = controller
    setLoggingIn(true)
    let owner: CodexLoginOwner | undefined
    let terminal = false
    let completed = false
    const popup = window.open('', '_blank', 'popup,width=480,height=720')
    if (popup === null) {
      loginAbortRef.current = null
      setLoggingIn(false)
      return
    }
    try { popup.opener = null } catch { /* browser may expose opener read-only */ }
    try {
      const started = await codexLoginStart(ownerSessionId, controller.signal)
      owner = { sessionId: ownerSessionId, loginId: started.loginId }
      rememberCodexLoginOwner(owner)
      if (controller.signal.aborted || loginEpochRef.current !== epoch || sessionRef.current !== sessionId) return
      if (!isAllowedCodexAuthUrl(started.authUrl)) {
        popup.close()
        return
      }
      popup.location.replace(started.authUrl)
      const deadline = Date.now() + 60_000
      while (Date.now() < deadline && !popup.closed && !controller.signal.aborted && loginEpochRef.current === epoch) {
        await waitWithAbort(500, controller.signal)
        if (popup.closed) controller.abort()
        if (controller.signal.aborted || loginEpochRef.current !== epoch) break
        const loginState = await codexLoginStatus(ownerSessionId, started.loginId, controller.signal)
        if (loginState.status === 'completed' && loginState.success === true) {
          forgetCodexLoginOwner(owner)
          completed = true
          terminal = true
          const status = await codexStatus(ownerSessionId, controller.signal)
          if (!controller.signal.aborted && loginEpochRef.current === epoch && sessionRef.current === sessionId) setAuth(status)
          break
        }
        if (loginState.status === 'completed' || loginState.status === 'failed' || loginState.status === 'canceled' || loginState.status === 'not_found') {
          forgetCodexLoginOwner(owner)
          terminal = true
          break
        }
      }
    } catch {
      // The button is a best-effort convenience; the status remains in the row.
    } finally {
      if (owner !== undefined && !terminal && !completed) {
        await cancelCodexLoginBestEffort(codexLoginCancel, owner)
      }
      popup.close()
      if (loginEpochRef.current === epoch) {
        loginAbortRef.current = null
        setLoggingIn(false)
      }
    }
  }, [auth, codexLoginCancel, codexLoginStart, codexLoginStatus, codexStatus, loggingIn, sessionId])

  const chooseCharacter = useCallback((event: ChangeEvent<HTMLSelectElement>) => {
    actions.setCharacter(event.target.value === 'xiaoman' ? 'xiaoman' : 'default')
  }, [actions])

  const codexLoggedIn = auth?.state === 'ready'
  const codexLabel = codexLoggedIn ? 'Codex ✓' : 'Codex'
  const codexTitle = codexLoggedIn
    ? auth.capability === 'unavailable'
      ? 'ChatGPT 已登录；Codex 执行暂不可用'
      : 'ChatGPT 已登录'
    : auth?.state === 'signed_out'
      ? 'ChatGPT 尚未登录'
      : 'Codex 执行暂不可用'

  return (
    <span className={css.root} aria-label="智能体模式">
      <button type="button" className={`${css.button} ${mode === 'dsh' ? css.active : ''}`} aria-pressed={mode === 'dsh'} disabled={busy} onClick={() => void choose('dsh')}>DSH</button>
      <button type="button" className={`${css.button} ${mode === 'codex' ? css.active : ''}`} aria-label={codexTitle} title={codexTitle} aria-pressed={mode === 'codex'} disabled={busy || !canSelectCodex(auth)} onClick={() => void choose('codex')}>{codexLabel}</button>
      {auth !== null && (
        auth.state === 'ready'
          ? auth.capability === 'unavailable' && <span className={css.status} role="status">Codex 执行暂不可用</span>
          : auth.state === 'signed_out'
            ? <>
              <button type="button" className={css.login} disabled={loggingIn} onClick={() => void openLogin()}>{loggingIn ? '正在检查登录…' : '登录 ChatGPT'}</button>
              {auth.capability === 'unavailable' && <span className={css.status} role="status">Codex 执行暂不可用</span>}
            </>
            : <span className={css.status} role="status">Codex 执行暂不可用</span>
      )}
      <select className={css.button} aria-label="角色" value={character} onChange={chooseCharacter} disabled={busy}>
        <option value="xiaoman">小满（默认）</option>
        <option value="default">通用助手</option>
      </select>
      {operationError !== null && <span className={css.status} role="status">{operationError}</span>}
    </span>
  )
})
