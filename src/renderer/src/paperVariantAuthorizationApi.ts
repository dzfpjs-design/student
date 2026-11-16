import { ApiError } from './api'

export type PaperParentRequest = (path: string, init?: RequestInit) => Promise<unknown>
export type PaperLicense = 'CC0-1.0' | 'original_author' | 'explicit_cloud_processing_permission'
type Local = { local_only: true; cloud_transmission_allowed: false }
export type PaperVariantAuthorization = Local & {
  child_id: string; mistake_id: string; revision: number
  status: 'authorization_required' | 'authorized' | 'invalidated' | 'blocked' | 'revoked'
  reason: string | null; message: string; question_preview: string | null
  question_version: string | null; source_version: string | null; authorization_id: string | null
  license_options: PaperLicense[]; rights_review_authority: 'parent_declaration_not_legal_or_network_verification'
}
type CommonBody = { expected_revision: number; client_action_id: string }
export type PaperVariantCommand = { child_id: string; mistake_id: string } & (
  { operation: 'revoke'; body: CommonBody } | { operation: 'grant'; body: CommonBody & {
    question_version: string; source_version: string; license_basis: { license_id: PaperLicense; reference: string; evidence: string }
  } })
export type PaperVariantReceipt = Local & { receipt: { event_id: string; revision: number; command: 'grant' | 'revoke' }
  current: PaperVariantAuthorization; generation_job: null | { job_id: string; status: 'pending' | 'processing' | 'completed' | 'blocked' | 'failed'; requested_count: number; error_code: string | null } }
export const paperLicenseLabels: Record<PaperLicense, string> = { 'CC0-1.0': 'CC0 公共领域声明', original_author: '我是原题作者', explicit_cloud_processing_permission: '已取得明确的云端处理许可' }
export const paperVariantAuthorizationClearedEvent = 'hsc-paper-variant-authorization-cleared'
export const paperVariantAuthorizationKey = (child: string, mistake: string) => `hsc-paper-variant-authorization:${encodeURIComponent(child)}:${encodeURIComponent(mistake)}`
export function clearPaperVariantAuthorizationLocalState(child?: string): void {
  const prefix = child === undefined ? 'hsc-paper-variant-authorization:' : `hsc-paper-variant-authorization:${encodeURIComponent(child)}:`
  for (const key of Object.keys(localStorage)) if (key.startsWith(prefix)) localStorage.removeItem(key)
  window.dispatchEvent(new CustomEvent(paperVariantAuthorizationClearedEvent, { detail: child }))
}
function text(v: unknown, max = 200): v is string { return typeof v === 'string' && !!v.trim() && v.length <= max }
function optionalText(v: unknown) { return v === null || typeof v === 'string' }
export function exactPaperKeys(v: object, keys: string[]): boolean { return Object.keys(v).sort().join('|') === [...keys].sort().join('|') }
export function validatePaperCommand(value: unknown): asserts value is PaperVariantCommand {
  const p = value as PaperVariantCommand, b = p?.body
  if (!p || !exactPaperKeys(p, ['child_id', 'mistake_id', 'operation', 'body']) || !text(p.child_id) || !text(p.mistake_id)
    || !['grant', 'revoke'].includes(p.operation) || !b || !Number.isInteger(b.expected_revision) || b.expected_revision < 0 || !text(b.client_action_id)
    || !exactPaperKeys(b, p.operation === 'grant' ? ['expected_revision', 'client_action_id', 'question_version', 'source_version', 'license_basis'] : ['expected_revision', 'client_action_id'])) throw new ApiError('本机原授权请求无法核对，请放弃旧请求后重新确认。', 422)
  if (p.operation === 'grant') {
    const b = p.body, l = b.license_basis
    if (!text(b.question_version) || !text(b.source_version) || !l || !exactPaperKeys(l, ['license_id', 'reference', 'evidence'])
      || !Object.hasOwn(paperLicenseLabels, l.license_id) || !text(l.reference, 1024) || !text(l.evidence, 2000) || l.evidence.trim().length < 20) throw new ApiError('权利依据不完整，未发送授权。', 422)
  }
}
function local(v: unknown): asserts v is Local {
  if (!(v as Local)?.local_only || (v as Local)?.local_only !== true || (v as Local)?.cloud_transmission_allowed !== false) throw new ApiError('无法确认本地授权响应。', 409)
}
function view(value: unknown, child: string, mistake: string): PaperVariantAuthorization {
  local(value); const v = value as PaperVariantAuthorization
  if (v.child_id !== child || v.mistake_id !== mistake || !Number.isInteger(v.revision) || v.revision < 0
    || !['authorization_required', 'authorized', 'invalidated', 'blocked', 'revoked'].includes(v.status) || !text(v.message, 2000)
    || ![v.reason, v.question_preview, v.question_version, v.source_version, v.authorization_id].every(optionalText)
    || !Array.isArray(v.license_options) || v.license_options.some(x => !Object.hasOwn(paperLicenseLabels, x))
    || new Set(v.license_options).size !== v.license_options.length
    || v.rights_review_authority !== 'parent_declaration_not_legal_or_network_verification'
    || (v.status === 'authorized' && (!text(v.authorization_id) || !text(v.question_version) || !text(v.source_version) || !text(v.question_preview, 1000)))) throw new ApiError('原题或孩子身份无法核对，请刷新。', 409)
  return v
}
export function createPaperVariantAuthorizationApi(request: PaperParentRequest) {
  const path = (child: string, mistake: string) => `/v1/parent/children/${encodeURIComponent(child)}/paper-variant-authorizations/${encodeURIComponent(mistake)}`
  return {
    read: async (child: string, mistake: string) => view(await request(path(child, mistake), { cache: 'no-store' }), child, mistake),
    perform: async (command: PaperVariantCommand): Promise<PaperVariantReceipt> => {
      validatePaperCommand(command)
      const result = await request(`${path(command.child_id, command.mistake_id)}/${command.operation}`, { method: 'POST', cache: 'no-store', body: JSON.stringify(command.body) }) as PaperVariantReceipt
      local(result)
      if (!result.receipt || !text(result.receipt.event_id) || result.receipt.command !== command.operation
        || result.receipt.revision !== command.body.expected_revision + 1) throw new ApiError('原操作回执无法核对，请保留原请求重试。')
      const current = view(result.current, command.child_id, command.mistake_id)
      if (current.revision < result.receipt.revision) throw new ApiError('当前授权早于回执，需重新读取。')
      const job = result.generation_job
      if (job !== null && (!job || !text(job.job_id) || !['pending', 'processing', 'completed', 'blocked', 'failed'].includes(job.status)
        || !Number.isInteger(job.requested_count) || job.requested_count < 1 || !optionalText(job.error_code))) throw new ApiError('准备任务回执无法核对，请保留原请求。')
      return { ...result, current }
    }
  }
}
