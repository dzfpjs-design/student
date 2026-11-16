import { useEffect, useRef, useState } from 'react'
import { requestTutorVisual } from './api'
import { VisualMaterialImage, type VisualImageDescriptor } from './VisualMaterialImage'

type Binding = { child_id: string; session_id: string; turn_id: string }
type View = Binding & { version: 'visual_teaching_v1'; prompt_id: string; item_version: string; source_version: string;
  teaching_scope: 'observation_scaffold'; local_only: true; cloud_transmission_allowed: false; mastery_claim_allowed: false;
  materials: { material_key: string; image_descriptor: VisualImageDescriptor }[] }
export function tutorVisualView(value: unknown, binding: Binding): View {
  const v = value as View | null
  if (!v || !(['child_id', 'session_id', 'turn_id'] as const).every(k => v[k] === binding[k])
    || v.version !== 'visual_teaching_v1' || v.teaching_scope !== 'observation_scaffold' || v.local_only !== true
    || v.cloud_transmission_allowed !== false || v.mastery_claim_allowed !== false
    || !(['prompt_id', 'item_version', 'source_version'] as const).every(k => typeof v[k] === 'string' && v[k].trim())
    || !Array.isArray(v.materials) || !v.materials.length || v.materials.length > 8
    || new Set(v.materials.map(m => m?.material_key)).size !== v.materials.length
    || v.materials.some(m => !m || !/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(m.material_key)
      || !m.image_descriptor || m.image_descriptor.mime_type !== 'image/png')) throw Error('tutor visual binding changed')
  return v
}

export function TutorVisual({ binding, read = requestTutorVisual, onUnavailable }: {
  binding: Binding; read?: typeof requestTutorVisual; onUnavailable?: () => void
}) {
  const identity = JSON.stringify(binding)
  const notify = useRef(onUnavailable); notify.current = onUnavailable
  const [state, setState] = useState<{ identity: string; view: View | null } | null>(null)
  const path = `/v1/sessions/${encodeURIComponent(binding.session_id)}/tutor-visual`
  const query = new URLSearchParams({ turn_id: binding.turn_id }).toString()
  useEffect(() => {
    let active = true, controller: AbortController | null = null, timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      controller?.abort()
      const request = new AbortController(); controller = request
      const timeout = setTimeout(() => request.abort(), 15_000)
      try {
        const view = tutorVisualView(await read(`${path}?${query}`, request.signal), binding)
        if (active && !request.signal.aborted) setState({ identity, view })
      } catch {
        if (active && controller === request) { setState({ identity, view: null }); notify.current?.() }
      } finally {
        clearTimeout(timeout)
        if (active && controller === request) timer = setTimeout(() => void refresh(), 15_000)
      }
    }
    void refresh()
    const invalidate = () => { clearTimeout(timer); setState(null); void refresh() }
    window.addEventListener('hsc-visual-review-cleared', invalidate)
    window.addEventListener('focus', invalidate)
    return () => { active = false; controller?.abort(); clearTimeout(timer)
      window.removeEventListener('hsc-visual-review-cleared', invalidate); window.removeEventListener('focus', invalidate) }
  }, [identity, path, query, read])
  const current = state?.identity === identity ? state : null
  if (!current) return <p role="status">正在核对这轮讲解的原图…</p>
  if (!current.view) return <p role="alert">这轮讲解的原图暂时无法读取，先不根据缺失的图形推断答案。</p>
  return <>{current.view.materials.map(m => <VisualMaterialImage key={JSON.stringify([identity, current.view?.source_version, m])}
    child_id={binding.child_id} session_id={binding.session_id} descriptor={m.image_descriptor} status="ready_local"
    onUnavailable={() => notify.current?.()}
    readImage={async (d, signal) => {
      const result = await read(`${path}/image?${query}&${new URLSearchParams({ material_key: m.material_key, sha256: d.sha256 })}`, signal)
      if (!(result instanceof ArrayBuffer)) throw Error('image response invalid')
      return result
    }} />)}</>
}
