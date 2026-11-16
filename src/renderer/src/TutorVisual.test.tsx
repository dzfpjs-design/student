import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TutorVisual, tutorVisualView } from './TutorVisual'

vi.mock('./VisualMaterialImage', () => ({ VisualMaterialImage: (p: { child_id: string }) => <span>original:{p.child_id}</span> }))
const binding = { child_id: 'child', session_id: 'session', turn_id: 'saved-turn' }
const manifest = () => ({ ...binding, version: 'visual_teaching_v1', source_version: 'source-hash',
  prompt_id: 'original-prompt', item_version: 'original-version', teaching_scope: 'observation_scaffold',
  local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false,
  materials: [{ material_key: 'figure', image_descriptor: { mime_type: 'image/png' } }] })
afterEach(() => cleanup())
it('restores the exact saved tutor figure automatically without child controls', async () => {
  const read = vi.fn().mockResolvedValue(manifest())
  render(<TutorVisual binding={binding} read={read} />)
  await screen.findByText('original:child')
  expect(read.mock.calls[0][0]).toBe('/v1/sessions/session/tutor-visual?turn_id=saved-turn')
  expect(screen.queryByRole('button')).toBeNull()
})
it.each(['child_id', 'session_id', 'turn_id', 'version', 'teaching_scope', 'local_only', 'cloud_transmission_allowed', 'mastery_claim_allowed'])('rejects mismatched %s', field => {
  expect(() => tutorVisualView({ ...manifest(), [field]: 'changed' }, binding)).toThrow()
})
it.each(['prompt_id', 'item_version', 'source_version'])('rejects missing %s', field => {
  expect(() => tutorVisualView({ ...manifest(), [field]: '' }, binding)).toThrow()
})
it('never substitutes an empty original or duplicated figure', () => {
  expect(() => tutorVisualView({ ...manifest(), materials: [] }, binding)).toThrow()
  expect(() => tutorVisualView({ ...manifest(), materials: [...manifest().materials, ...manifest().materials] }, binding)).toThrow()
})
it('discards old-child responses and hides a revoked original', async () => {
  let resolve!: (v: unknown) => void
  const read = vi.fn().mockImplementationOnce(() => new Promise(r => { resolve = r }))
    .mockResolvedValueOnce({ ...manifest(), child_id: 'other' }).mockRejectedValue(Error('withdrawn'))
  const view = render(<TutorVisual binding={binding} read={read} />)
  view.rerender(<TutorVisual binding={{ ...binding, child_id: 'other' }} read={read} />)
  await screen.findByText('original:other')
  await act(async () => resolve(manifest()))
  expect(screen.queryByText('original:child')).toBeNull()
  act(() => window.dispatchEvent(new Event('hsc-visual-review-cleared')))
  await screen.findByRole('alert')
  expect(screen.queryByText('original:other')).toBeNull()
})
it('notifies the playback owner when the exact image source is unavailable', async () => {
  const unavailable = vi.fn(), read = vi.fn().mockRejectedValue(Error('revoked'))
  render(<TutorVisual binding={binding} read={read} onUnavailable={unavailable} />)
  await screen.findByRole('alert')
  expect(unavailable).toHaveBeenCalledOnce()
})
