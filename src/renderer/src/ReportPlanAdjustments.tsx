import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Undo2, X } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ReportPlanAdjustment, ReportPlanConstraint, ReportPlanDecision, ReportPlanProposal } from './reportPlanAdjustment'
import './reportPlanAdjustments.css'

type Props = { childId: string; snapshotId: string; feedbackRevision: number | null }
const labels: Record<ReportPlanConstraint, string> = { daily_total_minutes_cap: '每天安排总时长', task_segment_minutes_cap: '每段学习时长' }
const states = { propose: '待确认', approve: '已批准', reject: '未采用', retract: '已撤回' }
const releaseReasons = { feedback_retracted: '原评价已撤回，上限已解除', adjustment_retracted: '上限已撤回', rejected: '未采用', expired: '日期已结束，上限已解除' }
function today(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

export function ReportPlanAdjustments({ childId, snapshotId, feedbackRevision }: Props): React.JSX.Element {
  const [opened, setOpened] = useState(false)
  const [items, setItems] = useState<ReportPlanAdjustment[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [kind, setKind] = useState<ReportPlanConstraint>('daily_total_minutes_cap')
  const [minutes, setMinutes] = useState(20)
  const [start, setStart] = useState(today)
  const [end, setEnd] = useState(today)
  const epoch = useRef(0)
  const pending = useRef<ReportPlanProposal | null>(null)
  useEffect(() => {
    epoch.current += 1
    setOpened(false); setItems(null); setBusy(false); setMessage(''); pending.current = null
    setKind('daily_total_minutes_cap'); setMinutes(20); setStart(today()); setEnd(today())
    return () => { epoch.current += 1 }
  }, [childId, snapshotId, feedbackRevision])

  function failure(error: unknown): string {
    if (error instanceof ApiError && error.status === 409) return '报告或评价已变化，这项调整不能继续批准。请核对最新报告和评价。'
    if (error instanceof ApiError && error.status === 422) return '调整未保存。请检查日期和分钟数，时长不能超过现有家庭规则。'
    return '尚未确认保存结果。请刷新核对或按原内容重试；不会重复保存。'
  }

  async function refresh(): Promise<void> {
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    try {
      const rows = await localApi.reportPlanAdjustments(childId, snapshotId)
      if (generation === epoch.current) setItems(rows)
    } catch { if (generation === epoch.current) setMessage('暂时无法读取计划调整，请重试。') }
    finally { if (generation === epoch.current) setBusy(false) }
  }

  async function propose(): Promise<void> {
    if (feedbackRevision === null || busy) return
    const values = { expected_feedback_revision: feedbackRevision, constraint_type: kind, value_minutes: minutes, start_date: start, end_date: end }
    const prior = pending.current
    const same = prior && Object.entries(values).every(([key, value]) => prior[key as keyof ReportPlanProposal] === value)
    const command = same ? prior : { ...values, client_action_id: crypto.randomUUID() }
    pending.current = command
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    try {
      const saved = await localApi.proposeReportPlanAdjustment(childId, snapshotId, command)
      if (generation !== epoch.current) return
      setItems(rows => [saved, ...(rows ?? []).filter(row => row.adjustment_id !== saved.adjustment_id)])
      pending.current = null; setMessage('调整已保存，等待家长确认。')
    } catch (error) { if (generation === epoch.current) setMessage(failure(error)) }
    finally { if (generation === epoch.current) setBusy(false) }
  }

  async function review(row: ReportPlanAdjustment, decision: ReportPlanDecision): Promise<void> {
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    try {
      const saved = await localApi.reviewReportPlanAdjustment(childId, row.adjustment_id, row.revision, decision)
      if (generation !== epoch.current) return
      setItems(rows => (rows ?? []).map(item => item.adjustment_id === saved.adjustment_id ? saved : item))
      setMessage(decision === 'approve' ? '已批准。日期范围内的后续排程采用此上限；正在执行的任务不会被自动中断。' : decision === 'retract' ? '已撤回。后续排程不再采用此上限，原记录保留。' : '已记录不采用。')
    } catch (error) { if (generation === epoch.current) setMessage(failure(error)) }
    finally { if (generation === epoch.current) setBusy(false) }
  }

  return <details className="report-plan-adjustments" open={opened} onToggle={event => {
    const value = event.currentTarget.open
    setOpened(value)
    if (value && items === null && !busy) void refresh()
  }}>
    <summary>后续计划的时长调整</summary>
    {opened && <div aria-busy={busy}>
      {message && <p role="status">{message}</p>}
      {busy && <p role="status">正在核对计划调整…</p>}
      {feedbackRevision === null ? <p>尚无当前有效评价，暂不能新增调整。</p> : <form onSubmit={event => { event.preventDefault(); void propose() }}>
        <fieldset disabled={busy}>
          <legend>新的时长上限</legend>
          <label>调整范围<select value={kind} onChange={event => setKind(event.target.value as ReportPlanConstraint)}>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>不超过（分钟）<input type="number" min={1} max={1440} step={1} required value={Number.isNaN(minutes) ? '' : minutes} onChange={event => setMinutes(event.target.valueAsNumber)} /></label>
          <label>开始日期<input type="date" required value={start} onChange={event => setStart(event.target.value)} /></label>
          <label>结束日期<input type="date" required min={start} value={end} onChange={event => setEnd(event.target.value)} /></label>
          <button type="submit">保存待确认调整</button>
        </fieldset>
      </form>}
      {!busy && items?.length === 0 && <p>还没有计划调整。</p>}
      <ul>{items?.map(row => <li key={row.adjustment_id}>
        <strong>{labels[row.constraint_type]}不超过{row.value_minutes}分钟</strong>
        <p><time>{row.start_date}</time> 至 <time>{row.end_date}</time> · {row.release_reason ? releaseReasons[row.release_reason] : states[row.operation]}{!row.release_reason && !row.source_current ? ' · 原评价已失效' : !row.release_reason && row.end_date < today() ? ' · 日期已结束' : ''}</p>
        {row.operation === 'approve' && row.end_date >= today() && row.authority === 'retained_parent_safety_guard' && row.requires_review && <p>原报告依据已变化。这项时长上限仍保守生效，范围限定为上述日期，等待家长核对；不会自动增加孩子的学习时长。</p>}
        <div className="report-plan-actions">
          {row.operation === 'propose' && <>
            <button type="button" disabled={busy || !row.source_current || row.end_date < today()} onClick={() => void review(row, 'approve')}><Check size={16} aria-hidden="true" />确认此上限</button>
            <button type="button" disabled={busy} onClick={() => void review(row, 'reject')}><X size={16} aria-hidden="true" />不采用</button>
          </>}
          {row.operation === 'approve' && <button type="button" disabled={busy} onClick={() => void review(row, 'retract')}><Undo2 size={16} aria-hidden="true" />撤回此上限</button>}
        </div>
      </li>)}</ul>
      <button type="button" disabled={busy} title="刷新计划调整" aria-label="刷新计划调整" onClick={() => void refresh()}><RefreshCw size={16} /></button>
    </div>}
  </details>
}
