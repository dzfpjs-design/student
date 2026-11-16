import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ParentPetMaterialsHost } from './ParentPetMaterialsHost'
const request = vi.hoisted(() => vi.fn())
vi.mock('./api', () => ({ requestParentLocal: request, requestParentPetLocal: vi.fn(), requestParentPetAudio: vi.fn() }))
vi.mock('./ParentPetAudioTeaching', () => ({ ParentPetAudioTeaching: (p: { childId: string }) => <aside aria-label="original audio purpose" data-child={p.childId} /> }))
vi.mock('./ParentPetMaterialsPanel', () => ({ ParentPetMaterialsPanel: (p: any) => <section>
  <span>{p.loading ? 'loading' : p.error ? 'error' : p.prompts.map((r: any) => r.prompt_id).join(',')}</span>
  <button onClick={p.onRefreshPrompts}>refresh</button></section> }))
const result = (child: string, prompt: string) => ({ child_id: child, local_only: true, cloud_transmission_allowed: false,
  prompts: [{ child_id: child, prompt_id: prompt }] })
beforeEach(() => request.mockReset())
afterEach(cleanup)
it('uses real parent endpoint and preserves child isolation against late results', async () => {
  let first: (value: unknown) => void = () => undefined
  request.mockImplementationOnce(() => new Promise(resolve => { first = resolve }))
    .mockResolvedValueOnce(result('b', 'b-question'))
  const view = render(<ParentPetMaterialsHost childId="a" />)
  view.rerender(<ParentPetMaterialsHost childId="b" />)
  await screen.findByText('b-question')
  expect(screen.getByLabelText('original audio purpose').getAttribute('data-child')).toBe('b')
  await act(async () => first(result('a', 'a-question')))
  expect(screen.queryByText('a-question')).toBeNull()
  expect(request.mock.calls[0][1].signal.aborted).toBe(true)
  expect(request.mock.calls[1][0]).toBe('/v1/parent/children/b/pet-prompts')
})
it('rejects wrong ownership and allows a fresh server retry without generating material', async () => {
  request.mockResolvedValueOnce(result('b', 'wrong')).mockResolvedValueOnce(result('a', 'correct'))
  render(<ParentPetMaterialsHost childId="a" />)
  await screen.findByText('error')
  fireEvent.click(screen.getByText('refresh'))
  await screen.findByText('correct')
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
  expect(request.mock.calls.every(call => !call[1].method)).toBe(true)
})
