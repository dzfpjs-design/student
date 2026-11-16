import { useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, ShieldCheck, Undo2, X } from 'lucide-react'
import { ApiError } from './api'
import { createWritingAuthorizationApi, validateWritingAuthorizationCommand, writingAuthorizationClearedEvent, writingAuthorizationLocalKey } from './curriculumSupplyApi'
import type { WritingAuthorization, WritingAuthorizationCommand, WritingLicense } from './curriculumSupplyApi'
import type { CurriculumChild, CurriculumParentRequest } from './curriculumSupplyTypes'

export type WritingQuestionAuthorizationPanelProps = {
  child: CurriculumChild; diagnosticItemId: string; request: CurriculumParentRequest
  onRequireParentAuth?: () => void
}
const labels: Record<WritingLicense, string> = {
  'CC0-1.0': 'CC0 公共领域声明', original_author: '我是原题作者',
  explicit_cloud_processing_permission: '已取得明确的云端处理许可'
}
type Declaration = { license: WritingLicense | ''; reference: string; evidence: string; declared: boolean }
type MaterialDraft = Declaration & { material_key: string }
type Draft = Declaration & { binding: string; materials?: MaterialDraft[] }
type Journal = { schema: 1 | 2; child: string; item: string; draft: Draft; pending: WritingAuthorizationCommand | null }
const blank = (): Declaration => ({ license: '', reference: '', evidence: '', declared: false })
const empty = (child: string, item: string): Journal => ({ schema: 2, child, item,
  draft: { ...blank(), binding: '', materials: [] }, pending: null })
const legacyBinding = (v: WritingAuthorization) => JSON.stringify([v.item_version, v.source_version, v.curriculum_version])
const binding = (v: WritingAuthorization) => JSON.stringify([v.purpose_version, legacyBinding(v),
  v.preview?.materials?.map(m => [m.material_key, m.source_version, m.text_hash]) ?? []])
function exact(value: object, keys: string[]) { return Object.keys(value).sort().join('|') === keys.sort().join('|') }
function validDeclaration(d: Declaration): boolean {
  return !!d && (d.license === '' || Object.hasOwn(labels, d.license)) && typeof d.reference === 'string' && d.reference.length <= 1024
    && typeof d.evidence === 'string' && d.evidence.length <= 2000 && typeof d.declared === 'boolean'
}
function load(child: string, item: string): Journal {
  const raw = localStorage.getItem(writingAuthorizationLocalKey(child, item))
  if (!raw) return empty(child, item)
  const value = JSON.parse(raw) as Journal
  const d = value?.draft
  if (![1, 2].includes(value?.schema) || value.child !== child || value.item !== item || !d
    || !exact(value, ['schema', 'child', 'item', 'draft', 'pending'])
    || !exact(d, ['license', 'reference', 'evidence', 'declared', 'binding', ...(value.schema === 2 ? ['materials'] : [])])
    || !validDeclaration(d) || typeof d.binding !== 'string'
    || (value.schema === 2 && (!Array.isArray(d.materials) || d.materials.length > 20
      || d.materials.some(m => !validDeclaration(m) || !exact(m, ['material_key', 'license', 'reference', 'evidence', 'declared'])
        || typeof m.material_key !== 'string' || !m.material_key || m.material_key.length > 200)
      || new Set(d.materials.map(m => m.material_key)).size !== d.materials.length))) throw new Error('invalid journal')
  if (value.pending !== null) {
    validateWritingAuthorizationCommand(value.pending, child, item)
    if (value.schema === 1 && value.pending.body.material_license_bases !== undefined) throw new Error('invalid legacy permission')
    if (value.schema === 2 && value.pending.kind === 'grant') {
      const b = value.pending.body
      const expected = JSON.stringify([b.material_license_bases ? 'writing_question_material_formative_text_v2' : 'writing_question_formative_text_v1',
        JSON.stringify([b.item_version, b.source_version, b.curriculum_version]),
        b.material_license_bases?.map(m => [m.material_key, m.source_version, m.text_hash]) ?? []])
      if (d.binding !== expected) throw new Error('pending does not match frozen scope')
    }
  }
  return value
}
function referenceValid(license: Declaration['license'], value: string): boolean {
  if (!value.trim()) return false
  try {
    const u = new URL(value)
    return u.protocol === 'https:' && !!u.hostname && !u.username && !u.password && !u.hash
  } catch { return license !== 'CC0-1.0' && !/^[\/\\]|^[a-z][a-z\d+.-]*:/i.test(value) }
}
function reasonText(reason: string | null): string {
  if (reason === 'WRITING_QUESTION_AUTHORIZATION_VERSION_CHANGED') return '原授权依据版本已变化。请核对当前原题，重新填写权利声明后才能批准新版本。'
  if (reason?.includes('PARENT_TEXT_CONSENT_REQUIRED')) return '尚未开启家长云端文字同意。请先在隐私设置核对；本次不会自动开启。'
  if (reason?.includes('RESTORE_REAUTHORIZATION_REQUIRED')) return '数据恢复后原授权已撤销，需要重新核对用途和权利依据。'
  if (reason?.includes('REVOKED')) return '该用途授权已撤销。历史批准回执不代表当前仍获许可。'
  return '题目、教材或许可依据当前不可用，需重新核对来源；不会沿用旧授权。'
}
const style = `
.writing-question-authorization{border-top:1px solid #ccd8d3;margin-top:20px;padding-top:16px;min-width:0;overflow-wrap:anywhere}
.writing-question-authorization h4{font-size:15px;margin:0 0 8px}.writing-question-authorization .wqa-declaration{display:flex;flex-direction:row;align-items:flex-start;gap:8px}
.writing-question-authorization .wqa-declaration input{width:18px;height:18px;flex:0 0 18px;margin-top:3px}
.writing-question-authorization .wqa-question{white-space:pre-wrap;border-left:3px solid #267d83;padding-left:10px}
.writing-question-authorization fieldset{border:0;margin:0;padding:0;min-width:0}.writing-question-authorization legend{font-weight:600;margin-bottom:8px}
.writing-question-authorization label{display:flex;flex-direction:column;gap:5px;margin:10px 0}.writing-question-authorization select,.writing-question-authorization textarea,.writing-question-authorization input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:8px}
.writing-question-authorization button{display:inline-flex;align-items:center;gap:6px;min-height:38px;padding:7px 10px;white-space:normal}
.writing-question-authorization button svg{width:16px;height:16px;flex-shrink:0}.writing-question-authorization .csr-actions{display:flex;flex-wrap:wrap;gap:8px}
.writing-question-authorization .wqa-material{border-top:1px solid #ccd8d3;padding-top:16px;margin-top:20px}
.writing-question-authorization .wqa-material legend{padding-top:16px;max-width:100%;overflow-wrap:anywhere}
`

function DeclarationFields({ value, options, material, onChange }: {
  value: Declaration; options: WritingLicense[]; material?: boolean; onChange: (patch: Partial<Declaration>) => void
}) {
  return <>
    <label>权利依据<select value={value.license} onChange={e => onChange({ license: e.target.value as WritingLicense, declared: false })}>
      <option value="">请选择依据</option>{options.map(option => <option key={option} value={option}>{material && option === 'original_author' ? '我是这份材料的作者' : labels[option]}</option>)}
    </select></label>
    <label>{value.license === 'CC0-1.0' ? 'CC0 声明的 HTTPS 链接' : '许可链接或声明名称'}<input type="text" maxLength={1024} value={value.reference} onChange={e => onChange({ reference: e.target.value, declared: false })} /></label>
    <label>权利依据说明（至少20字）<textarea rows={3} maxLength={2000} value={value.evidence} onChange={e => onChange({ evidence: e.target.value, declared: false })} /></label>
    <label className="wqa-declaration"><input type="checkbox" checked={value.declared} onChange={e => onChange({ declared: e.target.checked })} /><span>{material
      ? '我确认这份材料的声明真实，仅授权它参与本题列明的文字用途，不代表其他材料已获许可。'
      : '我确认上述声明真实，并同意仅按列明的文字用途处理这道题。'}</span></label>
  </>
}

export function WritingQuestionAuthorizationPanel(props: WritingQuestionAuthorizationPanelProps) {
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    const clear = (event: Event) => {
      const scope = (event as CustomEvent<{ child?: string; item?: string }>).detail
      if ((!scope?.child || scope.child === props.child.child_id) && (!scope?.item || scope.item === props.diagnosticItemId)) setEpoch(n => n + 1)
    }
    window.addEventListener(writingAuthorizationClearedEvent, clear)
    return () => window.removeEventListener(writingAuthorizationClearedEvent, clear)
  }, [props.child.child_id, props.diagnosticItemId])
  return <Authorization key={`${props.child.child_id}:${props.diagnosticItemId}:${epoch}`} {...props} />
}

function Authorization({ child, diagnosticItemId: item, request, onRequireParentAuth }: WritingQuestionAuthorizationPanelProps) {
  const api = useMemo(() => createWritingAuthorizationApi(request), [request])
  const [initial] = useState(() => { try { return { journal: load(child.child_id, item), failed: false } }
    catch { return { journal: empty(child.child_id, item), failed: true } } })
  const [journal, setJournal] = useState(initial.journal)
  const journalRef = useRef(journal)
  const [storageFailed, setStorageFailed] = useState(initial.failed)
  const [view, setView] = useState<WritingAuthorization | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [authNeeded, setAuthNeeded] = useState(false)
  const alive = useRef(false); const sequence = useRef(0); const writing = useRef(false)
  const current = (ticket: number) => alive.current && sequence.current === ticket

  function persist(next: Journal): boolean {
    try { localStorage.setItem(writingAuthorizationLocalKey(child.child_id, item), JSON.stringify(next)) }
    catch { setStorageFailed(true); setError('本机无法保存恢复记录，未发送新的授权操作。'); return false }
    journalRef.current = next; setJournal(next); return true
  }
  function discard(): boolean {
    try { localStorage.removeItem(writingAuthorizationLocalKey(child.child_id, item)) }
    catch { setStorageFailed(true); setError('本机记录未能清除，操作仍锁定。'); return false }
    const next = empty(child.child_id, item)
    journalRef.current = next; setJournal(next); setStorageFailed(false); return true
  }
  function report(problem: unknown) {
    const status = problem instanceof ApiError ? problem.status : undefined
    const consentRequired = problem instanceof ApiError && problem.code === 'WRITING_QUESTION_PARENT_TEXT_CONSENT_REQUIRED'
    setAuthNeeded(status === 401 || status === 403)
    setError(consentRequired ? reasonText(problem.code!) : status === 401 || status === 403 ? '家长身份已失效，请重新解锁后读取当前授权。'
      : status === 404 ? '题目不存在或不属于当前孩子，无法读取用途授权。'
        : status === 409 ? '版本或来源已变化。没有自动重做决定，请核对当前记录。'
        : status === 422 ? '提交未被接受，请核对权利依据后放弃旧请求并重新确认。'
          : '未能核对结果。可以刷新当前状态，或显式重试已保存的原请求。')
  }
  async function refresh() {
    const ticket = ++sequence.current
    setLoading(true); setView(null); setAuthNeeded(false)
    try {
      const next = await api.read(child.child_id, item)
      if (!current(ticket)) return
      const stored = journalRef.current
      const changed = stored.schema === 1
        ? next.purpose_version !== 'writing_question_formative_text_v1' || (!!stored.draft.binding && stored.draft.binding !== legacyBinding(next))
        : !!stored.draft.binding && stored.draft.binding !== binding(next)
      if ((stored.pending || stored.draft.binding) && (changed || ['blocked', 'invalidated'].includes(next.status)
        || next.reason?.includes('RESTORE_REAUTHORIZATION_REQUIRED'))) discard()
      setView(next)
    } catch (problem) { if (current(ticket)) report(problem) }
    finally { if (current(ticket)) setLoading(false) }
  }
  useEffect(() => {
    alive.current = true
    const clear = (event: Event) => {
      const scope = (event as CustomEvent<{ child?: string; item?: string }>).detail
      if ((!scope?.child || scope.child === child.child_id) && (!scope?.item || scope.item === item)) {
        alive.current = false; sequence.current++
      }
    }
    window.addEventListener(writingAuthorizationClearedEvent, clear)
    void refresh()
    return () => { alive.current = false; sequence.current++; window.removeEventListener(writingAuthorizationClearedEvent, clear) }
    // The keyed child/item boundary fences old reads and old write responses.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api])

  function edit(patch: Partial<Draft>) {
    if (!view || busy || journalRef.current.pending || storageFailed) return
    persist({ ...journalRef.current, schema: 2, draft: { ...journalRef.current.draft, materials: journalRef.current.draft.materials ?? [], ...patch, binding: binding(view) } })
  }
  function editMaterial(materialKey: string, patch: Partial<Declaration>) {
    const materials = journalRef.current.draft.materials ?? []
    const value = materials.find(m => m.material_key === materialKey) ?? { ...blank(), material_key: materialKey }
    edit({ materials: [...materials.filter(m => m.material_key !== materialKey), { ...value, ...patch }] })
  }
  async function execute(command: WritingAuthorizationCommand) {
    if (!alive.current || writing.current || storageFailed || authNeeded) return
    // Exact retries preserve legacy schema and payload; new decisions use the current binding.
    const stored = journalRef.current
    if (!persist(stored.pending ? { ...stored, pending: command } : { ...stored, schema: 2,
      draft: { ...stored.draft, materials: stored.draft.materials ?? [], binding: view ? binding(view) : stored.draft.binding }, pending: command })) return
    writing.current = true; setBusy(true); setError(''); setNotice('')
    const ticket = ++sequence.current
    try {
      const next = await api.perform(child.child_id, item, command)
      if (!current(ticket)) return
      setView(next)
      const cleared = discard()
      setNotice(cleared ? '操作回执已核对；以下显示服务端当前状态。' : '服务端操作已保存，本机恢复记录尚未清理；不要新建重复操作。')
    } catch (problem) {
      if (current(ticket)) {
        setView(null); report(problem)
        if (problem instanceof ApiError && problem.status === 409) await refresh()
      }
    } finally { writing.current = false; if (alive.current) setBusy(false) }
  }
  const d = journal.draft
  const materials = view?.preview?.materials ?? []
  const materialValue = (key: string): Declaration => d.materials?.find(m => m.material_key === key) ?? blank()
  const declarationValid = (value: Declaration) => view?.license_options.includes(value.license as WritingLicense)
    && value.declared && value.evidence.trim().length >= 20 && referenceValid(value.license, value.reference.trim())
  const locked = busy || loading || storageFailed || authNeeded || !!journal.pending
  const needsNewDecision = view && (['authorization_required', 'revoked'].includes(view.status)
    || (view.status === 'invalidated' && view.reason === 'WRITING_QUESTION_AUTHORIZATION_VERSION_CHANGED'))
  const canGrant = !!view?.preview && !!view.item_version && !!view.source_version && !!view.curriculum_version
    && needsNewDecision && declarationValid(d) && materials.every(m => declarationValid(materialValue(m.material_key))) && !locked
  function grant() {
    if (!canGrant || !view) return
    void execute({ child_id: child.child_id, diagnostic_item_id: item, kind: 'grant', body: {
      expected_revision: view.revision, client_action_id: crypto.randomUUID(), item_version: view.item_version!,
      source_version: view.source_version!, curriculum_version: view.curriculum_version!,
      license_basis: { license_id: d.license as WritingLicense, reference: d.reference.trim(), evidence: d.evidence.trim() },
      ...(view.purpose_version === 'writing_question_material_formative_text_v2' ? { material_license_bases: materials.map(m => {
        const value = materialValue(m.material_key)
        return { material_key: m.material_key, source_version: m.source_version, text_hash: m.text_hash,
          license_basis: { license_id: value.license as WritingLicense, reference: value.reference.trim(), evidence: value.evidence.trim() } }
      }) } : {})
    } })
  }
  return <section className="writing-question-authorization" aria-label={`${child.nickname}的题目文字用途授权`} aria-busy={busy || loading}>
    <style>{style}</style>
    <header><h4>题目文字用途授权</h4><button aria-label="刷新用途授权" title="刷新用途授权" disabled={busy || loading} onClick={() => { setError(''); void refresh() }}><RefreshCw /></button></header>
    <p>仅限这道题的题目文字{materials.length ? '、下方逐份确认的材料文字' : ''}、孩子本次回答与评分标准，用于云端形成性反馈。不会授权整本教材、图片、录音或其他家庭资料；发布题目不等于同意云端处理。</p>
    <p>权利依据由家长声明，系统不自动核验法律效力或网络来源。</p>
    {!!materials.length && <p>本题包含{materials.length}份材料。题干声明不包含材料许可，每份材料必须单独核对、声明；仅用于本题的文字形成性反馈，不授权整页或整本教材。</p>}
    {loading && <p role="status">正在核对当前用途许可…</p>}
    {error && <p role="alert">{error}</p>}
    {authNeeded && onRequireParentAuth && <button onClick={onRequireParentAuth}><ShieldCheck />重新解锁家长操作</button>}
    {notice && <p role="status">{notice}</p>}
    {view && <>
      <p role="status"><strong>{view.status === 'authorized' ? '当前已授权指定用途' : view.status === 'revoked' ? '当前授权已撤销' : view.status === 'authorization_required' ? '尚未授权此题用途' : '当前用途许可不可用'}</strong></p>
      {view.reason && <p className="csr-notice">{reasonText(view.reason)}</p>}
      {view.preview && <div><p className="wqa-question">{view.preview.question}</p><ul>{view.preview.task_points.map((point, i) => <li key={i}>{point}</li>)}</ul></div>}
    </>}
    {storageFailed && <p role="alert">本机恢复记录损坏或不可写。请先清除本机记录并重新核对；不会自动发送。</p>}
    {(journal.pending || storageFailed) && <div className="csr-notice"><p>原操作尚待核对，刷新或重启不会自动提交。</p><div className="csr-actions">
      {journal.pending && <button disabled={busy || loading || storageFailed || authNeeded} onClick={() => void execute(journal.pending!)}><RefreshCw />重试原授权请求</button>}
      <button disabled={busy} onClick={() => { if (discard()) { setError(''); void refresh() } }}><X />放弃本机授权记录并重新核对</button>
    </div></div>}
    {view?.preview && view.status !== 'authorized' && <fieldset disabled={locked}>
      <legend>{materials.length ? '题干权利声明' : '家长权利声明'}</legend>
      <DeclarationFields value={d} options={view.license_options} onChange={edit} />
    </fieldset>}
    {materials.map((m, index) => <fieldset key={m.material_key} className="wqa-material" disabled={locked}>
      <legend>材料{index + 1}：{m.title || '未命名材料'}</legend>
      <p className="wqa-question">{m.text}</p>
      <p>来源标记：公开。公开标记不代替本次用途许可声明。</p>
      {view!.status !== 'authorized' && <DeclarationFields material value={materialValue(m.material_key)} options={view!.license_options} onChange={patch => editMaterial(m.material_key, patch)} />}
    </fieldset>)}
    {view?.preview && view.status !== 'authorized' && <button disabled={!canGrant} onClick={grant}><ShieldCheck />确认用途授权</button>}
    {view && view.revision > 0 && view.status !== 'revoked' && <div className="csr-actions"><button disabled={locked} onClick={() => void execute({
      child_id: child.child_id, diagnostic_item_id: item, kind: 'revoke', body: { expected_revision: view.revision, client_action_id: crypto.randomUUID() }
    })}><Undo2 />撤销此题用途授权</button></div>}
  </section>
}
