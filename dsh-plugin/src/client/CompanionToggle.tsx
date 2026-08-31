/** Companion-window visibility toggle backed by the session store. */
import { memo, useCallback } from 'react'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceControlProps } from './contract.ts'
import css from './CompanionToggle.module.css'

export type CompanionToggleProps = VoiceControlProps

function DisplayIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  )
}

export const CompanionToggle = memo(function CompanionToggle({ t, useStore, actions }: CompanionToggleProps) {
  const on = useStore(state => state.companion)
  const toggle = useCallback(() => { actions.setCompanion(!on) }, [actions, on])

  return (
    <button
      type="button"
      className={on ? css.displayOn : css.displayOff}
      title={on ? t('companion.offHint') : t('companion.onHint')}
      aria-label={on ? t('companion.offHint') : t('companion.onHint')}
      aria-pressed={on}
      onClick={toggle}
    >
      <DisplayIcon />
    </button>
  )
})
