import type { CodexTerminalStatus } from '../../types.ts'

export function codexStatusLabel(status: 'running' | CodexTerminalStatus): '思考中' | '已完成' | '已中断' {
  return status === 'running' ? '思考中' : status === 'completed' ? '已完成' : '已中断'
}
