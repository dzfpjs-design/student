import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CurriculumVisualSourcePreview, visualOriginal } from './CurriculumVisualSourcePreview'
import { CurriculumFirstSourcePreview } from './CurriculumFirstSourcePreview'
import { createVisualMaterialApi } from './visualMaterialApi'

vi.mock('./VisualMaterialImage', () => ({ VisualMaterialImage: (p: { child_id: string }) => <span>image:{p.child_id}</span> }))
const binding = { artifact_id: 'a', revision_id: 'r', region_id: 'figure', region_version: 'a'.repeat(64), facts_version: 'b'.repeat(64), confirmation_id: 'confirmed' }
const source = { schema: 'reviewed_open_response_source_v3' as const, prompt: 'Explain this figure.', task_points: ['Use the visible marks.'],
  context_requirement: 'external_material', material_semantics: 'image_required', material_bindings: [{ material_key: 'figure', visual: binding }] }
function detail() { return { child_id: 'child', session_id: 'study', binding, source_current: true, fact_status: 'confirmed',
  image_availability: 'available', observations: [{ id: 'one', description: 'Closed outline.' }], image_descriptor: {},
  local_only: true, cloud_transmission_allowed: false } }
function api(result: unknown = detail()) {
  return { get: vi.fn().mockResolvedValue(result), image: vi.fn() } as unknown as ReturnType<typeof createVisualMaterialApi>
}
afterEach(cleanup)
describe('curriculum figure review', () => {
  it('shows current exact original image and facts beside the task', async () => {
    const f = api()
    render(<CurriculumVisualSourcePreview childId="child" source={source} api={f} />)
    expect(await screen.findByText('image:child')).toBeTruthy()
    expect(screen.getByText('Closed outline.')).toBeTruthy()
    expect(screen.getByText('Use the visible marks.')).toBeTruthy()
    expect(f.get).toHaveBeenCalledWith('child', expect.objectContaining(binding), expect.any(AbortSignal))
  })
  it.each(['confirmation', 'facts', 'missing', 'withdrawn'])('does not render stale original: %s', async fault => {
    const d = detail()
    if (fault === 'confirmation') d.binding = { ...binding, confirmation_id: 'changed' }
    if (fault === 'facts') d.binding = { ...binding, facts_version: 'c'.repeat(64) }
    if (fault === 'missing') d.image_availability = 'unavailable'
    if (fault === 'withdrawn') d.source_current = false
    render(<CurriculumVisualSourcePreview childId="child" source={source} api={api(d)} />)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText('image:child')).toBeNull()
    expect(screen.queryByText('Closed outline.')).toBeNull()
  })
  it('does not show the old child after a late request', async () => {
    let done!: (d: Awaited<ReturnType<ReturnType<typeof createVisualMaterialApi>['get']>>) => void
    const f = api()
    vi.mocked(f.get).mockImplementationOnce(() => new Promise(r => { done = r }))
    const view = render(<CurriculumVisualSourcePreview childId="child" source={source} api={f} />)
    view.rerender(<CurriculumVisualSourcePreview childId="other" source={source} api={api({ ...detail(), child_id: 'other' })} />)
    await screen.findByText('image:other')
    await act(async () => done(detail() as Awaited<ReturnType<typeof f.get>>))
    await waitFor(() => expect(screen.queryByText('image:child')).toBeNull())
  })
  it('fails closed for malformed binding or missing child, without raw JSON', () => {
    expect(visualOriginal(JSON.stringify(source))).toEqual(source)
    expect(visualOriginal(JSON.stringify({ ...source, material_bindings: [{ material_key: 'figure', visual: { ...binding, url: 'remote' } }] }))).toBeNull()
    expect(visualOriginal(JSON.stringify({ ...source, material_bindings: [{ material_key: 'figure', visual: { ...binding, confirmation_id: null } }] }))).toBeNull()
    render(<CurriculumFirstSourcePreview text={JSON.stringify(source)} />)
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByText(JSON.stringify(source))).toBeNull()
  })
})
