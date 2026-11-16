import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Download, Quote, RefreshCw, Save, ShieldCheck, Undo2, X } from 'lucide-react'
import { ApiError } from './api'
import { RubricVisualReview, currentRubricVisual, type RubricVisualMaterial, type RubricVisualEvidence } from './RubricVisualReview'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'

type Local = { local_only: true; cloud_transmission_allowed: false }
type Dimension = { dimension_id: string; criterion: string; max_score: number; pass_score: number; score_anchors: Record<string, string>; score: number | null; response_quotes: string[]; rationale: string }
type Scored = { dimension_id: string; score: number; response_quotes: string[]; source_quotes?: string[]; rationale: string }
type AudioObservation = { dimension_id: string; start_frame: number; end_frame: number; heard_text: string; rationale: string }
type AudioEvidence = { media_id: string; review: { schema: 'pet_speaking_audio_references_v1'; binding_version: string; audio_sha256: string; observations: AudioObservation[] } }
type AudioBinding = Local & { child_id: string; assessment_id: string; session_id: string; media_id: string; binding_version: string; audio: { sha256: string; frames: number; sample_rate: number }; dimensions_required: string[] }
type AudioReview = Local & { required: boolean; profile: string | null; dimensions: Dimension[]; bindings: AudioBinding[]; blockers: string[] }
type WritingProfile = 'pet_writing_text_v1' | 'school_open_written_v1'
type WritingEvidence = { binding_version: string; task_point_evidence: { task_point: string; response_quote: string }[] }
type WritingBinding = Local & { profile: WritingProfile; assessment_id: string; assessment_fingerprint: string; diagnostic_item_id: string;
  binding_version: string; task_points: string[]; original: Local & { artifact_id: string; kind: string; source_version: string; response_hash: string } }
type WritingReview = Local & { required: boolean; profile: string | null; binding: WritingBinding | null; blockers: string[] }
const writingProfiles: string[] = ['pet_writing_text_v1', 'school_open_written_v1']
type ReviewBody = { expected_revision: number; client_action_id: string; operation: 'approve' | 'retract'; reason: string; dimensions?: Scored[]; criterion_evidence?: { criterion: string; response_quote: string }[]; independent?: boolean; hint_level?: number; review_profile?: 'pet_speaking_audio_v1' | WritingProfile; audio_evidence?: AudioEvidence; writing_evidence?: WritingEvidence; visual_evidence?: RubricVisualEvidence }
type ReviewEvent = Local & ReviewBody & { assessment_id: string; review_revision: number; score_status: string; created_at: string; reviewer_role: string; passed: boolean }
type Assessment = Local & { exam_slot?: { position: number }; assessment_id: string; child_id: string; fingerprint: string; question: string | null; response: string; subject: string; profile: string; source_kind: string; source_status: string; score_status: string; review_revision: number; review: ReviewEvent | null; dimensions: Dimension[]; criteria: string[]; responded_at: string | null; material: { status: string; diagnostic_item_id?: string; stimulus_text?: string | null; stimulus_asset?: string | null; visual?: RubricVisualMaterial } }
function requiresMaterialQuote(assessment: Assessment, dimensionId: string, score: number): boolean {
  return !!assessment.material?.stimulus_text && dimensionId === 'evidence' && score > 0
    && (assessment.profile === 'chinese_open' || assessment.profile.startsWith('school_open_'))
}
const audioDimensionIds = ['audio_text_alignment', 'intelligibility', 'pronunciation', 'prosody', 'fluency'] as const
type AudioDimensionScore = { score: number | null; start_frame: number | null; end_frame: number | null; abstain_reason: string | null }
type AudioDimensionQualification = Partial<Local> & { status: 'unavailable' | 'reference' | 'calibrated_audio_dimensions';
  score_scope: 'original_audio_dimensions_only'; score_qualified: boolean; mastery_claim_allowed: false; certification_claim_allowed: false;
  assessment_id?: string; score_authority?: string; covered_dimensions?: string[]; dimensions?: Record<string, AudioDimensionScore>;
  provenance?: { child_id: string; selector: { assessment_id: string } } }
type Qualification = Local & { assessment_id: string; eligible_for_mastery_gate: boolean; automatic_certification: false; score_qualified?: boolean; receipt: { assessment_id: string; review_revision: number; score_status?: string } | null;
  original_audio_dimensions?: AudioDimensionQualification }
function validateAudioQualification(value: AudioDimensionQualification, owner: Assessment) {
  const invalid = () => { throw new ApiError('原音维度评分的范围或归属不一致', 409) }
  if (!value || !['unavailable', 'reference', 'calibrated_audio_dimensions'].includes(value.status)
    || value.score_scope !== 'original_audio_dimensions_only' || typeof value.score_qualified !== 'boolean'
    || value.mastery_claim_allowed !== false || value.certification_claim_allowed !== false) invalid()
  if (!value.score_qualified) { if (value.status === 'calibrated_audio_dimensions') invalid(); return }
  if (value.status !== 'calibrated_audio_dimensions' || value.local_only !== true || value.cloud_transmission_allowed !== false
    || value.assessment_id !== owner.assessment_id || owner.source_status !== 'current'
    || value.score_authority !== 'typed_cpu_original_audio_projection_v1'
    || value.provenance?.child_id !== owner.child_id || value.provenance?.selector?.assessment_id !== owner.assessment_id
    || !Array.isArray(value.covered_dimensions) || value.covered_dimensions.length < 1 || value.covered_dimensions.length > 5
    || new Set(value.covered_dimensions).size !== value.covered_dimensions.length
    || !value.covered_dimensions.every(id => (audioDimensionIds as readonly string[]).includes(id))
    || !value.dimensions || Object.keys(value.dimensions).length !== 5
    || !audioDimensionIds.every(id => Object.hasOwn(value.dimensions!, id))) invalid()
  for (const id of value.covered_dimensions!) {
    const score = value.dimensions![id]
    if (!score || !Number.isInteger(score.score) || score.score! < 0 || score.score! > 3
      || !Number.isSafeInteger(score.start_frame) || !Number.isSafeInteger(score.end_frame)
      || score.start_frame! < 0 || score.end_frame! <= score.start_frame! || score.abstain_reason !== null) invalid()
  }
}
type Detail = Local & { assessment: Assessment; audio_review?: AudioReview; writing_review?: WritingReview; history: ReviewEvent[]; qualification: Qualification }
type Listing = Local & { child_id: string; items: (Assessment & { qualification?: Qualification })[]; total: number; offset: number }
function currentCalibrated(assessment?: Assessment, qualification?: Qualification): boolean {
  return qualification?.score_qualified === true && qualification.receipt?.score_status === 'calibrated_model'
    && assessment?.source_status === 'current' && assessment.score_status === 'candidate' && assessment.review_revision === 0
}
type DimensionDraft = { score: string; quote: string; sourceQuote: string; rationale: string; heard?: string; start?: string; end?: string }
type Draft = { fingerprint: string; revision: number; dimensions: DimensionDraft[]; criteria: string[]; independent: string; hint: string; reason: string; pending: ReviewBody | null; audioBinding?: { media_id: string; binding_version: string }; writingBinding?: { profile: WritingProfile; binding_version: string }; taskQuotes?: string[] }
type Recovery = { childId: string; focus: string; offset: number; drafts: Record<string, Draft> }
export type LearningRubricReviewPanelProps = ParentWorkFocusProps & {
  child: { child_id: string; nickname: string; grade?: string }
  request: (path: string, init?: RequestInit) => Promise<unknown>
  requestAudio?: (path: string, init?: RequestInit) => Promise<ArrayBuffer>
  authneeded?: () => void
}
const key = (child: string) => `hsc-learning-rubric-review:${child}`
const cleared = 'hsc-learning-rubric-review-cleared'
export function clearLearningRubricLocalState(childId: string): void {
  localStorage.removeItem(key(childId))
  window.dispatchEvent(new CustomEvent(cleared, { detail: childId }))
}
function blank(assessment: Assessment, writing?: WritingReview): Draft {
  const binding = writing?.required && writing.binding && writingProfiles.includes(writing.binding.profile) ? writing.binding : null
  return { fingerprint: assessment.fingerprint, revision: assessment.review_revision,
    dimensions: assessment.dimensions.map(() => ({ score: '', quote: '', sourceQuote: '', rationale: '' })),
    criteria: assessment.criteria.map(() => ''), independent: '', hint: '', reason: '', pending: null,
    ...(binding ? { writingBinding: { profile: binding.profile, binding_version: binding.binding_version }, taskQuotes: binding.task_points.map(() => '') } : {}) }
}
function effective(detail: Detail): Assessment {
  return detail.audio_review?.required ? { ...detail.assessment, profile: 'pet_speaking_audio_v1', dimensions: detail.audio_review.dimensions } : detail.assessment
}
function load(childId: string): Recovery {
  const raw = localStorage.getItem(key(childId))
  if (!raw) return { childId, focus: '', offset: 0, drafts: {} }
  const data = JSON.parse(raw) as Recovery
  if (!data || data.childId !== childId || typeof data.focus !== 'string' || !Number.isInteger(data.offset) || data.offset < 0 || !data.drafts || Array.isArray(data.drafts)) throw new Error('invalid recovery')
  for (const draft of Object.values(data.drafts)) {
    if (!draft || typeof draft.fingerprint !== 'string' || !Number.isInteger(draft.revision) || draft.revision < 0
      || !Array.isArray(draft.dimensions) || !Array.isArray(draft.criteria) || draft.criteria.some(v => typeof v !== 'string')
      || ['reason', 'independent', 'hint'].some(v => typeof draft[v as 'reason'] !== 'string')
      || draft.dimensions.some(d => !d || ['score', 'quote', 'sourceQuote', 'rationale'].some(v => typeof d[v as 'score'] !== 'string') || ['heard', 'start', 'end'].some(v => d[v as 'heard'] != null && typeof d[v as 'heard'] !== 'string'))
      || draft.writingBinding && (!writingProfiles.includes(draft.writingBinding.profile) || typeof draft.writingBinding.binding_version !== 'string'
        || !Array.isArray(draft.taskQuotes) || draft.taskQuotes.some(v => typeof v !== 'string'))
      || draft.pending && (typeof draft.pending.client_action_id !== 'string' || !['approve', 'retract'].includes(draft.pending.operation) || draft.pending.expected_revision !== draft.revision)) throw new Error('invalid draft')
  }
  return data
}
const names: Record<string, string> = { answer: '结果', method: '方法', reasoning: '推理', claim: '观点与结论', limitations: '适用边界', task: '任务完成', evidence: '文本依据', expression: '表达', coherence: '连贯', language: '语言', audio_text_alignment: '原音与回答核对', intelligibility: '可理解性', pronunciation: '发音', prosody: '重音与语调', fluency: '口语连贯性' }
const statuses: Record<string, string> = { candidate: '待人工审核', reviewed_human: '已人工审核', withdrawn: '评分审核已撤回', source_changed: '原题或回答已变化' }
const style = `
.rubric-review{padding:16px;background:#fff;color:#27382f;font:14px/1.5 system-ui;border-top:1px solid #cfd9d3;overflow-wrap:anywhere;min-width:0;container-type:inline-size}.rubric-review *{box-sizing:border-box;min-width:0;letter-spacing:0}
.rubric-review h2{font-size:18px;margin:0}.rubric-review h3{font-size:16px;margin:0 0 10px}.rubric-review h4{font-size:14px;margin:12px 0 5px}.rubric-review header,.rr-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between}.rr-actions{justify-content:flex-start;margin-top:10px}
.rubric-review button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:38px;padding:7px 10px;border:1px solid #aabab0;border-radius:5px;background:#f6f8f7;color:inherit;font:inherit;cursor:pointer;white-space:normal}.rubric-review button svg{width:16px;height:16px;flex-shrink:0}.rubric-review button:disabled{opacity:.46;cursor:not-allowed}.rubric-review .rr-primary{background:#236745;color:white}.rubric-review .rr-danger{border-color:#d9b0b0;color:#993c3c}
.rubric-review button:focus-visible,.rubric-review textarea:focus-visible,.rubric-review select:focus-visible{outline:3px solid #2b7a82;outline-offset:2px}.rubric-review label{display:flex;flex-direction:column;gap:5px;margin:10px 0}.rubric-review select,.rubric-review textarea{width:100%;padding:8px;font:inherit;background:#fff;color:inherit;border:1px solid #aabab0;border-radius:4px}.rubric-review select{height:40px}.rubric-review textarea{min-height:70px;resize:vertical}
.rr-band{padding:18px 0;border-top:1px solid #dce3df;margin-top:16px}.rr-columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,330px),1fr));gap:22px}.rr-queue{display:flex;flex-direction:column;gap:6px;max-height:270px;overflow:auto;margin:14px 0}.rr-queue button{text-align:left;justify-content:space-between}.rr-queue button[aria-pressed=true]{background:#eaf3ed;border-color:#236745}.rr-question{flex:1;overflow-wrap:anywhere}.rr-muted{color:#647369}.rr-notice{background:#faf2e4;border-left:3px solid #aa803d;padding:10px;margin:10px 0}.rr-error{background:#fff1f1;border-left-color:#b64545}.rr-text{white-space:pre-wrap;border-left:3px solid #a8c7bd;padding-left:12px;margin:8px 0}.rubric-review fieldset{padding:12px 0;border:0;border-top:1px solid #dce3df}.rubric-review legend{font-weight:600;padding:8px 0}.rr-status{font-size:12px;flex-shrink:0}.rr-anchors{font-size:13px;color:#56675c}.rubric-review details{margin:10px 0}.rubric-review summary{cursor:pointer}.rr-history{padding-left:20px}.rr-history li{padding:8px 0}
@container (min-width:700px){.rr-evidence-pane{position:sticky;top:12px;align-self:start;max-height:calc(100vh - 24px);overflow-y:auto}}
`

export function LearningRubricReviewPanel(props: LearningRubricReviewPanelProps) {
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    const handler = (event: Event) => { if ((event as CustomEvent<string>).detail === props.child.child_id) setGeneration(v => v + 1) }
    window.addEventListener(cleared, handler)
    return () => window.removeEventListener(cleared, handler)
  }, [props.child.child_id])
  return <Panel key={`${props.child.child_id}:${generation}`} {...props} />
}

function OriginalAudio({ binding, path, request, onHeard }: { binding: AudioBinding; path: string; request: NonNullable<LearningRubricReviewPanelProps['requestAudio']>; onHeard: () => void }) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const objectUrl = useRef('')
  const player = useRef<HTMLAudioElement | null>(null)
  useEffect(() => () => { controller.current?.abort(); player.current?.pause(); if (objectUrl.current) URL.revokeObjectURL(objectUrl.current) }, [path])
  async function read() {
    controller.current?.abort()
    const token = new AbortController(); controller.current = token
    setLoading(true); setError('')
    try {
      const bytes = await request(path, { signal: token.signal })
      if (token.signal.aborted) return
      if (!(bytes instanceof ArrayBuffer) || bytes.byteLength > 24 * 1024 * 1024) throw new Error('audio')
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(v => v.toString(16).padStart(2, '0')).join('')
      if (hash !== binding.audio.sha256) throw new Error('source')
      const decoded = await new OfflineAudioContext(1, binding.audio.frames, binding.audio.sample_rate).decodeAudioData(bytes.slice(0))
      if (decoded.numberOfChannels !== 1 || decoded.length !== binding.audio.frames || decoded.sampleRate !== binding.audio.sample_rate) throw new Error('format')
      if (token.signal.aborted) return
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' })); setUrl(objectUrl.current)
    } catch { if (!token.signal.aborted) setError('原音读取或版本核对失败，未播放替代内容。可重试读取原音。') }
    finally { if (!token.signal.aborted) setLoading(false) }
  }
  return <div><button type="button" disabled={loading} onClick={() => void read()}><Download />{loading ? '正在读取原音' : '读取原音'}</button>
    {error && <p role="alert" className="rr-notice rr-error">{error}</p>}
    {url && <audio aria-label="本次口语原音" ref={node => { if (!node) player.current?.pause(); player.current = node }} controls preload="none" src={url} style={{ width: '100%', marginTop: 10 }} onEnded={onHeard} />}
  </div>
}

function Panel({ child, request, requestAudio, authneeded, focusRequest, onFocusResolved }: LearningRubricReviewPanelProps) {
  const [initial] = useState(() => { try { return { data: load(child.child_id), failed: false } } catch { return { data: { childId: child.child_id, focus: '', offset: 0, drafts: {} } as Recovery, failed: true } } })
  const [recovery, setRecovery] = useState(initial.data)
  const saved = useRef(recovery)
  const [storageFailed, setStorageFailed] = useState(initial.failed)
  const [listing, setListing] = useState<Listing | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [expired, setExpired] = useState(false)
  const [selectedMaterial, setSelectedMaterial] = useState<{ assessmentId: string; fingerprint: string; passage: string; quote: string } | null>(null)
  const active = useRef(true)
  const sequence = useRef(0)
  const writing = useRef(false)
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
  const root = `/v1/parent/children/${encodeURIComponent(child.child_id)}/learning-rubrics`
  const assessment = detail ? effective(detail) : undefined
  const calibrated = currentCalibrated(assessment, detail?.qualification)
  const selectedQuote = selectedMaterial && assessment?.assessment_id === selectedMaterial.assessmentId
    && assessment.fingerprint === selectedMaterial.fingerprint && assessment.material?.stimulus_text === selectedMaterial.passage
    ? selectedMaterial.quote : ''
  const draft = assessment ? recovery.drafts[assessment.assessment_id] : undefined
  const audioRequired = detail?.audio_review?.required === true
  const writingRequired = detail?.writing_review?.required === true
  const writingBinding = writingRequired && detail?.writing_review?.binding && writingProfiles.includes(detail.writing_review.binding.profile) ? detail.writing_review.binding : null
  const audioBinding = detail?.audio_review?.bindings.find(b => b.media_id === draft?.audioBinding?.media_id && b.binding_version === draft.audioBinding.binding_version)
  const [heardBinding, setHeardBinding] = useState('')
  const [visualReceipt, setVisualReceipt] = useState<{ owner: string; evidence: RubricVisualEvidence | null } | null>(null)
  const visual = assessment?.material?.visual
  const visualOwner = JSON.stringify([child.child_id, assessment?.assessment_id, assessment?.fingerprint, assessment?.review_revision, visual])
  const visualEvidence = visualReceipt?.owner === visualOwner ? visualReceipt.evidence : null
  const visualReady = !visual || currentRubricVisual(visual) && visualEvidence?.assessment_id === assessment?.assessment_id
    && visualEvidence?.assessment_fingerprint === assessment?.fingerprint && visualEvidence?.binding_version === visual.binding_version
    && visualEvidence.displays.length === visual.materials.length
  const writingStale = !!draft && (!!draft.writingBinding && (!writingBinding || draft.writingBinding.binding_version !== writingBinding.binding_version || draft.writingBinding.profile !== writingBinding.profile)
    || !!writingBinding && (!draft.writingBinding || draft.taskQuotes?.length !== writingBinding.task_points.length))
  const stale = !!assessment && !!draft && (draft.fingerprint !== assessment.fingerprint || draft.revision !== assessment.review_revision || draft.dimensions.length !== assessment.dimensions.length || !!draft.audioBinding && !audioBinding || writingStale)
  const locked = loading || busy || storageFailed || !!draft?.pending || stale

  function persist(next: Recovery): boolean {
    if (!active.current) return false
    try { localStorage.setItem(key(child.child_id), JSON.stringify(next)) }
    catch { setStorageFailed(true); setError('无法保存本机审核草稿，新审核尚未发送。'); return false }
    saved.current = next; setRecovery(next); return true
  }
  function edit(patch: Partial<Draft>) {
    if (!assessment || !draft || locked) return
    persist({ ...saved.current, drafts: { ...saved.current.drafts, [assessment.assessment_id]: { ...draft, ...patch } } })
  }
  function report(problem: unknown) {
    if (!active.current) return
    const status = problem instanceof ApiError ? problem.status : undefined
    setExpired(status === 401 || status === 403)
    setError(status === 409 ? '原回答或审核版本已变化。请核对最新记录，旧决定不会自动用于新版本。'
      : status === 401 || status === 403 ? '家长授权已失效；未确认的操作仍保留。'
      : status === 422 ? '审核内容未被接受，请核对评分、原话摘录与条件。'
      : '请求结果尚未核对。请刷新记录或用原操作重试。')
  }
  function local(value: unknown): asserts value is Local {
    const result = value as Partial<Local> | null
    if (!result || result.local_only !== true || result.cloud_transmission_allowed !== false) throw new ApiError('非本地评分响应', 409)
  }
  function validateAssessment(value: Assessment) {
    local(value)
    if (value.child_id !== child.child_id || !Array.isArray(value.dimensions) || !Array.isArray(value.criteria)) throw new ApiError('评分归属不一致', 409)
  }
  function validateQualification(value: Qualification, owner: Assessment) {
    local(value)
    if (value.assessment_id !== owner.assessment_id || value.automatic_certification !== false
      || typeof value.eligible_for_mastery_gate !== 'boolean'
      || value.score_qualified !== undefined && typeof value.score_qualified !== 'boolean'
      || value.receipt && (value.receipt.assessment_id !== owner.assessment_id || value.receipt.review_revision !== owner.review_revision)
      || (value.eligible_for_mastery_gate || value.score_qualified) && !value.receipt) throw new ApiError('评分资格归属或版本不一致', 409)
    if (value.original_audio_dimensions !== undefined) validateAudioQualification(value.original_audio_dimensions, owner)
  }
  function validateDetail(result: Detail, id: string) {
    local(result); validateAssessment(result.assessment); validateQualification(result.qualification, result.assessment)
    if (result.assessment.assessment_id !== id || result.qualification.assessment_id !== id || !Array.isArray(result.history)
      || result.history.some(e => e.assessment_id !== id)) throw new ApiError('回答版本绑定不一致', 409)
    result.history.forEach(local)
    if (result.audio_review) {
      local(result.audio_review)
      if (!Array.isArray(result.audio_review.bindings) || !Array.isArray(result.audio_review.dimensions)) throw new ApiError('原音绑定缺失', 409)
      result.audio_review.bindings.forEach(b => { local(b); if (b.child_id !== child.child_id || b.assessment_id !== id) throw new ApiError('原音归属不一致', 409) })
    }
    if (result.writing_review) {
      const review = result.writing_review; local(review)
      if (typeof review.required !== 'boolean' || !Array.isArray(review.blockers)) throw new ApiError('原稿审核合同缺失', 409)
      if (review.binding) {
        const b = review.binding; local(b); local(b.original)
        if (!review.required || result.audio_review?.required || b.profile !== review.profile || b.assessment_id !== id
          || b.assessment_fingerprint !== result.assessment.fingerprint || !b.diagnostic_item_id || b.diagnostic_item_id !== result.assessment.material?.diagnostic_item_id
          || !b.binding_version || !b.original.artifact_id || !b.original.source_version || !b.original.response_hash
          || !['native_editor', 'confirmed_document'].includes(b.original.kind)
          || !Array.isArray(b.task_points) || !b.task_points.length || b.task_points.some(v => typeof v !== 'string' || !v.trim())
          || new Set(b.task_points).size !== b.task_points.length) throw new ApiError('原稿与当前题目或回答绑定不一致', 409)
      }
    }
    const receipt = result.qualification.receipt
    if (Math.max(0, ...result.history.map(e => e.review_revision)) !== result.assessment.review_revision
      || receipt && (receipt.assessment_id !== id || receipt.review_revision !== result.assessment.review_revision)
      || result.qualification.eligible_for_mastery_gate && !receipt) throw new ApiError('审核历史与资格版本尚未一致', 409)
  }
  async function readDetail(id: string): Promise<Detail> {
    const result = await request(`${root}/${encodeURIComponent(id)}`) as Detail
    validateDetail(result, id)
    return result
  }
  async function refresh(offset = saved.current.offset, focus = saved.current.focus, nonce?: string) {
    const current = ++sequence.current
    setLoading(true)
    if (nonce) { setDetail(null); setFocusReady(''); setFocusFailure(null); setError('') }
    try {
      const list = await request(`${root}?offset=${offset}&limit=20`) as Listing
      local(list)
      if (list.child_id !== child.child_id || !Array.isArray(list.items)) throw new ApiError('孩子归属不一致', 409)
      list.items.forEach(item => {
        validateAssessment(item)
        if (item.qualification) validateQualification(item.qualification, item)
      })
      const id = focus || list.items[0]?.assessment_id
      const next = id ? await readDetail(id) : null
      if (!active.current || sequence.current !== current) return
      setListing(list); setDetail(next)
      if (!storageFailed) persist({ ...saved.current, offset, focus: id ?? '', drafts: next && !saved.current.drafts[next.assessment.assessment_id]
        ? { ...saved.current.drafts, [next.assessment.assessment_id]: blank(effective(next), next.writing_review) } : saved.current.drafts })
      if (nonce) {
        if (!next || next.assessment.source_status !== 'current' || next.assessment.score_status === 'withdrawn') {
          setFocusFailure({ nonce, reason: '原评分或题源已撤回、变化；仅保留原记录供核对，未替换为其他回答。' })
        } else setFocusReady(nonce)
      }
    } catch (problem) {
      if (active.current && sequence.current === current) {
        report(problem)
        if (nonce) setFocusFailure({ nonce, reason: '无法核对指定评分记录：可能无权限、记录缺失或归属不符。未加载其他回答。' })
      }
    }
    finally { if (active.current && sequence.current === current) setLoading(false) }
  }
  useEffect(() => {
    active.current = true
    const handler = (event: Event) => { if ((event as CustomEvent<string>).detail === child.child_id) { active.current = false; sequence.current++ } }
    window.addEventListener(cleared, handler)
    if (!focusRequest) void refresh()
    return () => { active.current = false; sequence.current++; focusAttempt.current = ''; window.removeEventListener(cleared, handler) }
    // Child identity is isolated by the keyed outer panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request])
  useEffect(() => {
    if (!focusRequest || busy || focusAttempt.current === focusRequest.nonce) return
    focusAttempt.current = focusRequest.nonce
    if (!focusRequest.objectId.trim()) {
      ++sequence.current; setDetail(null); setLoading(false)
      setFocusFailure({ nonce: focusRequest.nonce, reason: '评分对象未绑定，未选择其他回答。' })
      return
    }
    void refresh(saved.current.offset, focusRequest.objectId, focusRequest.nonce)
    // A new nonce is a new navigation, not a new review or draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest?.nonce, focusRequest?.objectId, request, busy])
  async function focus(id: string) {
    setDetail(null)
    await refresh(saved.current.offset, id)
  }
  async function submit(body: ReviewBody) {
    if (!assessment || !draft || writing.current || storageFailed || !active.current) return
    const id = assessment.assessment_id
    if (!persist({ ...saved.current, drafts: { ...saved.current.drafts, [id]: { ...draft, pending: body } } })) return
    writing.current = true; setBusy(true); setError(''); setExpired(false); ++sequence.current
    try {
      const result = await request(`${root}/${encodeURIComponent(id)}/review`, { method: 'POST', body: JSON.stringify(body) }) as Detail & { event: ReviewEvent }
      validateDetail(result, id); local(result.event)
      // Idempotent replay returns the original event plus the CURRENT projection.
      // An old accepted review must never replace a later retraction in the UI.
      if (result.event.assessment_id !== id || result.event.review_revision !== body.expected_revision + 1
        || result.event.operation !== body.operation || result.event.reason !== body.reason) throw new ApiError('原审核回执无法核对')
      if (!active.current) return
      setDetail(result)
      persist({ ...saved.current, drafts: { ...saved.current.drafts, [id]: blank(effective(result), result.writing_review) } })
      await refresh()
    } catch (problem) {
      report(problem)
      if (active.current && problem instanceof ApiError && problem.status === 409) await refresh()
    } finally { writing.current = false; if (active.current) setBusy(false) }
  }
  const quoteValid = (value: string, source: string | null | undefined, required = false) => value ? !!source?.includes(value) && !!value.trim() : !required
  const dimensionValid = !!assessment && !!draft && draft.dimensions.length === assessment.dimensions.length && assessment.dimensions.every((d, i) => {
    const form = draft.dimensions[i]; const number = Number(form.score)
    const passage = assessment.material?.stimulus_text
    const audioDimension = audioRequired && detail?.audio_review?.bindings.some(b => b.dimensions_required.includes(d.dimension_id))
    return form.score !== '' && Number.isInteger(number) && number >= 0 && number <= d.max_score
      && !!form.rationale.trim() && quoteValid(form.quote, assessment.response, number > 0 && !audioDimension)
      && (!audioDimension || !!audioBinding && !!form.heard?.trim() && form.start !== '' && form.start != null && form.end !== '' && form.end != null && Number(form.start) >= 0 && Number(form.end) > Number(form.start) && Math.round(Number(form.end) * audioBinding.audio.sample_rate) <= audioBinding.audio.frames)
      && quoteValid(form.sourceQuote, passage || assessment.question, requiresMaterialQuote(assessment, d.dimension_id, number))
  })
  const criteriaValid = !!assessment && !!draft && draft.criteria.length === assessment.criteria.length && draft.criteria.some(v => !!v.trim()) && draft.criteria.every(v => quoteValid(v, assessment.response))
  const approvalValid = !locked && !!assessment && !!draft && assessment.source_status === 'current' && !assessment.material?.stimulus_asset && dimensionValid && criteriaValid
    && visualReady
    && (!audioRequired || !!requestAudio && !!audioBinding && heardBinding === audioBinding.binding_version)
    && (!writingRequired || !!writingBinding && draft.taskQuotes?.length === writingBinding.task_points.length && draft.taskQuotes.every(v => quoteValid(v, assessment.response, true)))
    && ['yes', 'no'].includes(draft.independent) && draft.hint !== '' && Number.isInteger(Number(draft.hint)) && Number(draft.hint) >= 0 && Number(draft.hint) <= 5 && !!draft.reason.trim()
  function approve() {
    if (!approvalValid || !draft || !assessment) return
    void submit({ expected_revision: draft.revision, client_action_id: crypto.randomUUID(), operation: 'approve', reason: draft.reason.trim(),
      independent: draft.independent === 'yes', hint_level: Number(draft.hint),
      ...(visual && visualEvidence ? { visual_evidence: visualEvidence } : {}),
      ...(writingRequired && writingBinding ? { review_profile: writingBinding.profile, writing_evidence: { binding_version: writingBinding.binding_version,
        task_point_evidence: writingBinding.task_points.map((task_point, i) => ({ task_point, response_quote: draft.taskQuotes![i] })) } } : {}),
      ...(audioRequired && audioBinding ? { review_profile: 'pet_speaking_audio_v1' as const, audio_evidence: { media_id: audioBinding.media_id,
        review: { schema: 'pet_speaking_audio_references_v1' as const, binding_version: audioBinding.binding_version, audio_sha256: audioBinding.audio.sha256,
          observations: assessment.dimensions.flatMap((d, i) => audioBinding.dimensions_required.includes(d.dimension_id) ? [{ dimension_id: d.dimension_id,
            start_frame: Math.round(Number(draft.dimensions[i].start) * audioBinding.audio.sample_rate), end_frame: Math.round(Number(draft.dimensions[i].end) * audioBinding.audio.sample_rate),
            heard_text: draft.dimensions[i].heard!.trim(), rationale: draft.dimensions[i].rationale.trim() }] : []) } } } : {}),
      dimensions: assessment.dimensions.map((d, i) => ({ dimension_id: d.dimension_id, score: Number(draft.dimensions[i].score),
        response_quotes: draft.dimensions[i].quote ? [draft.dimensions[i].quote] : [], rationale: draft.dimensions[i].rationale.trim(),
        ...(draft.dimensions[i].sourceQuote ? { source_quotes: [draft.dimensions[i].sourceQuote] } : {}) })),
      criterion_evidence: assessment.criteria.flatMap((criterion, i) => draft.criteria[i] ? [{ criterion, response_quote: draft.criteria[i] }] : []) })
  }
  function resetDraft() {
    if (!assessment) return
    if (persist({ ...saved.current, drafts: { ...saved.current.drafts, [assessment.assessment_id]: blank(assessment, detail?.writing_review) } })) setError('')
  }
  const pendingElsewhere = Object.entries(recovery.drafts).find(([id, value]) => value.pending && id !== assessment?.assessment_id)
  return <section ref={rootRef} className="rubric-review" aria-label={`${child.nickname}的开放题评分审核`} aria-busy={busy || loading}>
    <style>{style}</style><header><div><h2>开放题评分审核</h2><span className="rr-muted">{child.nickname} · {child.grade}</span></div>
      <button aria-label="刷新评分审核" title="刷新评分审核" disabled={busy || loading} onClick={() => { setError(''); void refresh() }}><RefreshCw /></button></header>
    {loading && <p role="status">正在读取回答与审核记录…</p>}
    {error && <div role="alert" className="rr-notice rr-error">{error}{expired && authneeded && <div className="rr-actions"><button onClick={authneeded}><ShieldCheck />重新解锁家长操作</button></div>}</div>}
    {storageFailed && <div className="rr-notice">本机审核草稿无法恢复或保存，提交已锁定。<div className="rr-actions"><button onClick={() => clearLearningRubricLocalState(child.child_id)}><X />清除损坏草稿并重新读取</button></div></div>}
    {pendingElsewhere && <button disabled={busy} onClick={() => void focus(pendingElsewhere[0])}><RefreshCw />查看另一份待核对审核</button>}
    <div className="rr-queue">{listing?.items.map(listed => {
      const selected = detail?.assessment.assessment_id === listed.assessment_id ? detail : null
      const item = selected?.assessment || listed
      const qualified = currentCalibrated(item, selected ? selected.qualification : listed.qualification)
      return <button key={item.assessment_id} disabled={busy || loading || !!draft?.pending} aria-pressed={assessment?.assessment_id === item.assessment_id} onClick={() => void focus(item.assessment_id)}>
        <span className="rr-question">{item.subject} · {item.question || '原题缺失'}</span><span className="rr-status">{qualified ? '已校准' : item.score_status === 'reviewed_human' ? '已审' : item.score_status === 'withdrawn' ? '已撤回' : '待核对'} · 第{item.review_revision}次审核</span></button>
    })}</div>
    {listing && <div className="rr-actions"><button aria-label="上一页评分" title="上一页评分" disabled={locked || listing.offset === 0} onClick={() => { setDetail(null); void refresh(Math.max(0, listing.offset - 20), '') }}><ArrowLeft /></button>
      <span>{listing.total ? `${listing.offset + 1}至${Math.min(listing.offset + listing.items.length, listing.total)} / ${listing.total}` : '暂无待审核回答'}</span>
      <button aria-label="下一页评分" title="下一页评分" disabled={locked || listing.offset + listing.items.length >= listing.total} onClick={() => { setDetail(null); void refresh(listing.offset + 20, '') }}><ArrowRight /></button></div>}
    {assessment && draft && <>
      {draft.pending && <div className="rr-notice">有一笔审核待核对，原始决定和操作编号已保留。<div className="rr-actions"><button disabled={busy || storageFailed} onClick={() => void submit(draft.pending!)}><RefreshCw />重试原审核</button><button disabled={busy} onClick={resetDraft}><X />放弃旧请求并重新核对</button></div></div>}
      {stale && <div className="rr-notice">本机草稿绑定旧审核版本；不会自动继承到当前回答。<button disabled={busy} onClick={resetDraft}><Undo2 />放弃旧草稿并核对新版</button></div>}
      <div className="rr-band rr-columns"><div className="rr-evidence-pane" data-work-object={assessment.assessment_id}><h3>原题与原回答</h3><h4>题目</h4><p className="rr-text">{assessment.question || '原题未绑定'}</p>
        {writingRequired && <><h4>本次书面原稿</h4>{writingBinding ? <>
          <p>{assessment.subject}原稿人工核对 · {writingBinding.original.kind === 'confirmed_document' ? '已确认的纸稿文字' : '本机输入原稿'}</p>
          <p className="rr-muted">按本题学科标准核对，不自动推断掌握或考试达标。</p>
        </> : <p role="alert" className="rr-notice">缺少当前原稿与本次作答的有效绑定，或审核类型尚不支持；不能替用通用文字评分认证。</p>}</>}
        {audioRequired && <><h4>本次口语原音</h4><label>选择原始录音<select disabled={locked} value={draft.audioBinding?.media_id ?? ''} onChange={e => {
          const binding = detail?.audio_review?.bindings.find(b => b.media_id === e.target.value)
          setHeardBinding(''); edit({ audioBinding: binding ? { media_id: binding.media_id, binding_version: binding.binding_version } : undefined })
        }}><option value="">请选择本次录音</option>{detail?.audio_review?.bindings.map((b, i) => <option key={b.media_id} value={b.media_id}>录音{i + 1} · {(b.audio.frames / b.audio.sample_rate).toFixed(1)}秒</option>)}</select></label>
        {!detail?.audio_review?.bindings.length && <p role="alert" className="rr-notice">缺少当前题目与回答的有效原音，口语审核已阻塞。</p>}
        {!requestAudio && <p role="alert" className="rr-notice">本地原音读取尚未接通，不能仅凭文字认证口语。</p>}
        {audioBinding && requestAudio && <OriginalAudio key={audioBinding.binding_version} binding={audioBinding} path={`${root}/${encodeURIComponent(assessment.assessment_id)}/audio?media_id=${encodeURIComponent(audioBinding.media_id)}&audio_sha256=${audioBinding.audio.sha256}`} request={requestAudio} onHeard={() => setHeardBinding(audioBinding.binding_version)} />}
        <p className="rr-muted">仅本机人工审核；原音、转写及波形均不代表自动发音评分或PET考试认证。</p></>}
        {assessment.source_kind === 'exam_open_review' && assessment.exam_slot
          && Number.isInteger(assessment.exam_slot.position) && assessment.exam_slot.position > 0
          && <p className="rr-notice">整卷第{assessment.exam_slot.position}题原作答审核。评分会更新本卷参考结果，后续仍需独立复测。</p>}
        <h4>孩子原回答</h4><p className="rr-text">{assessment.response}</p><p className="rr-muted">{assessment.responded_at ? new Date(assessment.responded_at).toLocaleString('zh-CN') : '回答时间未确认'}</p>
        {visual && <RubricVisualReview key={visualOwner}
          childId={child.child_id} assessmentId={assessment.assessment_id} fingerprint={assessment.fingerprint} visual={visual}
          onEvidence={evidence => setVisualReceipt({ owner: visualOwner, evidence })} />}
        <h4>题目材料</h4>{assessment.material?.stimulus_text ? <textarea className="rr-text" aria-label="材料原文" readOnly rows={6}
          style={{ width: '100%', boxSizing: 'border-box' }} value={assessment.material.stimulus_text} onSelect={event => {
            const input = event.currentTarget
            const quote = input.value.slice(input.selectionStart, input.selectionEnd)
            setSelectedMaterial(quote.trim() && quote.length <= 2000 ? { assessmentId: assessment.assessment_id,
              fingerprint: assessment.fingerprint, passage: assessment.material.stimulus_text!, quote } : null)
          }} /> : <p className="rr-muted">暂无已绑定的文本材料。</p>}
        {assessment.material?.stimulus_asset && <p className="rr-notice">题目含图片材料，尚未接入本地图片核对，新的评分审核暂已锁定；已有审核仍可撤回。</p>}
        {assessment.source_status !== 'current' && <p className="rr-notice">原题、回答或来源已变化；这份旧记录不能重新认证，已有审核仍可撤回。</p>}
        {assessment.dimensions.length > 0 && <details><summary>{calibrated ? '本次模型评分' : '待核实的候选评分'}</summary>{assessment.dimensions.map((d, i) => <p key={d.dimension_id}>{names[d.dimension_id] || `维度${i + 1}`}：{d.score ?? '未评分'}{d.rationale !== 'not_assessed' && ` · ${d.rationale}`}</p>)}<p className="rr-muted">{calibrated ? '校准仅适用于本次题目、回答与评分版本，不代替独立作答和后续复测。' : '候选评分尚未构成人工审核结论。'}</p></details>}
        {calibrated && <p>本次模型评分已通过校准核验。</p>}
        {detail?.qualification.original_audio_dimensions && <section aria-label="原音维度评分">
          <h4>原音维度评分</h4>
          {detail.qualification.original_audio_dimensions.score_qualified ? <>
            <p>本次原音的以下维度已通过校准核验。</p>
            <ul>{detail.qualification.original_audio_dimensions.covered_dimensions!.map(id =>
              <li key={id}>{names[id]}：{detail.qualification.original_audio_dimensions!.dimensions![id].score} / 3分</li>)}</ul>
          </> : <p>本次原音尚无可用的校准评分。</p>}
          <p className="rr-muted">校准仅适用于列出的原音维度；英语内容、整题得分、独立掌握和考试认证仍需各自证据。</p>
        </section>}
        <h4>当前评分资格</h4><p>{detail?.qualification.eligible_for_mastery_gate ? '这份评分符合进入掌握门槛的条件。' : '这份评分当前不能用于独立掌握门槛。'}</p><p className="rr-muted">评分审核不等于已掌握；不同题目、实际独立性与延迟复测仍须分别满足。</p>
      </div><div><h3>{calibrated ? '家长复核与纠正' : statuses[assessment.score_status] || '待核对评分状态'}</h3>
        {assessment.source_kind === 'local_criteria_mapping' && <p className="rr-notice">本地评分已有结果，本次仅核对它与目标条目的对应依据，不另造维度或重新评分。</p>}
        {assessment.dimensions.map((d, index) => {
          const name = names[d.dimension_id] || `维度${index + 1}`; const form = draft.dimensions[index]
          if (!form) return null
          const change = (patch: Partial<DimensionDraft>) => edit({ dimensions: draft.dimensions.map((v, i) => i === index ? { ...v, ...patch } : v) })
          return <fieldset key={d.dimension_id} disabled={locked || assessment.source_status !== 'current'}><legend>{name}</legend><p>{d.criterion}</p>
            <label>{name}评分<select value={form.score} onChange={e => change({ score: e.target.value })}><option value="">尚未判断</option>{Array.from({ length: d.max_score + 1 }, (_, number) => <option key={number} value={number}>{number}分</option>)}</select></label>
            <p className="rr-anchors">满足本维度要求：至少{d.pass_score}分</p>
            {form.score !== '' && <p className="rr-anchors">{d.score_anchors[form.score]}</p>}
            {audioRequired && ['audio_text_alignment', 'intelligibility', 'pronunciation', 'prosody', 'fluency'].includes(d.dimension_id) && <>
              <label>{name}片段起点（秒）<input type="number" min="0" step="0.001" value={form.start ?? ''} onChange={e => change({ start: e.target.value })} /></label>
              <label>{name}片段终点（秒）<input type="number" min="0" step="0.001" value={form.end ?? ''} onChange={e => change({ end: e.target.value })} /></label>
              <label>{name}实际听到的内容<textarea maxLength={800} value={form.heard ?? ''} onChange={e => change({ heard: e.target.value })} /></label>
            </>}
            <label>{name}回答摘录<textarea maxLength={2000} value={form.quote} onChange={e => change({ quote: e.target.value })} /></label>
            {!quoteValid(form.quote, assessment.response) && <p className="rr-notice">回答摘录必须与这次孩子原话一致。</p>}
            {(assessment.material?.stimulus_text || d.dimension_id === 'evidence') && <label>{name}材料摘录<textarea maxLength={2000} aria-required={requiresMaterialQuote(assessment, d.dimension_id, Number(form.score))} value={form.sourceQuote} onChange={e => change({ sourceQuote: e.target.value })} /></label>}
            {assessment.material?.stimulus_text && d.dimension_id === 'evidence' && <button type="button" title="将所选原文填入本维度的材料摘录" disabled={locked || !selectedQuote || assessment.source_status !== 'current'}
              onClick={() => { if (selectedQuote) change({ sourceQuote: selectedQuote }) }}><Quote />引用选中材料</button>}
            {requiresMaterialQuote(assessment, d.dimension_id, Number(form.score)) && <p className="rr-muted">非零分须有可核对的材料原文依据，回答摘录不能代替材料摘录。</p>}
            {!quoteValid(form.sourceQuote, assessment.material?.stimulus_text || assessment.question) && <p className="rr-notice">材料摘录与绑定原文不一致。</p>}
            <label>{name}评分理由<textarea maxLength={800} value={form.rationale} onChange={e => change({ rationale: e.target.value })} /></label>
          </fieldset>
        })}
        {writingRequired && writingBinding && <><h4>原稿任务点依据</h4>{writingBinding.task_points.map((point, index) => <div key={point}><p>{index + 1}. {point}</p>
          <label>原稿任务点{index + 1}摘录<textarea maxLength={2000} value={draft.taskQuotes?.[index] ?? ''} disabled={locked || assessment.source_status !== 'current'}
            onChange={e => edit({ taskQuotes: writingBinding.task_points.map((_, i) => i === index ? e.target.value : draft.taskQuotes?.[i] ?? '') })} /></label>
          {!quoteValid(draft.taskQuotes?.[index] ?? '', assessment.response, true) && <p className="rr-notice">需保留这份原稿的实际摘录；不符合任务要求时应如实给分，不补写孩子未作答的内容。</p>}
        </div>)}</>}
        <h4>{assessment.source_kind === 'exam_open_review' ? '题目任务点依据' : '目标对应依据'}</h4>{assessment.criteria.map((criterion, index) => <div key={`${index}:${criterion}`}><p>{index + 1}. {criterion}</p>
          <label>目标{index + 1}回答摘录<textarea maxLength={2000} disabled={locked || assessment.source_status !== 'current'} value={draft.criteria[index] ?? ''} onChange={e => edit({ criteria: draft.criteria.map((v, i) => i === index ? e.target.value : v) })} /></label>
          {!quoteValid(draft.criteria[index] ?? '', assessment.response) && <p className="rr-notice">目标依据必须来自这次原回答。</p>}</div>)}
        {!assessment.criteria.length && <p className="rr-notice">未绑定目标条目，暂不能提交审核。</p>}
        <label>独立完成情况<select value={draft.independent} disabled={locked || assessment.source_status !== 'current'} onChange={e => edit({ independent: e.target.value })}><option value="">尚未确认</option><option value="yes">观察到独立完成</option><option value="no">观察到有帮助或非独立完成</option></select></label>
        <label>提示等级<select value={draft.hint} disabled={locked || assessment.source_status !== 'current'} onChange={e => edit({ hint: e.target.value })}><option value="">尚未确认</option>{Array.from({ length: 6 }, (_, n) => <option key={n} value={n}>{n === 0 ? '0 · 无提示' : `${n} · 提示等级${n}`}</option>)}</select></label>
        <label>审核说明<textarea value={draft.reason} maxLength={2000} disabled={locked} onChange={e => edit({ reason: e.target.value })} /></label>
        <div className="rr-actions"><button className="rr-primary" disabled={!approvalValid} onClick={approve}><Save />{assessment.review?.operation === 'approve' ? '保存评分修订' : '保存评分审核'}</button>
          {assessment.review?.operation === 'approve' && <button className="rr-danger" disabled={locked || !draft.reason.trim()} onClick={() => void submit({ expected_revision: draft.revision, client_action_id: crypto.randomUUID(), operation: 'retract', reason: draft.reason.trim() })}><Undo2 />撤回评分审核</button>}</div>
      </div></div>
      <div className="rr-band"><h3>持久审核记录</h3>{!detail?.history.length && <p className="rr-muted">尚无人工审核记录。</p>}<ol className="rr-history">{detail?.history.map(event => <li key={event.review_revision}><strong>第{event.review_revision}次 · {event.operation === 'retract' ? '撤回评分' : '人工评分审核'}</strong><span className="rr-muted"> · {new Date(event.created_at).toLocaleString('zh-CN')}</span><p>{event.reason}</p>
        {event.operation === 'approve' && <><p>独立完成：{event.independent ? '是' : '否'} · 提示等级：{event.hint_level}</p>{event.dimensions?.map((d, i) => <div key={d.dimension_id}><p>{names[d.dimension_id] || `维度${i + 1}`}：{d.score}分 · {d.rationale}</p><blockquote className="rr-text">{d.response_quotes.join('；')}</blockquote>{!!d.source_quotes?.length && <p>材料依据：{d.source_quotes.join('；')}</p>}</div>)}
          {event.audio_evidence && <><p>原音维度审核 · 原始音频版本已绑定</p>{event.audio_evidence.review.observations.map(o => <p key={o.dimension_id}>{names[o.dimension_id]}：{o.heard_text} · {o.rationale}</p>)}</>}
          {event.writing_evidence && <><p>{event.review_profile === 'school_open_written_v1' ? '普通学科原稿审核' : event.review_profile === 'pet_writing_text_v1' ? '英语写作原稿审核' : '原稿审核'} · 当时原稿版本已绑定</p>
            {event.writing_evidence.task_point_evidence.map((e, index) => <div key={index}><p>任务点：{e.task_point}</p><blockquote className="rr-text">{e.response_quote}</blockquote></div>)}</>}
          {event.criterion_evidence?.map((e, index) => <div key={index}><p>目标：{e.criterion}</p><blockquote className="rr-text">{e.response_quote}</blockquote></div>)}</>}
      </li>)}</ol></div>
    </>}
  </section>
}
