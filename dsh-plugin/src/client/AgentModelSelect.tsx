/** One composer model seat whose backend follows the explicit agent mode. */

import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { ChangeEvent } from 'react'
import type { ModelSelection } from '@deepseek-ai/dsh-api-remotes/client'
import { IconChevronDownOutline14 } from '@deepseek-ai/dsh-client-ui-primitives'
import type { CodexModelCatalogResult, CodexModelSelection } from '../types.ts'
import type { AgentModelSelectProps } from './contract.ts'
import { reconcileCodexSelection, selectionForModel } from './codex-model-selection.ts'
import css from './AgentModelSelect.module.css'

function dshSelectionKey(selection: ModelSelection): string {
  return `${selection.provider}\u0000${selection.model}`
}

export function AgentModelSelect({
  locked, useStore, dshModel, codexModels, getCodexSelection, setCodexSelection, t,
}: AgentModelSelectProps) {
  const mode = useStore(state => state.mode)
  const dsh = useSyncExternalStore(dshModel.directory.subscribe, dshModel.directory.getSnapshot)
  const [open, setOpen] = useState(false)
  const [catalog, setCatalog] = useState<CodexModelCatalogResult | null>(null)
  const [codexSelection, setLocalCodexSelection] = useState<CodexModelSelection>(() => getCodexSelection())
  const [catalogState, setCatalogState] = useState<'idle' | 'loading' | 'error'>('idle')
  const rootRef = useRef<HTMLDivElement | null>(null)
  const id = useId()

  const loadCodex = (): void => {
    const controller = new AbortController()
    setCatalogState('loading')
    void codexModels(controller.signal).then((next) => {
      const selection = reconcileCodexSelection(next, getCodexSelection())
      setCatalog(next)
      setLocalCodexSelection(selection)
      setCodexSelection(selection)
      setCatalogState('idle')
    }, () => { setCatalogState('error') })
  }

  useEffect(() => {
    if (mode === 'dsh') dshModel.load()
    else loadCodex()
    // The injected callbacks are stable for this mounted seat.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode])

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => { document.removeEventListener('mousedown', close) }
  }, [open])

  const dshRows = useMemo(() => dsh.groups.flatMap(group => group.models.map(model => ({ group, model }))), [dsh.groups])
  const dshCurrent = dsh.current === null
    ? undefined
    : dshRows.find(row => row.group.id === dsh.current?.provider && row.model.id === dsh.current.model)
  const dshReasoning = dshCurrent?.model.reasoning
  const dshEffort = dsh.current?.reasoningEffort ?? dshReasoning?.defaultEffort
  const codexModel = catalog?.models.find(model => model.id === codexSelection.model)
  const label = mode === 'codex'
    ? (codexModel?.displayName ?? codexSelection.model)
    : (dshCurrent?.model.name ?? '选择模型')
  const effort = mode === 'codex' ? codexSelection.reasoningEffort : dshEffort

  const chooseDshModel = (event: ChangeEvent<HTMLSelectElement>): void => {
    const row = dshRows.find(candidate => dshSelectionKey({ provider: candidate.group.id, model: candidate.model.id }) === event.target.value)
    if (row === undefined) return
    const selection: ModelSelection = {
      provider: row.group.id,
      model: row.model.id,
      ...(row.model.reasoning?.defaultEffort === undefined ? {} : { reasoningEffort: row.model.reasoning.defaultEffort }),
    }
    void dshModel.select(selection)
  }

  const chooseDshEffort = (event: ChangeEvent<HTMLSelectElement>): void => {
    if (dsh.current === null) return
    void dshModel.select({ ...dsh.current, reasoningEffort: event.target.value })
  }

  const commitCodex = (next: CodexModelSelection): void => {
    setLocalCodexSelection(next)
    setCodexSelection(next)
  }

  const chooseCodexModel = (event: ChangeEvent<HTMLSelectElement>): void => {
    const model = catalog?.models.find(candidate => candidate.id === event.target.value)
    if (model !== undefined) commitCodex(selectionForModel(model))
  }

  const chooseCodexEffort = (event: ChangeEvent<HTMLSelectElement>): void => {
    commitCodex({ ...codexSelection, reasoningEffort: event.target.value })
  }

  const chooseCodexTier = (event: ChangeEvent<HTMLSelectElement>): void => {
    commitCodex({ ...codexSelection, serviceTier: event.target.value === '' ? null : event.target.value })
  }

  return (
    <div ref={rootRef} className={css.root}>
      <button
        type="button"
        className={css.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? `${id}-menu` : undefined}
        disabled={locked}
        title={`${label}${effort === undefined ? '' : ` · ${effort}`}`}
        onClick={() => {
          const next = !open
          setOpen(next)
          if (next) mode === 'codex' ? loadCodex() : dshModel.load()
        }}
      >
        <span className={css.triggerLabel}>{label}</span>
        {effort !== undefined && <span className={css.triggerEffort}>{effort}</span>}
        <IconChevronDownOutline14 className={css.chevron} />
      </button>
      {open && (
        <div id={`${id}-menu`} className={css.menu} role="menu">
          {mode === 'dsh' ? (
            <>
              <label className={css.row}>
                <span>{t('model.model')}</span>
                <select value={dsh.current === null ? '' : dshSelectionKey(dsh.current)} onChange={chooseDshModel} disabled={dsh.status === 'loading' || dsh.status === 'selecting'}>
                  {dshRows.map(({ group, model }) => <option key={`${group.id}/${model.id}`} value={dshSelectionKey({ provider: group.id, model: model.id })}>{group.name} · {model.name}</option>)}
                </select>
              </label>
              {dshReasoning !== undefined && (
                <label className={css.row}>
                  <span>{t('model.effort')}</span>
                  <select value={dshEffort ?? ''} onChange={chooseDshEffort} disabled={dsh.status === 'selecting'}>
                    {dshReasoning.efforts.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}
                  </select>
                </label>
              )}
              {dsh.error !== null && <div className={css.error}>{t('model.error')}</div>}
            </>
          ) : catalogState === 'loading' && catalog === null ? (
            <div className={css.status}>{t('model.loading')}</div>
          ) : catalogState === 'error' && catalog === null ? (
            <button type="button" className={css.retry} onClick={loadCodex}>{t('model.error')} · {t('model.retry')}</button>
          ) : (
            <>
              <label className={css.row}>
                <span>{t('model.model')}</span>
                <select value={codexSelection.model} onChange={chooseCodexModel}>
                  {catalog?.models.map(model => <option key={model.id} value={model.id}>{model.displayName}</option>)}
                </select>
              </label>
              <label className={css.row}>
                <span>{t('model.effort')}</span>
                <select value={codexSelection.reasoningEffort} onChange={chooseCodexEffort}>
                  {codexModel?.supportedReasoningEfforts.map(option => <option key={option.id} value={option.id}>{option.id}</option>)}
                </select>
              </label>
              <label className={css.row}>
                <span>{t('model.speed')}</span>
                <select value={codexSelection.serviceTier ?? ''} onChange={chooseCodexTier}>
                  <option value="">{t('model.standard')}</option>
                  {codexModel?.serviceTiers.map(option => <option key={option.id} value={option.id}>{option.name}（{option.description}）</option>)}
                </select>
              </label>
              {catalogState === 'error' && <button type="button" className={css.retry} onClick={loadCodex}>{t('model.retry')}</button>}
            </>
          )}
        </div>
      )}
    </div>
  )
}
