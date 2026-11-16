import { ApiError } from './api'
import type { CurriculumChild, CurriculumParentRequest } from './curriculumSupplyTypes'
import { parseOralSource, oralFamilyNames, type OralFamily, type OralSource, type OralPictureBinding } from './OralTaskPreview'

export const oralAuthorKey = (child: string) => `hsc-curriculum-oral-author:${child}`
export type OralAuthorOption = { task_family: OralFamily; knowledge_node_id: string; seed_item_id: string; title: string
  answer_type: OralSource['answer_type']; stimulus_binding: OralPictureBinding | null }
export type OralAuthorOptions = { child_id: string; grade: string; options: OralAuthorOption[]; local_only: true; cloud_transmission_allowed: false }
export type OralImport = { source_type: 'school_material'; subject: '英语'; grade_band: string; title: string
  version_label: string; source_ref: string; license_status: 'family_owned_index'; parent_reviewed: true; content_text: string }
export function createOralSourceApi(request: CurriculumParentRequest) {
  const base = (child: string) => `/api/parent/children/${encodeURIComponent(child)}/curriculum-supply`
  return {
    options: async (child: CurriculumChild): Promise<OralAuthorOptions> => {
      const v = await request(base(child.child_id) + '/oral-authoring', { cache: 'no-store' }) as OralAuthorOptions
      if (!v || v.child_id !== child.child_id || v.grade !== child.grade || v.local_only !== true || v.cloud_transmission_allowed !== false
        || !Array.isArray(v.options) || new Set(v.options.map(o => o.task_family)).size !== v.options.length
        || v.options.some(o => !o || !Object.hasOwn(oralFamilyNames, o.task_family) || !o.seed_item_id || !o.title
          || !parseOralSource({ ...o, schema: 'pet_speaking_source_v1', subject: '英语', grade: v.grade,
            assessment_mode: 'pet_reference_rubric', prompt: 'validation', task_points: ['validation'], stimulus_text: 'validation' }))) {
        throw new ApiError('口语题型与当前孩子不一致，请刷新核对。', 409)
      }
      return v
    },
    save: async (child: CurriculumChild, body: OralImport): Promise<string> => {
      const original = parseOralSource(body.content_text)
      if (!original || body.grade_band !== child.grade || original.grade !== child.grade || body.parent_reviewed !== true
        || body.license_status !== 'family_owned_index' || body.subject !== '英语' || body.source_type !== 'school_material'
        || !body.source_ref || !body.title.trim() || !body.version_label.trim()) throw new ApiError('本机原题草稿不完整，尚未提交。', 422)
      const result = await request(`/v1/parent/children/${encodeURIComponent(child.child_id)}/knowledge-sources`,
        { method: 'POST', body: JSON.stringify(body) }) as { child_id: string; source_id: string; review_status: string }
      if (result?.child_id !== child.child_id || !result.source_id || result.review_status !== 'approved') throw new ApiError('保存回执无法核对，请重试原请求。', 409)
      const detail = await request(base(child.child_id) + `/sources/${encodeURIComponent(result.source_id)}/options`, { cache: 'no-store' }) as {
        child_id: string; source_id: string; local_only: boolean; cloud_transmission_allowed: boolean; excerpts: { text: string }[] }
      if (detail?.child_id !== child.child_id || detail.source_id !== result.source_id || detail.local_only !== true || detail.cloud_transmission_allowed !== false
        || !detail.excerpts?.some(e => e.text === body.content_text)) throw new ApiError('尚未读回完整原题，请重试原请求。', 409)
      return result.source_id
    }
  }
}
