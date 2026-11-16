import { requestParentLocal } from './api'
import { hasPrivacyIntent } from './privacyOperationRecovery'
import type { CurriculumStageContext, CurriculumStageReview } from './curriculumSupplyTypes'
import type { SpecializedSupplyCandidate } from './types'

export const specializedReviewKey = (child: string): string => `hsc-specialized-review:${child}`
export const specializedReviewCleared = 'hsc-specialized-review-cleared'
export type ReviewBody = {
  decision: 'approve' | 'reject'; note: string; client_action_id: string
  expected_binding_version?: string; stage_review?: CurriculumStageReview
}
export type ReviewDraft = {
  candidateId: string; title: string; note: string; binding: string
  stage: '' | CurriculumStageReview['stage']; sourceQuote: string; candidateQuote: string
  sameSkill: string; changedContext: string; pending: ReviewBody | null
}
type Journal = { version: 1; childId: string; entries: ReviewDraft[] }
export type StoredReviews = { raw: string | null; value: Journal }
export type ReviewCandidate = SpecializedSupplyCandidate & { review_action_id?: string | null }
export type ReviewContext = CurriculumStageContext & { candidate_expected_answer: SpecializedSupplyCandidate['expected_answer'] }
const text = (v: unknown, max = 1200): v is string => typeof v === 'string' && v.length <= max
const id = (v: unknown): v is string => text(v, 200) && !!v.trim()
const stage = (v: unknown): boolean => v === 'near_transfer' || v === 'far_transfer'
const fail = (): Error => new Error('本机审核记录无法安全保存或核对，未发送审核。请保留原记录并检查本机存储。')
export function assertReviewAvailable(signal?: AbortSignal): void {
  if (signal?.aborted || hasPrivacyIntent()) throw new Error('审核已暂停，原记录未重发。')
}
function validBody(b: ReviewBody): boolean {
  return !!b && ['approve', 'reject'].includes(b.decision) && text(b.note, 500)
    && id(b.client_action_id) && b.client_action_id.length >= 8
    && (b.decision === 'approve' ? id(b.expected_binding_version) : b.expected_binding_version === undefined)
    && Object.keys(b).every(k => ['decision', 'note', 'client_action_id', 'expected_binding_version', 'stage_review'].includes(k))
    && (b.stage_review === undefined || (b.decision === 'approve' && b.stage_review.schema === 'specialized_stage_review_v1'
      && stage(b.stage_review.stage) && id(b.stage_review.binding_version)
      && b.expected_binding_version === b.stage_review.binding_version
      && [b.stage_review.source_quote, b.stage_review.candidate_quote, b.stage_review.same_skill_rationale,
        b.stage_review.changed_context_rationale].every(v => text(v) && !!v.trim())))
}
export function readSpecializedReviews(child: string): StoredReviews {
  const raw = localStorage.getItem(specializedReviewKey(child))
  if (raw === null) return { raw, value: { version: 1, childId: child, entries: [] } }
  if (raw.length > 2000000) throw fail()
  const v = JSON.parse(raw) as Journal
  if (!v || v.version !== 1 || v.childId !== child || !Array.isArray(v.entries) || v.entries.length > 200
    || v.entries.some(d => !d || !id(d.candidateId) || !text(d.title, 10000) || !text(d.note, 500)
      || !text(d.binding, 200) || (d.stage !== '' && !stage(d.stage))
      || ![d.sourceQuote, d.candidateQuote, d.sameSkill, d.changedContext].every(v => text(v))
      || (d.pending !== null && (!validBody(d.pending) || d.pending.note !== d.note
        || (d.pending.decision === 'approve' && d.pending.expected_binding_version !== d.binding))))
    || new Set(v.entries.map(d => d.candidateId)).size !== v.entries.length) throw fail()
  return { raw, value: v }
}
export function saveSpecializedReviews(child: string, previous: StoredReviews, entries: ReviewDraft[]): StoredReviews {
  assertReviewAvailable()
  const key = specializedReviewKey(child)
  if (previous.value.childId !== child || localStorage.getItem(key) !== previous.raw) throw fail()
  const raw = JSON.stringify({ version: 1, childId: child, entries })
  if (raw.length > 2000000 || entries.length > 200) throw fail()
  localStorage.setItem(key, raw)
  if (localStorage.getItem(key) !== raw) throw fail()
  return readSpecializedReviews(child)
}
export function emptyReview(candidate: SpecializedSupplyCandidate): ReviewDraft {
  return { candidateId: candidate.candidate_id, title: candidate.prompt, note: '', binding: '', stage: '',
    sourceQuote: '', candidateQuote: '', sameSkill: '', changedContext: '', pending: null }
}
export function typedModelReview(candidate: SpecializedSupplyCandidate): boolean {
  return candidate.validation?.passed === true && candidate.validation.ruleset === 'specialized_closed_exact_v1'
    && candidate.expected_answer?.local_rule === 'local_exact_v1'
    && (candidate.expected_answer.answer_format === 'reviewed_exact_text_v1'
      || (candidate.expected_answer.answer_format === 'reviewed_spoken_numeric_v1'
        && ['integer', 'rational'].includes(String(candidate.expected_answer.numeric_kind))))
    && Array.isArray(candidate.expected_answer.exact_answers) && candidate.expected_answer.exact_answers.length > 0
    && candidate.expected_answer.exact_answers.every(a => typeof a === 'string' && !!a.trim())
}
export function sameReviewReference(a: unknown, b: unknown): boolean {
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) || Array.isArray(b)) return false
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>
  return Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(k =>
    Array.isArray(left[k]) ? Array.isArray(right[k]) && JSON.stringify(left[k]) === JSON.stringify(right[k]) : left[k] === right[k])
}
export async function readReviewContext(child: string, candidate: string, signal: AbortSignal): Promise<ReviewContext> {
  assertReviewAvailable(signal)
  const v = await requestParentLocal<ReviewContext>(`/v1/parent/children/${encodeURIComponent(child)}/specialized-supply/candidates/${encodeURIComponent(candidate)}/stage-review-context`, { cache: 'no-store', signal })
  assertReviewAvailable(signal)
  if (!v || v.child_id !== child || v.candidate_id !== candidate || !id(v.binding_version)
    || v.schema !== 'specialized_stage_review_v1' || v.local_only !== true || v.cloud_transmission_allowed !== false
    || v.qualification_scope !== 'explicit_human_source_stage_review_not_child_mastery'
    || !text(v.source_prompt, 20000) || !text(v.candidate_prompt, 20000) || !text(v.candidate_explanation, 20000)
    || !v.candidate_expected_answer || typeof v.candidate_expected_answer !== 'object' || Array.isArray(v.candidate_expected_answer)
    || !Array.isArray(v.allowed_stages) || v.allowed_stages.some(s => !stage(s))) throw fail()
  return v
}
function sameStage(a: unknown, b: CurriculumStageReview | undefined): boolean {
  if (b === undefined) return a == null
  if (!a || typeof a !== 'object') return false
  return Object.keys(a).length === Object.keys(b).length
    && Object.entries(b).every(([k, v]) => (a as Record<string, unknown>)[k] === v)
}
export function matchesReviewReceipt(row: ReviewCandidate, child: string, candidate: string, body: ReviewBody): boolean {
  const validation = row?.validation as unknown as Record<string, unknown> | undefined
  return !!row && row.child_id === child && row.candidate_id === candidate
    && row.review_action_id === body.client_action_id && row.review_note === body.note
    && row.status === (body.decision === 'approve' ? 'promoted' : 'rejected')
    && (body.decision !== 'approve' || !!row.diagnostic_item_id)
    && validation?.review_binding_version === body.expected_binding_version
    && sameStage(validation?.stage_review, body.stage_review)
}
export async function checkOriginalReview(child: string, candidate: string, body: ReviewBody, signal: AbortSignal): Promise<'saved' | 'unreviewed' | 'conflict'> {
  assertReviewAvailable(signal)
  const result = await requestParentLocal<{ items: ReviewCandidate[] }>(`/v1/parent/children/${encodeURIComponent(child)}/specialized-supply/candidates?status=`, { cache: 'no-store', signal })
  assertReviewAvailable(signal)
  if (!Array.isArray(result?.items) || result.items.some(row => row.child_id !== child)) throw fail()
  const rows = result.items.filter(row => row.candidate_id === candidate)
  if (rows.length !== 1) return 'conflict'
  if (matchesReviewReceipt(rows[0], child, candidate, body)) return 'saved'
  return rows[0].status === 'needs_review' && !rows[0].review_action_id ? 'unreviewed' : 'conflict'
}
export async function postOriginalReview(child: string, candidate: string, body: ReviewBody, signal: AbortSignal): Promise<void> {
  assertReviewAvailable(signal)
  if (!validBody(body)) throw fail()
  const row = await requestParentLocal<ReviewCandidate>(`/v1/parent/specialized-supply/candidates/${encodeURIComponent(candidate)}/review`,
    { method: 'POST', cache: 'no-store', body: JSON.stringify(body), signal })
  assertReviewAvailable(signal)
  if (!matchesReviewReceipt(row, child, candidate, body)) throw new Error('审核回执尚未核对，已保留原请求，请核对原审核。')
}
