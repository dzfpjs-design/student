import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { requestParentDraftNavigation } from './parentDraftNavigation'
import type { ScopedParentDraftRecoveryPort } from './parentDraftRecoveryPort'
import type { ParentDraftRecoveryDraftReceipt, ParentDraftRecoveryPrepared } from '../../shared/parentDraftRecovery'
const detaches: (() => void)[] = []
afterEach(() => { cleanup(); detaches.splice(0).forEach(detach => detach()) })
const grant = (): ParentDraftRecoveryPrepared => ({ leaseId: 'synthetic-current-lease', localRevision: 0, hasRecovery: false, protection: 'native_encrypted' })
const empty = (): ParentDraftRecoveryDraftReceipt => ({ child_id: 'synthetic-child', scope: 'capture', storage_generation: 'synthetic-generation', revision: 0, payload: null, last_action_id: null, last_expected_revision: null, updated_at: null, stale_refs: [], cloud_transmission_allowed: false })
/** Only native-port and API boundaries are synthetic; real component, Host and Journal run unchanged. */
function fixture(prepare: ScopedParentDraftRecoveryPort['prepare']) {
 let saved = empty(), failCheckpoint = false, localRevision = 0
 const checkpoint = vi.fn((_lease: string, sequence: number) => {
  if (failCheckpoint) throw Error('synthetic protection failure')
  return { stored: true as const, sequence, localRevision: ++localRevision }
 })
 const port: ScopedParentDraftRecoveryPort = { prepare, checkpoint, read: async () => { throw Error('unexpected read') }, discard: async () => { throw Error('unexpected discard') }, revoke: vi.fn() }
 const api = vi.fn(async (path: string, init?: RequestInit) => {
  if (path.includes('/drafts/')) {
   if (init?.method === 'PUT') { const request = JSON.parse(String(init.body)); saved = { ...saved, revision: saved.revision + 1, payload: request.payload, last_action_id: request.client_action_id, last_expected_revision: request.expected_revision } }
   return { draft: saved }
  }
  return { items: [], nodes: [] }
 })
 const owner = new ParentTextbookDraftHost(() => port)
 detaches.push(owner.attach())
 const entry = owner.entry(api, 'synthetic-child', 'capture')
 render(<TextbookLanguageCandidateReview draftHost={owner} api={api} childId="synthetic-child" captureOnly />)
 return { api, checkpoint, entry, failCheckpoint: () => { failCheckpoint = true } }
}
function unload(): boolean { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented }
it('allows untouched disabled editor to unload while native preparation is pending', async () => {
 const prepare = vi.fn(() => new Promise<ParentDraftRecoveryPrepared>(() => {})), f = fixture(prepare)
 await waitFor(() => expect(prepare).toHaveBeenCalledOnce())
 expect(screen.getByLabelText('教材页标题')).toBeDisabled()
 expect(f.entry.journal.state.payload).toBeNull(); expect(f.entry.capture).toBeNull()
 expect(f.checkpoint).not.toHaveBeenCalled(); expect(f.api.mock.calls.filter(([path]) => path.includes('/drafts/'))).toHaveLength(0)
 expect(requestParentDraftNavigation()).toBe(true)
 expect(unload()).toBe(false)
})
it('allows untouched disabled editor after preparation rejects, preserving disabled state and no save', async () => {
 const f = fixture(async () => { throw Error('synthetic preparation denied') })
 await screen.findByText(/尚未确认这台电脑的加密草稿保护/)
 expect(screen.getByLabelText('教材页标题')).toBeDisabled()
 expect(f.entry.journal.state.payload).toBeNull(); expect(f.entry.capture).toBeNull()
 expect(f.checkpoint).not.toHaveBeenCalled(); expect(f.api.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(0)
 expect(unload()).toBe(false)
})
it('keeps real entered unprotected editing and blocks both unload and navigation', async () => {
 const f = fixture(async () => grant())
 await waitFor(() => expect(screen.getByLabelText('教材页标题')).not.toBeDisabled())
 await waitFor(() => expect(f.entry.journal.state.phase).toBe('saved'))
 f.failCheckpoint()
 fireEvent.change(screen.getByLabelText('教材页标题'), { target: { value: 'synthetic original must survive' } })
 expect(f.entry.journal.state.payload?.page.title).toBe('synthetic original must survive')
 expect(unload()).toBe(true)
 let navigation = true; act(() => { navigation = requestParentDraftNavigation() }); expect(navigation).toBe(false)
 expect(screen.getByLabelText('教材页标题')).toHaveValue('synthetic original must survive')
})
it('does not ignore Host-owned unprotected work when another editor is still unready', async () => {
 const f = fixture(() => new Promise<ParentDraftRecoveryPrepared>(() => {}))
 const other = f.entry.journal
 other.keepEditing({ page: { title: 'synthetic retained original' }, capture_ref: '', capture_version: '', review_drafts: {}, action_ids: {} })
 expect(screen.getByLabelText('教材页标题')).toBeDisabled()
 expect(other.hasUnconfirmedEditing(other.state.payload)).toBe(true)
 expect(unload()).toBe(true)
 expect(other.state.payload?.page.title).toBe('synthetic retained original')
})
it('continues allowing a native-protected edited snapshot to unload without claiming a server ACK', async () => {
 const f = fixture(async () => grant())
 await waitFor(() => expect(screen.getByLabelText('教材页标题')).not.toBeDisabled())
 await waitFor(() => expect(f.entry.journal.state.phase).toBe('saved'))
 fireEvent.change(screen.getByLabelText('教材页标题'), { target: { value: 'synthetic protected edit' } })
 expect(f.entry.journal.state.payload?.page.title).toBe('synthetic protected edit')
 expect(f.checkpoint).toHaveBeenCalled()
 expect(unload()).toBe(false)
})
