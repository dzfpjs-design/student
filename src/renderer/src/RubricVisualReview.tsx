import { useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { VisualMaterialImage, type VisualImageDescriptor } from './VisualMaterialImage'
import { createVisualMaterialApi } from './visualMaterialApi'
import type { VisualBinding } from './ParentVisualMaterialReview'

export type RubricVisualMaterial = { version: 'rubric_visual_material_v1'; status: 'current' | 'unavailable';
  source_current: boolean; image_availability: 'available' | 'unavailable'; binding_version: string | null;
  materials: { material_key: string; binding: VisualBinding; image_descriptor: VisualImageDescriptor }[];
  reason: string | null; local_only: true; cloud_transmission_allowed: false }
export type RubricVisualEvidence = { assessment_id: string; assessment_fingerprint: string; binding_version: string;
  displays: { material_key: string; display_receipt_id: string }[] }
export function currentRubricVisual(v: RubricVisualMaterial): boolean {
  return v?.version === 'rubric_visual_material_v1' && v.status === 'current' && v.source_current === true
    && v.image_availability === 'available' && v.local_only === true && v.cloud_transmission_allowed === false
    && typeof v.binding_version === 'string' && !!v.binding_version && Array.isArray(v.materials)
    && v.materials.length > 0 && v.materials.length <= 8
    && new Set(v.materials.map(m => m?.material_key)).size === v.materials.length
    && v.materials.every(m => !!m && /^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(m.material_key) && !!m.binding && !!m.image_descriptor)
}
export function RubricVisualReview({ childId, assessmentId, fingerprint, visual, onEvidence, api: supplied }: {
  childId: string; assessmentId: string; fingerprint: string; visual: RubricVisualMaterial;
  onEvidence: (evidence: RubricVisualEvidence | null) => void; api?: ReturnType<typeof createVisualMaterialApi>
}) {
  const api = useMemo(() => supplied ?? createVisualMaterialApi(), [supplied])
  const callback = useRef(onEvidence); callback.current = onEvidence
  const [saved, setSaved] = useState<{ identity: string; values: Record<string, string> } | null>(null), [reload, setReload] = useState(0)
  const valid = currentRubricVisual(visual), identity = JSON.stringify([childId, assessmentId, fingerprint, visual, reload])
  const owner = useRef(identity); owner.current = identity
  const receipts = saved?.identity === identity ? saved.values : {}
  useEffect(() => { callback.current(null) }, [identity])
  useEffect(() => {
    callback.current(valid && visual.materials.every(m => receipts[m.material_key])
      ? { assessment_id: assessmentId, assessment_fingerprint: fingerprint, binding_version: visual.binding_version!,
        displays: visual.materials.map(m => ({ material_key: m.material_key, display_receipt_id: receipts[m.material_key] })) } : null)
  }, [saved, identity, valid])
  return <section aria-label="本次回答对应的原图">
    <h4>本题原图</h4>
    {!valid ? <p role="alert">原图或其审核版本已变化，不能仅凭题干重新认证这次回答。</p>
      : visual.materials.map(m => <Original key={JSON.stringify([identity, m])} childId={childId} material={m} api={api}
        done={receipt => { if (owner.current === identity) setSaved(r => ({ identity, values: { ...(r?.identity === identity ? r.values : {}), [m.material_key]: receipt } })) }}
        unavailable={() => { if (owner.current === identity) setSaved(r => { const next = { ...(r?.identity === identity ? r.values : {}) }; delete next[m.material_key]; return { identity, values: next } }) }} />)}
    <button type="button" title="重新读取本题原图" onClick={() => { callback.current(null); setSaved(null); setReload(v => v + 1) }}><RefreshCw />重新核对原图</button>
  </section>
}

function Original({ childId, material, api, done, unavailable }: { childId: string;
  material: RubricVisualMaterial['materials'][number]; api: ReturnType<typeof createVisualMaterialApi>;
  done: (receipt: string) => void; unavailable: () => void }) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof api.get>> | null>(null), [failed, setFailed] = useState(false)
  const controller = useRef<AbortController | null>(null), active = useRef(true)
  useEffect(() => {
    active.current = true
    const token = new AbortController(); controller.current = token
    const timeout = setTimeout(() => { token.abort(); if (active.current) setFailed(true) }, 15_000)
    void api.get(childId, { ...material.binding, title: '', captured_at: '', status: '' }, token.signal).then(d => {
      if (token.signal.aborted || !active.current) return
      if (!d.source_current || d.fact_status !== 'confirmed' || d.image_availability !== 'available'
        || d.binding.confirmation_id !== material.binding.confirmation_id || d.binding.facts_version !== material.binding.facts_version
        || (Object.keys(material.image_descriptor) as (keyof VisualImageDescriptor)[]).some(k => d.image_descriptor[k] !== material.image_descriptor[k])) throw Error('original changed')
      setDetail(d)
    }).catch(() => { if (active.current) setFailed(true) }).finally(() => clearTimeout(timeout))
    return () => { active.current = false; token.abort(); clearTimeout(timeout) }
  }, [childId, material, api])
  async function displayed() {
    if (!detail || !controller.current || controller.current.signal.aborted) return
    const token = controller.current
    const timeout = setTimeout(() => { token.abort(); if (active.current) { setFailed(true); unavailable() } }, 15_000)
    try {
      const receipt = await api.displayed({ ...detail, observations: { version: 'visual_facts_v1', observations: detail.observations, uncertainties: detail.uncertainties } }, token.signal)
      if (active.current && !token.signal.aborted) done(receipt.display_receipt_id)
    } catch { if (active.current) { setFailed(true); unavailable() } }
    finally { clearTimeout(timeout) }
  }
  if (failed) return <p role="alert">原图读取或显示核对失败；评分草稿仍保留，尚未允许新的批准。</p>
  if (!detail) return <p role="status">正在读取本次回答的原图…</p>
  return <VisualMaterialImage child_id={childId} session_id={detail.session_id} descriptor={detail.image_descriptor} status="ready_local"
    readImage={(d, signal) => api.image(childId, d, signal)} onDisplayed={() => void displayed()}
    onUnavailable={() => { setFailed(true); unavailable() }} />
}
