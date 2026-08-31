/** Pure mode/owner gate for paths that may contact the Codex bridge. */
export type CodexGateMode = 'dsh' | 'codex'

/** Native DSH voice must never probe Codex when no explicit owner is known. */
export function shouldInterruptCodex(mode: CodexGateMode, knownOwner: boolean): boolean {
  return mode === 'codex' && knownOwner
}
