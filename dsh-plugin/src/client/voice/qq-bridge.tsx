/**
 * QQBridge: hidden component (renders null) that bridges the conversation to
 * QQ via the bridge's /api/qq/ws WebSocket + NapCat:
 *
 *  - inbound: a QQ private message arrives -> bridge pushes
 *    { type: 'qq_message', text } -> we inject it via sendText (same
 *    steer/queue delivery as voice input).
 *  - outbound: when a new assistant reply settles, we push its text back to
 *    the bridge ({ type: 'reply' }), which synthesizes TTS voice and sends
 *    it to the configured QQ.
 *
 * Enabled only when the bridge config has `qq.enabled`; the WS simply fails
 * to connect otherwise (silent, no UI).
 */
import { memo, useEffect, useRef } from 'react'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls ui-conversation's SlotMap merge for PropsRuntime resolution.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { AssistantChatData } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceInjected } from '../contract.ts'
import type { VoiceStoreHandle } from '../agent-mode.ts'
import { readQqPush } from './qq-settings.ts'
import { cleanReplyText } from './clean.ts'
import { acceptsQqInbound, acceptsQqOutbound } from './qq-gate.ts'
import { MAX_QQ_TEXT_CHARS } from './qq-owner.ts'

/** Full props: framework runtime share + `voice` locale seat + injected face. */
export type QQBridgeProps = PropsRuntime<'conversation.input.left'> & PropsStore<VoiceStoreHandle> & PropsLocale<'voice'> & VoiceInjected


function assistantData(node: { kind: string; data: unknown }): AssistantChatData | undefined {
  if (node.kind !== 'assistant-step') return undefined
  return node.data as AssistantChatData
}

function nodeText(data: AssistantChatData): string {
  return data.blocks
    .filter((block) => block.kind === 'text')
    .map((block) => block.text)
    .join('\n')
}

/**
 * @param props - framework runtime + locale + injected sendText.
 */
export const QQBridge = memo(function QQBridge({ useSession, useStore, sendText, sessionId, registerQqSession, sendQqReply }: QQBridgeProps) {
  const lastReplyAnchorRef = useRef(0)
  const baselineOwnerRef = useRef<string | undefined>(undefined)
  const baselineReadyRef = useRef(false)
  const snapshot = useSession((s) => s)
  const mode = useStore(state => state.mode)
  const modeRef = useRef(mode)
  modeRef.current = mode

  // The apply-level owner gives this session the active route. It owns the
  // only socket and validates frame/text caps before this callback runs.
  useEffect(() => {
    const release = registerQqSession(sessionId, text => {
      if (text === '') return
      // QQ has no authenticated DSH session identity. Never route an inbound
      // message through native session.prompt in Codex mode.
      if (!acceptsQqInbound(modeRef.current, sessionId)) {
        console.info('[ui-voice] Codex 模式已禁用 QQ 入站消息：缺少安全会话身份')
        return
      }
      void sendText(text).catch(() => {
        console.error('[ui-voice] QQ 消息发送失败')
      })
    })
    return release
  }, [registerQqSession, sendText, sessionId])

  // New settled assistant reply -> push text to the bridge (it voices it to QQ).
  // Skips entirely when the QQ push toggle is off.
  useEffect(() => {
    if (!readQqPush()) return
    if (baselineOwnerRef.current !== sessionId) {
      baselineOwnerRef.current = sessionId
      baselineReadyRef.current = false
      lastReplyAnchorRef.current = 0
    }
    if (!acceptsQqOutbound(mode, sessionId) || snapshot.openState !== 'open') {
      // A Codex owner can never emit a native QQ reply.  Deferring the
      // baseline until DSH hydration also prevents remounts from replaying
      // the existing history.
      baselineReadyRef.current = false
      return
    }
    let maxAnchor = 0
    let newest: { anchor: number; text: string } | null = null
    for (const node of snapshot.chat.nodes.values()) {
      if (node.kind !== 'assistant-step') continue
      const data = assistantData(node)
      if (data === undefined || data.status !== 'settled') continue
      if (node.anchorSeq > maxAnchor) {
        maxAnchor = node.anchorSeq
        newest = { anchor: node.anchorSeq, text: cleanReplyText(nodeText(data), MAX_QQ_TEXT_CHARS) }
      }
    }
    if (!baselineReadyRef.current) {
      lastReplyAnchorRef.current = maxAnchor
      baselineReadyRef.current = true
      return
    }
    if (newest !== null && newest.anchor > lastReplyAnchorRef.current) {
      const text = newest.text.trim()
      if (text !== '' && sendQqReply(String(sessionId), text)) {
        lastReplyAnchorRef.current = newest.anchor
      }
    }
  }, [mode, sendQqReply, sessionId, snapshot])

  return null
})
