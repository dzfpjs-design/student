import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { AlertCircle, CheckCircle2 } from 'lucide-react'
import { ApiError, localApi } from './api'
import { MistakeAction } from './MistakeBook'
import { privacyStopPending } from './deviceCommandReceipt'
import type { MistakeItem, MistakeNextActivity } from './types'

type ActivityProps = {
  sessionId?: string
  childId: string
  mistakeId: string
  active: boolean
  onProgress: (item: MistakeItem, isCurrent: () => boolean) => Promise<boolean>
}

export function MistakePlanActivity(props: ActivityProps): React.JSX.Element {
  return <OwnedMistakePlanActivity key={JSON.stringify([props.sessionId, props.childId, props.mistakeId])} {...props} />
}

function OwnedMistakePlanActivity({ childId, mistakeId, active, onProgress, sessionId }: ActivityProps): React.JSX.Element {
  const [item, setItem] = useState<MistakeItem | null>(null)
  const [next, setNext] = useState<MistakeNextActivity | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [progressPending, setProgressPending] = useState<MistakeItem | null>(null)
  const progressPendingRef = useRef<MistakeItem | null>(null)
  const owner = useRef({ mounted: false, generation: 0, activeEpoch: 0, operation: null as 'load' | 'save' | 'sync' | null })
  const activity = useRef({ active, onProgress, epoch: 0 })
  if (activity.current.active !== active) activity.current.epoch++
  activity.current.active = active; activity.current.onProgress = onProgress

  useLayoutEffect(() => {
    owner.current.mounted = true
    return () => {
      owner.current.mounted = false
      owner.current.generation++
      owner.current.operation = null
    }
  }, [])

  const current = (generation = owner.current.generation, requiresActive = false): boolean =>
    owner.current.mounted && owner.current.generation === generation
      && (!sessionId || !privacyStopPending(sessionId))
      && (!requiresActive || activity.current.active && owner.current.activeEpoch === activity.current.epoch)
  const owns = (updated: MistakeItem): boolean => updated.child_id === childId && updated.mistake_id === mistakeId
  const begin = (operation: 'load' | 'save' | 'sync'): number | null => {
    if (!current() || operation !== 'load' && !activity.current.active
      || owner.current.operation && !(operation === 'load' && owner.current.operation === 'load')
      || operation !== 'sync' && progressPendingRef.current) return null
    const generation = ++owner.current.generation
    owner.current.activeEpoch = activity.current.epoch
    owner.current.operation = operation
    setBusy(true); setError(null)
    return generation
  }
  const finish = (generation: number): void => {
    if (!owner.current.mounted || owner.current.generation !== generation) return
    owner.current.operation = null
    setBusy(false)
  }
  const announce = (message: string): void => {
    window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message, sessionId, childId } }))
  }

  const load = async (): Promise<boolean> => {
    const generation = begin('load')
    if (generation === null) return false
    try {
      const result = await localApi.mistakes(childId)
      if (!current(generation)) return false
      const loaded = result.items.find(owns)
      if (!loaded) throw new ApiError('这道错题已被移除，请延期本任务并刷新计划。')
      const prepared = await localApi.nextMistakeActivity(mistakeId, childId)
      if (!current(generation)) return false
      setItem(loaded); setNext(prepared)
      return true
    } catch (cause) {
      if (!current(generation)) return false
      const message = cause instanceof ApiError ? cause.message : '错题步骤没有加载成功，请重试。'
      setError(message)
      announce(`${message} 你可以说“重试错题步骤”。`)
      return true
    } finally {
      finish(generation)
    }
  }

  useEffect(() => { void load() }, [childId, mistakeId])

  const syncProgress = async (updated: MistakeItem, generation: number): Promise<boolean> => {
    if (!current(generation) || !owns(updated)) return false
    // Saving and completing the plan are distinct operations. Retain a saved
    // receipt even if pause/privacy prevents its subsequent plan completion.
    progressPendingRef.current = updated
    setProgressPending(updated)
    const isCurrent = (): boolean => current(generation, true)
    if (!isCurrent()) return false
    const synced = await activity.current.onProgress(updated, isCurrent)
    if (!isCurrent()) return false
    if (synced) {
      progressPendingRef.current = null
      setProgressPending(null)
      setError(null)
      return true
    }
    // Keep an immediately readable copy for a voice retry arriving before
    // React has committed the state update and refreshed the event effect.
    setError('错题步骤已经保存，但今日计划状态还没有同步。请重试同步，不要重复作答。')
    announce('错题步骤已经保存，但计划进度还没有同步。请说“重试同步进度”，不要重复作答。')
    return true
  }

  const retryProgress = async (): Promise<boolean> => {
    const pending = progressPendingRef.current
    if (!pending) return false
    const generation = begin('sync')
    if (generation === null) return false
    try { return await syncProgress(pending, generation) }
    catch {
      if (!current(generation, true)) return false
      setError('错题步骤已经保存，但计划进度暂未同步。请重试同步，不要重复作答。')
      return true
    } finally { finish(generation) }
  }

  useEffect(() => {
    const recover = (event: Event): void => {
      const detail = (event as CustomEvent<{ action?: 'reload' | 'sync'; sessionId?: string; childId?: string;
        mistakeId?: string; resolve?: (handled: boolean) => void }>).detail
      if (!current() || detail?.sessionId && detail.sessionId !== sessionId || detail?.childId && detail.childId !== childId
        || detail?.mistakeId && detail.mistakeId !== mistakeId) return
      if (detail?.action === 'reload') {
        void load().then(handled => detail.resolve?.(handled))
        return
      }
      const pending = progressPendingRef.current
      if (detail?.action === 'sync' && pending) {
        void retryProgress().then(handled => detail.resolve?.(handled))
        return
      }
      detail?.resolve?.(false)
    }
    window.addEventListener('hsc-mistake-recovery-voice-action', recover)
    return () => window.removeEventListener('hsc-mistake-recovery-voice-action', recover)
  }, [childId, mistakeId, sessionId])

  const run = async (action: () => Promise<MistakeItem>, success: string): Promise<void> => {
    if (!current()) return
    if (!activity.current.active) {
      setError('请先说“开始这项”，再完成本次错题步骤。')
      return
    }
    const generation = begin('save')
    if (generation === null) return
    try {
      const updated = await action()
      if (!current(generation)) return
      if (!owns(updated)) throw new ApiError('保存回执不属于本次错题，未推进计划。请重新核对。')
      setItem(updated)
      setMessage(updated.attempt_result?.feedback ?? success)
      await syncProgress(updated, generation)
    } catch (cause) {
      if (current(generation)) setError(progressPendingRef.current
        ? '错题步骤已经保存，但计划进度暂未同步。请重试同步，不要重复作答。'
        : cause instanceof ApiError ? cause.message : '步骤没有保存，请重试。')
    } finally {
      finish(generation)
    }
  }

  if (busy && !item) return <section className="mistake-plan-activity"><span className="loader" /><p>正在准备今天的错题步骤...</p></section>
  if (error && !item) return <section className="mistake-plan-activity is-error"><AlertCircle size={21} /><p>{error}</p><button className="secondary-button" onClick={() => void load()}>重试加载</button></section>
  if (!item) return <></>

  return <section className="mistake-plan-activity" aria-label="今日错题强化">
    <header><div><span>{item.subject} · {item.knowledge_node.title}</span><strong>{item.title}</strong></div><b>{active ? '本次只推进一步' : '等待开始'}</b></header>
    {!active && <p className="mistake-plan-note">开始计时后再作答。完成一次诊断、补救或变式步骤就会结束本任务。</p>}
    {message && <div className="mistake-feedback"><CheckCircle2 size={16} />{message}</div>}
    {error && <div className="inline-error">{error}</div>}
    {progressPending ? <button className="primary-button" disabled={busy || !active} onClick={() => void retryProgress()}>重试同步计划进度</button> : <MistakeAction focusEnabled={active} sessionId={sessionId} item={item} next={next} childId={childId} busy={busy || !active} run={run} />}
  </section>
}
