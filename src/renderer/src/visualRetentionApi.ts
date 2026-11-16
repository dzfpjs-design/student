import { ApiError, requestParentVisualLocal } from './api'

export type RetentionCommand = { action: 'revoke'; expected_revision: string | null; client_action_id: string }
  | { action: 'enable' | 'update'; expected_revision: string | null; client_action_id: string;
    session_id: string | null; retention_days: number; authorization_days: number; max_bytes: number }
export type RetentionView = {
  child_id: string; local_only: true; cloud_transmission_allowed: false
  policy: { purpose: 'local_derived_visual_retention_v1'; status: 'disabled' | 'enabled' | 'expired' | 'revoked' | 'quarantined';
    revision: string | null; scope: null | { child_id: string; session_id: string | null; session_state: 'study' };
    retention_days: number | null; authorization_expires_at: string | null; max_bytes: number | null; used_bytes: number; reason: string | null }
  limits: { retention_days: [number, number]; authorization_days: [number, number]; max_bytes: [number, number] }
}
export type RetentionResult = RetentionView & { receipt: { receipt_id: string; child_id: string; client_action_id: string;
  action: RetentionCommand['action']; status: 'enabled' | 'updated' | 'revoked'; revision: string; request_hash: string }; replayed: boolean }
const id = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 128
const integer = (v: unknown, low: number, high: number) => Number.isSafeInteger(v) && Number(v) >= low && Number(v) <= high
const invalid = () => new ApiError('学习裁图保留设置的归属或版本无法核对。', 409)
export function validRetentionCommand(value: unknown): value is RetentionCommand {
  const c = value as RetentionCommand | null
  if (!c || !id(c.client_action_id) || !(c.expected_revision === null || id(c.expected_revision))) return false
  const common = ['action', 'expected_revision', 'client_action_id']
  if (c.action === 'revoke') return Object.keys(c).every(k => common.includes(k))
  return ['enable', 'update'].includes(c.action) && Object.keys(c).every(k => [...common, 'session_id', 'retention_days', 'authorization_days', 'max_bytes'].includes(k))
    && (c.session_id === null || id(c.session_id)) && integer(c.retention_days, 1, 30)
    && integer(c.authorization_days, 1, 365) && integer(c.max_bytes, 1048576, 268435456)
}
export function retentionView(child: string, raw: unknown): RetentionView {
  const v = raw as RetentionView, p = v?.policy
  if (!v || v.child_id !== child || v.local_only !== true || v.cloud_transmission_allowed !== false || !p
    || p.purpose !== 'local_derived_visual_retention_v1' || !['disabled','enabled','expired','revoked','quarantined'].includes(p.status)
    || !(p.revision === null || id(p.revision)) || !integer(p.used_bytes, 0, Number.MAX_SAFE_INTEGER)
    || !(p.reason === null || typeof p.reason === 'string')
    || p.scope !== null && (!p.scope || p.scope.child_id !== child || p.scope.session_state !== 'study' || !(p.scope.session_id === null || id(p.scope.session_id)))
    || !(p.retention_days === null || integer(p.retention_days, 1, 30))
    || !(p.max_bytes === null || integer(p.max_bytes, 1048576, 268435456))
    || !(p.authorization_expires_at === null || typeof p.authorization_expires_at === 'string' && Number.isFinite(Date.parse(p.authorization_expires_at)))
    || p.status === 'enabled' && (!p.revision || !p.scope || !p.retention_days || !p.max_bytes || !p.authorization_expires_at)
    || !v.limits || JSON.stringify(v.limits.retention_days) !== '[1,30]' || JSON.stringify(v.limits.authorization_days) !== '[1,365]'
    || JSON.stringify(v.limits.max_bytes) !== '[1048576,268435456]') throw invalid()
  return v
}
export function createVisualRetentionApi(request: typeof requestParentVisualLocal = requestParentVisualLocal) {
  const path = (child: string) => `/v1/parent/children/${encodeURIComponent(child)}/visual-retention`
  return {
    async get(child: string, signal: AbortSignal) { return retentionView(child, await request(path(child), { signal })) },
    async submit(child: string, command: RetentionCommand, signal: AbortSignal): Promise<RetentionResult> {
      if (!validRetentionCommand(command)) throw invalid()
      const raw = await request<RetentionResult>(path(child), { method: 'POST', signal, body: JSON.stringify(command) })
      retentionView(child, raw)
      const r = raw.receipt
      if (!r || r.child_id !== child || r.client_action_id !== command.client_action_id || r.action !== command.action
        || !id(r.receipt_id) || r.revision !== r.receipt_id || typeof r.request_hash !== 'string' || !/^[a-f0-9]{64}$/.test(r.request_hash)
        || r.status !== ({ enable: 'enabled', update: 'updated', revoke: 'revoked' } as const)[command.action]
        || typeof raw.replayed !== 'boolean') throw invalid()
      return raw
    },
  }
}
