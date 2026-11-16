import { useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, ShieldCheck, Undo2, X } from 'lucide-react'
import { ApiError } from './api'
import { createPaperVariantAuthorizationApi, exactPaperKeys, paperLicenseLabels, paperVariantAuthorizationClearedEvent,
  paperVariantAuthorizationKey, validatePaperCommand } from './paperVariantAuthorizationApi'
import type { PaperLicense, PaperParentRequest, PaperVariantAuthorization, PaperVariantCommand, PaperVariantReceipt } from './paperVariantAuthorizationApi'

export type PaperVariantAuthorizationPanelProps = {
  childId: string; mistakeId: string; request: PaperParentRequest
  onRequireParentAuth?: () => void; onChanged?: () => void | Promise<void>
}
type Draft = { license: PaperLicense | ''; reference: string; evidence: string; declared: boolean; binding: string; revision: number }
type Journal = { schema: 1; child: string; mistake: string; draft: Draft; pending: PaperVariantCommand | null }
const empty = (child: string, mistake: string): Journal => ({ schema: 1, child, mistake,
  draft: { license: '', reference: '', evidence: '', declared: false, binding: '', revision: 0 }, pending: null })
const binding = (v: PaperVariantAuthorization) => JSON.stringify([v.question_version, v.source_version])
function load(child: string, mistake: string): Journal {
  const raw = localStorage.getItem(paperVariantAuthorizationKey(child, mistake))
  if (!raw) return empty(child, mistake)
  const j = JSON.parse(raw) as Journal, d = j?.draft
  if (!j || !exactPaperKeys(j, ['schema', 'child', 'mistake', 'draft', 'pending']) || j.schema !== 1 || j.child !== child || j.mistake !== mistake
    || !d || !exactPaperKeys(d, ['license', 'reference', 'evidence', 'declared', 'binding', 'revision'])
    || (d.license !== '' && !Object.hasOwn(paperLicenseLabels, d.license)) || typeof d.reference !== 'string' || d.reference.length > 1024
    || typeof d.evidence !== 'string' || d.evidence.length > 2000 || typeof d.declared !== 'boolean' || typeof d.binding !== 'string'
    || !Number.isInteger(d.revision) || d.revision < 0) throw new Error('invalid journal')
  if (j.pending !== null) {
    validatePaperCommand(j.pending)
    if (j.pending.child_id !== child || j.pending.mistake_id !== mistake) throw new Error('invalid scope')
  }
  return j
}
function referenceValid(license: Draft['license'], value: string) {
  if (!value.trim()) return false
  try { const u = new URL(value); return u.protocol === 'https:' && !!u.hostname && !u.username && !u.password && !u.hash }
  catch { return license !== 'CC0-1.0' && !/^[\/\\]|^[a-z][a-z\d+.-]*:/i.test(value) }
}
const style = `
.paper-variant-authorization{border-top:1px solid #cbd5d1;margin-top:16px;padding-top:12px;min-width:0;overflow-wrap:anywhere}
.paper-variant-authorization h4{font-size:15px;margin:0}.paper-variant-authorization header,.paper-variant-authorization .pva-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.paper-variant-authorization header{justify-content:space-between}.paper-variant-authorization .pva-question{white-space:pre-wrap;border-left:3px solid #267d83;padding-left:10px}
.paper-variant-authorization fieldset{border:0;margin:0;padding:0;min-width:0}.paper-variant-authorization label{display:flex;flex-direction:column;gap:5px;margin:10px 0}
.paper-variant-authorization select,.paper-variant-authorization textarea,.paper-variant-authorization input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:8px}
.paper-variant-authorization .pva-declaration{flex-direction:row;align-items:flex-start;gap:8px}.paper-variant-authorization input[type=checkbox]{width:18px;height:18px;flex:0 0 18px;margin-top:3px}
.paper-variant-authorization button{display:inline-flex;align-items:center;gap:6px;min-height:38px;padding:7px 10px;white-space:normal}.paper-variant-authorization button svg{width:16px;height:16px;flex-shrink:0}
`
export function PaperVariantAuthorizationPanel(props: PaperVariantAuthorizationPanelProps) {
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    const clear = (event: Event) => { const child = (event as CustomEvent<string | undefined>).detail
      if (child === undefined || child === props.childId) setEpoch(n => n + 1) }
    window.addEventListener(paperVariantAuthorizationClearedEvent, clear)
    return () => window.removeEventListener(paperVariantAuthorizationClearedEvent, clear)
  }, [props.childId])
  return <Authorization key={`${props.childId}:${props.mistakeId}:${epoch}`} {...props} />
}
function Authorization({ childId: child, mistakeId: mistake, request, onRequireParentAuth, onChanged }: PaperVariantAuthorizationPanelProps) {
  const api = useMemo(() => createPaperVariantAuthorizationApi(request), [request])
  const [initial] = useState(() => { try { return { journal: load(child, mistake), failed: false } }
    catch { return { journal: empty(child, mistake), failed: true } } })
  const [journal, setJournal] = useState(initial.journal), journalRef = useRef(journal)
  const [storageFailed, setStorageFailed] = useState(initial.failed)
  const [view, setView] = useState<PaperVariantAuthorization | null>(null)
  const [job, setJob] = useState<PaperVariantReceipt['generation_job']>(null)
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [authNeeded, setAuthNeeded] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState('')
  const alive = useRef(false), sequence = useRef(0), writing = useRef(false)
  const current = (ticket: number) => alive.current && sequence.current === ticket
  function persist(next: Journal) {
    try { localStorage.setItem(paperVariantAuthorizationKey(child, mistake), JSON.stringify(next)) }
    catch { setStorageFailed(true); setError('本机无法保存恢复记录，未发送新的授权操作。'); return false }
    journalRef.current = next; setJournal(next); return true
  }
  function discard() {
    try { localStorage.removeItem(paperVariantAuthorizationKey(child, mistake)) }
    catch { setStorageFailed(true); setError('本机记录未能清除，操作仍锁定。'); return false }
    const next = empty(child, mistake); journalRef.current = next; setJournal(next); setStorageFailed(false); return true
  }
  function report(problem: unknown) {
    const status = problem instanceof ApiError ? problem.status : undefined
    setAuthNeeded(status === 401 || status === 403)
    if (status === 404) { setView(null); setJob(null); discard() }
    setError(status === 404 ? '原题不存在或不属于当前孩子，不能沿用旧预览。'
      : status === 401 || status === 403 ? '家长身份已失效，请重新解锁后核对当前授权。'
        : status === 409 ? '来源或版本已变化，请核对当前原题；没有自动重做决定。'
          : status === 422 ? '提交未被接受，请放弃旧请求后重新核对权利依据。'
            : '未能核对操作结果。刷新不会自动提交，可以显式重试已保存的原请求。')
  }
  async function refresh() {
    const ticket = ++sequence.current; setLoading(true); setView(null); setJob(null); setAuthNeeded(false)
    try {
      const next = await api.read(child, mistake)
      if (!current(ticket)) return
      const j = journalRef.current
      const sourceChanged = j.draft.binding && j.draft.binding !== binding(next)
      if ((j.pending || j.draft.binding) && (sourceChanged || next.status === 'blocked' || next.status === 'invalidated'
        || next.reason?.includes('RESTORE_REAUTHORIZATION_REQUIRED') || (!j.pending && j.draft.revision !== next.revision))) discard()
      setView(next)
    } catch (problem) { if (current(ticket)) report(problem) }
    finally { if (current(ticket)) setLoading(false) }
  }
  useEffect(() => {
    alive.current = true
    const clear = (event: Event) => { const scope = (event as CustomEvent<string | undefined>).detail
      if (scope === undefined || scope === child) { alive.current = false; sequence.current++ } }
    window.addEventListener(paperVariantAuthorizationClearedEvent, clear)
    void refresh()
    return () => { alive.current = false; sequence.current++; window.removeEventListener(paperVariantAuthorizationClearedEvent, clear) }
    // The keyed scope prevents late responses from publishing into another child or cleared journal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api])
  function edit(patch: Partial<Draft>) {
    if (!view || busy || journalRef.current.pending || storageFailed) return
    persist({ ...journalRef.current, draft: { ...journalRef.current.draft, ...patch, binding: binding(view), revision: view.revision } })
  }
  async function execute(command: PaperVariantCommand) {
    if (!alive.current || writing.current || storageFailed || authNeeded || command.child_id !== child || command.mistake_id !== mistake) return
    if (!persist({ ...journalRef.current, pending: command })) return
    writing.current = true; setBusy(true); setError(''); setNotice('')
    const ticket = ++sequence.current
    try {
      const result = await api.perform(command)
      if (!current(ticket)) return
      setView(result.current); setJob(result.generation_job)
      const cleared = discard()
      setNotice(cleared ? '原操作回执已核对；以下显示服务端当前状态。' : '服务端操作已保存，本机恢复记录未能清理；不要新建重复操作。')
      try { await onChanged?.() }
      catch { if (current(ticket)) setNotice('授权操作已保存，但准备列表未刷新；请刷新列表，不要重复授权。') }
    } catch (problem) {
      if (current(ticket)) {
        setView(null); setJob(null); report(problem)
        if (problem instanceof ApiError && problem.status === 409) await refresh()
      }
    } finally { writing.current = false; if (alive.current) setBusy(false) }
  }
  const d = journal.draft, locked = loading || busy || storageFailed || authNeeded || !!journal.pending
  const needsDecision = view && (['authorization_required', 'revoked'].includes(view.status)
    || (view.status === 'invalidated' && view.reason === 'PAPER_VARIANT_AUTHORIZATION_VERSION_CHANGED'))
  const canGrant = !!view?.question_preview && !!view.question_version && !!view.source_version && !!needsDecision
    && view.license_options.includes(d.license as PaperLicense) && d.declared && d.evidence.trim().length >= 20
    && referenceValid(d.license, d.reference.trim()) && !locked
  function grant() {
    if (!canGrant || !view) return
    void execute({ child_id: child, mistake_id: mistake, operation: 'grant', body: { expected_revision: view.revision,
      client_action_id: crypto.randomUUID(), question_version: view.question_version!, source_version: view.source_version!,
      license_basis: { license_id: d.license as PaperLicense, reference: d.reference.trim(), evidence: d.evidence.trim() } } })
  }
  return <section className="paper-variant-authorization" aria-label="纸面原题变式用途授权" aria-busy={loading || busy}>
    <style>{style}</style>
    <header><h4>纸面原题变式用途授权</h4><button title="刷新变式用途授权" aria-label="刷新变式用途授权" disabled={busy || loading} onClick={() => { setError(''); setNotice(''); void refresh() }}><RefreshCw /></button></header>
    <p>仅授权这道已确认原题的题文用于云端准备变式，不发送整页、图片或孩子原答案。授权不代表题目已审核通过，也不证明孩子掌握。</p>
    <p>权利依据由家长声明，系统不自动核验法律效力或网络来源。</p>
    {loading && <p role="status">正在核对原题与当前授权…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {authNeeded && onRequireParentAuth && <button onClick={onRequireParentAuth}><ShieldCheck />重新解锁家长操作</button>}
    {view && <>
      <p role="status"><strong>{view.status === 'authorized' ? '当前已授权题文变式用途' : view.status === 'revoked' ? '当前变式用途已撤回'
        : view.status === 'authorization_required' ? '尚未授权本题变式用途' : '当前题文用途不可用'}</strong></p>
      <p>{view.message}</p>
      {view.question_preview && <div><h4>核对的原题</h4><p className="pva-question">{view.question_preview}</p></div>}
      {view.status === 'revoked' && <p>历史回执不会恢复已撤回的授权或旧任务；再次授权需要新的明确决定。</p>}
      {job && <p>此回执关联的准备任务：{({ pending: '等待准备', processing: '正在准备', completed: '准备结束，候选仍需审核', blocked: '已停止或等待处理', failed: '准备失败，尚无合格结果' })[job.status]}。</p>}
    </>}
    {storageFailed && <p role="alert">本机恢复记录损坏或不可写；清除并重新核对前不会发送操作。</p>}
    {(journal.pending || storageFailed) && <div><p>原操作尚待核对，刷新或重启不会自动提交。</p><div className="pva-actions">
      {journal.pending && <button disabled={busy || loading || storageFailed || authNeeded} onClick={() => void execute(journal.pending!)}><RefreshCw />重试原授权请求</button>}
      <button disabled={busy} onClick={() => { if (discard()) { setError(''); void refresh() } }}><X />放弃本机授权记录并重新核对</button>
    </div></div>}
    {view?.question_preview && needsDecision && <fieldset disabled={locked}>
      <legend>家长权利声明</legend>
      <label>权利依据<select value={d.license} onChange={e => edit({ license: e.target.value as Draft['license'], declared: false })}>
        <option value="">请选择依据</option>{view.license_options.map(option => <option key={option} value={option}>{paperLicenseLabels[option]}</option>)}
      </select></label>
      <label>{d.license === 'CC0-1.0' ? 'CC0 声明的 HTTPS 链接' : '许可链接或声明名称'}<input type="text" maxLength={1024} value={d.reference} onChange={e => edit({ reference: e.target.value, declared: false })} /></label>
      <label>权利依据说明（至少20字）<textarea rows={3} maxLength={2000} value={d.evidence} onChange={e => edit({ evidence: e.target.value, declared: false })} /></label>
      <label className="pva-declaration"><input type="checkbox" checked={d.declared} onChange={e => edit({ declared: e.target.checked })} />我确认权利声明真实，仅同意以上题文变式用途。</label>
      <button disabled={!canGrant} onClick={grant}><ShieldCheck />确认授权并准备变式</button>
    </fieldset>}
    {view && view.revision > 0 && view.status !== 'revoked' && <div className="pva-actions"><button disabled={locked} onClick={() => void execute({
      child_id: child, mistake_id: mistake, operation: 'revoke', body: { expected_revision: view.revision, client_action_id: crypto.randomUUID() }
    })}><Undo2 />撤回变式用途授权</button></div>}
  </section>
}
