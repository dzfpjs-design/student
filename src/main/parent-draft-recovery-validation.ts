import type { ParentDraftRecoverySnapshot, ParentDraftRecoveryScope } from '../shared/parentDraftRecovery'

export class ParentDraftRecoveryError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'ParentDraftRecoveryError' }
}
export function need(value: unknown, code = 'DRAFT_RECOVERY_INVALID_SNAPSHOT'): asserts value {
  if (!value) throw new ParentDraftRecoveryError(code)
}
const record = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v)
const keys = (v: unknown, required: string[], optional: string[] = []) => record(v) && required.every(k => Object.hasOwn(v, k)) && Object.keys(v).every(k => required.includes(k) || optional.includes(k))
const str = (v: unknown, max: number, min = 0): v is string => typeof v === 'string' && Array.from(v).length <= max && Array.from(v).length >= min
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0
export function identity(childId: unknown, scope: unknown): asserts childId is string {
  need(str(childId, 128, 1) && (scope === 'capture' || scope === 'language'), 'DRAFT_RECOVERY_INVALID_IDENTITY')
}
/** Inspect own descriptors, never invoke renderer getters/toJSON; reject non-JSON values. */
export function canonical(value: unknown): string {
  let nodes = 0
  const ancestors = new Set<object>()
  const visit = (v: unknown, depth: number): string => {
    need(++nodes <= 50000 && depth <= 20)
    if (v === null || typeof v === 'boolean' || typeof v === 'string') return JSON.stringify(v)
    if (typeof v === 'number') { need(Number.isFinite(v)); return JSON.stringify(v) }
    need(typeof v === 'object' && v !== null)
    need(!ancestors.has(v))
    need(Array.isArray(v) ? Object.getPrototypeOf(v) === Array.prototype : Object.getPrototypeOf(v) === Object.prototype || Object.getPrototypeOf(v) === null)
    ancestors.add(v)
    const descriptors = Object.getOwnPropertyDescriptors(v)
    need(Reflect.ownKeys(v).every(k => typeof k === 'string'))
    for (const [k, d] of Object.entries(descriptors)) {
      if (Array.isArray(v) && k === 'length') continue
      need(d.enumerable && Object.hasOwn(d, 'value') && !['__proto__', 'prototype', 'constructor'].includes(k))
    }
    let result: string
    if (Array.isArray(v)) {
      need(v.length <= 50000 && Object.keys(v).length === v.length && Object.keys(v).every((k, i) => k === String(i)))
      result = `[${v.map((_, i) => visit(descriptors[String(i)].value, depth + 1)).join(',')}]`
    } else {
      result = `{${Object.keys(v).sort().map(k => `${JSON.stringify(k)}:${visit(descriptors[k].value, depth + 1)}`).join(',')}}`
    }
    ancestors.delete(v)
    return result
  }
  return visit(value, 0)
}
function payload(v: any, scope: ParentDraftRecoveryScope): void {
  if (v === null) return
  need(keys(v, ['page', 'capture_ref', 'capture_version', 'review_drafts', 'action_ids'], ['objective_draft']))
  const pageFields = ['material_kind', 'title', 'ocr_text', 'grade_scope', 'subject_scope', 'textbook_version', 'volume', 'unit', 'lesson', 'page_number', 'source_ref']
  need(record(v.page) && Object.keys(v.page).every(k => pageFields.includes(k)))
  for (const [k, field] of Object.entries(v.page)) need(k === 'page_number' ? integer(field) && field >= 1 && field <= 100000 : str(field, k === 'ocr_text' ? 200000 : 600))
  need(!Object.hasOwn(v.page, 'material_kind') || ['textbook', 'workbook'].includes(v.page.material_kind))
  need(str(v.capture_ref, 80) && str(v.capture_version, 80) && Boolean(v.capture_ref) === Boolean(v.capture_version))
  need(record(v.review_drafts) && Object.keys(v.review_drafts).length <= 50)
  for (const [ref, item] of Object.entries(v.review_drafts)) need(str(ref, 80, 1) && keys(item, ['text', 'source_version']) && str((item as any).text, 200000) && str((item as any).source_version, 80, 1))
  need(record(v.action_ids) && Object.keys(v.action_ids).length <= 100)
  for (const [k, action] of Object.entries(v.action_ids)) need(str(k, 160, 1) && str(action, 128, 8))
  const o = v.objective_draft
  if (o !== undefined && o !== null) {
    need(scope === 'capture' && keys(o, ['question_number', 'full_question', 'official_answers', 'candidate_node_id', 'paper', 'answer_key'], ['raw_editing']))
    need(str(o.question_number, 40) && str(o.full_question, 1200) && Array.isArray(o.official_answers) && o.official_answers.length <= 24 && o.official_answers.every((x: unknown) => str(x, 200)))
    need(o.candidate_node_id === null || str(o.candidate_node_id, 120, 1))
    for (const role of ['paper', 'answer_key']) need(keys(o[role], ['capture_ref', 'source_version']) && str(o[role].capture_ref, 80, 1) && str(o[role].source_version, 80, 1))
    need(o.paper.capture_ref !== o.answer_key.capture_ref)
    if (Object.hasOwn(o, 'raw_editing')) {
      const raw = o.raw_editing
      need(keys(raw, ['full_question', 'official_answers_text']) && str(raw.full_question, 100000) && str(raw.official_answers_text, 100000))
      const answers = raw.official_answers_text.split('\n') as string[]
      const normalized = answers.length <= 24 && answers.every(x => Array.from(x).length <= 200) ? answers : []
      need(o.full_question === (Array.from(raw.full_question).length <= 1200 ? raw.full_question : '') && canonical(o.official_answers) === canonical(normalized))
    }
  }
  need(Buffer.byteLength(canonical(v), 'utf8') <= 1000000)
}
export function validateSnapshot(value: unknown, childId: string, scope: ParentDraftRecoveryScope): { snapshot: ParentDraftRecoverySnapshot; bytes: string } {
  identity(childId, scope)
  const bytes = canonical(value)
  need(Buffer.byteLength(bytes, 'utf8') <= 4 * 1024 * 1024, 'DRAFT_RECOVERY_TOO_LARGE')
  const v = JSON.parse(bytes)
  need(keys(v, ['schema', 'childId', 'scope', 'acknowledged', 'latest', 'pending']) && v.schema === 1 && v.childId === childId && v.scope === scope)
  payload(v.latest, scope)
  const a = v.acknowledged
  if (a !== null) {
    need(keys(a, ['child_id', 'scope', 'storage_generation', 'revision', 'payload', 'last_action_id', 'last_expected_revision', 'updated_at', 'stale_refs', 'cloud_transmission_allowed']))
    need(a.child_id === childId && a.scope === scope && str(a.storage_generation, 200, 8) && integer(a.revision) && a.cloud_transmission_allowed === false)
    need(a.last_action_id === null || str(a.last_action_id, 128, 8))
    need(a.last_expected_revision === null || integer(a.last_expected_revision))
    need(a.updated_at === null || str(a.updated_at, 200, 1))
    need(Array.isArray(a.stale_refs) && a.stale_refs.length <= 256 && a.stale_refs.every((x: unknown) => str(x, 1000)))
    payload(a.payload, scope)
  }
  if (v.pending !== null) {
    const p = v.pending
    need(keys(p, ['expected_revision', 'expected_generation', 'client_action_id', 'payload']))
    need(integer(p.expected_revision) && str(p.expected_generation, 200, 8) && str(p.client_action_id, 128, 8))
    need(a !== null && p.expected_revision === a.revision && p.expected_generation === a.storage_generation)
    payload(p.payload, scope)
  }
  return { snapshot: v, bytes }
}
export function isDirty(snapshot: ParentDraftRecoverySnapshot): boolean {
  return snapshot.pending !== null || canonical(snapshot.latest) !== canonical(snapshot.acknowledged?.payload ?? null)
}
