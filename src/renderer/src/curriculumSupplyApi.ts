import { ApiError } from './api'
import { parseOralSource, oralPictureSupported, sameOralSource } from './OralTaskPreview'
import { oralAuthorKey } from './oralSourceApi'
import type { CurriculumAudit, CurriculumDraftState, CurriculumOptions, CurriculumPackage, CurriculumParentRequest, CurriculumPending, CurriculumSelection, CurriculumSourceOptions } from './curriculumSupplyTypes'

export const curriculumLocalKey = (childId: string): string => `hsc-curriculum-review:${childId}`
export const curriculumStateClearedEvent = 'hsc-curriculum-review-cleared'
export function clearCurriculumSupplyLocalState(childId?: string): void {
  if (childId !== undefined) localStorage.removeItem(curriculumLocalKey(childId))
  else for (const key of Object.keys(localStorage)) if (key.startsWith('hsc-curriculum-review:')) localStorage.removeItem(key)
  clearWritingAuthorizationLocalState(childId)
  if(childId!==undefined)localStorage.removeItem(`hsc-paper-learning-selection-pending:${childId}`)
  else for(const key of Object.keys(localStorage))if(key.startsWith('hsc-paper-learning-selection-pending:'))localStorage.removeItem(key)
  if (childId !== undefined) localStorage.removeItem(oralAuthorKey(childId))
  else for (const key of Object.keys(localStorage)) if (key.startsWith('hsc-curriculum-oral-author:')) localStorage.removeItem(key)
  window.dispatchEvent(new CustomEvent(curriculumStateClearedEvent, { detail: childId }))
}

export const writingAuthorizationClearedEvent = 'hsc-writing-authorization-cleared'
export const writingAuthorizationLocalKey = (child: string, item: string): string =>
  `hsc-writing-authorization:${encodeURIComponent(child)}:${encodeURIComponent(item)}`
export function clearWritingAuthorizationLocalState(child?: string, item?: string): void {
  const prefix = child === undefined ? 'hsc-writing-authorization:' : `hsc-writing-authorization:${encodeURIComponent(child)}:`
  for (const key of Object.keys(localStorage)) {
    if (key.startsWith(prefix) && (item === undefined || key === writingAuthorizationLocalKey(child!, item))) localStorage.removeItem(key)
  }
  window.dispatchEvent(new CustomEvent(writingAuthorizationClearedEvent, { detail: { child, item } }))
}

export type WritingLicense = 'CC0-1.0' | 'original_author' | 'explicit_cloud_processing_permission'
export type WritingMaterial = { material_key: string; text: string; title: string; license_status: 'public'; source_version: string; text_hash: string }
type WritingLicenseBasis = { license_id: WritingLicense; reference: string; evidence: string }
export type WritingMaterialLicense = Pick<WritingMaterial, 'material_key' | 'source_version' | 'text_hash'> & { license_basis: WritingLicenseBasis }
export type WritingAuthorization = {
  child_id: string; diagnostic_item_id: string; revision: number
  status: 'authorization_required' | 'authorized' | 'invalidated' | 'blocked' | 'revoked'
  reason: string | null; item_version: string | null; source_version: string | null; curriculum_version: string | null
  purpose_version: 'writing_question_formative_text_v1' | 'writing_question_material_formative_text_v2' | null
  preview: { question: string; task_points: string[]; materials?: WritingMaterial[] } | null
  authorization_id: string | null; license_options: WritingLicense[]
  rights_review_authority: 'parent_declaration_not_legal_or_network_verification'
  local_only: true; cloud_transmission_allowed: false
}
export type WritingAuthorizationCommand = {
  child_id: string; diagnostic_item_id: string; kind: 'grant' | 'revoke'
  body: { expected_revision: number; client_action_id: string; item_version?: string; source_version?: string; curriculum_version?: string
    license_basis?: WritingLicenseBasis; material_license_bases?: WritingMaterialLicense[] }
}
const rights: WritingLicense[] = ['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission']
const boundId = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0 && value.length <= 200
const exactKeys = (value: object, keys: string[]) => Object.keys(value).sort().join('|') === [...keys].sort().join('|')
function validLicense(value: WritingLicenseBasis | undefined): boolean {
  return !!value && exactKeys(value, ['license_id', 'reference', 'evidence']) && rights.includes(value.license_id)
    && typeof value.reference === 'string' && !!value.reference.trim() && value.reference.length <= 1024
    && typeof value.evidence === 'string' && value.evidence.trim().length >= 20 && value.evidence.length <= 2000
}
export function validateWritingAuthorizationCommand(command: WritingAuthorizationCommand, child: string, item: string): void {
  if (command?.child_id !== child || command.diagnostic_item_id !== item) throw new ApiError('待提交授权不属于当前题目。', 409)
  const b = command.body
  const materials = b?.material_license_bases
  const keys = command.kind === 'grant'
    ? ['expected_revision', 'client_action_id', 'item_version', 'source_version', 'curriculum_version', 'license_basis',
      ...(materials !== undefined ? ['material_license_bases'] : [])] : ['expected_revision', 'client_action_id']
  if (!exactKeys(command, ['child_id', 'diagnostic_item_id', 'kind', 'body']) || !['grant', 'revoke'].includes(command.kind)
    || !b || !exactKeys(b, keys) || !Number.isInteger(b.expected_revision) || b.expected_revision < 0 || !boundId(b.client_action_id)
    || (command.kind === 'grant' && (![b.item_version, b.source_version, b.curriculum_version].every(boundId) || !validLicense(b.license_basis)))
    || (materials !== undefined && (!Array.isArray(materials) || materials.length < 1 || materials.length > 20
      || materials.some(m => !m || !exactKeys(m, ['material_key', 'source_version', 'text_hash', 'license_basis'])
        || ![m.material_key, m.source_version, m.text_hash].every(boundId) || !validLicense(m.license_basis))
      || new Set(materials.map(m => m.material_key)).size !== materials.length))) {
    throw new ApiError('本机授权请求无法核对，请放弃旧请求并重新确认。', 422)
  }
}
function authorizationView(value: unknown, child: string, item: string): WritingAuthorization {
  const v = value as WritingAuthorization
  if (!v || v.child_id !== child || v.diagnostic_item_id !== item || v.local_only !== true || v.cloud_transmission_allowed !== false
    || !Number.isInteger(v.revision) || v.revision < 0 || !['authorization_required', 'authorized', 'invalidated', 'blocked', 'revoked'].includes(v.status)
    || v.rights_review_authority !== 'parent_declaration_not_legal_or_network_verification'
    || !Array.isArray(v.license_options) || v.license_options.some(r => !rights.includes(r))
    || ![null, 'writing_question_formative_text_v1', 'writing_question_material_formative_text_v2'].includes(v.purpose_version)
    || ![v.item_version, v.source_version, v.curriculum_version, v.authorization_id, v.reason].every(x => x === null || typeof x === 'string')
    || (v.preview !== null && (!v.preview || typeof v.preview.question !== 'string' || !Array.isArray(v.preview.task_points)
      || v.preview.task_points.some(p => typeof p !== 'string') || !v.purpose_version
      || (v.purpose_version === 'writing_question_formative_text_v1' && v.preview.materials !== undefined)
      || (v.purpose_version === 'writing_question_material_formative_text_v2' && (!Array.isArray(v.preview.materials)
        || v.preview.materials.length < 1 || v.preview.materials.length > 20
        || v.preview.materials.some(m => !m || !exactKeys(m, ['material_key', 'text', 'title', 'license_status', 'source_version', 'text_hash'])
          || ![m.material_key, m.source_version, m.text_hash].every(boundId) || typeof m.text !== 'string' || !m.text.trim()
          || typeof m.title !== 'string' || m.license_status !== 'public')
        || new Set(v.preview.materials.map(m => m.material_key)).size !== v.preview.materials.length))))
    || (v.status === 'authorized' && (!v.authorization_id || !v.item_version || !v.source_version || !v.curriculum_version || !v.preview))) {
    throw new ApiError('用途授权响应无法核对，请刷新。', undefined, 'WRITING_AUTH_RESPONSE_INVALID')
  }
  return v
}
export function createWritingAuthorizationApi(request: CurriculumParentRequest) {
  const base = (child: string, item: string) => `/v1/parent/children/${encodeURIComponent(child)}/writing-question-authorizations/${encodeURIComponent(item)}`
  return {
    read: async (child: string, item: string) => authorizationView(await request(base(child, item), { cache: 'no-store' }), child, item),
    perform: async (child: string, item: string, command: WritingAuthorizationCommand) => {
      validateWritingAuthorizationCommand(command, child, item)
      const result = await request(`${base(child, item)}/${command.kind}`, { method: 'POST', cache: 'no-store', body: JSON.stringify(command.body) }) as {
        receipt: { event_id: string; revision: number; command: string }; current: unknown }
      if (!result?.receipt || result.receipt.command !== command.kind || !result.receipt.event_id
        || result.receipt.revision !== command.body.expected_revision + 1) throw new ApiError('操作回执无法核对，请重试原请求。')
      const current = authorizationView(result.current, child, item)
      if (current.revision < result.receipt.revision) throw new ApiError('当前授权早于操作回执，请刷新核对。')
      return current
    }
  }
}

export function loadCurriculumLocalState(childId: string): Partial<CurriculumDraftState> {
  const raw = localStorage.getItem(curriculumLocalKey(childId))
  if (!raw) return {}
  const value = JSON.parse(raw) as CurriculumDraftState
  if (!value || typeof value !== 'object') throw new Error('invalid curriculum recovery state')
  if (value.difficulty !== undefined && typeof value.difficulty !== 'string') throw new Error('invalid curriculum difficulty')
  for (const key of ['sourceId', 'semester', 'standardKey', 'excerptId', 'seedId', 'explanation', 'note', 'focusedId'] as const) {
    if (typeof value[key] !== 'string') throw new Error('invalid curriculum recovery field')
  }
  if (value.pending) {
    if (value.pending.child_id !== childId || !['select', 'generate', 'first-sources', 'validate', 'review', 'publish', 'withdraw'].includes(value.pending.kind)
      || typeof value.pending.body?.action_id !== 'string' || !Number.isInteger(value.pending.body.expected_version)
      || value.pending.body.expected_version < 0) throw new Error('invalid curriculum pending operation')
    if (!['select', 'generate', 'first-sources'].includes(value.pending.kind) && !('candidate_id' in value.pending && typeof value.pending.candidate_id === 'string')) throw new Error('invalid curriculum candidate')
    if (value.pending.kind === 'first-sources') {
      const s = value.pending.body.source
      if (!s || !exactKeys(s, ['knowledge_node_id','excerpt_id','requirement_id','standard_page','standard_quote','explanation','difficulty'])
        || ![s.knowledge_node_id,s.excerpt_id,s.requirement_id].every(boundId)
        || ![s.standard_quote,s.explanation].every(v => typeof v === 'string' && !!v.trim() && v.length <= 1200)
        || !Number.isInteger(s.standard_page) || s.standard_page < 1 || !Number.isInteger(s.difficulty) || s.difficulty < 1 || s.difficulty > 5
        || value.pending.body.expected_version !== 0 || !Number.isInteger(value.pending.body.expected_scope_version)
        || value.pending.body.expected_scope_version < 1) throw new Error('invalid first-source pending operation')
    }
  }
  return value
}

export function validateOralPackage(item: CurriculumPackage): void {
  if (item.payload?.validator !== 'pet_speaking_rubric_source_v1') return
  const source = parseOralSource(item.payload.pet_oral)
  const packet = item.review_packet
  const original = packet && parseOralSource(packet.textbook.content_text)
  const packetSource = packet && parseOralSource(packet.pet_oral)
  if (!source || !oralPictureSupported(source) || source.prompt !== item.payload.prompt || item.payload.expected_answer !== null
    || (packet && (!original || !packetSource || !sameOralSource(source, original) || !sameOralSource(source, packetSource)))) {
    throw new ApiError('口语原题、材料或配图无法完整核对，未开启发布。', 409)
  }
}
export function createCurriculumSupplyApi(request: CurriculumParentRequest) {
  const base = (child: string): string => `/api/parent/children/${encodeURIComponent(child)}/curriculum-supply`
  async function scoped<T>(child: string, path: string): Promise<T> {
    const result = await request(base(child) + path) as { child_id?: string; local_only?: boolean; cloud_transmission_allowed?: boolean }
    if (result?.child_id !== child || result.local_only !== true || result.cloud_transmission_allowed !== false) {
      throw new ApiError('课程响应与当前孩子不一致，请刷新后重试。', 409, 'CURRICULUM_RESPONSE_INVALID')
    }
    return result as T
  }
  return {
    options: (child: string) => scoped<CurriculumOptions>(child, '/options'),
    sourceOptions: async (child: string, source: string): Promise<CurriculumSourceOptions> => {
      const result = await scoped<CurriculumSourceOptions>(child, `/sources/${encodeURIComponent(source)}/options`)
      if (result.source_id !== source) throw new ApiError('教材响应已过期，请刷新。', 409)
      if (result.first_sources !== undefined && (!Array.isArray(result.first_sources) || result.first_sources.some(v =>
        !v || ![v.knowledge_node_id,v.excerpt_id,v.title,v.prompt].every(x => typeof x === 'string' && !!x.trim())
        || !result.excerpts.some(e => e.excerpt_id === v.excerpt_id)))) throw new ApiError('首题来源选项无法核对，请刷新。', 409)
      return result
    },
    audit: (child: string) => scoped<CurriculumAudit>(child, '/audit'),
    get: async (child: string, candidate: string): Promise<CurriculumPackage> => {
      const result = await scoped<CurriculumPackage>(child, `/${encodeURIComponent(candidate)}`)
      if (result.candidate_id !== candidate) throw new ApiError('题目响应已过期，请刷新。', 409)
      validateOralPackage(result)
      return result
    },
    perform: async (child: string, command: CurriculumPending): Promise<CurriculumPackage | CurriculumSelection> => {
      if (command.child_id !== child) throw new ApiError('不能把待提交操作用于另一名孩子。', 409)
      const path = command.kind === 'select' || command.kind === 'generate' || command.kind === 'first-sources' ? `/${command.kind}`
        : `/${encodeURIComponent(command.candidate_id)}/${command.kind}`
      const result = await request(base(child) + path, { method: 'POST', body: JSON.stringify(command.body) }) as CurriculumPackage | CurriculumSelection
      if (result?.local_only !== true || result.cloud_transmission_allowed !== false || ('child_id' in result && result.child_id !== child)) throw new ApiError('提交响应暂不能核对，请用原请求重试。', undefined, 'CURRICULUM_RESPONSE_INVALID')
      if ('candidate_id' in result) validateOralPackage(result)
      return result
    }
  }
}

const reasons: Record<string, string> = {
  LOCAL_TEMPLATE_UNAVAILABLE: '缺少该知识点的题目模板', LOCAL_STAGE_UNSUPPORTED: '当前题型不能测量所需迁移阶段',
  CURRICULUM_TEXTBOOK_UNREVIEWED: '教材材料尚未审核或授权不明确', CURRICULUM_TEXTBOOK_UNAVAILABLE: '缺少教材题目摘录',
  CURRICULUM_TEXTBOOK_EXPIRED: '教材授权已到期或尚未生效', CURRICULUM_SCOPE_MISMATCH: '教材版本、年级或科目尚未匹配',
  CURRICULUM_STANDARD_UNREVIEWED: '课标条目尚未审核', CURRICULUM_STANDARD_QUOTE_MISMATCH: '缺少可核对的课标原文',
  CURRICULUM_SOURCE_CHANGED: '审核后来源已修订，需要重新核对', CURRICULUM_NOT_PUBLISHED: '尚未发布到有效题库',
  CURRICULUM_STAGE_VALIDATOR_UNAVAILABLE: '缺少该迁移阶段的独立校验', CURRICULUM_VALIDATOR_UNAVAILABLE: '该题型的独立校验尚未接入',
  CURRICULUM_QUESTION_FORM_UNSUPPORTED: '这段材料不是当前校验器支持的完整计算题',
  CURRICULUM_ANSWER_MISMATCH: '复算结果与草案答案不一致', CURRICULUM_DUPLICATE_QUESTION: '这道题已有版本，请核对原题或修订版本',
  CURRICULUM_WITHDRAWN: '题目已撤回', CURRICULUM_REJECTED: '题目审核未通过',
  SUPPLY_SOURCE_UNAVAILABLE: '题源当前不可用', CURRICULUM_SELECTION_REQUIRED: '尚未选定当前教材',
  CURRICULUM_SCOPE_VERSION_CONFLICT: '另一窗口已更改教材范围', CURRICULUM_ACTION_CONFLICT: '这次操作的内容与原请求不一致',
  CURRICULUM_FIRST_REVIEW_AFTER_RESTORE: '备份恢复后，原题需重新校验与审核；旧发布操作不能恢复资格',
  CURRICULUM_FIRST_SOURCE_CONFLICT: '这份首题状态已变化，请刷新并核对现有题目，不重复建立',
  CURRICULUM_FIRST_NODE_UNAVAILABLE: '该知识点尚未具备当前孩子可用的课程依据，请先核对知识点与来源',
  CURRICULUM_FIRST_BASELINE_ONLY: '首题只能作为原题基线，不能直接认定为近迁移或远迁移',
  CURRICULUM_FIRST_SOURCE_UNAVAILABLE: '原首题已撤回或尚未审核发布，不能作为当前学习与评分依据'
}
export const curriculumReason = (code: string): string => reasons[code] ?? '材料、来源或校验条件仍需核对'
