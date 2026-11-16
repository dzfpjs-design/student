import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { ParentVisualMaterialsHost } from './ParentVisualMaterialsHost'
import { createVisualMaterialApi, validateVisualDetail, type VisualListItem } from './visualMaterialApi'

const flags = { local_only: true, cloud_transmission_allowed: false } as const
const ref: VisualListItem = { artifact_id: 'p1', revision_id: 'r1', region_id: 'figure', region_version: 'a'.repeat(64), facts_version: null,
  title: '数学作业图形', captured_at: '2026-09-10T15:00:00Z', status: 'needs_storage' }
const detail = { ...flags, child_id: 'child-a', session_id: 'ended', binding: { ...ref, confirmation_id: null },
  image_descriptor: { kind: 'bounded_diagram_region', artifact_id: 'p1', revision_id: 'r1', region_id: 'figure', region_version: ref.region_version,
    sha256: 'b'.repeat(64), width: 1, height: 1, mime_type: 'image/png' },
  observations: [], uncertainties: [], facts_version: null, fact_status: 'unconfirmed', status: 'needs_storage', storage_status: 'not_stored',
  image_availability: 'unavailable', parent_confirmation_ready: false, source_current: true }
function api() {
  return { list: vi.fn(async () => ({ ...flags, child_id: 'child-a', items: [ref], offset: 0, total: 1 })), get: vi.fn(async () => detail),
    store: vi.fn(async () => ({ ...detail, storage_status: 'stored', image_availability: 'available' })), image: vi.fn(), displayed: vi.fn(), submit: vi.fn(), clearImages: vi.fn() } as unknown as ReturnType<typeof createVisualMaterialApi>
}
beforeEach(() => { localStorage.clear(); vi.stubGlobal('crypto', webcrypto) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
describe('actual parent visual host flow', () => {
  it('rejects a null-facts response carrying another non-null fact binding', () => {
    expect(() => validateVisualDetail('child-a', ref, { ...detail, binding: { ...detail.binding, facts_version: 'c'.repeat(64) } })).toThrow()
    expect(validateVisualDetail('child-a', ref, detail).facts_version).toBeNull()
  })
  it('does not offer new retention for a stale source', async () => {
    const client = api()
    vi.mocked(client.get).mockResolvedValue({ ...detail, source_current: false, storage_status: 'revoked' } as never)
    render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    await screen.findByText('这幅图尚无可核对的标记与关系，暂不据此评分。')
    expect(screen.queryByRole('button', { name: '重新授权本机使用' })).toBeNull()
    expect(client.store).not.toHaveBeenCalled()
  })
  it.each(['revoked', 'quarantined'])('requires new explicit consent for %s storage', async storage_status => {
    const client = api()
    vi.mocked(client.get).mockResolvedValue({ ...detail, storage_status } as never)
    render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    const button = await screen.findByRole('button', { name: '重新授权本机使用' })
    expect(button).toBeDisabled()
    expect(client.store).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(button)
    await waitFor(() => expect(client.store).toHaveBeenCalledTimes(1))
    expect(vi.mocked(client.store).mock.calls[0][1].facts_version).toBeNull()
  })
  it('requires a scoped retain choice, durably saves its action before request, and displays the stored result', async () => {
    const client = api()
    vi.mocked(client.store).mockImplementation(async (_child, _ref, action) => {
      expect(JSON.parse(localStorage.getItem('hsc-visual-storage:child-a')!).client_action_id).toBe(action)
      return { ...detail, storage_status: 'stored', image_availability: 'available' } as never
    })
    render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    expect(await screen.findByRole('button', { name: '保留这幅原图' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '保留这幅原图' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '保留这幅原图' })).toBeNull())
    expect(client.store).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('hsc-visual-storage:child-a')).toBeNull()
    expect(screen.getByText('这幅图尚无可核对的标记与关系，暂不据此评分。')).toBeVisible()
  })
  it('recovers the same storage operation after restart even when current detail cannot be read', async () => {
    const client = api()
    vi.mocked(client.store).mockRejectedValueOnce(new Error('response lost'))
    const view = render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    await screen.findByRole('checkbox'); fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '保留这幅原图' }))
    await screen.findByRole('alert'); const original = vi.mocked(client.store).mock.calls[0]
    view.unmount(); vi.mocked(client.get).mockRejectedValue(new Error('backend reloading'))
    render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    fireEvent.click(screen.getByRole('button', { name: '核对原保留操作' }))
    await waitFor(() => expect(client.store).toHaveBeenCalledTimes(2))
    expect(vi.mocked(client.store).mock.calls[1].slice(0, 3)).toEqual(original.slice(0, 3))
    await waitFor(() => expect(localStorage.getItem('hsc-visual-storage:child-a')).toBeNull())
  })
  it('clear notification removes in-memory recovery as well as the stored operation', async () => {
    localStorage.setItem('hsc-visual-storage:child-a', JSON.stringify({ child_id: 'child-a', ref, client_action_id: 'original' }))
    const client = api(); render(<ParentVisualMaterialsHost childId="child-a" api={client} />)
    expect(screen.getByRole('button', { name: '核对原保留操作' })).toBeVisible()
    await act(async () => {
      localStorage.removeItem('hsc-visual-storage:child-a')
      window.dispatchEvent(new CustomEvent('hsc-visual-review-cleared', { detail: 'child-a' }))
    })
    expect(screen.queryByRole('button', { name: '核对原保留操作' })).toBeNull()
    expect(client.clearImages).toHaveBeenCalled()
  })
})
