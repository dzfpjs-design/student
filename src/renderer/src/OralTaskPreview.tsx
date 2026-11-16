import petStudyRoom from './assets/pet-study-room.png'

export const oralFamilyNames = {
  topic_response: '话题表达', picture_description: '看图描述', read_aloud: '短文朗读',
  interaction: '互动讨论', extended_turn: '连续表达', daily_dialogue: '日常对话', retell: '故事复述'
} as const
export type OralFamily = keyof typeof oralFamilyNames
export type OralPictureBinding = { diagnostic_item_id: string; source_version: string; stimulus_asset: string }
export type OralSource = {
  schema: 'pet_speaking_source_v1'; subject: '英语'; grade: string; knowledge_node_id: string
  answer_type: 'speaking' | 'read_aloud'; assessment_mode: 'pet_reference_rubric'; task_family: OralFamily
  prompt: string; task_points: string[]; stimulus_text: string | null; stimulus_binding: OralPictureBinding | null
}
export function parseOralSource(value: unknown): OralSource | null {
  try {
    const v = (typeof value === 'string' ? JSON.parse(value) : value) as OralSource
    if (!v || v.schema !== 'pet_speaking_source_v1' || v.subject !== '英语' || !Object.hasOwn(oralFamilyNames, v.task_family)
      || v.assessment_mode !== 'pet_reference_rubric' || v.answer_type !== (v.task_family === 'read_aloud' ? 'read_aloud' : 'speaking')
      || typeof v.grade !== 'string' || !v.grade.trim() || typeof v.knowledge_node_id !== 'string' || !v.knowledge_node_id
      || typeof v.prompt !== 'string' || !v.prompt.trim() || v.prompt.length > 1200
      || !Array.isArray(v.task_points) || !v.task_points.length || v.task_points.length > 12
      || v.task_points.some(p => typeof p !== 'string' || !p.trim() || p.length > 400)
      || new Set(v.task_points.map(p => p.trim())).size !== v.task_points.length
      || !(v.stimulus_text === null || typeof v.stimulus_text === 'string' && !!v.stimulus_text.trim() && v.stimulus_text.length <= 6000)
      || (['read_aloud', 'retell'].includes(v.task_family) && !v.stimulus_text)) return null
    const b = v.stimulus_binding
    if (v.task_family === 'picture_description' ? !b || typeof b.diagnostic_item_id !== 'string' || !b.diagnostic_item_id
      || !/^[a-f0-9]{64}$/.test(b.source_version) || typeof b.stimulus_asset !== 'string' || !b.stimulus_asset : b !== null) return null
    return v
  } catch { return null }
}
export function oralPictureSupported(source: OralSource): boolean {
  return source.stimulus_binding === null || source.stimulus_binding.stimulus_asset === 'pet-study-room'
}
export function sameOralSource(a: OralSource, b: OralSource): boolean {
  return (['schema', 'subject', 'grade', 'knowledge_node_id', 'answer_type', 'assessment_mode', 'task_family', 'prompt', 'stimulus_text'] as const)
    .every(key => a[key] === b[key]) && JSON.stringify(a.task_points) === JSON.stringify(b.task_points)
    && (a.stimulus_binding === null ? b.stimulus_binding === null : !!b.stimulus_binding
      && (['diagnostic_item_id', 'source_version', 'stimulus_asset'] as const).every(key => a.stimulus_binding![key] === b.stimulus_binding![key]))
}
export function OralTaskPreview({ source }: { source: OralSource }) {
  return <section aria-label={`${oralFamilyNames[source.task_family]}完整原题`}>
    <h4>{oralFamilyNames[source.task_family]} · 完整原题</h4><p className="csr-question">{source.prompt}</p>
    {source.stimulus_binding && (oralPictureSupported(source)
      ? <img src={petStudyRoom} alt="原题配图：两名孩子在书房学习，一名阅读，一名书写，家长正在放下一杯水"
        style={{ width: '100%', maxWidth: 520, height: 'auto', display: 'block' }} />
      : <p role="alert" className="csr-notice">本机不能展示这份原题配图，暂不能核对并发布。</p>)}
    {source.stimulus_text && <><h4>{source.task_family === 'retell' ? '完整复述材料' : source.task_family === 'read_aloud' ? '完整朗读材料' : '补充材料'}</h4>
      <blockquote className="csr-evidence">{source.stimulus_text}</blockquote></>}
    <h4>参考任务点</h4><ol>{source.task_points.map(point => <li key={point} style={{ whiteSpace: 'pre-wrap' }}>{point}</li>)}</ol>
    <p className="csr-muted">原题与任务点仅供内容参考；发布不认证发音、流利度、独立性或掌握。</p>
  </section>
}
export function CurriculumOriginal({ text }: { text: string }) {
  const oral = parseOralSource(text)
  if (oral) return <OralTaskPreview source={oral} />
  if (text.includes('pet_speaking_source_v1')) return <p role="alert" className="csr-notice">口语原题结构无法核对，请重新保存完整原题。</p>
  return <blockquote className="csr-evidence">{text}</blockquote>
}
