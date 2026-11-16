import { ParentWritingSourceExcerpt } from './ParentWritingSourceExcerpt'
import { CurriculumVisualSourcePreview, visualOriginal } from './CurriculumVisualSourcePreview'
import type { ParentCurrentMaterials } from './curriculumSupplyTypes'

export function openTextOriginal(text: string): { prompt: string; points: string[]; materialKeys: string[] } | null {
  try {
    const source = JSON.parse(text)
    if (!source || !['reviewed_open_response_source_v1', 'reviewed_open_response_source_v2'].includes(source.schema)
      || (source.schema === 'reviewed_open_response_source_v1' ? source.context_requirement !== 'self_contained'
        : source.context_requirement !== 'external_material' || source.material_semantics !== 'text_only')
      || typeof source.prompt !== 'string' || !source.prompt.trim() || source.prompt.length > 1200
      || !Array.isArray(source.task_points) || !source.task_points.length || source.task_points.length > 12
      || source.task_points.some((point: unknown) => typeof point !== 'string' || !point.trim() || point.length > 400)) return null
    const keys: unknown[] = source.schema === 'reviewed_open_response_source_v2'
      ? Array.isArray(source.material_bindings) ? source.material_bindings.map((m: { material_key?: unknown } | null) => m?.material_key) : [] : []
    if (source.schema === 'reviewed_open_response_source_v2' && (!keys.length || keys.length > 8
      || keys.some(key => typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(key))
      || new Set(keys).size !== keys.length)) return null
    return { prompt: source.prompt, points: source.task_points, materialKeys: keys as string[] }
  } catch { return null }
}

export function parentMaterialsReady(text: string, materials?: ParentCurrentMaterials | null): boolean {
  let source: { schema?: unknown }
  try { source = JSON.parse(text) } catch { return true }
  if (source?.schema !== 'reviewed_open_response_source_v2') return true
  const original = openTextOriginal(text)
  return !!original && materials?.status === 'ready' && materials.local_only === true
    && materials.cloud_transmission_allowed === false && materials.mastery_claim_allowed === false
    && Array.isArray(materials.materials) && materials.materials.length === original.materialKeys.length
    && materials.materials.every((m, index) => !!m && m.material_key === original.materialKeys[index]
      && typeof m.title === 'string' && typeof m.version_label === 'string'
      && typeof m.text === 'string' && !!m.text.trim() && m.text.length <= 1000)
}

export function objectiveOriginal(text: string): { prompt: string; answers: string[] } | null {
  let value: unknown
  try { value = JSON.parse(text) } catch { return null }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (source.schema !== 'curriculum_objective_source_v1' || typeof source.prompt !== 'string' || !source.prompt.trim()) return null
  const expected = source.expected_answer
  if (!expected || typeof expected !== 'object' || Array.isArray(expected)) return null
  const answers = (expected as Record<string, unknown>).exact_answers
  if (!Array.isArray(answers) || !answers.length || !answers.every(answer => typeof answer === 'string' && answer.trim())) return null
  return { prompt: source.prompt, answers }
}

export function CurriculumFirstSourcePreview({ text, childId, materials }: { text: string; childId?: string; materials?: ParentCurrentMaterials | null }): React.JSX.Element {
  const visual = visualOriginal(text)
  if (visual && childId) return <CurriculumVisualSourcePreview key={childId + text} childId={childId} source={visual} />
  if (text.includes('reviewed_open_response_source_v3')) return <p role="alert">图形原题或孩子归属无法核对，请重新读取完整来源。</p>
  const open = openTextOriginal(text)
  if (open) return <section aria-label="开放题完整原文">
    <blockquote className="csr-evidence">{open.prompt}</blockquote>
    {open.materialKeys.length > 0 && (parentMaterialsReady(text, materials)
      ? materials!.materials.map((material, index) => <section key={material.material_key} aria-label={`原题材料 ${index + 1}`}>
        <h4>原题材料 {index + 1}</h4><p>{material.title} · {material.version_label || '版本未标注'}</p>
        {material.heading && <p>{material.heading}</p>}<blockquote className="csr-evidence">{material.text}</blockquote>
      </section>)
      : <p role="alert">绑定原材料暂时无法核对或已经变化。请刷新并核对当前来源；不能继续审核发布。</p>)}
    <h4>原题参考任务点</h4><ol>{open.points.map((point, index) => <li key={index} style={{ whiteSpace: 'pre-wrap' }}>{point}</li>)}</ol>
    <p className="csr-muted">原题与参考任务点不代表孩子已经理解或掌握；作答和评分证据另行核对。</p>
  </section>
  if (text.includes('reviewed_open_response_source_v1') || text.includes('reviewed_open_response_source_v2')) return <p role="alert">开放题原文结构无法核对，请重新读取完整原题与任务点。</p>
  const original = objectiveOriginal(text)
  if (!original) return <ParentWritingSourceExcerpt text={text} />
  return <div>
    <blockquote className="csr-evidence">{original.prompt}</blockquote>
    <h4>原题完整参考答案</h4>
    <ul>{original.answers.map((answer, index) => <li key={index}>{answer}</li>)}</ul>
    <p className="csr-muted">请对照原来源核对；参考答案不是孩子的作答或能力证明。</p>
  </div>
}
