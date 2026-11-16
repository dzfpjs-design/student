import { useEffect, useRef, useState } from 'react'

export type VisualImageDescriptor = {
  kind: 'bounded_diagram_region'; artifact_id: string; revision_id: string; region_id: string
  region_version: string; sha256: string; width: number; height: number; mime_type: 'image/png'
}
type SourceStatus = 'ready_local' | 'needs_clarification' | 'revoked' | 'unavailable'
type DisplayReceipt = {
  child_id: string; session_id: string; descriptor: VisualImageDescriptor
  source_status: 'ready_local' | 'needs_clarification'; evidence: 'image_displayed'; mastery_claim_allowed: false
}
type Props = {
  child_id: string; session_id: string; descriptor: VisualImageDescriptor; status: SourceStatus
  readImage: (descriptor: VisualImageDescriptor, signal: AbortSignal) => Promise<ArrayBuffer>
  reloadVersion?: number; onDisplayed?: (receipt: DisplayReceipt) => void; onUnavailable?: () => void
}
const fields = ['kind', 'artifact_id', 'revision_id', 'region_id', 'region_version', 'sha256', 'width', 'height', 'mime_type']
const digestPattern = /^[a-f0-9]{64}$/
function validDescriptor(value: VisualImageDescriptor): boolean {
  return !!value && typeof value === 'object' && Object.keys(value).length === fields.length
    && Object.keys(value).every(key => fields.includes(key))
    && value.kind === 'bounded_diagram_region' && value.mime_type === 'image/png'
    && [value.artifact_id, value.revision_id, value.region_id].every(v => typeof v === 'string' && !!v && v.length <= 128)
    && [value.region_version, value.sha256].every(v => typeof v === 'string' && digestPattern.test(v))
    && [value.width, value.height].every(v => Number.isInteger(v) && v > 0 && v <= 8192)
    && value.width * value.height <= 4_000_000
}
async function verifyPng(bytes: ArrayBuffer, expected: VisualImageDescriptor): Promise<void> {
  if (!(bytes instanceof ArrayBuffer) || bytes.byteLength < 33 || bytes.byteLength > 2 * 1024 * 1024) throw new Error('image size')
  const head = new Uint8Array(bytes, 0, 24), view = new DataView(bytes)
  if (![137, 80, 78, 71, 13, 10, 26, 10].every((v, i) => v === head[i])
    || view.getUint32(8) !== 13 || ![73, 72, 68, 82].every((v, i) => v === head[i + 12])
    || view.getUint32(16) !== expected.width || view.getUint32(20) !== expected.height) throw new Error('image format')
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
    .map(v => v.toString(16).padStart(2, '0')).join('')
  if (hash !== expected.sha256) throw new Error('image version')
}

export function VisualMaterialImage({ child_id, session_id, descriptor, status, readImage, reloadVersion = 0, onDisplayed, onUnavailable }: Props) {
  const valid = validDescriptor(descriptor) && [child_id, session_id].every(v => typeof v === 'string' && !!v && v.length <= 128)
  const allowed = valid && (status === 'ready_local' || status === 'needs_clarification')
  const identity = JSON.stringify([child_id, session_id, fields.map(k => descriptor?.[k as keyof VisualImageDescriptor]), status, reloadVersion])
  const current = useRef(identity); current.current = identity
  const reader = useRef(readImage); reader.current = readImage
  const notify = useRef(onDisplayed); notify.current = onDisplayed
  const unavailableNotify = useRef(onUnavailable); unavailableNotify.current = onUnavailable
  const [image, setImage] = useState<{ identity: string; url: string; error: boolean } | null>(null)
  const displayed = useRef('')
  const decoding = useRef<{ identity: string; complete: () => boolean; fail: () => void } | null>(null)
  useEffect(() => {
    if (!allowed) return
    const token = new AbortController(), frozen = Object.freeze({ ...descriptor })
    let url = ''
    let complete = false
    const release = () => { if (url) { URL.revokeObjectURL(url); url = '' } }
    const fail = () => {
      token.abort()
      clearTimeout(timeout)
      release()
      if (current.current === identity) { setImage({ identity, url: '', error: true }); unavailableNotify.current?.() }
    }
    const timeout = setTimeout(fail, 15_000)
    const operation = { identity, fail, complete: () => {
      if (token.signal.aborted || complete || current.current !== identity) return false
      complete = true; clearTimeout(timeout); return true
    } }
    decoding.current = operation
    displayed.current = ''
    void (async () => {
      try {
        const bytes = await reader.current(frozen, token.signal)
        if (token.signal.aborted || current.current !== identity) return
        await verifyPng(bytes, frozen)
        if (token.signal.aborted || current.current !== identity) return
        url = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }))
        setImage({ identity, url, error: false })
      } catch {
        if (!token.signal.aborted && current.current === identity) fail()
      }
    })()
    return () => { token.abort(); clearTimeout(timeout); release(); if (decoding.current === operation) decoding.current = null }
  }, [identity, allowed])
  const visible = image?.identity === identity ? image : null
  const unavailable = !allowed || visible?.error
  return <figure aria-label="本题图形材料" style={{ margin: '12px 0', maxWidth: 720 }}>
    {unavailable ? <p role="alert">{status === 'revoked' ? '这份图形材料已经撤回，暂不用于判断答案。' : '这幅图暂时无法核对，先不按它判断答案。'}</p>
      : <div style={{ aspectRatio: `${descriptor.width} / ${descriptor.height}`, maxHeight: 620, width: '100%', display: 'grid', placeItems: 'center' }}>
        {visible?.url ? <img key={identity} src={visible.url} alt="本题的图形材料" width={descriptor.width} height={descriptor.height}
          style={{ width: '100%', height: '100%', maxHeight: 620, objectFit: 'contain', minWidth: 0 }}
          onLoad={event => {
            if (current.current !== identity || displayed.current === identity) return
            if (event.currentTarget.naturalWidth !== descriptor.width || event.currentTarget.naturalHeight !== descriptor.height) {
              if (decoding.current?.identity === identity) decoding.current.fail()
              return
            }
            if (decoding.current?.identity !== identity || !decoding.current.complete()) return
            displayed.current = identity
            notify.current?.({ child_id, session_id, descriptor: { ...descriptor }, source_status: status as DisplayReceipt['source_status'],
              evidence: 'image_displayed', mastery_claim_allowed: false })
          }} onError={() => { if (current.current === identity && decoding.current?.identity === identity) decoding.current.fail() }} />
          : <p role="status">图形载入中…</p>}
      </div>}
    {!unavailable && status === 'needs_clarification' && <figcaption>图中内容待核对</figcaption>}
  </figure>
}
