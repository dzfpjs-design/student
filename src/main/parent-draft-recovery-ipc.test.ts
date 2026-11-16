// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import { createParentDraftRecoveryAuthorizer, registerParentDraftRecoveryIpc } from './parent-draft-recovery-ipc'

function fixture() {
  const frame = { isDestroyed: () => false, url: 'file:///owned/index.html', processId: 40, routingId: 50 }
  const contents = { id: 7, isDestroyed: () => false, mainFrame: frame }
  let owner: any = { frame, contents }
  let runtime = { status: 'ready', localApiUrl: 'http://127.0.0.1:9123', sessionToken: 'synthetic-session' }
  let managed = true, profile = 'trusted-profile'
  const draft = { child_id: 'child-a', scope: 'capture', storage_generation: 'original-generation', revision: 4,
    payload: null, last_action_id: null, last_expected_revision: null, updated_at: null, stale_refs: [], cloud_transmission_allowed: false }
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ draft }), { status: 200 }))
  const auth = createParentDraftRecoveryAuthorizer({ owner: () => owner, runtime: () => runtime, managed: () => managed, profileId: () => profile, fetch: fetcher as typeof fetch })
  return { frame, contents, draft, fetcher, auth, owner: () => owner, setOwner: (value: any) => { owner = value },
    runtime, setRuntime: (value: typeof runtime) => { runtime = value }, setManaged: (value: boolean) => { managed = value }, setProfile: (value: string) => { profile = value } }
}
describe('native recovery original parent authority', () => {
  it('uses original child and scope GET with both ephemeral tokens and bounded no-redirect transport', async () => {
    const f = fixture()
    await expect(f.auth(7, 'child-a', 'capture', 'synthetic-parent')).resolves.toEqual({ profileId: 'trusted-profile' })
    expect(f.fetcher).toHaveBeenCalledTimes(1)
    expect(f.fetcher.mock.calls[0]).toEqual(['http://127.0.0.1:9123/v1/parent/knowledge-graph/textbook-capture/child-a/drafts/capture', expect.objectContaining({
      headers: { 'X-HSC-Session': 'synthetic-session', 'X-HSC-Parent': 'synthetic-parent' }, redirect: 'error', cache: 'no-store', signal: expect.any(AbortSignal)
    })])
  })
  it.each(['https://127.0.0.1:9123', 'http://example.test', 'http://127.0.0.1:9123/path', 'http://user:pass@localhost:9123', 'http://localhost:9123?x=1'])('rejects non-managed base %s before HTTP', async url => {
    const f = fixture(); f.setRuntime({ ...f.runtime, localApiUrl: url })
    await expect(f.auth(7, 'child-a', 'capture', 'parent')).rejects.toThrow('AUTHORITY_CHANGED')
    expect(f.fetcher).not.toHaveBeenCalled()
  })
  it('rejects external configured loopback and wrong sender before HTTP', async () => {
    const f = fixture(); f.setManaged(false)
    await expect(f.auth(7, 'child-a', 'capture', 'parent')).rejects.toThrow()
    f.setManaged(true)
    await expect(f.auth(8, 'child-a', 'capture', 'parent')).rejects.toThrow()
    expect(f.fetcher).not.toHaveBeenCalled()
  })
  it.each(['session', 'profile', 'owner', 'frame-url', 'frame-process'])('rejects %s changed while original GET pending', async change => {
    const f = fixture()
    f.fetcher.mockImplementationOnce(async () => {
      if (change === 'session') f.setRuntime({ ...f.runtime, sessionToken: 'new-session' })
      if (change === 'profile') f.setProfile('different-profile')
      if (change === 'owner') f.setOwner(null)
      if (change === 'frame-url') f.frame.url = 'file:///navigated/index.html'
      if (change === 'frame-process') f.frame.processId++
      return new Response(JSON.stringify({ draft: f.draft }))
    })
    await expect(f.auth(7, 'child-a', 'capture', 'parent')).rejects.toThrow('AUTHORITY_CHANGED')
  })
  it.each([{ child_id: 'child-b' }, { scope: 'language' }, { storage_generation: 'short' }, { revision: -1 }, { cloud_transmission_allowed: true }, { stale_refs: [1] }, { last_action_id: {} }, { payload: [] }, { payload: {} }])('rejects malformed or cross-scope receipt %j', async patch => {
    const f = fixture(); f.fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ draft: { ...f.draft, ...patch } })))
    await expect(f.auth(7, 'child-a', 'capture', 'parent')).rejects.toThrow()
  })
  it('rejects oversized streamed receipt and redacts invalid private JSON errors', async () => {
    const f = fixture(); f.fetcher.mockResolvedValueOnce(new Response('x'.repeat(1_100_001)))
    await expect(f.auth(7, 'child-a', 'capture', 'parent')).rejects.toThrow('AUTHORITY_CHANGED')
    f.fetcher.mockResolvedValueOnce(new Response('{PRIVATE_SYNTHETIC_MARKER'))
    const error = await f.auth(7, 'child-a', 'capture', 'parent').catch(e => e)
    expect(error.message).not.toContain('PRIVATE_SYNTHETIC_MARKER')
  })
})
describe('native recovery IPC frame fencing and sync settlement', () => {
  function ipcFixture() {
    const f = fixture(), handlers = new Map<string, Function>(), listeners = new Map<string, Function>()
    const coordinator = { prepare: vi.fn(), read: vi.fn(), discard: vi.fn(), checkpoint: vi.fn(() => ({ stored: true, localRevision: 1, sequence: 1 })), revokeSender: vi.fn() }
    registerParentDraftRecoveryIpc({ handle: (name: string, fn: Function) => { handlers.set(name, fn) }, on: (name: string, fn: Function) => { listeners.set(name, fn) } } as any, f.owner, coordinator as any)
    const event: any = { sender: f.contents, senderFrame: f.frame }
    return { ...f, handlers, listeners, coordinator, event }
  }
  it('rejects same-webContents subframe before all async and sync operations', () => {
    const f = ipcFixture(); f.event.senderFrame = { ...f.frame }
    for (const handler of f.handlers.values()) expect(() => handler(f.event, 'child-a', 'capture', 'parent', 0, true)).toThrow()
    f.listeners.get('parent-draft-recovery:checkpoint')!(f.event, 'lease', 1, {})
    expect(f.event.returnValue).toEqual({ ok: false, error: 'PARENT_DRAFT_RECOVERY_UNCONFIRMED' })
    expect(f.coordinator.checkpoint).not.toHaveBeenCalled()
  })
  it('returns sync durable ACK only after coordinator accepts and failure never hangs or emits private error', () => {
    const f = ipcFixture(), call = f.listeners.get('parent-draft-recovery:checkpoint')!
    call(f.event, 'lease', 1, {})
    expect(f.event.returnValue).toEqual({ ok: true, value: { stored: true, localRevision: 1, sequence: 1 } })
    f.coordinator.checkpoint.mockImplementationOnce(() => { throw Error('private-synthetic-raw') })
    call(f.event, 'lease', 2, {})
    expect(f.event.returnValue).toEqual({ ok: false, error: 'PARENT_DRAFT_RECOVERY_UNCONFIRMED' })
    f.listeners.get('parent-draft-recovery:revoke')!(f.event)
    expect(f.coordinator.revokeSender).toHaveBeenCalledWith(7)
    expect(f.event.returnValue).toEqual({ ok: true })
  })
})
