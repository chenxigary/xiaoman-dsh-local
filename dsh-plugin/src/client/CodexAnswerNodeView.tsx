import { memo } from 'react'
import { MarkdownText, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ChatNodeViewProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import css from './CodexAnswerNode.module.css'

export const CodexAnswerNodeView = memo(function CodexAnswerNodeView({ node }: ChatNodeViewProps<'codex-answer'>) {
  const data = node.data
  const terminal = data.status !== 'running'
  return (
    <div className={css.root} data-codex-execution={data.executionId}>
      <div className={css.header}>
        <StateDot state={data.status === 'failed' ? 'error' : data.status === 'interrupted' ? 'warning' : data.status === 'running' ? 'ongoing' : 'done'} />
        <span>Codex 回复</span>
        {data.safeToolSummary !== undefined && <span className={css.tool}>{data.safeToolSummary}</span>}
        {terminal && <span className={css.status}>{data.status === 'completed' ? '已完成' : data.status === 'interrupted' ? '已中断' : '失败'}</span>}
      </div>
      {data.text !== '' && <MarkdownText text={data.text} streaming={!terminal} />}
    </div>
  )
})
