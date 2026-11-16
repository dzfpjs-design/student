import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, FilePlus, FolderOpen, RefreshCw, Save, ShieldCheck, Undo2, Upload, X } from 'lucide-react'
import { ApiError } from './api'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'
import { clearWritingAuthorizationLocalState, createCurriculumSupplyApi, curriculumLocalKey, curriculumReason, curriculumStateClearedEvent, loadCurriculumLocalState } from './curriculumSupplyApi'
import { WritingQuestionAuthorizationPanel } from './WritingQuestionAuthorizationPanel'
import { PaperLearningSelectionPanel } from './PaperLearningSelectionPanel'
import { OralSourceAuthorPanel } from './OralSourceAuthorPanel'
import { parseOralSource } from './OralTaskPreview'
import { CurriculumFirstSourcePreview as CurriculumOriginal, objectiveOriginal, openTextOriginal, parentMaterialsReady } from './CurriculumFirstSourcePreview'
import { visualOriginal } from './CurriculumVisualSourcePreview'
import type { CurriculumAudit, CurriculumChild, CurriculumDraftState, CurriculumOptions, CurriculumPackage, CurriculumParentRequest, CurriculumPending, CurriculumScope, CurriculumSourceOptions, CurriculumStageReview } from './curriculumSupplyTypes'

export type CurriculumSupplyReviewPanelProps = ParentWorkFocusProps & {
  child: CurriculumChild
  request: CurriculumParentRequest
  onOpenMaterials?: () => void
  onRequireParentAuth?: () => void
  isCurrentAuthority?: () => boolean
}

const emptyDraft: CurriculumDraftState = { sourceId: '', semester: '', standardKey: '', excerptId: '', seedId: '', explanation: '', note: '', focusedId: '', pending: null, difficulty: '' }
type ReviewDraft = CurriculumDraftState & { notesByCandidate?: Record<string, string>; stagesByCandidate?: Record<string, CurriculumStageReview> }
const stageNames = { near_transfer: '近迁移', far_transfer: '远迁移' }
const stageFields = { source_quote: '原题依据摘录', candidate_quote: '新题依据摘录', same_skill_rationale: '相同技能依据', changed_context_rationale: '情境变化依据' }
const states = { draft: '待校验', blocked: '材料待补齐', validated: '待审核', reviewed: '待发布', published: '已发布', withdrawn: '已撤回', rejected: '已退回' }
const eventNames: Record<string, string> = { draft: '建立草案', validate: '独立校验', review: '家长审核', publish: '发布题目', withdraw: '撤回题目' }
function excerptValidator(text?: string): string | null {
  if (!text) return null
  try {
    const source: unknown = JSON.parse(text)
    if (!source || typeof source !== 'object' || Array.isArray(source)) return null
    const schema = (source as Record<string, unknown>).schema
    if (typeof schema !== 'string') return null
    const validators: Record<string, string> = {
      curriculum_objective_source_v1: 'curriculum_objective_exact_source_v1',
      reviewed_open_response_source_v1: 'reviewed_open_response_rubric_source_v1',
      reviewed_open_response_source_v2: 'reviewed_open_response_rubric_source_v1',
      reviewed_open_response_source_v3: 'reviewed_open_response_rubric_source_v1',
      pet_speaking_source_v1: 'pet_speaking_rubric_source_v1',
      pet_writing_source_v1: 'pet_writing_rubric_source_v1',
      pet_listening_source_v1: 'pet_listening_exact_source_v1',
      school_listening_source_v1: 'pet_listening_exact_source_v1',
    }
    return validators[schema] ?? null
  } catch { return null }
}
const style = `
.curriculum-supply-review{color:#26352f;background:#fff;min-width:0;padding:16px;border-top:1px solid #cbd3cf;font-size:14px;line-height:1.5;overflow-wrap:anywhere}
.curriculum-supply-review *{box-sizing:border-box;letter-spacing:0;min-width:0}
.curriculum-supply-review h2{font-size:18px;margin:0}.curriculum-supply-review h3{font-size:16px;margin:0 0 12px}.curriculum-supply-review h4{font-size:14px;margin:12px 0 4px}
.curriculum-supply-review header,.csr-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;justify-content:space-between}.csr-actions{justify-content:flex-start;margin-top:12px}
.curriculum-supply-review button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:38px;white-space:normal;border:1px solid #abbcb2;border-radius:5px;background:#f6f8f7;padding:7px 10px;color:#26352f;font:inherit;cursor:pointer}
.curriculum-supply-review button svg{width:16px;height:16px;flex-shrink:0}.curriculum-supply-review button:disabled{opacity:.48;cursor:not-allowed}.curriculum-supply-review button:focus-visible,.curriculum-supply-review select:focus-visible,.curriculum-supply-review textarea:focus-visible{outline:3px solid #267d83;outline-offset:2px}
.curriculum-supply-review .csr-primary{background:#236745;color:white;border-color:#236745}.curriculum-supply-review .csr-danger{color:#9a3636;border-color:#dcb6b6}
.csr-band{border-top:1px solid #dde4e0;padding:18px 0;margin-top:16px}.csr-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:20px}
.csr-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr));gap:10px}.curriculum-supply-review label{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
.curriculum-supply-review select,.curriculum-supply-review textarea,.curriculum-supply-review input{font:inherit;color:inherit;width:100%;background:#fff;border:1px solid #aebdb5;border-radius:4px;padding:8px}.curriculum-supply-review select{height:40px}.curriculum-supply-review textarea{min-height:80px;resize:vertical}
.csr-notice{background:#f9f3e8;border-left:3px solid #b88534;padding:10px;margin:12px 0}.csr-error{background:#fff2f2;border-left-color:#b44343}.csr-muted{color:#617068}.csr-question{font-size:16px;white-space:pre-wrap}.csr-evidence{white-space:pre-wrap;margin:6px 0;border-left:3px solid #a8c5c0;padding-left:10px}
.csr-queue{display:flex;flex-direction:column;gap:6px;max-height:280px;overflow:auto}.curriculum-supply-review .csr-queue button{justify-content:space-between;text-align:left}.csr-queue button[aria-pressed=true]{border-color:#236745;background:#edf5ef}.csr-queue span{flex-shrink:0;font-size:12px}.csr-queue .csr-prompt{flex:1;font-size:14px;overflow-wrap:anywhere}
.csr-history{padding-left:20px}.csr-history li{padding:5px 0}.csr-stats{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0}.csr-badge{font-size:12px;background:#edf2ef;padding:3px 7px;border-radius:4px}
`

export function CurriculumSupplyReviewPanel(props: CurriculumSupplyReviewPanelProps) {
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    const clear = (event: Event) => {
      if ((event as CustomEvent<string | null>).detail == null || (event as CustomEvent<string>).detail === props.child.child_id) setGeneration(value => value + 1)
    }
    window.addEventListener(curriculumStateClearedEvent, clear)
    return () => window.removeEventListener(curriculumStateClearedEvent, clear)
  }, [props.child.child_id])
  return <ReviewPanel key={`${props.child.child_id}:${generation}`} {...props} />
}

function ReviewPanel({ child, request, onOpenMaterials, onRequireParentAuth, focusRequest, onFocusResolved, isCurrentAuthority }: CurriculumSupplyReviewPanelProps) {
  const api = useMemo(() => createCurriculumSupplyApi(request), [request])
  const [recovery] = useState(() => {
    try {
      const draft: ReviewDraft = { ...emptyDraft, ...loadCurriculumLocalState(child.child_id) }
      if (draft.notesByCandidate && (typeof draft.notesByCandidate !== 'object' || Array.isArray(draft.notesByCandidate)
        || Object.values(draft.notesByCandidate).some(note => typeof note !== 'string'))) throw new Error('invalid review notes')
      if (draft.stagesByCandidate && (typeof draft.stagesByCandidate !== 'object' || Array.isArray(draft.stagesByCandidate)
        || Object.values(draft.stagesByCandidate).some(value => !value || value.schema !== 'specialized_stage_review_v1'
          || !Object.hasOwn(stageNames, value.stage) || typeof value.binding_version !== 'string'
          || Object.keys(stageFields).some(key => typeof value[key as keyof typeof stageFields] !== 'string')))) throw new Error('invalid stage review')
      return { draft, failed: false }
    }
    catch { return { draft: { ...emptyDraft }, failed: true } }
  })
  const [draft, setDraft] = useState<ReviewDraft>(recovery.draft)
  const draftRef = useRef(draft)
  const [storageFailed, setStorageFailed] = useState(recovery.failed)
  const [options, setOptions] = useState<CurriculumOptions | null>(null)
  const [sources, setSources] = useState<CurriculumSourceOptions | null>(null)
  const [audit, setAudit] = useState<CurriculumAudit | null>(null)
  const [item, setItem] = useState<CurriculumPackage | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [expired, setExpired] = useState(false)
  const active = useRef(true)
  const isCurrent=():boolean=>active.current&&(isCurrentAuthority?.()??true)
  const writing = useRef(false)
  const refreshSequence = useRef(0)
  const detailSequence = useRef(0)
  const rootRef = useRef<HTMLElement>(null)
  const [focusReady, setFocusReady] = useState('')
  const [focusFailure, setFocusFailure] = useState<{ nonce: string; reason: string } | null>(null)
  const focusAttempt = useRef('')
  const failedNonce = useRef('')
  useParentWorkFocus(focusRequest, !!focusRequest && focusReady === focusRequest.nonce && !loading, rootRef, onFocusResolved)
  useEffect(() => {
    if (!focusFailure || focusFailure.nonce !== focusRequest?.nonce || failedNonce.current === focusFailure.nonce) return
    failedNonce.current = focusFailure.nonce
    onFocusResolved?.(focusFailure.nonce, focusFailure.reason)
  }, [focusFailure, focusRequest?.nonce, onFocusResolved])

  function persist(patch: Partial<ReviewDraft>): boolean {
    if(isCurrentAuthority&&!isCurrentAuthority())return false
    const current = draftRef.current
    const notes = { ...current.notesByCandidate, ...(current.focusedId ? { [current.focusedId]: current.note } : {}), ...patch.notesByCandidate }
    const focusedId = patch.focusedId ?? current.focusedId
    const note = patch.note ?? (focusedId !== current.focusedId ? (Object.hasOwn(notes, focusedId) ? notes[focusedId] : '') : current.note)
    const next = { ...current, ...patch, note, notesByCandidate: { ...notes, ...(focusedId ? { [focusedId]: note } : {}) } }
    try { localStorage.setItem(curriculumLocalKey(child.child_id), JSON.stringify(next)) }
    catch { setStorageFailed(true); setError('本机无法保存待提交操作，尚未发送新操作。请恢复浏览器存储后重试。'); return false }
    draftRef.current = next
    setDraft(next)
    return true
  }
  function report(problem: unknown) {
    if (!isCurrent()) return
    const failure = problem instanceof ApiError ? problem : null
    setExpired(failure?.status === 401 || failure?.status === 403)
    setError(failure?.status === 409 ? '版本已变化，请核对刷新后的证据。旧请求不会自动改成新的审核决定。'
      : failure?.status === 401 || failure?.status === 403 ? '家长授权已失效。已保留本机待提交操作，请重新解锁。'
      : failure?.status === 422 ? '操作未被接受，请核对材料和备注。' : '请求未能完成核对。请刷新查看已保存结果，或重试原请求。')
  }
  async function refresh(exactId?: string, nonce?: string) {
    const sequence = ++refreshSequence.current
    const detailVersion = ++detailSequence.current
    setLoading(true)
    if (nonce) { setItem(null); setFocusReady(''); setFocusFailure(null); setError('') }
    try {
      const nextOptions = await api.options(child.child_id)
      const nextAudit = await api.audit(child.child_id)
      if (!isCurrent() || sequence !== refreshSequence.current) return
      setOptions(nextOptions); setAudit(nextAudit)
      const current = draftRef.current
      const selection = nextAudit.current_curriculum[0]
      if (exactId === `selection:${child.child_id}`) {
        if (nonce) setFocusReady(nonce)
        return
      }
      const focus = exactId ?? (current.focusedId || nextAudit.packages[0]?.candidate_id || '')
      const fresh = focus ? await api.get(child.child_id, focus) : null
      if (!isCurrent() || sequence !== refreshSequence.current || detailVersion !== detailSequence.current) return
      setItem(fresh)
      if (!storageFailed) persist({ ...(!current.sourceId && selection ? { sourceId: selection.scope.textbook_source_id, semester: selection.scope.semester } : {}), focusedId: focus })
      if (nonce) {
        if (!fresh || fresh.state === 'withdrawn') setFocusFailure({ nonce, reason: '指定草案已撤回或不存在；未替换为其他题目，请核对原记录。' })
        else setFocusReady(nonce)
      }
    } catch (problem) {
      if (isCurrent() && sequence === refreshSequence.current) {
        report(problem)
        if (nonce) setFocusFailure({ nonce, reason: '无法核对指定课程草案：可能无权限、记录缺失或归属不符。未加载其他题目。' })
      }
    }
    finally { if (isCurrent() && sequence === refreshSequence.current) setLoading(false) }
  }
  useEffect(() => {
    active.current = true
    const privacyClear = (event: Event) => {
      if ((event as CustomEvent<string | null>).detail == null || (event as CustomEvent<string>).detail === child.child_id) {
        active.current = false
        refreshSequence.current++; detailSequence.current++
      }
    }
    window.addEventListener(curriculumStateClearedEvent, privacyClear)
    if (!focusRequest) void refresh()
    return () => {
      window.removeEventListener(curriculumStateClearedEvent, privacyClear)
      active.current = false; refreshSequence.current++; detailSequence.current++; focusAttempt.current = ''
    }
    // The keyed wrapper separates children; a changed host adapter reloads authorization.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api])
  useEffect(() => {
    if (!focusRequest || busy || focusAttempt.current === focusRequest.nonce) return
    focusAttempt.current = focusRequest.nonce
    if (!focusRequest.objectId.trim()) {
      ++refreshSequence.current; ++detailSequence.current; setItem(null); setLoading(false)
      setFocusFailure({ nonce: focusRequest.nonce, reason: '课程对象未绑定，未选择其他草案。' })
      return
    }
    void refresh(focusRequest.objectId, focusRequest.nonce)
    // Selection preserves generation fields, pending requests and each original note.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest?.nonce, focusRequest?.objectId, api, busy])
  useEffect(() => {
    let cancelled = false
    setSources(null)
    if (draft.sourceId) void api.sourceOptions(child.child_id, draft.sourceId).then(value => {
      if (!cancelled) setSources(value)
    }).catch(problem => { if (!cancelled) report(problem) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, draft.sourceId, options])

  async function focus(candidateId: string) {
    const sequence = ++detailSequence.current
    setItem(null)
    try {
      const value = await api.get(child.child_id, candidateId)
      if (isCurrent() && sequence === detailSequence.current && persist({ focusedId: candidateId })) setItem(value)
    } catch (problem) { if (sequence === detailSequence.current) report(problem) }
  }
  async function submit(command: CurriculumPending) {
    if (writing.current || storageFailed || !isCurrent()) return
    if (!persist({ pending: command })) return
    writing.current = true; setBusy(true); setError(''); setExpired(false)
    ++detailSequence.current
    try {
      const result = await api.perform(child.child_id, command)
      if (!isCurrent()) return
      if ('candidate_id' in result) {
        if (command.kind === 'withdraw' && item?.diagnostic_item_id) clearWritingAuthorizationLocalState(child.child_id, item.diagnostic_item_id)
        if (command.kind === 'generate' && command.body.supersedes && item?.diagnostic_item_id) clearWritingAuthorizationLocalState(child.child_id, item.diagnostic_item_id)
        setItem(result)
        persist({ pending: null, focusedId: result.candidate_id, ...(command.kind === 'generate' ? { note: '' } : {}) })
      } else persist({ pending: null })
      await refresh()
    } catch (problem) {
      report(problem)
      if (isCurrent() && problem instanceof ApiError && problem.status === 409) await refresh()
    } finally { writing.current = false; if (isCurrent()) setBusy(false) }
  }
  const source = options?.sources.find(s => s.source_id === draft.sourceId)
  const standard = sources?.standards.find(s => `${s.requirement_id}:${s.page_number}` === draft.standardKey)
  const excerpt = sources?.excerpts.find(s => s.excerpt_id === draft.excerptId)
  const selectedValidator = excerptValidator(excerpt?.text)
  const seedOptions = sources?.seeds.filter(s => !selectedValidator || s.validator === selectedValidator)
  const seed = seedOptions?.find(s => s.seed_item_id === draft.seedId)
  const firstSource = sources?.first_sources?.find(s => s.excerpt_id === draft.excerptId)
  const semester = draft.semester || options?.semesters[0] || ''
  const scope: CurriculumScope | null = source?.available && source.edition && standard?.available && semester
    ? { grade: options!.grade, subject: source.subject, edition: source.edition, semester, standard_scope: standard.stage_scope, textbook_source_id: source.source_id } : null
  const selected = audit?.current_curriculum.find(s => s.scope.subject === source?.subject)
  const scopeMatches = !!scope && !!selected && Object.entries(scope).every(([key, value]) => selected.scope[key as keyof CurriculumScope] === value)
  const locked = loading || busy || !!draft.pending || storageFailed
  const difficulty = Number(draft.difficulty)
  const canGenerate = !locked && scopeMatches && !!seed?.stages.length && !!excerpt && parentMaterialsReady(excerpt.text, excerpt.current_materials) && !!draft.explanation.trim() && Number.isInteger(difficulty) && difficulty >= 1 && difficulty <= 5
  const canEstablishFirst = !locked && scopeMatches && !!firstSource && !!excerpt && !!draft.explanation.trim()
    && Number.isInteger(difficulty) && difficulty >= 1 && difficulty <= 5
  const commandBase = () => ({ action_id: crypto.randomUUID(), expected_version: item!.version })
  const rawContext = item?.stage_review_context
  const stageContext = rawContext?.child_id === child.child_id && rawContext.candidate_id === item?.candidate_id
    && rawContext.candidate_prompt === item.payload.prompt && rawContext.schema === 'specialized_stage_review_v1'
    && rawContext.local_only === true && rawContext.cloud_transmission_allowed === false ? rawContext : null
  const stageReview = item ? draft.stagesByCandidate?.[item.candidate_id] : undefined
  const stageCurrent = !!stageContext && stageReview?.binding_version === stageContext.binding_version
  const stageReady = !stageReview || stageCurrent && stageContext.allowed_stages.includes(stageReview.stage)
    && stageReview.source_quote.trim().length >= 4 && stageContext.source_prompt.includes(stageReview.source_quote)
    && stageReview.candidate_quote.trim().length >= 4 && stageContext.candidate_prompt.includes(stageReview.candidate_quote)
    && stageReview.same_skill_rationale.trim().length >= 12 && stageReview.changed_context_rationale.trim().length >= 12
  function chooseStage(stage: string) {
    if (!item || locked) return
    const values = { ...draft.stagesByCandidate }
    if (!stage) delete values[item.candidate_id]
    else if (stageContext && (stage === 'near_transfer' || stage === 'far_transfer')) {
      values[item.candidate_id] = { schema: 'specialized_stage_review_v1', stage, binding_version: stageContext.binding_version,
        source_quote: '', candidate_quote: '', same_skill_rationale: '', changed_context_rationale: '' }
    }
    persist({ stagesByCandidate: values })
  }
  function editStage(key: keyof typeof stageFields, value: string) {
    if (item && stageReview && stageCurrent && !locked) persist({ stagesByCandidate: {
      ...draft.stagesByCandidate, [item.candidate_id]: { ...stageReview, [key]: value }
    } })
  }
  function mutate(kind: 'validate' | 'publish' | 'withdraw' | 'review', decision: 'approve' | 'reject' = 'approve') {
    if (!item || locked) return
    if (kind === 'review' && decision === 'approve' && !stageReady) return
    const shared = { child_id: child.child_id, candidate_id: item.candidate_id }
    const body = commandBase()
    void submit(kind === 'review' ? { ...shared, kind, body: { ...body, decision, note: draft.note.trim(),
      ...(decision === 'approve' && stageReview ? { stage_review: stageReview } : {}) } }
      : kind === 'withdraw' ? { ...shared, kind, body: { ...body, note: draft.note.trim() } } : { ...shared, kind, body })
  }
  function generate(revision = false) {
    if (!canGenerate || !standard || !seed || !excerpt || !selected) return
    void submit({ child_id: child.child_id, kind: 'generate', body: {
      action_id: crypto.randomUUID(), expected_version: 0, expected_scope_version: selected.version,
      source: { seed_item_id: seed.seed_item_id, stage: seed.stages[0], excerpt_id: excerpt.excerpt_id,
        requirement_id: standard.requirement_id, standard_page: standard.page_number, standard_quote: standard.quote,
        validator: seed.validator, explanation: draft.explanation.trim(), difficulty },
      ...(revision && item ? { supersedes: item.candidate_id, supersedes_version: item.version } : {})
    } })
  }
  function establishFirst() {
    if (!canEstablishFirst || !firstSource || !standard || !selected) return
    void submit({ child_id: child.child_id, kind: 'first-sources', body: {
      action_id: crypto.randomUUID(), expected_version: 0, expected_scope_version: selected.version,
      source: { knowledge_node_id: firstSource.knowledge_node_id, excerpt_id: firstSource.excerpt_id,
        requirement_id: standard.requirement_id, standard_page: standard.page_number, standard_quote: standard.quote,
        explanation: draft.explanation.trim(), difficulty }
    } })
  }
  const packet = item?.review_packet
  const packetReadable = !!packet && parentMaterialsReady(packet.textbook.content_text, packet.current_materials)
  const checked = item?.events.filter(e => e.result.check).at(-1)?.result.check
  useEffect(() => {
    if (item?.diagnostic_item_id && (item.state === 'withdrawn' || (item.state === 'published' && !item.eligible))) {
      clearWritingAuthorizationLocalState(child.child_id, item.diagnostic_item_id)
    }
  }, [child.child_id, item?.diagnostic_item_id, item?.state, item?.eligible])
  return <section ref={rootRef} className="curriculum-supply-review" aria-label={`${child.nickname}的课程供给审核`} aria-busy={busy || loading}>
    <style>{style}</style>
    <header><div><h2>课程供给</h2><span className="csr-muted">{child.nickname} · {child.grade}</span></div>
      <button type="button" aria-label="刷新课程供给" title="刷新课程供给" disabled={busy || loading} onClick={() => { setError(''); void refresh() }}><RefreshCw /></button></header>
    {loading && <p role="status">正在读取教材与审核记录…</p>}
    {error && <div role="alert" className="csr-notice csr-error">{error}{expired && onRequireParentAuth && <div className="csr-actions"><button onClick={onRequireParentAuth}><ShieldCheck />重新解锁家长操作</button></div>}</div>}
    {storageFailed && <div role="alert" className="csr-notice">本机恢复记录无法读取或保存。提交操作暂已锁定。
      <div className="csr-actions"><button onClick={() => { if (persist({ ...emptyDraft })) { setStorageFailed(false); setError(''); void refresh() } }}><Undo2 />清除本机草稿并重新读取</button></div></div>}
    {draft.pending && <div className="csr-notice" role="status">有一笔待核对操作。离开页面后仍会保留，服务端结果以刷新后的记录为准。
      <div className="csr-actions"><button disabled={busy || storageFailed} onClick={() => void submit(draft.pending!)}><RefreshCw />重试原请求</button>
        <button disabled={busy} onClick={() => { persist({ pending: null }); setError('') }}><X />放弃旧请求</button></div></div>}
    {audit && <><div className="csr-stats"><strong>当前有效题目：{audit.eligible_curriculum_items}道</strong><span>审核记录：{audit.packages.length}份</span></div>
      {Object.keys(audit.blocked_job_counts).length > 0 && <div className="csr-notice"><strong>仍待补齐的专项供给</strong>{Object.entries(audit.blocked_job_counts).map(([code, count]) => <div key={code}>{curriculumReason(code)}：{count}项</div>)}</div>}
      <p className="csr-muted">已发布的基础计算题不代表竞赛思维、表征转换或远迁移供给已补齐。</p></>}
    <OralSourceAuthorPanel child={child} request={request} disabled={locked} onRequireParentAuth={onRequireParentAuth}
      onSaved={sourceId => { if (isCurrent() && persist({ sourceId, standardKey: '', excerptId: '', seedId: '' })) void refresh() }} />
    <div className="csr-band csr-grid">
      <div data-work-object={`selection:${child.child_id}`}><h3>当前教材与学期</h3><div className="csr-fields">
        <label>当前教材<select value={draft.sourceId} disabled={locked} onChange={e => persist({ sourceId: e.target.value, standardKey: '', excerptId: '', seedId: '' })}>
          <option value="">选择教材</option>{options?.sources.map(s => <option key={s.source_id} value={s.source_id} disabled={!s.available}>{s.title} · {s.subject} · {s.edition || '版本待补齐'}{!s.available ? '（待补材料）' : ''}</option>)}</select></label>
        <label>学期<select value={semester} disabled={locked} onChange={e => persist({ semester: e.target.value })}>
          {!semester && <option value="">选择学期</option>}{options?.semesters.map(s => <option key={s}>{s}</option>)}</select></label></div>
        <label>对照课标<select value={draft.standardKey} disabled={locked || !sources} onChange={e => persist({ standardKey: e.target.value })}>
          <option value="">选择已审核课标</option>{sources?.standards.map(s => <option key={`${s.requirement_id}:${s.page_number}`} value={`${s.requirement_id}:${s.page_number}`} disabled={!s.available}>{s.source_title} · {s.title} · {s.stage_scope} · 第{s.page_number}页{!s.available ? '（待核对）' : ''}</option>)}</select></label>
        {standard && <blockquote className="csr-evidence">{standard.quote || standard.requirement_text}</blockquote>}
        <button disabled={locked || !scope} onClick={() => scope && void submit({ child_id: child.child_id, kind: 'select', body: { action_id: crypto.randomUUID(), expected_version: selected?.version ?? 0, scope } })}><Save />保存当前教材</button>
        {selected && <p className="csr-muted">已保存：{selected.scope.subject} · {selected.scope.edition} · {selected.scope.semester} · 版本{selected.version}</p>}
        {!loading && (!options?.sources.some(s => s.available) || (sources && !sources.standards.some(s => s.available))) && <p className="csr-notice">缺少已审核教材或可核对课标，暂不能提取可认证题目。</p>}
        {options?.sources.filter(s => !s.available).map(s => <p key={s.source_id} className="csr-muted">{s.title}：{s.blockers.map(curriculumReason).join('；')}</p>)}
        {sources?.standards.filter(s => !s.available).map(s => <p key={`${s.requirement_id}:${s.page_number}`} className="csr-muted">{s.title}：{s.blockers.map(curriculumReason).join('；')}</p>)}
        {onOpenMaterials && <div className="csr-actions"><button onClick={onOpenMaterials}><FolderOpen />管理教材材料</button></div>}
      </div>
      <div><h3>从教材提取草案</h3><label>教材题目<select value={draft.excerptId} disabled={locked || !sources} onChange={e => persist({ excerptId: e.target.value, seedId: '' })}>
        <option value="">选择教材原题</option>{sources?.excerpts.map(e => <option key={e.excerpt_id} value={e.excerpt_id}>{(objectiveOriginal(e.text)?.prompt ?? openTextOriginal(e.text)?.prompt ?? visualOriginal(e.text)?.prompt ?? parseOralSource(e.text)?.prompt ?? e.text).slice(0, 100)}</option>)}</select></label>
        {excerpt && <CurriculumOriginal text={excerpt.text} childId={child.child_id} materials={excerpt.current_materials} />}
        <label>题型依据<select value={draft.seedId} disabled={locked || !sources || !excerpt} onChange={e => persist({ seedId: e.target.value })}>
          <option value="">选择可校验题型</option>{seedOptions?.map(s => <option key={`${s.seed_item_id}:${s.validator}`} value={s.seed_item_id}>{s.title} · {s.prompt.slice(0, 70)}</option>)}</select></label>
        {seed && <p className="csr-muted">已审核原型：{seed.prompt}</p>}
        {firstSource && <div className="csr-notice"><strong>建立首题 · {firstSource.title}</strong><p>这个知识点暂无可用题目。先核对这份原题、完整答案和课标，再审核发布；不会借用其他知识点，也不认定迁移已覆盖。</p></div>}
        {sources && !seedOptions?.length && !firstSource && <p className="csr-notice">该教材暂无符合资格的校验题型。若知识点尚未建立，需先审核知识点与来源；材料可保留，不能发布为有效题目。</p>}
        <label>题目难度（1至5）<input type="number" min={1} max={5} step={1} value={draft.difficulty ?? ''} disabled={locked} onChange={e => persist({ difficulty: e.target.value })} /></label>
        <label>解题说明<textarea maxLength={2000} value={draft.explanation} disabled={locked} onChange={e => persist({ explanation: e.target.value })} /></label>
        <div className="csr-actions"><button disabled={!canGenerate} onClick={() => generate()}><FilePlus />提取审核草案</button>
          {firstSource && <button disabled={!canEstablishFirst} onClick={establishFirst}><FilePlus />建立原来源首题草案</button>}
          {item && <button disabled={!canGenerate} onClick={() => generate(true)}><FilePlus />创建修订草案并撤回旧版</button>}</div>
      </div>
    </div>
    <div className="csr-band"><h3>审核队列</h3><div className="csr-queue">{audit?.packages.map(p => <button key={p.candidate_id} disabled={busy || !!draft.pending || loading} aria-pressed={draft.focusedId === p.candidate_id} onClick={() => void focus(p.candidate_id)}><span className="csr-prompt">{p.prompt}</span><span>{states[p.state]} · 版本{p.version}</span></button>)}</div>
      {audit && !audit.packages.length && <p className="csr-muted">还没有审核草案。</p>}</div>
    {item && <div className="csr-band csr-grid">
      <div data-work-object={item.candidate_id}><h3>题目与依据 <span className="csr-badge">版本{item.version}</span></h3><p className="csr-question">{item.payload.prompt}</p><p className="csr-muted">家长标注难度：{item.payload.difficulty} / 5</p>
        {item.supersedes && <div className="csr-actions"><span>这是修订题目，旧版已撤回。</span><button disabled={locked} onClick={() => void focus(item.supersedes!)}><Undo2 />查看旧版</button></div>}
        <h4>解题说明</h4><p>{item.payload.explanation}</p>
        {packet ? <><h4>教材原文</h4><p>{packet.textbook.title} · {packet.textbook.version_label}</p><CurriculumOriginal text={packet.textbook.content_text} childId={child.child_id} materials={packet.current_materials} />
          <h4>课标原文 · 第{packet.standard.page_number}页</h4><p>{packet.standard.requirement_text}</p><blockquote className="csr-evidence">{packet.standard.quote}</blockquote></>
          : <p className="csr-notice">当前缺少完整来源证据。不能认证发布，请补齐材料后建立修订草案。</p>}
        {checked && <><h4>{checked.validator !== 'arithmetic_expression_v1' ? '审核原始参考答案' : '独立复算结果'}</h4><output>{checked.computed_answer}</output><p className="csr-muted">{checked.validator !== 'arithmetic_expression_v1' ? '来自已审核原题的完整参考答案，不是自动推算或口语评价。' : '基础四则运算规则复算；不证明思维或迁移能力。'}</p></>}
        {checked?.expected_answer?.answer_format === 'reviewed_spoken_numeric_tuple_v1' && <p><strong>口述数值评分：</strong>按原题顺序核对{checked.expected_answer.tuple_size}个{checked.expected_answer.numeric_kind === 'integer' ? '整数' : '有理数'}。有明确分隔的“三、四”可以核对；“三四”、引用或混合说明会保留待核对，不自动提取数字，也不据此认定掌握。</p>}
        {stageContext && <><h4>对照原题</h4><blockquote className="csr-evidence">{stageContext.source_prompt}</blockquote></>}
        {item.state === 'published' && item.diagnostic_item_id && ['pet_writing_rubric_source_v1', 'reviewed_open_response_rubric_source_v1'].includes(item.payload.validator) && <WritingQuestionAuthorizationPanel
          key={`${item.diagnostic_item_id}:${item.version}:${item.eligible}`} child={child} diagnosticItemId={item.diagnostic_item_id}
          request={request} onRequireParentAuth={onRequireParentAuth} />}
      </div>
      <div><h3>审核决定 <span className="csr-badge">{states[item.state]}</span></h3>
        {item.state === 'published' && <p>{item.eligible ? '已计入当前有效题库。' : '当前不具备供给资格，不计入有效题库。'}</p>}
        {item.state === 'published' && item.eligible && item.payload.validator==='curriculum_objective_exact_source_v1' && <PaperLearningSelectionPanel key={child.child_id+item.candidate_id} childId={child.child_id} candidateId={item.candidate_id} candidateVersion={item.version} request={request} isCurrentAuthority={isCurrentAuthority} onRequireParentAuth={onRequireParentAuth} disabled={locked}/>}
        {item.blockers.map(code => <p className="csr-notice" key={code}>{curriculumReason(code)}</p>)}
        {item.state === 'validated' && item.payload.validator !== 'pet_speaking_rubric_source_v1' && (stageContext || stageReview) && <>
          <label>题目迁移阶段<select value={stageReview?.stage || ''} disabled={locked} onChange={e => chooseStage(e.target.value)}>
            <option value="">不追加迁移认定</option>
            {Object.entries(stageNames).map(([value, label]) => <option key={value} value={value} disabled={!stageContext?.allowed_stages.includes(value as CurriculumStageReview['stage'])}>{label}</option>)}
          </select></label>
          <p className="csr-muted">仅审核题目的迁移属性；孩子的独立作答、原音交付与延迟复测资格另行核对。</p>
          {stageReview && !stageCurrent && <p role="alert" className="csr-notice">原题依据版本已变化，旧迁移草稿不能提交。原草稿仍保留，重新选择阶段后核对。</p>}
          {stageReview && Object.entries(stageFields).map(([key, label]) => <label key={key}>{label}<textarea
            value={stageReview[key as keyof typeof stageFields]} maxLength={1200} disabled={locked || !stageCurrent}
            onChange={e => editStage(key as keyof typeof stageFields, e.target.value)} /></label>)}
        </>}
        <label>审核备注<textarea value={draft.note} maxLength={500} disabled={locked} onChange={e => persist({ note: e.target.value })} /></label>
        <div className="csr-actions">
          {['draft', 'blocked'].includes(item.state) && <button disabled={locked || !packetReadable} onClick={() => mutate('validate')}><ShieldCheck />独立校验</button>}
          {item.state === 'validated' && <button className="csr-primary" disabled={locked || !packetReadable || !draft.note.trim() || !stageReady} onClick={() => mutate('review')}><Check />审核通过</button>}
          {['draft', 'blocked', 'validated', 'reviewed'].includes(item.state) && <button disabled={locked || !draft.note.trim()} onClick={() => mutate('review', 'reject')}><X />退回</button>}
          {item.state === 'reviewed' && <button className="csr-primary" disabled={locked || !packetReadable} onClick={() => mutate('publish')}><Upload />发布到题库</button>}
          {item.state !== 'withdrawn' && <button className="csr-danger" disabled={locked || !draft.note.trim()} onClick={() => mutate('withdraw')}><Undo2 />撤回题目</button>}
        </div>
        <h4>已保存记录</h4><ol className="csr-history">{item.events.map(e => <li key={e.version}>{eventNames[e.kind] || '状态更新'} · 版本{e.version} · {e.result.decision === 'reject' ? '退回' : states[e.result.state as keyof typeof states] || '已记录'}<br /><time>{new Date(e.created_at).toLocaleString('zh-CN')}</time>{e.result.note && <div>{e.result.note === 'superseded by a new draft' ? '已由修订草案替代' : e.result.note}</div>}{e.result.stage_review && <details><summary>题目{stageNames[e.result.stage_review.stage]}审核依据</summary>{Object.entries(stageFields).map(([key, label]) => <p key={key}>{label}：{e.result.stage_review![key as keyof typeof stageFields]}</p>)}</details>}</li>)}</ol>
      </div>
    </div>}
  </section>
}
