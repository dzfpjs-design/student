import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DiagnosticVisual, diagnosticVisualView } from './DiagnosticVisual'

vi.mock('./VisualMaterialImage', () => ({ VisualMaterialImage: (p: { child_id: string }) => <span>figure:{p.child_id}</span> }))
const binding = { child_id: 'child', session_id: 'study', prompt_id: 'prompt', item_version: 'version' }
const view = () => ({ ...binding, version: 'diagnostic_visual_v1', materials: [{ material_key: 'figure', image_descriptor: { mime_type: 'image/png' } }],
  local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false })
afterEach(() => { cleanup(); vi.useRealTimers() })
it('reads exact original automatically without child controls or parent authority', async () => {
  const read = vi.fn().mockResolvedValue(view())
  render(<DiagnosticVisual binding={binding} read={read} />)
  expect(await screen.findByText('figure:child')).toBeTruthy()
  expect(read.mock.calls[0][0]).toBe('/v1/sessions/study/diagnostic-visual?prompt_id=prompt&item_version=version')
  expect(screen.queryByRole('button')).toBeNull()
})
it.each(['child_id', 'session_id', 'prompt_id', 'item_version', 'cloud_transmission_allowed', 'mastery_claim_allowed'])('rejects mismatched %s', field => {
  expect(() => diagnosticVisualView({ ...view(), [field]: 'changed' }, binding)).toThrow()
})
it('hides old child before the next response and discards a late old request', async () => {
  let finish!: (v: unknown) => void
  const read = vi.fn().mockImplementationOnce(() => new Promise(r => { finish = r })).mockResolvedValue({ ...view(), child_id: 'other' })
  const rendered = render(<DiagnosticVisual binding={binding} read={read} />)
  rendered.rerender(<DiagnosticVisual binding={{ ...binding, child_id: 'other' }} read={read} />)
  expect(await screen.findByText('figure:other')).toBeTruthy()
  await act(async () => finish(view()))
  expect(screen.queryByText('figure:child')).toBeNull()
})
it('does not retain a revoked figure after a recheck', async () => {
  const read = vi.fn().mockResolvedValueOnce(view()).mockRejectedValue(Error('withdrawn'))
  render(<DiagnosticVisual binding={binding} read={read} />)
  await screen.findByText('figure:child')
  act(() => window.dispatchEvent(new Event('hsc-visual-review-cleared')))
  await screen.findByRole('alert')
  expect(screen.queryByText('figure:child')).toBeNull()
})
it('nonvisual questions stay empty and never request substitute pictures', async () => {
  const read = vi.fn().mockResolvedValue({ ...view(), materials: [] })
  const r = render(<DiagnosticVisual binding={binding} read={read} />)
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  expect(r.container.textContent).toBe('')
  expect(read).toHaveBeenCalledTimes(1)
})
