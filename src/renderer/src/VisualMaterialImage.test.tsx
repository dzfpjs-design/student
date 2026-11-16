import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VisualMaterialImage, type VisualImageDescriptor } from './VisualMaterialImage'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7S8AAAAASUVORK5CYII=', 'base64')
const descriptor: VisualImageDescriptor = { kind: 'bounded_diagram_region', artifact_id: 'page-a', revision_id: 'revision-a',
  region_id: 'figure1', region_version: 'a'.repeat(64), sha256: createHash('sha256').update(png).digest('hex'),
  width: 1, height: 1, mime_type: 'image/png' }
const bytes = () => Uint8Array.from(png).buffer
const scope = { child_id: 'child-primary-4', session_id: 'session-a', descriptor }
function decoded() {
  const image = screen.getByRole('img')
  Object.defineProperties(image, { naturalWidth: { value: 1 }, naturalHeight: { value: 1 } })
  fireEvent.load(image)
}
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('URL', class extends URL { static createObjectURL = vi.fn(() => 'blob:local-diagram'); static revokeObjectURL = vi.fn() })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('exact local diagram presentation', () => {
  it('automatically verifies bytes and actual decode before reporting display, without child buttons', async () => {
    const read = vi.fn(async () => bytes()), onDisplayed = vi.fn()
    render(<VisualMaterialImage {...scope} status="ready_local" readImage={read} onDisplayed={onDisplayed} />)
    await screen.findByRole('img')
    expect(read).toHaveBeenCalledTimes(1)
    expect(onDisplayed).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).toBeNull()
    decoded()
    expect(onDisplayed).toHaveBeenCalledWith(expect.objectContaining({ child_id: scope.child_id, session_id: scope.session_id,
      descriptor, evidence: 'image_displayed', mastery_claim_allowed: false }))
    fireEvent.load(screen.getByRole('img'))
    expect(onDisplayed).toHaveBeenCalledTimes(1)
  })

  it('shows an uncertain candidate without certifying its contents', async () => {
    const onDisplayed = vi.fn()
    render(<VisualMaterialImage {...scope} status="needs_clarification" readImage={async () => bytes()} onDisplayed={onDisplayed} />)
    await screen.findByRole('img'); decoded()
    expect(screen.getByText('图中内容待核对')).toBeVisible()
    expect(onDisplayed.mock.calls[0][0]).toMatchObject({ source_status: 'needs_clarification', mastery_claim_allowed: false })
  })

  it.each(['wrong_hash', 'not_png', 'dimensions', 'remote_field', 'oversize'] as const)('refuses %s without a display receipt', async fault => {
    const onDisplayed = vi.fn(), read = vi.fn(async () => fault === 'not_png' ? new TextEncoder().encode('<svg/>').buffer : bytes())
    const broken = { ...descriptor, ...(fault === 'wrong_hash' ? { sha256: 'b'.repeat(64) } : {}),
      ...(fault === 'dimensions' ? { width: 2 } : {}), ...(fault === 'remote_field' ? { url: 'https://untrusted.invalid/image' } : {}),
      ...(fault === 'oversize' ? { width: 20000 } : {}) }
    render(<VisualMaterialImage {...scope} descriptor={broken} status="ready_local" readImage={read} onDisplayed={onDisplayed} />)
    await screen.findByRole('alert')
    expect(screen.queryByRole('img')).toBeNull()
    expect(onDisplayed).not.toHaveBeenCalled()
    if (fault === 'remote_field' || fault === 'oversize') expect(read).not.toHaveBeenCalled()
  })

  it('discards an old child response and only displays the current scope', async () => {
    let deliver!: (value: ArrayBuffer) => void
    const old = vi.fn((_descriptor: VisualImageDescriptor, _signal: AbortSignal) => new Promise<ArrayBuffer>(resolve => { deliver = resolve })), next = vi.fn(async () => bytes()), onDisplayed = vi.fn()
    const view = render(<VisualMaterialImage {...scope} status="ready_local" readImage={old} onDisplayed={onDisplayed} />)
    await waitFor(() => expect(old).toHaveBeenCalledTimes(1))
    view.rerender(<VisualMaterialImage {...scope} child_id="child-junior-3" status="ready_local" readImage={next} onDisplayed={onDisplayed} />)
    deliver(bytes())
    await screen.findByRole('img'); decoded()
    expect(onDisplayed).toHaveBeenCalledTimes(1)
    expect(onDisplayed.mock.calls[0][0].child_id).toBe('child-junior-3')
    expect(old.mock.calls[0][1].aborted).toBe(true)
  })

  it('immediately hides and releases a previously decoded diagram on revocation', async () => {
    const read = vi.fn(async () => bytes()), onDisplayed = vi.fn()
    const view = render(<VisualMaterialImage {...scope} status="ready_local" readImage={read} onDisplayed={onDisplayed} />)
    await screen.findByRole('img'); decoded()
    view.rerender(<VisualMaterialImage {...scope} status="revoked" readImage={read} onDisplayed={onDisplayed} />)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('已经撤回')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-diagram')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('allows a voice-owned retry token after failure, without requiring a click', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(bytes())
    const view = render(<VisualMaterialImage {...scope} status="ready_local" readImage={read} />)
    await screen.findByRole('alert')
    view.rerender(<VisualMaterialImage {...scope} status="ready_local" readImage={read} reloadVersion={1} />)
    await screen.findByRole('img'); decoded()
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('bounds a stalled local read and aborts it without a retry loop', async () => {
    vi.useFakeTimers()
    const read = vi.fn((_descriptor: VisualImageDescriptor, _signal: AbortSignal) => new Promise<ArrayBuffer>(() => {}))
    render(<VisualMaterialImage {...scope} status="ready_local" readImage={read} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001) })
    expect(screen.getByRole('alert')).toBeVisible()
    expect(read.mock.calls[0][1].aborted).toBe(true)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('does not acknowledge a browser decode error', async () => {
    const onDisplayed = vi.fn()
    render(<VisualMaterialImage {...scope} status="ready_local" readImage={async () => bytes()} onDisplayed={onDisplayed} />)
    fireEvent.error(await screen.findByRole('img'))
    expect(screen.getByRole('alert')).toBeVisible()
    expect(onDisplayed).not.toHaveBeenCalled()
  })

  it('bounds browser decoding after bytes arrive and ignores a late decode', async () => {
    vi.useFakeTimers()
    const onDisplayed = vi.fn()
    render(<VisualMaterialImage {...scope} status="ready_local" readImage={async () => bytes()} onDisplayed={onDisplayed} />)
    await act(async () => { await vi.waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled(), { timeout: 2000 }) })
    const image = screen.getByRole('img')
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001) })
    expect(screen.getByRole('alert')).toBeVisible()
    expect(screen.queryByRole('img')).toBeNull()
    fireEvent.load(image)
    expect(onDisplayed).not.toHaveBeenCalled()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-diagram')
  })

  it('does not expire an image that has actually decoded', async () => {
    vi.useFakeTimers()
    const onDisplayed = vi.fn()
    render(<VisualMaterialImage {...scope} status="ready_local" readImage={async () => bytes()} onDisplayed={onDisplayed} />)
    // WebCrypto completes off-thread; one fake millisecond is not a decode gate.
    await act(async () => { await vi.waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled(), { timeout: 2000 }) })
    decoded()
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001) })
    expect(screen.getByRole('img')).toBeVisible()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onDisplayed).toHaveBeenCalledTimes(1)
  })
})
