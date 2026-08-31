/** Reply-voice toggle; playback cancellation is generation-fenced. */
import { memo, useCallback, useEffect } from 'react'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceControlProps } from './contract.ts'
import css from './VoiceToggle.module.css'

export type VoiceToggleProps = VoiceControlProps

function SpeakerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
    </svg>
  )
}

export const VoiceToggle = memo(function VoiceToggle({ t, useStore, actions, speaker, abortTts, registerSessionMount }: VoiceToggleProps) {
  const on = useStore(state => state.voice)
  useEffect(() => {
    registerSessionMount(true)
    return () => registerSessionMount(false)
  }, [registerSessionMount])
  const toggle = useCallback(() => {
    const next = !on
    actions.setVoice(next)
    actions.bumpTtsEpoch()
    if (!next) {
      speaker.stop()
      abortTts()
    }
  }, [abortTts, actions, on, speaker])

  return (
    <button
      type="button"
      className={on ? css.speakerOn : css.speakerOff}
      title={on ? t('toggle.offHint') : t('toggle.onHint')}
      aria-label={on ? t('toggle.offHint') : t('toggle.onHint')}
      aria-pressed={on}
      onClick={toggle}
    >
      <SpeakerIcon />
    </button>
  )
})
