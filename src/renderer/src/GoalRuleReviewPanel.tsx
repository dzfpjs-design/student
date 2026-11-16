import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, ShieldCheck } from 'lucide-react'
import { ApiError, localApi } from './api'
import { GoalExamRules } from './GoalExamRules'
import type { GoalRuleConfirmation, GoalRuleReview } from './goalRuleReviewTypes'
import { formatExamItemTarget, formatExamSectionDuration } from './examRuleDisplay'

const prefix = 'hsc:goal-rule-confirmation:'
export function clearGoalRuleReviewLocalState(childId?: string): void {
  const target = childId ? `${prefix}${encodeURIComponent(childId)}:` : prefix
  Object.keys(localStorage).filter(key => key.startsWith(target)).forEach(key => localStorage.removeItem(key))
}
function pendingRequest(key: string): GoalRuleConfirmation | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? 'null') as GoalRuleConfirmation | null
    return value && typeof value.client_action_id === 'string' && typeof value.expected_rule_version === 'string'
      && /^[a-f0-9]{64}$/.test(value.expected_rule_hash) && /^[a-f0-9]{64}$/.test(value.expected_program_version) ? value : null
  } catch { return null }
}

export function GoalRuleReviewPanel({ childId, programId, track, onSaved }: {
  childId: string; programId: string; track: string; onSaved: () => Promise<void> | void
}): React.JSX.Element {
  const identity = `${childId}:${programId}:${track}`
  const current = useRef(identity)
  current.current = identity
  const journal = `${prefix}${encodeURIComponent(childId)}:${encodeURIComponent(programId)}:${track}`
  const [open, setOpen] = useState(false)
  const [review, setReview] = useState<GoalRuleReview | null>(null)
  const [checked, setChecked] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState<GoalRuleConfirmation | null>(() => pendingRequest(journal))
  useEffect(() => {
    setOpen(false); setReview(null); setChecked(false); setBusy(false); setMessage('')
    setPending(pendingRequest(journal))
  }, [identity, journal])
  const owned = (value: GoalRuleReview): boolean => value.child_id === childId && value.program_id === programId
    && value.track === track && value.local_only === true && value.cloud_transmission_allowed === false
  const load = async (): Promise<void> => {
    setOpen(true); setBusy(true); setReview(null); setChecked(false); setMessage('')
    try {
      const value = await localApi.goalRuleReview(childId, track)
      if (current.current !== identity) return
      if (!owned(value)) throw new Error('owner')
      setReview(value)
    } catch (cause) {
      if (current.current === identity) setMessage(cause instanceof ApiError ? cause.message : '规则没有读取成功，请重新核对。')
    } finally { if (current.current === identity) setBusy(false) }
  }
  const confirm = async (): Promise<void> => {
    if (!pending && (!checked || !review?.can_confirm || !review.rules.current_snapshot || !owned(review))) return
    const snapshot = review?.rules.current_snapshot
    const request = pending ?? { expected_program_version: review!.program_version,
      expected_rule_version: snapshot!.version, expected_rule_hash: snapshot!.snapshot_hash, client_action_id: crypto.randomUUID() }
    setBusy(true); setMessage('')
    try {
      // Persist the exact command before transmission. A lost response must not acknowledge a newer version.
      localStorage.setItem(journal, JSON.stringify(request)); setPending(request)
      const saved = await localApi.confirmGoalRules(childId, track, request)
      if (current.current !== identity) return
      if (!owned(saved.review) || saved.receipt.child_id !== childId || saved.receipt.program_id !== programId
          || saved.receipt.rule_hash !== request.expected_rule_hash) throw new Error('owner')
      localStorage.removeItem(journal); setPending(null); setReview(saved.review); setChecked(false)
      setMessage(saved.review.rules.binding_status === 'current'
        ? '已保存这次核对。新路线采用此参考，旧任务不会自动换版。'
        : '上次核对已保存，但当前规则又有变化，请重新核对。')
      try { await onSaved() } catch { setMessage('核对已保存；路线暂未刷新，请重新读取，不必重复确认。') }
    } catch (cause) {
      if (current.current !== identity) return
      if (cause instanceof ApiError && [404, 409].includes(cause.status ?? 0)) {
        localStorage.removeItem(journal); setPending(null); setReview(null); setChecked(false)
      }
      setMessage(cause instanceof ApiError ? cause.message : '尚未确认保存结果，请重试原请求。')
    } finally { if (current.current === identity) setBusy(false) }
  }
  return <section aria-label="核对目标规则">
    <button className="secondary-button" disabled={busy} onClick={() => void load()}><ShieldCheck size={16} />核对规则与适用范围</button>
    {open && <>
      {busy && <p role="status">正在核对</p>}
      {review && owned(review) && <>
        <GoalExamRules rules={review.rules} />
        {review.rules.current_snapshot && <>
          <details open><summary>本次规划参考内容</summary>
            {review.rules.current_snapshot.blueprint_snapshots.map(blueprint => <div key={blueprint.blueprint_id}>
              <strong>{blueprint.title}</strong><ul>{blueprint.sections.map(section => <li key={section.section_id}>
                {section.title} · {formatExamSectionDuration(section)}
                {section.item_target != null ? ` · ${formatExamItemTarget(section.item_target, section.item_target_unit)}` : ''}
              </li>)}</ul>
            </div>)}
          </details>
          <details><summary>参考版本记录</summary><p>{review.rules.current_snapshot.version}</p>
            <p style={{ overflowWrap: 'anywhere' }}>{review.rules.current_snapshot.snapshot_hash}</p></details>
        </>}
        {!review.can_confirm ? <p>当前没有可确认的参考，或目标已暂停；不能通过确认生成已核实规则。</p> : <>
          <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, margin: '12px 0', lineHeight: 1.6 }}>
            <input type="checkbox" style={{ width: 18, height: 18, minHeight: 18, flex: '0 0 18px', marginTop: 4 }} checked={checked} disabled={busy || !!pending} onChange={event => setChecked(event.target.checked)} />
            <span>我已核对以上内容，同意作为此目标的规划参考；这不确认报名、未来规则或考试能力。</span></label>
          <button disabled={busy || !checked || !!pending} onClick={() => void confirm()}><Check size={16} />确认本次参考</button>
        </>}
      </>}
    </>}
    {pending && <button disabled={busy} onClick={() => void confirm()}><RefreshCw size={16} />重试上次规则确认</button>}
    {message && <p role="status">{message}</p>}
  </section>
}
