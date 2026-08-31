import { memo } from 'react'
import { MessageText } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ChatNodeViewProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import css from './CodexAnswerNode.module.css'

export const CodexUserNodeView = memo(function CodexUserNodeView({ node }: ChatNodeViewProps<'codex-user'>) {
  return (
    <div className={css.userRoot} data-codex-execution={node.data.executionId}>
      <span className={css.userLabel}>Codex 请求</span>
      <MessageText text={node.data.text} />
    </div>
  )
})
