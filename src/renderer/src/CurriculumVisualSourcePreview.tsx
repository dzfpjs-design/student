import { useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { VisualBinding } from './ParentVisualMaterialReview'
import { VisualMaterialImage } from './VisualMaterialImage'
import { createVisualMaterialApi } from './visualMaterialApi'

type Source = { schema: 'reviewed_open_response_source_v3'; prompt: string; task_points: string[];
  material_bindings: { material_key: string; visual: VisualBinding }[] }
export function visualOriginal(text: string): Source | null {
  try {
    const s = JSON.parse(text) as Source & { material_semantics: string; context_requirement: string }
    if (s?.schema !== 'reviewed_open_response_source_v3' || s.material_semantics !== 'image_required'
      || s.context_requirement !== 'external_material' || typeof s.prompt !== 'string' || !s.prompt.trim() || s.prompt.length > 1200
      || !Array.isArray(s.task_points) || !s.task_points.length || s.task_points.length > 12
      || s.task_points.some(p => typeof p !== 'string' || !p.trim() || p.length > 400)
      || !Array.isArray(s.material_bindings) || !s.material_bindings.length || s.material_bindings.length > 8
      || s.material_bindings.some(m => !m || typeof m.material_key !== 'string' || !/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(m.material_key)
        || !m.visual || Object.keys(m.visual).length !== 6
        || (['artifact_id', 'region_id', 'revision_id', 'confirmation_id'] as const).some(k => typeof m.visual[k] !== 'string' || !m.visual[k] || m.visual[k]!.length > 128)
        || (['region_version', 'facts_version'] as const).some(k => typeof m.visual[k] !== 'string' || !/^[a-f0-9]{64}$/.test(m.visual[k]!)))) return null
    return s
  } catch { return null }
}

export function CurriculumVisualSourcePreview({ childId, source, api: provided }: {
  childId: string; source: Source; api?: ReturnType<typeof createVisualMaterialApi>
}) {
  const api = useMemo(() => provided ?? createVisualMaterialApi(), [provided])
  return <section aria-label="图形原题与依据">
    <blockquote className="csr-evidence">{source.prompt}</blockquote>
    {source.material_bindings.map((m, index) => <Figure key={JSON.stringify([childId, m])} childId={childId} binding={m.visual} number={index + 1} api={api} />)}
    <h4>原题任务点</h4><ol>{source.task_points.map((p, i) => <li key={i}>{p}</li>)}</ol>
    <p className="csr-muted">图形与参考任务点不代表孩子已经理解或掌握。</p>
  </section>
}

function Figure({ childId, binding, number, api }: {
  childId: string; binding: VisualBinding; number: number; api: ReturnType<typeof createVisualMaterialApi>
}) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof api.get>> | null>(null)
  const [failed, setFailed] = useState(false), [reload, setReload] = useState(0)
  useEffect(() => {
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15_000)
    let active = true
    setDetail(null); setFailed(false)
    void api.get(childId, { ...binding, title: '', captured_at: '', status: '', facts_version: binding.facts_version }, abort.signal)
      .then(d => {
        if (abort.signal.aborted || !active) return
        if (d.binding.confirmation_id !== binding.confirmation_id || d.binding.facts_version !== binding.facts_version
          || !d.source_current || d.fact_status !== 'confirmed' || d.image_availability !== 'available') throw Error('source changed')
        setDetail(d)
      }).catch(() => { if (active) setFailed(true) })
      .finally(() => clearTimeout(timer))
    return () => { active = false; clearTimeout(timer); abort.abort() }
  }, [childId, binding, api, reload])
  return <div><h4>原题配图 {number}</h4>
    {failed ? <p role="alert">原题配图已变化或暂时无法核对，不能据此判断答案。</p>
      : detail ? <><VisualMaterialImage child_id={childId} session_id={detail.session_id} descriptor={detail.image_descriptor}
        status="ready_local" reloadVersion={reload} readImage={(d, signal) => api.image(childId, d, signal)} />
        <h4>已核对的图形记录</h4><ul>{detail.observations.map(o => <li key={o.id}>{o.description}</li>)}</ul></>
        : <p role="status">正在核对原题配图…</p>}
    <button type="button" aria-label={`重新核对配图 ${number}`} onClick={() => setReload(n => n + 1)}><RefreshCw size={16} />重新核对</button>
  </div>
}
