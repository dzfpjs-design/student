import { ApiError, requestParentVisualImage, requestParentVisualLocal } from './api'
import type { VisualImageDescriptor } from './VisualMaterialImage'
import type { VisualReviewCommand, VisualReviewDetail, VisualReviewResult } from './ParentVisualMaterialReview'

type Local = { local_only: true; cloud_transmission_allowed: false }
export type VisualListItem = { artifact_id: string; revision_id: string; region_id: string; region_version: string;
  title: string; captured_at: string; status: string; facts_version: string | null }
export type VisualListing = Local & { child_id: string; items: VisualListItem[]; offset: number; total: number }
type RawDetail = Omit<VisualReviewDetail, 'observations'> & {
  status: string; storage_status: string; reason?: string
  observations: VisualReviewDetail['observations']['observations']; uncertainties: string[]; facts_version: string | null
}
const digest = /^[a-f0-9]{64}$/
const root = (childId: string) => `/v1/parent/children/${encodeURIComponent(childId)}/visual-materials`
export function visualRegionPath(childId: string, ref: Pick<VisualListItem, 'artifact_id' | 'region_id'>) {
  return `${root(childId)}/${encodeURIComponent(ref.artifact_id)}/regions/${encodeURIComponent(ref.region_id)}`
}
const query = (ref: Pick<VisualListItem, 'revision_id' | 'region_version'>) => new URLSearchParams({ revision_id: ref.revision_id, region_version: ref.region_version }).toString()
function local(value: unknown): asserts value is Local {
  const r = value as Partial<Local> | null
  if (!r || r.local_only !== true || r.cloud_transmission_allowed !== false) throw new ApiError('本地图形响应无法核对。', 409)
}
export function validateVisualDetail(childId: string, ref: Pick<VisualListItem, 'artifact_id' | 'revision_id' | 'region_id' | 'region_version'>, raw: unknown): RawDetail {
  local(raw)
  const d = raw as RawDetail
  if (d.child_id !== childId || typeof d.session_id !== 'string' || !d.session_id || !d.binding
    || (['artifact_id', 'revision_id', 'region_id', 'region_version'] as const).some(k => d.binding[k] !== ref[k])
    || !['unconfirmed', 'confirmed', 'retracted'].includes(d.fact_status)
    || !['available', 'unavailable', 'host_unverified'].includes(d.image_availability)
    || typeof d.parent_confirmation_ready !== 'boolean' || typeof d.source_current !== 'boolean'
    || !d.image_descriptor || !Array.isArray(d.observations) || !Array.isArray(d.uncertainties)
    || d.uncertainties.some(v => typeof v !== 'string')
    || d.observations.some(v => !v || typeof v.id !== 'string' || typeof v.description !== 'string' || !Array.isArray(v.bbox))
    || d.binding.facts_version !== d.facts_version
    || d.facts_version !== null && !digest.test(d.facts_version)) throw new ApiError('图形原件的孩子或版本已经变化。', 409)
  return d
}
export function visualReviewDetail(raw: RawDetail): VisualReviewDetail {
  return { ...raw, observations: { version: 'visual_facts_v1', observations: raw.observations, uncertainties: raw.uncertainties } }
}
export function createVisualMaterialApi(
  request: typeof requestParentVisualLocal = requestParentVisualLocal,
  imageRequest: typeof requestParentVisualImage = requestParentVisualImage,
) {
  // Ephemeral one-read tokens are never stored as parent decisions or grants.
  const displayedReads = new Map<string, string>()
  const imageKey = (childId: string, d: VisualImageDescriptor) => JSON.stringify([childId, d])
  return {
    async list(childId: string, offset: number, signal: AbortSignal): Promise<VisualListing> {
      const result = await request<VisualListing>(`${root(childId)}?offset=${offset}&limit=20`, { signal })
      local(result)
      if (result.child_id !== childId || result.offset !== offset || !Number.isInteger(result.total) || result.total < 0
        || !Array.isArray(result.items) || result.items.length > 20 || result.items.some(v => !v || typeof v.title !== 'string'
          || typeof v.captured_at !== 'string' || !digest.test(v.region_version)
          || ['artifact_id', 'region_id', 'revision_id'].some(k => typeof v[k as keyof VisualListItem] !== 'string' || !v[k as keyof VisualListItem]))) throw new ApiError('图形列表归属或页码无法核对。', 409)
      return result
    },
    async get(childId: string, ref: VisualListItem, signal: AbortSignal): Promise<RawDetail> {
      return validateVisualDetail(childId, ref, await request(`${visualRegionPath(childId, ref)}?${query(ref)}`, { signal }))
    },
    async image(childId: string, descriptor: VisualImageDescriptor, signal: AbortSignal): Promise<ArrayBuffer> {
      const key = imageKey(childId, descriptor)
      displayedReads.delete(key)
      const response = await imageRequest(`${visualRegionPath(childId, descriptor)}/image?${query(descriptor)}&sha256=${descriptor.sha256}`, signal)
      if (signal.aborted) throw new DOMException('cancelled', 'AbortError')
      if (displayedReads.size >= 20) displayedReads.clear()
      displayedReads.set(key, response.display_token)
      return response.bytes
    },
    async displayed(detail: VisualReviewDetail, signal: AbortSignal) {
      const key = imageKey(detail.child_id, detail.image_descriptor), display_token = displayedReads.get(key)
      if (!display_token) throw new ApiError('原图未成功读取，不能记录显示。', 409)
      const d = detail.image_descriptor
      const response = await request<Local & { display_receipt_id: string; detail: RawDetail }>(`${visualRegionPath(detail.child_id, d)}/displayed`, { method: 'POST', signal, body: JSON.stringify({
        revision_id: d.revision_id, region_version: d.region_version, facts_version: detail.binding.facts_version,
        display_token, sha256: d.sha256, width: d.width, height: d.height, client_action_id: crypto.randomUUID(),
      }) })
      local(response); validateVisualDetail(detail.child_id, d, response.detail)
      if (typeof response.display_receipt_id !== 'string' || !response.display_receipt_id) throw new ApiError('原图显示回执缺失。', 409)
      displayedReads.delete(key)
      return { display_receipt_id: response.display_receipt_id, local_only: true as const, cloud_transmission_allowed: false as const }
    },
    async submit(childId: string, command: VisualReviewCommand, signal: AbortSignal): Promise<VisualReviewResult> {
      if (command.child_id !== childId) throw new ApiError('不能发送另一名孩子的图形决定。', 409)
      const b = command.binding
      const response = await request<Local & { receipt: VisualReviewResult; detail: RawDetail }>(`${visualRegionPath(childId, b)}/${command.decision === 'confirm' ? 'confirm' : 'retract'}`, {
        method: 'POST', signal, body: JSON.stringify({ revision_id: b.revision_id, region_version: b.region_version, facts_version: b.facts_version,
          client_action_id: command.client_action_id, ...(command.decision === 'confirm' ? { display_receipt_id: command.display_receipt_id } : {}) }),
      })
      local(response); local(response.receipt)
      validateVisualDetail(childId, b, response.detail)
      return response.receipt
    },
    async store(childId: string, ref: VisualListItem, actionId: string, signal: AbortSignal) {
      const response = await request<Local & { detail: RawDetail }>(`${visualRegionPath(childId, ref)}/storage`, { method: 'POST', signal,
        body: JSON.stringify({ revision_id: ref.revision_id, region_version: ref.region_version, facts_version: ref.facts_version,
          client_action_id: actionId, purpose: 'local_visual_material_review_v1' }) })
      local(response)
      return validateVisualDetail(childId, ref, response.detail)
    },
    clearImages() { displayedReads.clear() },
  }
}
