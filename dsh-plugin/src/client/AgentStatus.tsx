/** Compact observable Codex/DSH projection for the composer row. */
import { memo } from 'react'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceControlProps } from './contract.ts'
import type { CodexAnswerChatData } from './codex-conversation.ts'
import { codexStatusLabel } from './voice/status-label.ts'
import css from './AgentStatus.module.css'

export type AgentStatusProps = VoiceControlProps

export const AgentStatus = memo(function AgentStatus({ useSession, useStore }: AgentStatusProps) {
  const mode = useStore(state => state.mode)
  const codex = useSession((session) => {
    let latest: CodexAnswerChatData | undefined
    for (const node of session.chat.nodes.values()) {
      if (node.kind !== 'codex-answer') continue
      latest = node.data as CodexAnswerChatData
    }
    return latest
  })
  // The finished answer already lives in the conversation. Keeping its full
  // text in the compact composer row duplicates content and squeezes the
  // controls, especially while the companion occupies the right-hand side.
  if (mode !== 'codex' || codex?.status !== 'running') return null
  return (
    <span className={css.status} role="status" aria-live="polite">
      <span className={css.state}>Codex：{codexStatusLabel(codex.status)}</span>
    </span>
  )
})
