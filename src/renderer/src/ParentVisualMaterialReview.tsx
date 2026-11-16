import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Undo2 } from 'lucide-react'
import { VisualMaterialImage, type VisualImageDescriptor } from './VisualMaterialImage'
import './visualMaterialReview.css'

export type VisualBinding = Pick<VisualImageDescriptor, 'artifact_id' | 'revision_id' | 'region_id' | 'region_version'> & {
  facts_version: string | null; confirmation_id: string | null
}
export type VisualReviewDetail = {
  child_id: string; session_id: string; binding: VisualBinding; image_descriptor: VisualImageDescriptor
  fact_status: 'unconfirmed' | 'confirmed' | 'retracted'; image_availability: 'available' | 'unavailable' | 'host_unverified'
  parent_confirmation_ready: boolean; source_current: boolean
  storage_status?: string
  observations: { version: 'visual_facts_v1'; observations: { id: string; kind: string; description: string; bbox: number[] }[]; uncertainties: string[] }
  local_only: true; cloud_transmission_allowed: false
}
export type VisualReviewCommand = {
  schema: 'visual_material_review_command_v1'; child_id: string; session_id: string; binding: VisualBinding
  decision: 'confirm' | 'retract'; client_action_id: string
  display_receipt_id?: string
}
export type VisualReviewResult = {
  status: 'ready_local' | 'withdrawn' | 'superseded'; binding: VisualBinding
  local_only: true; cloud_transmission_allowed: false
}
type Props = {
  detail: VisualReviewDetail
  readImage: (descriptor: VisualImageDescriptor, signal: AbortSignal) => Promise<ArrayBuffer>
  submit: (command: VisualReviewCommand, signal: AbortSignal) => Promise<VisualReviewResult>
  refresh: () => Promise<void>
  recordDisplayed: (detail: VisualReviewDetail, signal: AbortSignal) => Promise<{ display_receipt_id: string; local_only: true; cloud_transmission_allowed: false }>
}
const slot = (d: VisualReviewDetail) => `hsc-visual-review:${JSON.stringify([d.child_id, d.binding.artifact_id, d.binding.revision_id, d.binding.region_id])}`
const version = (d: VisualReviewDetail) => JSON.stringify([d.child_id, d.session_id, d.binding, d.image_descriptor, d.fact_status, d.image_availability, d.source_current])
const sameFacts = (a: VisualBinding, b: VisualBinding) => a.artifact_id === b.artifact_id && a.revision_id === b.revision_id
  && a.region_id === b.region_id && a.region_version === b.region_version && a.facts_version === b.facts_version
function recover(d: VisualReviewDetail): { pending: VisualReviewCommand | null; corrupt: boolean } {
  try {
    const text = localStorage.getItem(slot(d))
    if (!text) return { pending: null, corrupt: false }
    const p = JSON.parse(text) as VisualReviewCommand
    if (p.schema !== 'visual_material_review_command_v1' || p.child_id !== d.child_id || p.session_id !== d.session_id
      || typeof p.client_action_id !== 'string' || !p.client_action_id || !['confirm', 'retract'].includes(p.decision)
      || !p.binding || p.binding.artifact_id !== d.binding.artifact_id || p.binding.revision_id !== d.binding.revision_id
      || p.binding.region_id !== d.binding.region_id || !/^[a-f0-9]{64}$/.test(p.binding.region_version)
      || !(p.binding.facts_version === null && p.decision === 'retract'
        || typeof p.binding.facts_version === 'string' && /^[a-f0-9]{64}$/.test(p.binding.facts_version))) throw new Error('invalid recovery')
    return { pending: p, corrupt: false }
  } catch { return { pending: null, corrupt: true } }
}

export function ParentVisualMaterialReview(props: Props) {
  return <Review key={slot(props.detail)} {...props} />
}

function Review({ detail, readImage, submit, refresh, recordDisplayed }: Props) {
  const [initial] = useState(() => recover(detail))
  const [pending, setPending] = useState(initial.pending)
  const [storageFailed, setStorageFailed] = useState(initial.corrupt)
  const [displayed, setDisplayed] = useState('')
  const [displayReceipt, setDisplayReceipt] = useState<{ identity: string; id: string } | null>(null)
  const [checked, setChecked] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const active = useRef(true), running = useRef(false), request = useRef<AbortController | null>(null)
  const displayRequest = useRef<AbortController | null>(null)
  const identity = version(detail), current = useRef(identity); current.current = identity
  useEffect(() => { active.current = true; return () => { active.current = false; request.current?.abort(); displayRequest.current?.abort() } }, [])
  useEffect(() => { displayRequest.current?.abort(); setDisplayReceipt(null); setDisplayed(''); setChecked('') }, [identity])
  const local = detail.local_only === true && detail.cloud_transmission_allowed === false
  const exact = ['artifact_id', 'revision_id', 'region_id', 'region_version'].every(k =>
    detail.binding[k as keyof VisualBinding] === detail.image_descriptor[k as keyof VisualImageDescriptor])
  const hasFacts = typeof detail.binding.facts_version === 'string' && /^[a-f0-9]{64}$/.test(detail.binding.facts_version)
  const canConfirm = hasFacts && local && exact && detail.source_current && detail.parent_confirmation_ready
    && detail.image_availability === 'available' && detail.fact_status === 'unconfirmed'
    && !detail.observations.uncertainties.length && displayed === identity && checked === identity && displayReceipt?.identity === identity
  const canRetract = local && (detail.fact_status === 'confirmed'
    || detail.fact_status === 'unconfirmed' && ['stored', 'missing', 'corrupt'].includes(detail.storage_status ?? ''))
  const stalePending = !!pending && !sameFacts(pending.binding, detail.binding)
  const disabled = busy || storageFailed || !!pending
  const imageStatus = !local || !exact ? 'unavailable' : detail.fact_status === 'retracted' ? 'revoked'
    : !detail.source_current || detail.image_availability !== 'available' ? 'unavailable'
    : detail.fact_status === 'confirmed' ? 'ready_local' : 'needs_clarification'

  async function send(command: VisualReviewCommand) {
    if (running.current || storageFailed) return
    running.current = true; setBusy(true); setError(''); setNotice('')
    const at = current.current, abort = new AbortController(); request.current = abort
    const timeout = setTimeout(() => abort.abort(), 30_000)
    try {
      const result = await submit(command, abort.signal)
      if (!active.current || abort.signal.aborted) return
      if (result.local_only !== true || result.cloud_transmission_allowed !== false || !sameFacts(command.binding, result.binding)
        || !['ready_local', 'withdrawn', 'superseded'].includes(result.status)
        || result.status !== 'superseded' && result.status !== (command.decision === 'confirm' ? 'ready_local' : 'withdrawn')) throw new Error('wrong receipt')
      await refresh()
      if (!active.current || abort.signal.aborted) return
      try { localStorage.removeItem(slot(detail)) } catch { setStorageFailed(true); throw new Error('storage failed') }
      setPending(null); setChecked(''); setDisplayed('')
      setNotice(result.status === 'superseded' ? '原操作已核对，但图形内容后来发生了变化，请核对当前记录。'
        : command.decision === 'confirm' ? '图形内容的确认已保存；这不代表孩子已经掌握。'
          : command.binding.facts_version === null ? '图形的本机使用授权已撤回。' : '图形内容的确认已撤回。')
    } catch {
      if (active.current) setError('操作结果尚未核对，原操作已保留。恢复连接后核对原操作，不会再次生成新决定。')
    } finally {
      clearTimeout(timeout); running.current = false
      if (active.current) { setBusy(false); if (current.current !== at) { setChecked(''); setDisplayed('') } }
    }
  }
  function decide(decision: 'confirm' | 'retract') {
    if (disabled || decision === 'confirm' && !canConfirm || decision === 'retract' && !canRetract) return
    const command: VisualReviewCommand = { schema: 'visual_material_review_command_v1', child_id: detail.child_id,
      session_id: detail.session_id, binding: { ...detail.binding }, decision, client_action_id: crypto.randomUUID(),
      ...(decision === 'confirm' ? { display_receipt_id: displayReceipt!.id } : {}) }
    try { localStorage.setItem(slot(detail), JSON.stringify(command)) }
    catch { setStorageFailed(true); setError('本机无法保存审核操作，本次决定尚未发送。'); return }
    setPending(command); void send(command)
  }
  async function reread() {
    if (running.current) return
    running.current = true; setBusy(true); setError(''); setChecked(''); setDisplayed('')
    try { await refresh(); if (active.current) setReload(n => n + 1) }
    catch { if (active.current) setError('无法读取最新图形记录，未采用旧内容。') }
    finally { running.current = false; if (active.current) setBusy(false) }
  }
  async function acknowledgeDisplay() {
    displayRequest.current?.abort()
    const abort = new AbortController(); displayRequest.current = abort
    const at = identity
    const timeout = setTimeout(() => {
      abort.abort()
      if (active.current && current.current === at) { setDisplayReceipt(null); setError('原图显示回执尚未核对，暂不能确认。请重新读取原图。') }
    }, 15_000)
    try {
      const result = await recordDisplayed(detail, abort.signal)
      if (!active.current || abort.signal.aborted || current.current !== at) return
      if (!result.display_receipt_id || result.local_only !== true || result.cloud_transmission_allowed !== false) throw new Error('display receipt')
      setDisplayed(at); setDisplayReceipt({ identity: at, id: result.display_receipt_id })
    } catch { if (active.current && !abort.signal.aborted && current.current === at) setError('原图显示回执尚未核对，暂不能确认。请重新读取原图。') }
    finally { clearTimeout(timeout) }
  }
  return <section className="visual-review" aria-label="图形原件核对" style={{ borderTop: '1px solid #cfd9d3', padding: 16, minWidth: 0, overflowWrap: 'anywhere' }}>
    <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
      <h3 style={{ margin: 0, fontSize: 16 }}>图形原件核对</h3>
      <button className="visual-icon" type="button" title="重新读取原图与核对记录" aria-label="重新读取原图与核对记录" disabled={busy} onClick={() => void reread()}><RefreshCw size={18} /></button>
    </header>
    <div className="visual-review-body">
    <VisualMaterialImage child_id={detail.child_id} session_id={detail.session_id} descriptor={detail.image_descriptor}
      status={imageStatus} readImage={readImage} reloadVersion={reload}
      onDisplayed={() => { if (current.current === identity) void acknowledgeDisplay() }} onUnavailable={() => { displayRequest.current?.abort(); setDisplayReceipt(null); setDisplayed(''); setChecked('') }} />
    <div className="visual-review-facts">
    <h4>待核对的标记与关系</h4>
    {!hasFacts && <p>这幅图尚无可核对的标记与关系，暂不据此评分。</p>}
    <ol>{detail.observations.observations.map(item => <li key={item.id}>{item.description}</li>)}</ol>
    {!!detail.observations.uncertainties.length && <div role="status"><p>以下内容尚不确定，本次不能确认：</p><ul>{detail.observations.uncertainties.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
    {storageFailed && <p role="alert">本机审核恢复记录无法读取或保存，新的审核已暂停，原记录未删除。</p>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {pending ? <div><p>{stalePending ? '图形版本已经变化，仍需核对之前操作的结果，旧决定不会自动用于新图。' : '有一次审核操作等待核对。'}</p>
      <button disabled={busy || storageFailed} type="button" onClick={() => void send(pending)}><RefreshCw size={16} />核对原操作</button></div>
      : <div style={{ display: 'grid', gap: 10 }}>
        {hasFacts && detail.fact_status === 'unconfirmed' && <label><input type="checkbox" checked={checked === identity}
          disabled={disabled || displayed !== identity || !detail.source_current || !detail.parent_confirmation_ready || !!detail.observations.uncertainties.length}
          onChange={e => setChecked(e.target.checked ? identity : '')} />上述标记与关系和原图一致</label>}
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {hasFacts && detail.fact_status === 'unconfirmed' && <button type="button" disabled={disabled || !canConfirm} onClick={() => decide('confirm')}><Check size={16} />确认图形内容</button>}
          {detail.fact_status === 'confirmed' && <button type="button" disabled={disabled || !canRetract} onClick={() => decide('retract')}><Undo2 size={16} />撤回确认</button>}
          {detail.fact_status !== 'confirmed' && canRetract && <button type="button" disabled={disabled} onClick={() => decide('retract')}><Undo2 size={16} />撤回本机使用</button>}
        </div>
      </div>}
    </div></div>
  </section>
}
