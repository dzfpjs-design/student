import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RubricVisualReview, type RubricVisualMaterial } from './RubricVisualReview'
import type { createVisualMaterialApi } from './visualMaterialApi'
vi.mock('./VisualMaterialImage', () => ({ VisualMaterialImage: (p: { onDisplayed: () => void }) => <button onClick={p.onDisplayed}>synthetic decoded image</button> }))
const b = { artifact_id: 'page', revision_id: 'r', region_id: 'figure', region_version: 'a'.repeat(64), facts_version: 'b'.repeat(64), confirmation_id: 'confirmation' }
const d = { kind: 'bounded_diagram_region' as const, artifact_id: 'page', revision_id: 'r', region_id: 'figure', region_version: b.region_version,
  sha256: 'c'.repeat(64), width: 100, height: 120, mime_type: 'image/png' as const }
const visual: RubricVisualMaterial = { version: 'rubric_visual_material_v1', status: 'current', source_current: true,
  image_availability: 'available', binding_version: 'visual-v1', materials: [{ material_key: 'figure', binding: b, image_descriptor: d }],
  reason: null, local_only: true, cloud_transmission_allowed: false }
const detail = () => ({ child_id: 'child', session_id: 'capture', binding: b, image_descriptor: d, observations: [], uncertainties: [],
  source_current: true, fact_status: 'confirmed', image_availability: 'available', local_only: true, cloud_transmission_allowed: false })
function makeApi(value: unknown = detail()) {
  return { get: vi.fn().mockResolvedValue(value), displayed: vi.fn().mockResolvedValue({ display_receipt_id: 'display-1' }), image: vi.fn() } as unknown as ReturnType<typeof createVisualMaterialApi>
}
afterEach(cleanup)
it('requires verified image display before emitting exact assessment evidence', async () => {
  const onEvidence = vi.fn(), api = makeApi()
  render(<RubricVisualReview childId="child" assessmentId="a" fingerprint="f" visual={visual} api={api} onEvidence={onEvidence} />)
  await screen.findByText('synthetic decoded image')
  expect(onEvidence.mock.calls.every(([v]) => v === null)).toBe(true)
  fireEvent.click(screen.getByText('synthetic decoded image'))
  await waitFor(() => expect(onEvidence).toHaveBeenLastCalledWith({ assessment_id: 'a', assessment_fingerprint: 'f', binding_version: 'visual-v1', displays: [{ material_key: 'figure', display_receipt_id: 'display-1' }] }))
  expect(api.displayed).toHaveBeenCalledOnce()
})
it.each(['confirmation', 'facts', 'pixels', 'withdrawn'])('rejects changed source %s without a display receipt', async fault => {
  const data = detail()
  if (fault === 'confirmation') data.binding = { ...b, confirmation_id: 'other' }
  if (fault === 'facts') data.binding = { ...b, facts_version: 'd'.repeat(64) }
  if (fault === 'pixels') data.image_descriptor = { ...d, sha256: 'e'.repeat(64) }
  if (fault === 'withdrawn') data.source_current = false
  const api = makeApi(data), onEvidence = vi.fn()
  render(<RubricVisualReview childId="child" assessmentId="a" fingerprint="f" visual={visual} api={api} onEvidence={onEvidence} />)
  await screen.findByRole('alert')
  expect(api.displayed).not.toHaveBeenCalled()
  expect(onEvidence.mock.calls.every(([v]) => v === null)).toBe(true)
})
it('old assessment display completion cannot authorize a newer answer', async () => {
  const api = makeApi(), onEvidence = vi.fn()
  let finish!: (v: never) => void
  vi.mocked(api.displayed).mockImplementationOnce(() => new Promise(r => { finish = r }))
  const view = render(<RubricVisualReview childId="child" assessmentId="a" fingerprint="f" visual={visual} api={api} onEvidence={onEvidence} />)
  fireEvent.click(await screen.findByText('synthetic decoded image'))
  view.rerender(<RubricVisualReview childId="child" assessmentId="other" fingerprint="new" visual={visual} api={api} onEvidence={onEvidence} />)
  await act(async () => finish({ display_receipt_id: 'old' } as never))
  expect(onEvidence.mock.calls.every(([v]) => v === null)).toBe(true)
})
it('retry clears old display qualification until the new decode finishes', async () => {
  const api = makeApi(), onEvidence = vi.fn()
  render(<RubricVisualReview childId="child" assessmentId="a" fingerprint="f" visual={visual} api={api} onEvidence={onEvidence} />)
  fireEvent.click(await screen.findByText('synthetic decoded image'))
  await waitFor(() => expect(onEvidence.mock.calls.at(-1)?.[0]).not.toBeNull())
  fireEvent.click(screen.getByTitle('重新读取本题原图'))
  await screen.findByText('synthetic decoded image')
  expect(onEvidence).toHaveBeenLastCalledWith(null)
})
