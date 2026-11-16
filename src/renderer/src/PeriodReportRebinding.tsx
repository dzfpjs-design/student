import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Undo2, X } from 'lucide-react'
import { ApiError } from './api'
import { clearPeriodRebindingLocalState, readRebindingLocal, rebindingApi, writeRebindingLocal } from './reportRebindingContract'
import type { LocalRebinding, RebindingAdjustment, RebindingCommand, RebindingContext, RebindingTerms } from './reportRebindingContract'
import './periodReportRebinding.css'

const labels = { daily_total_minutes_cap: '每天安排总时长', task_segment_minutes_cap: '每段学习时长' }
const states = { propose: '待家长确认', approve: '家长已确认', reject: '未采用', retract: '已撤回' }
function familyTomorrow(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(Date.now() + 86400000))
}
const defaults = (): RebindingTerms => ({ constraint_type: 'daily_total_minutes_cap', value_minutes: 20, start_date: familyTomorrow(), end_date: familyTomorrow() })

function Evidence({ context }: { context: RebindingContext }): React.JSX.Element {
  const content = context.report.content
  const edit = context.report.parent_edit
  const summary = content.summary as { interpretation?: string }
  return <div className="rebinding-evidence">
    <section aria-label="原报告反馈"><h5>原报告的家长反馈</h5><p>{context.historical_report.content.headline}</p>
      <p>帮助程度：{context.feedback.helpfulness ?? '未评价'}；沟通压力：{context.feedback.stress_rating ?? '未评价'}</p>
      <blockquote>{context.feedback.notes || '未填写备注'}</blockquote><p>第{context.feedback.revision}次反馈 · {context.feedback.created_at.slice(0, 10)}</p>
    </section>
    <section aria-label="当前已审核报告"><h5>当前已审核报告</h5><p>{content.headline}</p>
      {summary.interpretation && <p>{summary.interpretation}</p>}
      <p>{content.parent_coaching?.action?.what}</p>
      <ul>{content.parent_coaching?.script?.map((line, index) => <li key={index}>{line}</li>)}</ul>
      {edit.parent_note && <p>家长备注：{edit.parent_note}</p>}
      {edit.coaching_action && <p>家长核定行动：{edit.coaching_action}</p>}
    </section>
  </div>
}

function Confirmation({ row, busy, onReview }: { row: RebindingAdjustment; busy: boolean; onReview: (row: RebindingAdjustment, decision: 'approve' | 'reject' | 'retract', seen: boolean) => void }): React.JSX.Element {
  const [seen, setSeen] = useState(false)
  useEffect(() => setSeen(false), [row.review_hash, row.source_current])
  return <li className="rebinding-record">
    <strong>{labels[row.terms.constraint_type]}不超过{row.terms.value_minutes}分钟</strong>
    <p>{row.terms.start_date} 至 {row.terms.end_date} · {states[row.operation]}</p>
    {!row.source_current && <p role="status">依据或审核已变化，当前不能采用这项关联。请核对并审核新版后重新提出。</p>}
    {row.operation === 'propose' && <>
      <Evidence context={row.evidence} />
      <label className="rebinding-confirm"><input type="checkbox" checked={seen} disabled={busy || !row.source_current} onChange={event => setSeen(event.target.checked)} />我已核对原反馈、当前报告及上述日期和上限</label>
      <button disabled={busy || !row.source_current || !seen} onClick={() => onReview(row, 'approve', seen)}><Check size={16} />确认此上限</button>
      <button disabled={busy} onClick={() => onReview(row, 'reject', false)}><X size={16} />不采用</button>
    </>}
    {row.operation === 'approve' && <button disabled={busy} onClick={() => onReview(row, 'retract', false)}><Undo2 size={16} />撤回此上限</button>}
  </li>
}

type Props = { childId: string; snapshotId: string; onRefreshReports?: () => Promise<void>; onOpenReport?: (snapshot: string) => void }
export function PeriodReportRebinding(props: Props): React.JSX.Element {
  return <RebindingContent key={`${props.childId}:${props.snapshotId}`} {...props} />
}

function RebindingContent({ childId, snapshotId, onRefreshReports, onOpenReport }: Props): React.JSX.Element {
  const [initial] = useState(() => { try { return { value: readRebindingLocal(childId), error: false } } catch { return { value: null, error: true } } })
  const [opened, setOpened] = useState(Boolean(initial.value?.pending))
  const [terms, setTerms] = useState(initial.value?.snapshot === snapshotId ? initial.value.terms : defaults)
  const [pending, setPending] = useState(initial.value?.pending ?? null)
  const [corrupt, setCorrupt] = useState(initial.error)
  const [contexts, setContexts] = useState<RebindingContext[] | null>(null)
  const [choice, setChoice] = useState('')
  const [rows, setRows] = useState<RebindingAdjustment[]>([])
  const [recovered, setRecovered] = useState<RebindingAdjustment | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(initial.error ? '本机恢复记录无法读取，未发送任何操作。请先核对服务器记录。' : '')
  const generation = useRef(0)
  const active = useRef(true)
  const locked = useRef(false)
  useEffect(() => {
    active.current = true
    const pause = (): void => { generation.current++; setOpened(false); setContexts(null); setBusy(false) }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => { active.current = false; generation.current++; window.removeEventListener('hsc-privacy-pause', pause) }
  }, [])
  const selected = contexts?.find(item => item.binding.feedback_event_id === choice) ?? contexts?.[0]
  function store(nextTerms: RebindingTerms, nextPending: LocalRebinding['pending']): void {
    writeRebindingLocal({ version: 1, child: childId, snapshot: snapshotId, terms: nextTerms, pending: nextPending })
  }
  function change(next: RebindingTerms): void {
    setTerms(next)
    try { store(next, pending); setMessage('') } catch { setMessage('本机未能保存输入，提交已停止。请检查本机存储后重试。') }
  }
  function accept(row: RebindingAdjustment): void {
    if (row.snapshot_id === snapshotId) setRows(items => [row, ...items.filter(item => item.adjustment_id !== row.adjustment_id)])
  }
  async function refresh(): Promise<void> {
    if (locked.current) return
    locked.current = true; setBusy(true)
    const epoch = ++generation.current
    try {
      const [options, saved] = await Promise.all([rebindingApi.contexts(childId, snapshotId), rebindingApi.list(childId, snapshotId)])
      if (!active.current || epoch !== generation.current) return
      setContexts(options.items); setRows(saved.items)
    } catch { if (active.current && epoch === generation.current) setMessage('暂时无法核对报告和调整，请重试。') }
    finally { locked.current = false; if (active.current && epoch === generation.current) setBusy(false) }
  }
  useEffect(() => { if (opened && contexts === null) void refresh() }, [opened])
  async function submit(write: (key: string) => Promise<RebindingAdjustment>): Promise<void> {
    if (locked.current || pending || corrupt) return
    const holder = { key: crypto.randomUUID(), snapshot: snapshotId }
    try { store(terms, holder) } catch { setMessage('本机未能保存核对编号，未发送。'); return }
    setPending(holder); locked.current = true; setBusy(true); setMessage('')
    const epoch = ++generation.current
    try {
      const saved = await write(holder.key)
      if (!active.current || epoch !== generation.current) return
      accept(saved); store(terms, null); setPending(null)
      setMessage(saved.operation === 'approve' ? '确认已保存，后续符合日期且依据有效的排程采用此上限。' : saved.operation === 'propose' ? '提案已保存，等待你核对并确认。' : '决定已保存。')
    } catch (error) {
      if (!active.current || epoch !== generation.current) return
      if (error instanceof ApiError && [409, 422].includes(error.status ?? 0)) {
        try { store(terms, null); setPending(null) } catch { /* Keep the durable recovery identity. */ }
        setMessage('依据、审核或输入已变化，操作未获批准。请刷新后重新核对。')
      } else setMessage('保存结果尚未确认。请核对原操作，不会自动重新提交。')
    } finally { locked.current = false; if (active.current && epoch === generation.current) setBusy(false) }
  }
  async function recover(cancel = false): Promise<void> {
    if (!pending || locked.current) return
    locked.current = true; setBusy(true)
    const epoch = ++generation.current
    try {
      const result: RebindingCommand = await (cancel ? rebindingApi.cancel(childId, pending.key, pending.snapshot) : rebindingApi.command(childId, pending.key))
      if (!active.current || epoch !== generation.current) return
      if ('current' in result) {
        accept(result.current)
        if (result.current.snapshot_id !== snapshotId) setRecovered(result.current)
      }
      store(terms, null); setPending(null)
      setMessage('current' in result ? '原操作已保存，已读回当前状态；没有重复提交。' : '该次未完成提交已取消，迟到请求不会执行。')
    } catch { if (active.current && epoch === generation.current) setMessage('尚未查到确定结果；可以继续核对，或明确取消这次未完成提交。') }
    finally { locked.current = false; if (active.current && epoch === generation.current) setBusy(false) }
  }
  return <details className="period-report-rebinding" open={opened} onToggle={event => setOpened(event.currentTarget.open)}>
    <summary>根据历史反馈确认后续上限</summary>
    {opened && <div aria-busy={busy}>
      {message && <p role="status">{message}</p>}
      {busy && <p role="status">正在核对…</p>}
      {pending && <div role="alert"><p>有一次操作等待核对。刷新或重启后仍保留原编号。</p>
        <button disabled={busy} onClick={() => void recover()}><RefreshCw size={16} />核对原操作</button>
        <button disabled={busy} onClick={() => void recover(true)}><X size={16} />取消未完成提交</button>
      </div>}
      {recovered && <section aria-label="原操作所在报告"><p>原操作属于另一份报告：{labels[recovered.terms.constraint_type]}不超过{recovered.terms.value_minutes}分钟 · {states[recovered.operation]}</p>
        {onOpenReport && <button onClick={() => onOpenReport(recovered.snapshot_id)}>查看原操作所在报告</button>}
      </section>}
      {corrupt && contexts !== null && <button disabled={busy} onClick={() => { try { clearPeriodRebindingLocalState(childId); setCorrupt(false); setMessage('已清除损坏的本机记录；服务器记录保留。') } catch { setMessage('仍无法清理本机记录。') } }}>已核对服务器记录，清理损坏的本机记录</button>}
      {contexts?.length === 0 && <p>当前没有可关联的历史反馈。请刷新报告，核对并批准已纳入原反馈的后继报告，无需再给后继报告评分。</p>}
      {contexts?.length === 0 && onRefreshReports && <button disabled={busy} onClick={() => void onRefreshReports().then(refresh).catch(() => setMessage('报告刷新失败，请重试。'))}><RefreshCw size={16} />刷新报告列表</button>}
      {selected && !rows.some(row => row.operation === 'propose') && <form onSubmit={event => { event.preventDefault(); void submit(key => rebindingApi.propose(childId, selected.binding, terms, key)) }}>
        {contexts!.length > 1 && <label>选择原反馈<select value={selected.binding.feedback_event_id} disabled={busy || Boolean(pending)} onChange={event => setChoice(event.target.value)}>{contexts!.map(item => <option key={item.binding.feedback_event_id} value={item.binding.feedback_event_id}>{item.feedback.created_at.slice(0, 10)} · {item.feedback.notes || '家长评价'}</option>)}</select></label>}
        <Evidence context={selected} />
        <fieldset disabled={busy || Boolean(pending) || corrupt}><legend>具体时长上限</legend>
          <label>调整范围<select value={terms.constraint_type} onChange={event => change({ ...terms, constraint_type: event.target.value as RebindingTerms['constraint_type'] })}>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          <label>不超过（分钟）<input required type="number" min={1} max={1440} step={1} value={terms.value_minutes} onChange={event => change({ ...terms, value_minutes: Number(event.target.value) })} /></label>
          <label>开始日期<input required type="date" value={terms.start_date} onChange={event => change({ ...terms, start_date: event.target.value })} /></label>
          <label>结束日期<input required type="date" min={terms.start_date} value={terms.end_date} onChange={event => change({ ...terms, end_date: event.target.value })} /></label>
          <button type="submit">保存关联提案</button>
        </fieldset>
      </form>}
      <ul className="rebinding-records">{rows.map(row => <Confirmation key={row.adjustment_id} row={row} busy={busy || Boolean(pending) || corrupt} onReview={(item, decision, seen) => void submit(key => rebindingApi.review(childId, item, decision, seen, key))} />)}</ul>
      <button title="刷新关联依据" aria-label="刷新关联依据" disabled={busy} onClick={() => void refresh()}><RefreshCw size={16} /></button>
    </div>}
  </details>
}
