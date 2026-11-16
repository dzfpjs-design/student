import { useEffect, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { DocumentCameraCapture, type ParentDocumentCapturePort } from './DocumentCameraCapture'
import type { OcrArtifact } from './types'
import { canonicalCandidatePayload, candidatePayloadHash, clearCandidateCommand, commandForCandidate, pendingCandidateCommand, reconcileCandidateCommand } from './textbookCandidateCommands'
import { ParentTextbookDraftJournal, type TextbookEditingPayload, type TextbookDraftComparison } from './parentTextbookDraftJournal'
import type { PaperObjectiveDraft } from './parentTextbookDraftJournal'
import { ParentPaperObjectiveDraft } from './ParentPaperObjectiveDraft'
import { guardParentDraftNavigation } from './parentDraftNavigation'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import type { ParentDraftRecoveryFactory } from './parentDraftRecoveryPort'
import { createParentDraftRecoveryPort } from './api'
import { parentDraftAuthorityVersion } from './parentDraftAuthority'

type Page = {
  page_ref: string
  title: string
  grade_scope?: string
  subject_scope?: string
  textbook_version?: string
  volume?: string
  unit?: string
  lesson?: string
  page_number: number
  material_kind?: 'textbook' | 'workbook'
}
type Candidate = {
  candidate_ref: string
  language: 'english' | 'chinese'
  term: string
  focus_kind: 'word' | 'character' | 'phrase'
  proposed_json?: string
  status: 'pending' | 'projecting' | 'confirmed' | 'deleted' | 'stale' | 'conflicted' | 'withdrawn'
}
type CaptureArtifact = Omit<OcrArtifact, 'artifact_id' | 'status'> & { capture_ref: string, source_version: string, page_context: Record<string, unknown>, raw_image_retained: boolean, raw_image_media_type?: string, raw_image_byte_length: number, status: 'processing' | 'needs_review' | 'ready' | 'importing' | 'imported' | 'skipped' | 'failed' }
export type ParentTextbookSourceReader = (childId: string, captureRef: string, signal: AbortSignal) => Promise<{ bytes: ArrayBuffer, mediaType: 'image/png' | 'image/jpeg' }>
type SourceReader = ParentTextbookSourceReader
type Props = { api: (path: string, init?: RequestInit) => Promise<unknown>, childId?: string, captureOnly?: boolean, readSource?: SourceReader, draftHost?: ParentTextbookDraftHost, recoveryFactory?:ParentDraftRecoveryFactory }

type PageDraft = {
  material_kind: 'textbook' | 'workbook'
  title: string
  ocr_text: string
  grade_scope: string
  subject_scope: string
  textbook_version: string
  volume: string
  unit: string
  lesson: string
  page_number: number
  source_ref: string
}

const blankPage: PageDraft = { material_kind: 'textbook', title: '', ocr_text: '', grade_scope: '', subject_scope: '', textbook_version: '', volume: '', unit: '', lesson: '', page_number: 1, source_ref: 'local_ocr_capture' }

function DraftComparisonContent({ heading, payload }: {heading: string; payload: TextbookEditingPayload | null}): React.JSX.Element {
  const fields: [string,string][] = [['title','书名与标题'],['material_kind','资料类型'],['grade_scope','年级'],['subject_scope','学科'],
    ['textbook_version','教材版本'],['volume','册次'],['unit','单元'],['lesson','课文或课题'],['page_number','页码']]
  return <div aria-label={heading} className="parent-draft-comparison">
    <h4>{heading}</h4>
    {!payload ? <p>没有草稿内容，可能已清理。</p> : <>
      <dl>{fields.map(([key,label]) => <div key={key}><dt>{label}</dt><dd>{key === 'material_kind'
        ? payload.page[key] === 'workbook' ? '习题册' : '教材' : String(payload.page[key] ?? '') || '未填写'}</dd></div>)}</dl>
      <textarea aria-label={`${heading}的 OCR 文字`} readOnly value={String(payload.page.ocr_text ?? '')} />
      {Object.values(payload.review_drafts).map((item,index) => <label key={index}>第{index+1}份校对文字
        <textarea aria-label={`${heading}的第${index+1}份校对文字`} readOnly value={item.text} />
      </label>)}
      {payload.objective_draft&&<section aria-label={`${heading}的单题审核草稿`}>
        <p>题号：{payload.objective_draft.question_number||'未填写'}</p>
        <p style={{whiteSpace:'pre-wrap'}}>{payload.objective_draft.raw_editing?.full_question??payload.objective_draft.full_question}</p>
        <p style={{whiteSpace:'pre-wrap'}}>{payload.objective_draft.raw_editing?.official_answers_text??payload.objective_draft.official_answers.join('\n')}</p>
        <p>仅为待审核草稿，没有批准题源或建立复测。</p>
      </section>}
      {Object.keys(payload.action_ids).length > 0 && <p>含尚待核对的提交记录，不会自动重发。</p>}
    </>}
  </div>
}

/** Parent-facing OCR vocabulary review. It deliberately never renders KG IDs. */
export function TextbookLanguageCandidateReview(props: Props): React.JSX.Element {
  return <DraftAuthorityBoundary key={JSON.stringify([props.childId ?? '', props.captureOnly ?? false])} {...props} />
}

type JournalEntry=ReturnType<ParentTextbookDraftHost['entry']>
function DraftAuthorityBoundary(props:Props):React.JSX.Element {
  const [privateHost]=useState(()=>new ParentTextbookDraftHost(props.recoveryFactory??createParentDraftRecoveryPort))
  const host=props.draftHost??privateHost
  useEffect(()=>props.draftHost?undefined:host.attach(),[host,props.draftHost])
  const [entry]=useState(()=>props.childId?host.entry(props.api,props.childId,props.captureOnly?'capture':'language'):null)
  const [,changed]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState(''),[discard,setDiscard]=useState(false)
  useEffect(()=>entry?.journal.subscribe(()=>changed(n=>n+1)),[entry])
  if(entry?.journal.authoritySuspended)return <section aria-label="本窗口草稿恢复"><h2>本窗口草稿已暂停</h2>
    <p>{entry.journal.recoveryMessage||'原文与未确认请求仍保留在本窗口内存。'}锁定后不会读取或联网保存。请在家长 PIN 解锁后，先核对当前孩子的储存版本再恢复；不会自动重发。</p>
    {error&&<p role="alert">{error}</p>}<button disabled={busy} onClick={()=>{setBusy(true);setError('');void entry.journal.reconcileAuthority().catch(()=>setError('尚未核对当前身份或储存版本，原文仍保留，没有重发。')).finally(()=>setBusy(false))}}>核对并恢复本窗口草稿</button>
    {entry.journal.recoveryAvailable&&<div><label><input type="checkbox" checked={discard} disabled={busy} onChange={event=>setDiscard(event.target.checked)}/>我明确放弃这台电脑尚未确认的恢复草稿</label><button disabled={!discard||busy} onClick={()=>{setBusy(true);setError('');void entry.journal.discardNativeRecovery(true).catch(()=>setError('未能确认放弃，原恢复草稿仍保留。')).finally(()=>setBusy(false))}}>放弃本机恢复草稿并读取已保存版本</button></div>}</section>
  return <ScopedTextbookLanguageCandidateReview {...props} journalEntry={entry}/>
}
/** Keep the just-entered value synchronously, before a lock can unmount React. */
function useDraftField<T>(initial:T):[T,Dispatch<SetStateAction<T>>,{current:T}] {
  const [value,commit]=useState(initial),ref=useRef(value)
  const set:Dispatch<SetStateAction<T>>=next=>{ref.current=typeof next==='function'?(next as (value:T)=>T)(ref.current):next;commit(ref.current)}
  return [value,set,ref]
}
function ScopedTextbookLanguageCandidateReview({ api:baseApi, childId = '', captureOnly = false, readSource, journalEntry, recoveryFactory=createParentDraftRecoveryPort }: Props&{journalEntry:JournalEntry|null}): React.JSX.Element {
  const [api]=useState(()=>journalEntry?journalEntry.journal.editingApi():baseApi)
  const [pages, setPages] = useState<Page[]>([])
  const [selected, setSelected] = useState('')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [page, setPage, pageRef] = useDraftField(blankPage)
  const [manualTerm, setManualTerm] = useState('')
  const [manualKind, setManualKind] = useState<Candidate['focus_kind']>('word')
  const [manualLanguage, setManualLanguage] = useState<Candidate['language']>('english')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [captureRef, setCaptureRef, captureRefRef] = useDraftField('')
  const [captureVersion, setCaptureVersion, captureVersionRef] = useDraftField('')
  const [reviewArtifacts, setReviewArtifacts] = useState<CaptureArtifact[]>([])
  const [reviewDrafts, setReviewDrafts, reviewDraftsRef] = useDraftField<Record<string, string>>({})
  const [reviewVersions, setReviewVersions, reviewVersionsRef] = useDraftField<Record<string, string>>({})
  const [objectiveDraft, setObjectiveDraft, objectiveDraftRef] = useDraftField<PaperObjectiveDraft|null>(null)
  const [sourcePreview, setSourcePreview] = useState<{ captureRef: string, url: string } | null>(null)
  const [sourceLoading, setSourceLoading] = useState('')
  const captureLease = useRef('')
  const sourceRequest = useRef<AbortController | null>(null)
  const candidateRequest = useRef(0)
  const pendingActionIds = useRef<Record<string, string>>({})
  const pendingFileRequest = useRef<{ id: string; payload: Record<string, unknown> } | null>(null)
  const [draftJournal] = useState(() => childId ? (journalEntry?.journal??new ParentTextbookDraftJournal(api, childId, captureOnly ? 'capture' : 'language',parentDraftAuthorityVersion,recoveryFactory(childId,captureOnly?'capture':'language'))) : null)
  const [draftReady, setDraftReady] = useState(!childId)
  const [draftStatus, setDraftStatus] = useState(draftJournal?.state)
  const [draftComparison, setDraftComparison] = useState<TextbookDraftComparison | null>(null)
  const [discardLocalConfirmed, setDiscardLocalConfirmed] = useState(false)
  const [navigationBlocked,setNavigationBlocked]=useState(false)
  const mounted = useRef(true)
  // Rendering defaults is a read, not new parent labour. Preserve the exact
  // saved/native payload until the first actual change to its displayed view.
  const hydratedEditing=useRef<{view:string;payload:TextbookEditingPayload|null}|null>(null)
  const editingPayload = (): TextbookEditingPayload | null => {
    const value:TextbookEditingPayload={page:{...pageRef.current},capture_ref:captureRefRef.current,capture_version:captureVersionRef.current,
      review_drafts:Object.fromEntries(Object.entries(reviewDraftsRef.current).map(([ref,text]) => [ref,{text,source_version:reviewVersionsRef.current[ref] || ''}])),
      action_ids:{...pendingActionIds.current},...(objectiveDraftRef.current?{objective_draft:objectiveDraftRef.current}:{})}
    const hydrated=hydratedEditing.current
    if(hydrated){
      if(hydrated.view===canonicalCandidatePayload(value))return structuredClone(hydrated.payload)
      hydratedEditing.current=null
    }
    return value
  }
  const currentEditing=useRef(editingPayload);currentEditing.current=editingPayload
  const canonicalEditingPayload=(payload:TextbookEditingPayload|null):string=>payload===null?'null':canonicalCandidatePayload(payload)
  const nativeRestorationValue=useRef<string|null>(null)
  useLayoutEffect(()=>{
    if(!journalEntry||!draftReady)return
    journalEntry.capture=()=>currentEditing.current()
    return()=>{journalEntry.capture=null}
  },[journalEntry,draftReady])
  const currentDraftReady=useRef(draftReady);currentDraftReady.current=draftReady
  useLayoutEffect(()=>guardParentDraftNavigation(
    ()=>!draftJournal||!currentDraftReady.current||!draftJournal.hasUnconfirmedEditing(currentEditing.current()),
    ()=>setNavigationBlocked(true)),[draftJournal])
  const applyEditingPayload = (payload: TextbookEditingPayload | null): void => {
    hydratedEditing.current=null
    setPage({...blankPage,...payload?.page} as PageDraft)
    setCaptureRef(payload?.capture_ref || ''); setCaptureVersion(payload?.capture_version || '')
    setReviewDrafts(Object.fromEntries(Object.entries(payload?.review_drafts || {}).map(([ref,item]) => [ref,item.text])))
    setReviewVersions(Object.fromEntries(Object.entries(payload?.review_drafts || {}).map(([ref,item]) => [ref,item.source_version])))
    pendingActionIds.current = {...payload?.action_ids}
    pendingFileRequest.current = null
    setObjectiveDraft(payload?.objective_draft??null)
    hydratedEditing.current={view:canonicalEditingPayload(currentEditing.current()),payload:structuredClone(payload)}
    nativeRestorationValue.current=canonicalEditingPayload(payload)
  }
  const restoreDraft = async (): Promise<void> => {
    if (!draftJournal) return
    try {
      const result = journalEntry&&draftJournal.state.phase!=='loading'?{payload:draftJournal.state.payload}:await draftJournal.load()
      if (!mounted.current) return
      applyEditingPayload(result.payload)
      setDraftReady(true)
    } catch { /* recovery failure leaves editing disabled, never overwrites saved work */ }
  }
  useEffect(() => {
    mounted.current=true
    const unsubscribe = draftJournal?.subscribe(() => { if (mounted.current) setDraftStatus({...draftJournal.state}) })
    void restoreDraft()
    return () => { mounted.current=false; if(journalEntry&&draftReady)draftJournal?.keepEditing(currentEditing.current()); unsubscribe?.() }
  }, [draftJournal])
  useEffect(() => {
    if (draftReady && draftJournal) {
      const payload=editingPayload()
      if(nativeRestorationValue.current!==null){
        if(nativeRestorationValue.current===canonicalEditingPayload(payload))return
        nativeRestorationValue.current=null
      }
      draftJournal.update(payload)
    }
  }, [draftReady, draftJournal, page, captureRef, captureVersion, reviewDrafts, reviewVersions, objectiveDraft])
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent): void => {
      if (draftJournal && currentDraftReady.current && draftJournal.hasUnconfirmedEditing(currentEditing.current())) { event.preventDefault(); event.returnValue='' }
    }
    window.addEventListener('beforeunload',guard)
    return () => window.removeEventListener('beforeunload',guard)
  }, [draftJournal])
  const flushEditing = async (): Promise<TextbookEditingPayload> => {
    const value = editingPayload()
    if (draftJournal) {
      draftJournal.update(value)
      if(draftJournal.state.phase==='rejected')await draftJournal.retry()
      else await draftJournal.flush()
    }
    return value??{page:{...pageRef.current},capture_ref:captureRefRef.current,capture_version:captureVersionRef.current,
      review_drafts:Object.fromEntries(Object.entries(reviewDraftsRef.current).map(([ref,text])=>[ref,{text,source_version:reviewVersionsRef.current[ref]||''}])),
      action_ids:{...pendingActionIds.current},...(objectiveDraftRef.current?{objective_draft:objectiveDraftRef.current}:{})}
  }
  const inspectDraftConflict = async (): Promise<void> => {
    if (!draftJournal) return
    setBusy(true); setDraftComparison(null); setDiscardLocalConfirmed(false)
    try {
      const comparison = await draftJournal.inspectConflict()
      if (mounted.current) setDraftComparison(comparison)
    } catch {
      if (mounted.current) setMessage('已保存草稿暂时无法核对；本窗口输入仍保留，没有覆盖或重发。')
    } finally { if (mounted.current) setBusy(false) }
  }
  const adoptSavedDraft = async (): Promise<void> => {
    if (!draftJournal || !discardLocalConfirmed || !draftComparison) return
    setBusy(true)
    try {
      const payload = await draftJournal.adoptSavedConflict()
      if (!mounted.current) return
      applyEditingPayload(payload); setDraftComparison(null); setDiscardLocalConfirmed(false)
      setMessage('已采用核对过的已保存版本；没有重新提交页面、校对或导入请求。')
    } catch {
      if (mounted.current) {
        setDraftComparison(null); setDiscardLocalConfirmed(false)
        setMessage('版本或连接已变化，请重新对照草稿；本窗口输入仍保留，没有覆盖。')
      }
    } finally { if (mounted.current) setBusy(false) }
  }
  const reconcileEditingCommand = async (): Promise<void> => {
    if (!draftJournal?.state.payload) return
    const before = structuredClone(draftJournal.state.payload)
    const commands = Object.entries(before.action_ids)
    const key = commands.find(([key]) => before.capture_ref && key === `page-import:${candidatePayloadHash({page:before.page,captureRef:before.capture_ref})}`)
      ?? commands.find(([key]) => Object.entries(before.review_drafts).some(([ref,item]) => key === `${ref}:review:${candidatePayloadHash({text:item.text.trim()})}`))
    if (!key) { setMessage('没有可核对的上次校对或导入请求。'); return }
    const ref = key[0].startsWith('page-import:') ? before.capture_ref : key[0].split(':review:')[0]
    try {
      const result = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/artifacts/${ref}/actions/${key[1]}`) as {
        status:string;client_action_id:string;action:string;request:Record<string, unknown>;artifact:CaptureArtifact;page?:Page;child_id:string;storage_generation:string
      }
      if (!mounted.current || canonicalCandidatePayload(before) !== canonicalCandidatePayload(draftJournal.state.payload || {})) return
      if (result.status !== 'confirmed' || result.client_action_id !== key[1] || result.artifact?.capture_ref !== ref
        || result.child_id !== childId || result.storage_generation !== draftJournal.storageGeneration) {
        setMessage('上次提交结果仍未知，原请求和草稿保留；没有自动重发。'); return
      }
      if (result.action === 'imported' && result.page && result.artifact.status === 'imported'
        && Object.entries(before.page).every(([name,value]) => name === 'source_ref' || result.request[name] === value)) {
        clearActionId(key[0]); setPage(blankPage); setCaptureRef(''); setCaptureVersion(''); setSelected(result.page.page_ref)
        draftJournal.update({...before,page:{...blankPage},capture_ref:'',capture_version:'',action_ids:{...pendingActionIds.current}})
        await draftJournal.flush(); await loadPages(); setMessage('上次页面已真实保存，未重复导入；草稿收尾已核对。')
      } else if (result.action === 'reviewed' && result.artifact.status === 'ready'
        && result.request.extracted_text === before.review_drafts[ref]?.text.trim()) {
        clearActionId(key[0]); setReviewDrafts((items) => { const {[ref]:_,...rest} = items; return rest })
        setReviewVersions((items) => { const {[ref]:_,...rest} = items; return rest })
        useCaptureArtifact(result.artifact); await loadCaptureArtifacts()
      } else setMessage('原回执与当前草稿不一致，已保留两者，请对照原页核对。')
    } catch { setMessage('上次提交回执暂时无法读取；原请求及草稿保留。') }
  }
  const actionId = (key: string): string => pendingActionIds.current[key] ?? (pendingActionIds.current[key] = `textbook-review-${crypto.randomUUID()}`)
  const clearActionId = (key: string): void => { delete pendingActionIds.current[key] }

  const loadPages = async (): Promise<void> => {
    try {
      const result = await api('/v1/parent/knowledge-graph/textbook-pages') as { items: Page[] }
      // The parent queue shares a request adapter with this component.  Ignore
      // malformed/foreign list payloads rather than rendering <option key={undefined}>
      // and leaving a React key warning in the actual review screen.
      const validPages = result.items.filter((item): item is Page => typeof item.page_ref === 'string' && item.page_ref.length > 0)
      setPages(validPages)
      if (!selected && validPages[0]) setSelected(validPages[0].page_ref)
    } catch { setMessage('教材 OCR 页暂时无法读取，可稍后重试。') }
  }
  useEffect(() => { void loadPages() }, [])
  const loadCandidates = async (pageRef = selected): Promise<void> => {
    if (!pageRef) return
    const request = ++candidateRequest.current
    try {
      const result = await api(`/v1/parent/knowledge-graph/textbook-pages/${pageRef}/language-candidates`) as { items: Candidate[] }
      if (request !== candidateRequest.current) return
      result.items.forEach(reconcileCandidateCommand)
      setCandidates(result.items)
    } catch { /* the page picker remains usable; extract offers recovery */ }
  }
  useEffect(() => {
    if (captureOnly) return
    setBusy(false)
    void loadCandidates()
    return () => { candidateRequest.current += 1 }
  }, [captureOnly, selected])
  const loadCaptureArtifacts = async (): Promise<void> => {
    if (!childId) return
    try { setReviewArtifacts((await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/artifacts`) as { items: CaptureArtifact[] }).items) } catch { /* parent can still paste OCR */ }
  }
  useEffect(() => { void loadCaptureArtifacts() }, [childId])
  useEffect(() => () => {
    sourceRequest.current?.abort()
    if (sourcePreview?.url) URL.revokeObjectURL(sourcePreview.url)
  }, [sourcePreview?.url])
  useEffect(() => {
    const processing = candidates.find((candidate) => candidate.status === 'projecting' && pendingCandidateCommand(candidate.candidate_ref, 'confirm'))
    if (!processing) return
    const command = pendingCandidateCommand(processing.candidate_ref, 'confirm')!
    const timer = window.setTimeout(() => { void (async () => {
      try {
        const result = await api(`/v1/parent/knowledge-graph/language-candidates/${processing.candidate_ref}/actions/${command.clientActionId}`) as { item: Candidate, action_status: string }
        setCandidates((items) => items.map((item) => item.candidate_ref === processing.candidate_ref ? result.item : item))
        if (result.action_status === 'confirmed') { clearCandidateCommand(processing.candidate_ref, 'confirm'); setMessage('已确认并投影为可诊断词字原子。') }
        else if (result.action_status === 'failed_recoverable') setMessage('确认投影中断，可点击“恢复批准”安全重试；学生端仍不可见。')
      } catch { setMessage('确认状态暂时无法读取；本机已保存操作，稍后会自动继续核对。') }
    })() }, 800)
    return () => window.clearTimeout(timer)
  }, [api, candidates])

  const importPage = async (): Promise<void> => {
    const importKey = `page-import:${candidatePayloadHash({page,captureRef})}`
    setBusy(true); setMessage('')
    try {
      const clientActionId = captureRef && childId ? actionId(importKey) : undefined
      const submitted = await flushEditing()
      const result = await api('/v1/parent/knowledge-graph/textbook-pages', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...page, ...(clientActionId ? { capture_ref: captureRef, child_id: childId, client_action_id: clientActionId } : {}) }) }) as { page: Page }
      if (!mounted.current) return
      clearActionId(importKey)
      setSelected(result.page.page_ref)
      if (!draftJournal || canonicalCandidatePayload(submitted) === canonicalCandidatePayload(draftJournal.state.payload || {})) {
        setPage(blankPage); setCaptureRef(''); setCaptureVersion('')
        if (draftJournal) {
          draftJournal.update({...submitted,page:{...blankPage},capture_ref:'',capture_version:'',action_ids:{...pendingActionIds.current}})
          await draftJournal.flush()
        }
      }
      await loadPages()
      setMessage(captureOnly
        ? '本机 OCR 页面及原页来源已保存。学科知识尚未核验，不能直接作为教学或掌握度证据。'
        : '本机 OCR 页面已保存。请先抽取候选，再逐项确认。')
    } catch { setMessage('页面提交或草稿收尾尚未核对；原请求与校对草稿保留，请核对后重试。') }
    finally { setBusy(false) }
  }
  const extract = async (): Promise<void> => {
    if (!selected) return
    const request = ++candidateRequest.current
    setBusy(true); setMessage('')
    try {
      const result = await api(`/v1/parent/knowledge-graph/textbook-pages/${selected}/language-candidates/extract`, { method: 'POST' }) as { items: Candidate[] }
      if (request !== candidateRequest.current) return
      result.items.forEach(reconcileCandidateCommand)
      setCandidates(result.items)
      setMessage(result.items.length ? '只显示明确词汇标记或引号短语；请确认后才会用于学习。' : '这一页没有保守候选。可在下方补入页面中清晰可见的词或字。')
    } catch { if (request === candidateRequest.current) setMessage('候选抽取失败；页面和已保存的确认结果没有改变。') }
    finally { if (request === candidateRequest.current) setBusy(false) }
  }
  const save = async (candidate: Candidate, status: Candidate['status'], term = candidate.term, proposed: Record<string, unknown> = {}): Promise<void> => {
    const payload = { term, focus_kind: candidate.focus_kind, proposed, status }
    const action = status === 'confirmed' ? 'confirm' : 'save'
    const command = commandForCandidate(candidate.candidate_ref, action, payload)
    setBusy(true); setMessage('')
    try {
      const result = await api(`/v1/parent/knowledge-graph/language-candidates/${candidate.candidate_ref}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...payload, client_action_id: command.clientActionId }) }) as { item: Candidate }
      if (result.item.status === status || (status === 'confirmed' && result.item.status === 'confirmed')) clearCandidateCommand(candidate.candidate_ref, action)
      if (result.item.status === 'projecting') {
        setCandidates((items) => items.map((item) => item.candidate_ref === candidate.candidate_ref ? result.item : item))
        setMessage('确认处理中：本机正在持续核对最终状态；学生端暂不可见。')
        return
      }
      if (status === 'deleted') setCandidates((items) => items.filter((item) => item.candidate_ref !== candidate.candidate_ref))
      else await extract()
      setMessage(status === 'confirmed' ? '已确认并投影为可诊断词字原子。' : '已删除该候选；它不会进入学习图谱。')
    } catch { setMessage('保存失败；请检查编辑后的词、词组或字确实出现在此页 OCR 中。') }
    finally { setBusy(false) }
  }
  const addManual = async (): Promise<void> => {
    if (!selected || !manualTerm.trim()) return
    setBusy(true); setMessage('')
    try {
      await api(`/v1/parent/knowledge-graph/textbook-pages/${selected}/language-candidates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ language: manualLanguage, term: manualTerm.trim(), focus_kind: manualKind, proposed: {} }) })
      setManualTerm(''); await extract(); setMessage('已加入待确认候选；仍不会自动进入学习图谱。')
    } catch { setMessage('无法加入：词或字必须完整出现在这页 OCR 文字中。') }
    finally { setBusy(false) }
  }
  const withdraw = async (candidate: Candidate): Promise<void> => {
    const command = commandForCandidate(candidate.candidate_ref, 'withdraw', { action: 'withdraw' })
    setBusy(true); setMessage('')
    try {
      const result = await api(`/v1/parent/knowledge-graph/language-candidates/${candidate.candidate_ref}/withdraw`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_action_id: command.clientActionId }) }) as { item: Candidate }
      if (result.item.status === 'withdrawn') clearCandidateCommand(candidate.candidate_ref, 'withdraw')
      await extract()
      setMessage('已撤销确认并停用该词字的讲解映射；学生端将不再显示它。')
    } catch { setMessage('撤销没有保存；原确认状态仍有效。') }
    finally { setBusy(false) }
  }

  const parentPort: ParentDocumentCapturePort | undefined = childId ? {
    acquire: async (systemIdHash, capabilities) => { const lease = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/leases`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ system_id_hash: systemIdHash, capabilities }) }) as { lease_id: string, label?: string }; captureLease.current = lease.lease_id; return lease },
    renew: async (leaseId) => api(`/v1/parent/knowledge-graph/textbook-capture/leases/${leaseId}/renew`, { method: 'POST' }),
    release: async (leaseId) => { await api(`/v1/parent/knowledge-graph/textbook-capture/leases/${leaseId}/release`, { method: 'POST' }); if (captureLease.current === leaseId) captureLease.current = '' },
    recognize: async (imageBase64, leaseId, transform) => {
      try {
        const result = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/ocr`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lease_id: leaseId, image_base64: imageBase64, image_transform: transform, page_context: page, client_action_id: `textbook-ocr-${crypto.randomUUID()}` }) }) as { artifact: CaptureArtifact }
        return { ...result.artifact, artifact_id: result.artifact.capture_ref } as unknown as OcrArtifact
      } catch (error) { void loadCaptureArtifacts(); throw error }
    },
    recognizeFile: async (imageBase64, transform, clientActionId) => {
      if (pendingFileRequest.current?.id !== clientActionId) pendingFileRequest.current = { id: clientActionId,
        payload: { image_base64: imageBase64, image_transform: structuredClone(transform),
          page_context: structuredClone(page), client_action_id: clientActionId } }
      const request = pendingFileRequest.current
      try {
        const result = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/file-ocr`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request.payload),
        }) as { artifact: CaptureArtifact }
        if (pendingFileRequest.current === request) pendingFileRequest.current = null
        return { ...result.artifact, artifact_id: result.artifact.capture_ref } as unknown as OcrArtifact
      } catch (error) { void loadCaptureArtifacts(); throw error }
    },
  } : undefined
  const useCaptureArtifact = (artifact: CaptureArtifact): void => {
    // Original OCR quality remains audit evidence after a persisted correction.
    if (artifact.status !== 'ready') {
      setReviewArtifacts((items) => [artifact, ...items.filter((item) => item.capture_ref !== artifact.capture_ref)])
      setMessage(artifact.status === 'needs_review'
        ? '该页需要人工校对；校对完成前不会带入教材页，也不能导入图谱。'
        : '该页尚未取得可导入状态，原记录保留在队列中；不能带入教材表单。')
      return
    }
    const context = artifact.page_context as Partial<PageDraft>
    setPage((current) => captureRef === artifact.capture_ref
      ? {...current,ocr_text:artifact.extracted_text}
      : { ...blankPage, ...context, ocr_text: artifact.extracted_text })
    setCaptureRef(artifact.capture_ref); setCaptureVersion(artifact.source_version)
    setMessage('本机 OCR 文字已带入教材定位表单。请核对后保存页面。')
  }
  const completeReview = async (artifact: CaptureArtifact, corrected = reviewDrafts[artifact.capture_ref] ?? artifact.extracted_text): Promise<void> => {
    if (!corrected.trim()) return
    const key = `${artifact.capture_ref}:review:${candidatePayloadHash({text:corrected.trim()})}`
    setBusy(true)
    try {
      const clientActionId = actionId(key)
      await flushEditing()
      const result = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/artifacts/${artifact.capture_ref}/review`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ extracted_text: corrected.trim(), client_action_id: clientActionId }) }) as { artifact: CaptureArtifact }
      if (!mounted.current) return
      clearActionId(key)
      setReviewDrafts((drafts) => { const { [artifact.capture_ref]: _, ...rest } = drafts; return rest })
      setReviewVersions((versions) => { const { [artifact.capture_ref]: _, ...rest } = versions; return rest })
      useCaptureArtifact(result.artifact); await loadCaptureArtifacts()
    } catch { setMessage('校对没有保存；原 OCR artifact 仍在队列中。') }
    finally { setBusy(false) }
  }
  const artifactAction = async (artifact: CaptureArtifact, action: 'skip' | 'restore' | 'retry' | 'undo-review'): Promise<void> => {
    const key = `${artifact.capture_ref}:${action}`
    setBusy(true)
    try {
      const clientActionId = actionId(key)
      await flushEditing()
      const result = await api(`/v1/parent/knowledge-graph/textbook-capture/${childId}/artifacts/${artifact.capture_ref}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_action_id: clientActionId }) }) as { artifact: CaptureArtifact }
      if (!mounted.current) return
      clearActionId(key)
      setReviewArtifacts((items) => [result.artifact, ...items.filter((item) => item.capture_ref !== artifact.capture_ref)])
      setReviewVersions((versions) => versions[artifact.capture_ref]
        ? {...versions,[artifact.capture_ref]:result.artifact.source_version} : {...versions})
      if (captureRef === artifact.capture_ref) setCaptureVersion(result.artifact.source_version)
      setMessage(action === 'retry' ? '已使用本地加密保留的原页重新识别，无需重拍。' : action === 'skip' ? '已放弃该 OCR artifact；可随时撤销恢复。' : action === 'undo-review' ? '已撤销校对，恢复为待校对的原始 OCR 文字。' : '已恢复到原审核状态。')
    } catch { setMessage('操作未保存，当前 OCR artifact 状态不变。') }
    finally { setBusy(false) }
  }
  const closeSource = (): void => {
    sourceRequest.current?.abort(); sourceRequest.current = null
    setSourceLoading(''); setSourcePreview(null)
  }
  const showSource = async (artifact: CaptureArtifact): Promise<void> => {
    if (!readSource || !artifact.raw_image_retained) return
    if (sourcePreview?.captureRef === artifact.capture_ref) { closeSource(); return }
    sourceRequest.current?.abort()
    const controller = new AbortController(); sourceRequest.current = controller
    setSourceLoading(artifact.capture_ref); setMessage('')
    try {
      const source = await readSource(childId, artifact.capture_ref, controller.signal)
      if (controller.signal.aborted || sourceRequest.current !== controller) return
      setSourcePreview({ captureRef: artifact.capture_ref, url: URL.createObjectURL(new Blob([source.bytes], { type: source.mediaType })) })
    } catch (error) {
      if (!controller.signal.aborted) setMessage('原页暂时无法读取；OCR 文字和已保存校对仍然保留。')
    } finally {
      if (sourceRequest.current === controller) sourceRequest.current = null
      setSourceLoading((current) => current === artifact.capture_ref ? '' : current)
    }
  }
  const sourceControl = (artifact: CaptureArtifact): React.JSX.Element | null => readSource && artifact.raw_image_retained
    ? <button disabled={busy || sourceLoading === artifact.capture_ref} onClick={() => void showSource(artifact)}>{sourcePreview?.captureRef === artifact.capture_ref ? '关闭原页' : sourceLoading === artifact.capture_ref ? '读取原页中' : '查看原页'}</button>
    : null
  const sourceImage = (artifact: CaptureArtifact): React.JSX.Element | null => sourcePreview?.captureRef === artifact.capture_ref
    ? <figure className="parent-ocr-source"><img src={sourcePreview.url} alt="待校对教材原页" /><figcaption>本机加密原页，仅用于家长校对，不上传云端。</figcaption></figure>
    : null

  return <section aria-label="教材与习题册拍摄 OCR" className="knowledge-graph-review-queue" onChange={()=>{if(draftReady)draftJournal?.keepEditing(currentEditing.current())}}>
    <header><h2>教材与习题册拍摄 OCR</h2><p>家长可用高拍仪逐页采集教材或习题册。系统自动对焦、转正并在本机 OCR；只有文字本身无法辨认时才请家长对照原书校对，不要求家长审核学科知识。</p><button disabled={busy} onClick={() => {void loadPages();void loadCaptureArtifacts()}}>刷新资料页</button></header>
    {message && <p role="status">{message}</p>}
    {navigationBlocked&&<p role="alert">{draftJournal?.hasUnconfirmedEditing(editingPayload())
      ?'还有未确认保存的草稿，已留在当前孩子页面，没有换孩或离开。请先核对保存状态；需要时恢复连接并重试保存，确认后再选择目的地。'
      :'草稿已确认保存。请再次选择要去的页面；没有自动执行之前的离开操作。'}</p>}
    {draftStatus && <div aria-label="教材编辑草稿状态"><p role="status">{draftStatus.message}</p>
      {draftJournal?.recoveryMessage&&<p role="status">{draftJournal.recoveryMessage}</p>}
      {(draftStatus.phase === 'error'||draftStatus.phase === 'rejected') && <button onClick={() => void (draftReady ? draftJournal?.retry() : restoreDraft())?.catch(() => undefined)}>重试草稿保存或恢复</button>}
      {draftStatus.phase === 'conflict' && <div>
        <p>已暂停提交，没有覆盖已保存版本。请对照两份草稿；能否关闭窗口请以本机加密保护状态为准。</p>
        <button disabled={busy} onClick={() => void inspectDraftConflict()}>对照两份草稿</button>
        {draftComparison && <>
          {draftComparison.generationChanged && <p>本机数据已删除或恢复换代，旧窗口内容不能自动恢复。</p>}
          <div className="parent-ocr-review-grid">
            <DraftComparisonContent heading="本窗口未保存内容" payload={draftComparison.local} />
            <DraftComparisonContent heading="本机已保存内容" payload={draftComparison.saved} />
          </div>
          <label className="parent-draft-consent"><input type="checkbox" checked={discardLocalConfirmed} disabled={busy} onChange={event => setDiscardLocalConfirmed(event.target.checked)} />我已核对，放弃本窗口未保存内容，采用已保存版本</label>
          <button disabled={busy || !discardLocalConfirmed} onClick={() => void adoptSavedDraft()}>采用已保存版本</button>
        </>}
      </div>}
      {draftReady && draftStatus.phase !== 'conflict' && Object.keys(pendingActionIds.current).length > 0 && <button disabled={busy} onClick={() => void reconcileEditingCommand()}>核对上次提交</button>}
    </div>}
    <fieldset disabled={!draftReady || busy || draftStatus?.phase === 'conflict'} style={{border:0,padding:0,margin:0}}>
    <details open><summary>导入一页本机 OCR 文字</summary><div className="knowledge-fields">
      <label>资料类型<select aria-label="资料类型" value={page.material_kind} onChange={(event) => setPage({ ...page, material_kind: event.target.value as 'textbook' | 'workbook' })}><option value="textbook">教材</option><option value="workbook">习题册</option></select></label>
      <label>书名与页面标题<input aria-label="教材页标题" value={page.title} onChange={(event) => setPage({ ...page, title: event.target.value })} /></label>
      <label>年级<input aria-label="教材页年级" value={page.grade_scope} onChange={(event) => setPage({ ...page, grade_scope: event.target.value })} /></label>
      <label>学科<input aria-label="教材页学科" value={page.subject_scope} onChange={(event) => setPage({ ...page, subject_scope: event.target.value })} /></label>
      <label>教材版本<input aria-label="教材版本" value={page.textbook_version} onChange={(event) => setPage({ ...page, textbook_version: event.target.value })} /></label>
      <label>册次<input aria-label="册次" value={page.volume} onChange={(event) => setPage({ ...page, volume: event.target.value })} /></label>
      <label>单元<input aria-label="单元" value={page.unit} onChange={(event) => setPage({ ...page, unit: event.target.value })} /></label>
      <label>课文或课题<input aria-label="课文或课题" value={page.lesson} onChange={(event) => setPage({ ...page, lesson: event.target.value })} /></label>
      <label>页码<input aria-label="页码" type="number" min="1" value={page.page_number} onChange={(event) => setPage({ ...page, page_number: Number(event.target.value) || 1 })} /></label>
    </div>{parentPort && <div><h3>本机原页采集</h3><p>完整页原图会加密保留在本机，用于校对、重识别、备份和恢复；默认不上传云端。高拍仪与孩子观察摄像头必须是已绑定的不同设备。</p><DocumentCameraCapture sessionId={`parent-textbook-capture:${childId}`} parentPort={parentPort} onArtifact={(artifact) => useCaptureArtifact(artifact as unknown as CaptureArtifact)} /></div>}<textarea aria-label="教材页 OCR 文字" value={page.ocr_text} disabled={Boolean(captureRef)} onChange={(event) => setPage({ ...page, ocr_text: event.target.value })} /><button disabled={busy || Boolean(draftStatus?.staleRefs.length) || !page.title.trim() || !page.ocr_text.trim()} onClick={() => void importPage()}>保存本机 OCR 页</button></details>
    {reviewArtifacts.filter((artifact) => artifact.status === 'processing').map((artifact) => <article key={artifact.capture_ref}><h3>教材 OCR 正在处理</h3><p>本机正在处理此页；原页已加密保留，处理中断后可恢复。</p></article>)}
    {reviewArtifacts.filter((artifact) => artifact.status === 'importing').map((artifact) => <article key={artifact.capture_ref}><h3>教材页正在保存</h3><p>本机正在核对教材页写入结果；请稍后刷新，勿用另一份表单重复导入。</p></article>)}
    {reviewArtifacts.filter((artifact) => artifact.status === 'needs_review').map((artifact, index) => <article key={artifact.capture_ref}><h3>待人工校对的教材 OCR</h3><p>低置信、手写或公式内容不会自动进入教材页。</p><div className="parent-ocr-review-grid">{sourceImage(artifact)}<textarea aria-label={`校对后的教材 OCR 文字（第 ${index + 1} 项）`} value={reviewDrafts[artifact.capture_ref] ?? artifact.extracted_text} onChange={(event) => { setReviewDrafts((drafts) => ({ ...drafts, [artifact.capture_ref]: event.target.value })); setReviewVersions((versions) => ({...versions,[artifact.capture_ref]:artifact.source_version})) }} /></div>{sourceControl(artifact)}<button disabled={busy || Boolean(draftStatus?.staleRefs.includes(artifact.capture_ref))} onClick={() => void completeReview(artifact)}>校对并带入表单</button><button disabled={busy} onClick={() => void artifactAction(artifact,'skip')}>放弃</button></article>)}
    {reviewArtifacts.filter((artifact) => artifact.status === 'ready').map((artifact) => <article key={artifact.capture_ref}><h3>已校对，可带入教材表单</h3><p>服务端已保存 OCR 文本，刷新或重启后仍可继续。</p>{sourceImage(artifact)}{sourceControl(artifact)}<button disabled={busy} onClick={() => useCaptureArtifact(artifact)}>带入教材表单</button><button disabled={busy} onClick={() => void artifactAction(artifact,'undo-review')}>撤销本次校对</button><button disabled={busy} onClick={() => void artifactAction(artifact,'skip')}>放弃</button></article>)}
    {reviewArtifacts.filter((artifact) => artifact.status === 'skipped').map((artifact) => <article key={artifact.capture_ref}><h3>已放弃的教材 OCR</h3><button disabled={busy} onClick={() => void artifactAction(artifact,'restore')}>撤销放弃</button></article>)}
    {reviewArtifacts.filter((artifact) => artifact.status === 'failed').map((artifact) => <article key={artifact.capture_ref}><h3>OCR 未完成</h3><p>{artifact.raw_image_retained ? '原页已在本机加密保留，可直接重新识别。' : '原页不可用，需重新采集。'}</p>{sourceImage(artifact)}{sourceControl(artifact)}<button disabled={busy || !artifact.raw_image_retained} onClick={() => void artifactAction(artifact,'retry')}>重新识别</button><button disabled={busy} onClick={() => void artifactAction(artifact,'skip')}>放弃</button></article>)}
    {captureOnly && childId && draftReady && <ParentPaperObjectiveDraft childId={childId} api={api}
      artifacts={reviewArtifacts} value={objectiveDraft} onChange={setObjectiveDraft} onSave={flushEditing}
      readSource={readSource} blocked={busy || draftStatus?.phase==='conflict'}/>} 
    {captureOnly && pages.length > 0 && <p className="report-uncertainty">已保存 {pages.length} 页资料。这些页面的学科知识尚未核验；保存页面不代表已形成可用的教学知识。</p>}
    {!captureOnly && pages.length > 0 && <div><label>选择教材页<select aria-label="选择教材页" value={selected} onChange={(event) => { setSelected(event.target.value); setCandidates([]) }}><option value="">请选择</option>{pages.map((item) => <option key={item.page_ref} value={item.page_ref}>{item.title} · {item.textbook_version || '未标版本'} · {item.volume || '未标册次'} · 第{item.page_number}页</option>)}</select></label><button disabled={busy || !selected} onClick={() => void extract()}>抽取保守候选</button></div>}
    {!captureOnly && selected && <div><h3>补入页面中可见的词、词组或多音字</h3><label>语言<select aria-label="补入语言" value={manualLanguage} onChange={(event) => setManualLanguage(event.target.value as Candidate['language'])}><option value="english">英语</option><option value="chinese">语文</option></select></label><label>类型<select aria-label="补入类型" value={manualKind} onChange={(event) => setManualKind(event.target.value as Candidate['focus_kind'])}><option value="word">词</option><option value="phrase">词组</option><option value="character">字</option></select></label><input aria-label="补入词或字" value={manualTerm} onChange={(event) => setManualTerm(event.target.value)} /><button disabled={busy || !manualTerm.trim()} onClick={() => void addManual()}>加入待确认</button></div>}
    {!captureOnly && candidates.map((candidate) => candidate.status === 'pending'
      ? <CandidateEditor key={candidate.candidate_ref} candidate={candidate} busy={busy} onSave={save} />
      : <article key={candidate.candidate_ref}><h3>{candidate.term}</h3><p>{candidate.status === 'projecting' ? '上一次批准在本机投影时中断，学生端仍不可见；请恢复同一批准。' : candidate.status === 'confirmed' ? '已确认并可用于学生学习。若判断有误，可显式撤销确认；词条本身不可静默改写。' : candidate.status === 'withdrawn' ? '家长已撤销确认，相关学习映射已停用，学生不会再看到它。' : candidate.status === 'conflicted' ? '此页重扫后已找不到该词字，旧投影已停用，需导入更正后的新版页面。' : '此候选的页面证据已过期，未进入学习图谱。'}</p>{candidate.status === 'projecting' && <button disabled={busy} onClick={() => void save(candidate, 'confirmed', candidate.term, JSON.parse(candidate.proposed_json || '{}') as Record<string, unknown>)}>恢复批准</button>}{candidate.status === 'confirmed' && <button disabled={busy} onClick={() => void withdraw(candidate)}>撤销确认</button>}</article>)}
    </fieldset>
  </section>
}

function CandidateEditor({ candidate, busy, onSave }: { candidate: Candidate, busy: boolean, onSave: (candidate: Candidate, status: Candidate['status'], term?: string, proposed?: Record<string, unknown>) => Promise<void> }): React.JSX.Element {
  const [term, setTerm] = useState(candidate.term)
  const [senses, setSenses] = useState('')
  const [pronunciations, setPronunciations] = useState('')
  const proposed = (): Record<string, unknown> => ({ ...(senses.trim() ? { senses: senses.split(/[，,]/).map((item) => item.trim()).filter(Boolean) } : {}), ...(pronunciations.trim() ? { pronunciations: pronunciations.split(/[，,]/).map((item) => item.trim()).filter(Boolean) } : {}) })
  return <article><h3>{candidate.language === 'english' ? '英语' : '语文'} · {candidate.focus_kind === 'phrase' ? '词组' : candidate.focus_kind === 'character' ? '字' : '词'}</h3><label>词或字<input aria-label="编辑词或字" value={term} onChange={(event) => setTerm(event.target.value)} /></label><label>释义（逗号分隔）<input aria-label="释义" value={senses} onChange={(event) => setSenses(event.target.value)} /></label>{candidate.language === 'chinese' && <label>读音（逗号分隔）<input aria-label="读音" placeholder="例如 xíng, háng" value={pronunciations} onChange={(event) => setPronunciations(event.target.value)} /></label>}<button disabled={busy || !term.trim()} onClick={() => void onSave(candidate, 'confirmed', term.trim(), proposed())}>批准并用于学习</button><button disabled={busy} onClick={() => void onSave(candidate, 'deleted', term.trim())}>不采用</button></article>
}
