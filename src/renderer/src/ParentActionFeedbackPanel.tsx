import { useEffect, useRef, useState } from 'react'
import { RefreshCw, Save, Undo2 } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ParentActionFeedbackInput, ParentActionSnapshot, ParentReportFeedback } from './types'

export const parentActionPendingPrefix = (childId: string): string => `hsc-parent-action-pending:${childId}:`

export function clearParentActionPending(childId: string): void {
  Object.keys(localStorage).filter((key) => key.startsWith(parentActionPendingPrefix(childId))).forEach((key) => localStorage.removeItem(key))
}

function readPending(key: string, snapshot: ParentActionSnapshot): ParentActionFeedbackInput | null {
  const encoded = localStorage.getItem(key)
  if (!encoded) return null
  const parsed = JSON.parse(encoded) as ParentActionFeedbackInput
  if (parsed.action_snapshot_id !== snapshot.snapshot_id || typeof parsed.client_action_id !== 'string'
    || typeof parsed.expected_revision !== 'number' || !['record', 'retract'].includes(parsed.operation ?? '')) throw new Error('invalid pending feedback')
  return parsed
}

function localTime(iso?: string | null): string {
  if (!iso) return ''
  const value = new Date(iso)
  if (!Number.isFinite(value.getTime())) return ''
  return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

export function ParentActionFeedbackPanel({ snapshot, historical = false }: { snapshot: ParentActionSnapshot; historical?: boolean }): React.JSX.Element {
  const key = parentActionPendingPrefix(snapshot.child_id) + snapshot.snapshot_id
  const [recovery] = useState(() => {
    try { return { pending: readPending(key, snapshot), invalid: false } }
    catch { return { pending: null, invalid: true } }
  })
  const [feedback, setFeedback] = useState<ParentReportFeedback | null>(snapshot.feedback)
  const [pending, setPending] = useState(recovery.pending)
  const initial = recovery.pending ?? (snapshot.feedback?.operation === 'retract' ? null : snapshot.feedback)
  const [executed, setExecuted] = useState(initial?.executed === true ? 'yes' : initial?.executed === false ? 'no' : 'unknown')
  const [executedAt, setExecutedAt] = useState(localTime(initial?.executed_at))
  const [effect, setEffect] = useState(initial?.effectiveness?.toString() ?? '')
  const [stress, setStress] = useState(initial?.stress_rating?.toString() ?? '')
  const [notes, setNotes] = useState(initial?.notes_summary ?? '')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [message, setMessage] = useState(recovery.invalid ? '上次反馈的恢复记录无法读取，请先核对已保存反馈。' : recovery.pending ? '上次保存结果尚未确认，核对后可用原请求重试。' : '')
  const [invalid, setInvalid] = useState(recovery.invalid)

  const save = async (operation: 'record' | 'retract'): Promise<void> => {
    if (busyRef.current || invalid || (!pending && operation === 'record' && snapshot.feedback_eligibility?.record_allowed === false)) return
    const moment = executedAt ? new Date(executedAt) : null
    if (!pending && operation === 'record' && executed === 'yes' && (!moment || !Number.isFinite(moment.getTime()) || moment.getTime() > Date.now())) {
      setMessage('请填写实际执行时间，不能使用未来时间。'); return
    }
    const request: ParentActionFeedbackInput = pending ?? {
      action_snapshot_id: snapshot.snapshot_id, expected_revision: feedback?.revision ?? 0,
      client_action_id: crypto.randomUUID(), operation,
      executed: operation === 'retract' ? null : executed === 'unknown' ? null : executed === 'yes',
      executed_at: operation === 'record' && executed === 'yes' ? moment!.toISOString() : null,
      effectiveness: operation === 'record' && executed === 'yes' && effect ? Number(effect) : null,
      stress_rating: operation === 'record' && executed === 'yes' && stress ? Number(stress) : null,
      notes_summary: operation === 'record' ? notes.trim() : '',
      ...(operation === 'retract' ? { reason: reason.trim() } : {}),
    }
    busyRef.current = true; setBusy(true)
    try {
      localStorage.setItem(key, JSON.stringify(request))
      setPending(request)
      const saved = snapshot.report_id
        ? await localApi.saveParentReportFeedback(snapshot.report_id, request)
        : await localApi.saveParentActionFeedback(snapshot.child_id, snapshot.snapshot_id, request)
      setFeedback(saved)
      localStorage.removeItem(key); setPending(null)
      setMessage(saved.operation === 'retract' ? '反馈已撤回，原记录保留，后续策略不再使用它。' : '反馈已保存到这一个行动版本；效果仍是家长报告，不等于已证明因果。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '保存结果尚未确认。填写内容已保留；可以核对已保存反馈或重试原请求。')
    } finally { busyRef.current = false; setBusy(false) }
  }

  const reconcile = async (): Promise<void> => {
    if (busyRef.current) return
    busyRef.current = true; setBusy(true)
    try {
      const current = snapshot.report_id
        ? (await localApi.parentActions(snapshot.report_id)).find((item) => item.snapshot_id === snapshot.snapshot_id)
        : await localApi.parentAction(snapshot.child_id, snapshot.snapshot_id)
      if (!current) { setMessage('这个行动已不可用，请重新载入报告；没有重新提交旧反馈。'); return }
      setFeedback(current.feedback)
      if (pending && current.feedback?.client_action_id === pending.client_action_id) {
        localStorage.removeItem(key); setPending(null); setMessage('已核对：上次反馈已保存，没有重复提交。')
      } else if (pending && (current.feedback?.revision ?? 0) !== pending.expected_revision) {
        setMessage('另一个窗口已更新反馈。你的填写内容仍保留，请确认后放弃旧请求，再提交修订。')
      } else setMessage('已载入服务器的反馈状态；未确认的请求不会自动提交。')
      setInvalid(false)
    } catch { setMessage('暂时无法核对反馈，原填写内容仍保留。') }
    finally { busyRef.current = false; setBusy(false) }
  }

  if (snapshot.execution_allowed === false) return <section className="report-feedback" aria-label="行动版本反馈">
    <h3>本次已选择跳过</h3>
    <p>无需执行原建议，也不会记录行动成功或效果评分。</p>
  </section>

  return <section className="report-feedback" aria-label="行动版本反馈">
    <h3>行动执行反馈 · 第{snapshot.version}版</h3>
    {historical && <p role="note">这是当时已批准的历史建议，来源或审核状态已变化。可记录当时实际情况或撤回已有反馈；这不会重新批准执行，也不证明学习效果。</p>}
    {snapshot.feedback_eligibility?.record_allowed === false && <p role="status">该版本已被明确撤销或批准依据不可验证，不能补记新反馈；已有反馈仍可核对和撤回。</p>}
    <p><strong>{snapshot.coaching.action?.what}</strong> · {snapshot.timing.duration_minutes ?? '未定'}分钟</p>
    <p className="muted-copy">{snapshot.coaching.scenario}</p>
    <fieldset disabled={busy || !!pending || invalid}>
      <label>执行情况<select value={executed} onChange={(event) => setExecuted(event.target.value)}><option value="unknown">暂未记录</option><option value="no">暂未执行</option><option value="yes">已经执行</option></select></label>
      {executed === 'yes' && <div className="feedback-ratings">
        <label>实际执行时间<input type="datetime-local" value={executedAt} onChange={(event) => setExecutedAt(event.target.value)} /></label>
        <label>实际效果<select value={effect} onChange={(event) => setEffect(event.target.value)}><option value="">尚不能判断</option>{[1, 2, 3, 4, 5].map((v) => <option key={v} value={v}>{v} · {v === 1 ? '没有帮助' : v === 5 ? '很有帮助' : '部分帮助'}</option>)}</select></label>
        <label>沟通压力<select value={stress} onChange={(event) => setStress(event.target.value)}><option value="">尚未记录</option>{[1, 2, 3, 4, 5].map((v) => <option key={v} value={v}>{v} · {v === 1 ? '很轻松' : v === 5 ? '压力很大' : '中等'}</option>)}</select></label>
      </div>}
      <label>家庭备注<textarea value={notes} maxLength={500} onChange={(event) => setNotes(event.target.value)} /></label>
      <button disabled={snapshot.feedback_eligibility?.record_allowed === false} onClick={() => void save('record')}><Save size={16} />保存行动反馈</button>
      {!!feedback && feedback.operation !== 'retract' && <><label>撤回原因<input value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label><button disabled={reason.trim().length < 2} onClick={() => void save('retract')}><Undo2 size={16} />撤回这次反馈</button></>}
    </fieldset>
    <button disabled={busy} onClick={() => void reconcile()}><RefreshCw size={16} />核对已保存反馈</button>
    {pending && <><button disabled={busy || invalid} onClick={() => void save(pending.operation ?? 'record')}>重试原请求</button><button disabled={busy} onClick={() => { localStorage.removeItem(key); setPending(null); setMessage('仅放弃本机待重试请求；服务器已保存的记录不会删除。') }}>放弃旧请求，保留填写内容</button></>}
    {message && <p role="status">{message}</p>}
    {feedback && <p>已保存反馈版本：{feedback.revision} · {feedback.operation === 'retract' ? '已撤回' : feedback.executed === true ? '已执行' : feedback.executed === false ? '未执行' : '暂未记录'}</p>}
  </section>
}

/** Original approved versions only; no current execution/plan authorization. */
export function HistoricalParentActionFeedback({ childId, reportId, actions, excludeSnapshotId }: {
  childId: string; reportId?: string; actions?: ParentActionSnapshot[]; excludeSnapshotId?: string
}): React.JSX.Element | null {
  const [loaded, setLoaded] = useState<{ childId: string; reportId: string; excludeSnapshotId?: string; items: ParentActionSnapshot[] } | null>(null)
  const [failed, setFailed] = useState(false)
  const [loading, setLoading] = useState(Boolean(reportId))
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let current = true
    setLoaded(null); setFailed(false); setLoading(Boolean(reportId))
    if (reportId) void localApi.parentActions(reportId).then(items => {
      if (!current) return
      if (items.some(item => item.child_id !== childId || item.report_id !== reportId)) throw new Error('历史行动归属不一致')
      setLoaded({ childId, reportId, excludeSnapshotId, items })
    }).catch(() => { if (current) setFailed(true) }).finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [childId, reportId, excludeSnapshotId, retry])
  const offered = reportId ? (loaded?.childId === childId && loaded.reportId === reportId && loaded.excludeSnapshotId === excludeSnapshotId ? loaded.items : []) : actions ?? []
  const items = offered.filter(item => item.child_id === childId && item.snapshot_id !== excludeSnapshotId
    && item.review_binding && item.feedback_eligibility?.approval_witness_valid
    && (item.feedback_eligibility.record_allowed || item.feedback))
  if (!reportId && !items.length) return null
  return <section aria-label="历史已批准行动反馈"><h3>补记历史建议的执行情况</h3>
    <p>先评价报告、之后再反馈行动也可以；请核对当时批准的具体版本。</p>
    {reportId && <><p role="status">{loading ? '正在读取历史行动…' : failed ? '历史行动反馈暂不可读取；没有自动重提或批准任何行动。' : items.length === 0 ? '当前没有可补记的历史行动。' : '历史行动已载入。'}</p>
      <button disabled={loading} onClick={() => setRetry(value => value + 1)}>重新读取历史行动</button></>}
    {items.map(item => <details key={item.snapshot_id}><summary>第{item.version}版 · {item.coaching.action?.what ?? '已跳过的建议'}</summary>
      <ParentActionFeedbackPanel snapshot={item} historical /></details>)}
  </section>
}
