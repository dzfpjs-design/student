import { useEffect, useState } from 'react'
import { requestDiagnosticVisual } from './api'
import { VisualMaterialImage, type VisualImageDescriptor } from './VisualMaterialImage'

export type DiagnosticVisualBinding = { child_id: string; session_id: string; prompt_id: string; item_version: string }
type Material = { material_key: string; image_descriptor: VisualImageDescriptor }
type View = DiagnosticVisualBinding & { version: 'diagnostic_visual_v1'; materials: Material[];
  local_only: true; cloud_transmission_allowed: false; mastery_claim_allowed: false }
const same = (a: DiagnosticVisualBinding, b: DiagnosticVisualBinding) =>
  (['child_id', 'session_id', 'prompt_id', 'item_version'] as const).every(k => a[k] === b[k])
export function diagnosticVisualView(value: unknown, binding: DiagnosticVisualBinding): View {
  const v = value as View | null
  if (!v || !same(v, binding) || v.version !== 'diagnostic_visual_v1' || v.local_only !== true
    || v.cloud_transmission_allowed !== false || v.mastery_claim_allowed !== false
    || !Array.isArray(v.materials) || v.materials.length > 8
    || new Set(v.materials.map(m => m?.material_key)).size !== v.materials.length
    || v.materials.some(m => !m || !/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(m.material_key)
      || !m.image_descriptor || m.image_descriptor.mime_type !== 'image/png')) throw Error('diagnostic visual binding changed')
  return v
}

export function DiagnosticVisual({ binding, read = requestDiagnosticVisual }: {
  binding: DiagnosticVisualBinding; read?: typeof requestDiagnosticVisual
}) {
  const identity = JSON.stringify(binding)
  const [state, setState] = useState<{ identity: string; view: View | null; failed: boolean } | null>(null)
  const path = `/v1/sessions/${encodeURIComponent(binding.session_id)}/diagnostic-visual`
  const query = new URLSearchParams({ prompt_id: binding.prompt_id, item_version: binding.item_version }).toString()
  useEffect(() => {
    let active = true, controller: AbortController | null = null, timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      controller?.abort()
      const request = new AbortController(); controller = request
      const timeout = setTimeout(() => request.abort(), 15_000)
      try {
        const view = diagnosticVisualView(await read(`${path}?${query}`, request.signal), binding)
        if (active && !request.signal.aborted) setState({ identity, view, failed: false })
      } catch {
        if (active && controller === request) setState({ identity, view: null, failed: true })
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
  if (!current) return <p role="status">正在核对本题材料…</p>
  if (current.failed) return <p role="alert">本题材料暂时无法核对，先不按缺失的图形判断答案。</p>
  return <>{current.view?.materials.map(m => <VisualMaterialImage key={JSON.stringify([identity, m])}
    child_id={binding.child_id} session_id={binding.session_id} descriptor={m.image_descriptor} status="ready_local"
    readImage={async (d, signal) => {
      const result = await read(`${path}/image?${query}&${new URLSearchParams({ material_key: m.material_key, sha256: d.sha256 })}`, signal)
      if (!(result instanceof ArrayBuffer)) throw Error('image response invalid')
      return result
    }} />)}</>
}
