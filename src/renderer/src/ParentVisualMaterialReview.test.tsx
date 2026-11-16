import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ParentVisualMaterialReview, type VisualReviewDetail, type VisualReviewCommand } from './ParentVisualMaterialReview'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7S8AAAAASUVORK5CYII=', 'base64')
const flags = { local_only: true, cloud_transmission_allowed: false } as const
const binding = { artifact_id: 'page-1', revision_id: 'revision-1', region_id: 'diagram', region_version: 'a'.repeat(64), facts_version: 'b'.repeat(64), confirmation_id: null }
const detail: VisualReviewDetail = { ...flags, child_id: 'fourth-grade', session_id: 'ended-session', binding,
  fact_status: 'unconfirmed', image_availability: 'available', parent_confirmation_ready: true, source_current: true,
  image_descriptor: { kind: 'bounded_diagram_region', ...binding, sha256: createHash('sha256').update(png).digest('hex'), width: 1, height: 1, mime_type: 'image/png' },
  observations: { version: 'visual_facts_v1', observations: [{ id: 'length', kind: 'label', description: 'AB长6厘米', bbox: [0, 0, 1, 1] }], uncertainties: [] } }
// Descriptor deliberately carries no approval metadata.
delete (detail.image_descriptor as unknown as Record<string, unknown>).facts_version
delete (detail.image_descriptor as unknown as Record<string, unknown>).confirmation_id
const display = () => Promise.resolve({ ...flags, display_receipt_id: 'shown-on-local-app' })
const readImage = () => Promise.resolve(Uint8Array.from(png).buffer)
const refresh = vi.fn(async () => {})
const result = (c: VisualReviewCommand) => ({ ...flags, status: c.decision === 'confirm' ? 'ready_local' as const : 'withdrawn' as const,
  binding: { ...c.binding, confirmation_id: c.decision === 'confirm' ? 'visual:confirmed' : null } })
async function seeAndCheck() {
  const img = await screen.findByRole('img')
  Object.defineProperties(img, { naturalWidth: { value: 1 }, naturalHeight: { value: 1 } })
  fireEvent.load(img)
  await waitFor(() => expect(screen.getByRole('checkbox')).toBeEnabled())
  fireEvent.click(screen.getByRole('checkbox'))
}
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks(); vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('URL', class extends URL { static createObjectURL = vi.fn(() => 'blob:local-image'); static revokeObjectURL = vi.fn() })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('parent exact visual facts review', () => {
  it('withdraws stored unparsed material and recovers its null-facts operation after restart', async () => {
    const unparsed = { ...detail, storage_status: 'stored', parent_confirmation_ready: false,
      binding: { ...binding, facts_version: null }, observations: { ...detail.observations, observations: [] } } as unknown as VisualReviewDetail
    const submit = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockImplementation(async (c: VisualReviewCommand) => result(c))
    const props = { detail: unparsed, readImage, recordDisplayed: display, submit, refresh }
    const view = render(<ParentVisualMaterialReview {...props} />)
    expect(screen.queryByRole('button', { name: '确认图形内容' })).toBeNull()
    expect(await screen.findByRole('img')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '撤回本机使用' }))
    await screen.findByRole('alert')
    const original = submit.mock.calls[0][0]
    expect(original.binding.facts_version).toBeNull()
    view.unmount(); render(<ParentVisualMaterialReview {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '核对原操作' }))
    await screen.findByText('图形的本机使用授权已撤回。')
    expect(submit.mock.calls[1][0]).toEqual(original)
    expect(localStorage.length).toBe(0)
  })
  it('requires a decoded original and durable display receipt before sending confirmation', async () => {
    const submit = vi.fn(async (c: VisualReviewCommand) => {
      expect(localStorage.length).toBe(1)
      expect(JSON.parse(localStorage.getItem(localStorage.key(0)!)!).client_action_id).toBe(c.client_action_id)
      return result(c)
    })
    render(<ParentVisualMaterialReview detail={detail} readImage={readImage} recordDisplayed={display} submit={submit} refresh={refresh} />)
    expect(screen.getByRole('button', { name: '确认图形内容' })).toBeDisabled()
    await seeAndCheck()
    fireEvent.click(screen.getByRole('button', { name: '确认图形内容' }))
    await screen.findByText('图形内容的确认已保存；这不代表孩子已经掌握。')
    expect(submit.mock.calls[0][0]).toMatchObject({ decision: 'confirm', display_receipt_id: 'shown-on-local-app', binding })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(localStorage.length).toBe(0)
  })

  it('recovers the identical operation after lost response and component restart', async () => {
    const submit = vi.fn().mockRejectedValueOnce(new Error('lost response')).mockImplementation(async (c: VisualReviewCommand) => result(c))
    const props = { detail, readImage, recordDisplayed: display, submit, refresh }
    const view = render(<ParentVisualMaterialReview {...props} />)
    await seeAndCheck(); fireEvent.click(screen.getByRole('button', { name: '确认图形内容' }))
    await screen.findByRole('alert')
    const original = submit.mock.calls[0][0]
    view.unmount(); render(<ParentVisualMaterialReview {...props} />)
    expect(submit).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '核对原操作' }))
    await screen.findByText('图形内容的确认已保存；这不代表孩子已经掌握。')
    expect(submit.mock.calls[1][0]).toEqual(original)
    expect(localStorage.length).toBe(0)
  })

  it('never sends a new decision if durable local recovery cannot be written', async () => {
    const submit = vi.fn()
    render(<ParentVisualMaterialReview detail={detail} readImage={readImage} recordDisplayed={display} submit={submit} refresh={refresh} />)
    await seeAndCheck()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full disk') })
    fireEvent.click(screen.getByRole('button', { name: '确认图形内容' }))
    await screen.findByText('本机无法保存审核操作，本次决定尚未发送。')
    expect(submit).not.toHaveBeenCalled()
  })

  it('can retract previous confirmation even after image bytes disappear', async () => {
    const submit = vi.fn(async (c: VisualReviewCommand) => result(c))
    const lost: VisualReviewDetail = { ...detail, fact_status: 'confirmed', image_availability: 'unavailable', binding: { ...binding, confirmation_id: 'visual:previous' } }
    render(<ParentVisualMaterialReview detail={lost} readImage={readImage} recordDisplayed={display} submit={submit} refresh={refresh} />)
    fireEvent.click(screen.getByRole('button', { name: '撤回确认' }))
    await screen.findByText('图形内容的确认已撤回。')
    expect(submit.mock.calls[0][0].decision).toBe('retract')
    expect(submit.mock.calls[0][0].display_receipt_id).toBeUndefined()
  })

  it('a late display acknowledgement cannot authorize another child', async () => {
    let release!: (v: Awaited<ReturnType<typeof display>>) => void
    const recordDisplayed = vi.fn(() => new Promise<Awaited<ReturnType<typeof display>>>(resolve => { release = resolve }))
    const submit = vi.fn(), props = { readImage, recordDisplayed, submit, refresh }
    const view = render(<ParentVisualMaterialReview detail={detail} {...props} />)
    const img = await screen.findByRole('img')
    Object.defineProperties(img, { naturalWidth: { value: 1 }, naturalHeight: { value: 1 } }); fireEvent.load(img)
    await waitFor(() => expect(recordDisplayed).toHaveBeenCalledTimes(1))
    view.rerender(<ParentVisualMaterialReview detail={{ ...detail, child_id: 'junior-third', session_id: 'session-other' }} {...props} />)
    await act(async () => release(await display()))
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: '确认图形内容' })).toBeDisabled()
    expect(submit).not.toHaveBeenCalled()
  })

  it('uncertain descriptions and host-unverified assets do not unlock confirmation', async () => {
    render(<ParentVisualMaterialReview detail={{ ...detail, image_availability: 'host_unverified', observations: { ...detail.observations, uncertainties: ['角度标记看不清'] } }}
      readImage={readImage} recordDisplayed={display} submit={vi.fn()} refresh={refresh} />)
    expect(screen.getByText('角度标记看不清')).toBeVisible()
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByRole('button', { name: '确认图形内容' })).toBeDisabled()
  })

  it('a persisted command is not discarded when authoritative refresh fails', async () => {
    const submit = vi.fn(async (c: VisualReviewCommand) => result(c))
    render(<ParentVisualMaterialReview detail={detail} readImage={readImage} recordDisplayed={display} submit={submit} refresh={async () => { throw new Error('restart') }} />)
    await seeAndCheck(); fireEvent.click(screen.getByRole('button', { name: '确认图形内容' }))
    await screen.findByRole('alert')
    expect(localStorage.length).toBe(1)
    expect(screen.getByRole('button', { name: '核对原操作' })).toBeEnabled()
  })
  it('shows the actual retraction reason even when retraction has made bytes unavailable', () => {
    render(<ParentVisualMaterialReview detail={{ ...detail, fact_status: 'retracted', image_availability: 'unavailable' }} readImage={readImage}
      recordDisplayed={display} submit={vi.fn()} refresh={refresh} />)
    expect(screen.getByText('这份图形材料已经撤回，暂不用于判断答案。')).toBeVisible()
    expect(screen.queryByRole('img')).toBeNull()
  })
})
