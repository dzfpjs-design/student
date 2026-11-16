import type { IpcMain, IpcMainEvent, IpcMainInvokeEvent, WebContents, WebFrameMain } from 'electron'
import type { ParentDraftRecoveryScope, ParentDraftRecoverySnapshot, ParentDraftRecoveryPrepared, ParentDraftRecoveryRecovered, ParentDraftRecoveryCheckpoint } from '../shared/parentDraftRecovery'
import { validateSnapshot } from './parent-draft-recovery-validation'

type Owner = { contents: WebContents; frame: WebFrameMain }
type Runtime = { status: string; localApiUrl: string; sessionToken: string }
type Authority = {
  owner: () => Owner | null
  runtime: () => Runtime
  managed: () => boolean
  profileId: () => string
  fetch: typeof fetch
}
const failure = (): Error => Error('PARENT_DRAFT_RECOVERY_AUTHORITY_CHANGED')

function validateScope(childId: unknown, scope: unknown, token: unknown): asserts childId is string {
  if (typeof childId !== 'string' || !childId.trim() || childId.length > 128
    || typeof scope !== 'string' || !['capture', 'language'].includes(scope)
    || typeof token !== 'string' || !token || token.length > 4096) throw failure()
}

async function boundedJson(response: Response): Promise<unknown> {
  const limit = 1_100_000
  if (!response.body || Number(response.headers.get('content-length') ?? 0) > limit) throw failure()
  const reader = response.body.getReader(), parts: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > limit) throw failure()
      parts.push(next.value)
    }
  } finally { await reader.cancel().catch(() => undefined) }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts))) }
  catch { throw failure() }
}

export function createParentDraftRecoveryAuthorizer(deps: Authority) {
  return async (senderId: number, childId: string, scope: ParentDraftRecoveryScope, parentToken: string): Promise<{ profileId: string }> => {
    validateScope(childId, scope, parentToken)
    const owner = deps.owner(), before = deps.runtime(), profileId = deps.profileId()
    const frameUrl = owner?.frame.url, frameProcess = owner?.frame.processId, frameRouting = owner?.frame.routingId
    const current = (): boolean => {
      const afterOwner = deps.owner(), after = deps.runtime()
      return deps.managed() && !!afterOwner && afterOwner.contents === owner?.contents
        && afterOwner.frame === owner?.frame && !afterOwner.contents.isDestroyed() && !afterOwner.frame.isDestroyed()
        && afterOwner.frame.url === frameUrl && afterOwner.frame.processId === frameProcess && afterOwner.frame.routingId === frameRouting
        && afterOwner.contents.mainFrame === afterOwner.frame && afterOwner.contents.id === senderId
        && after.status === 'ready' && after.localApiUrl === before.localApiUrl
        && after.sessionToken === before.sessionToken && deps.profileId() === profileId
    }
    let base: URL
    try { base = new URL(before.localApiUrl) } catch { throw failure() }
    if (before.status !== 'ready' || !before.sessionToken || !profileId || !current()
      || base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
      || base.username || base.password || base.pathname !== '/' || base.search || base.hash) throw failure()
    let response: Response
    try {
      response = await deps.fetch(`${base.origin}/v1/parent/knowledge-graph/textbook-capture/${encodeURIComponent(childId)}/drafts/${scope}`, {
        headers: { 'X-HSC-Session': before.sessionToken, 'X-HSC-Parent': parentToken },
        redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(5000)
      })
    } catch { throw failure() }
    if (!response.ok) throw failure()
    const body = await boundedJson(response) as { draft?: Record<string, unknown> }
    const receipt = body?.draft
    if (!receipt || receipt.child_id !== childId || receipt.scope !== scope
      || typeof receipt.storage_generation !== 'string' || receipt.storage_generation.length < 8
      || !Number.isSafeInteger(receipt.revision) || (receipt.revision as number) < 0
      || receipt.cloud_transmission_allowed !== false || !Array.isArray(receipt.stale_refs)
      || !receipt.stale_refs.every(ref => typeof ref === 'string')
      || !(receipt.payload === null || (typeof receipt.payload === 'object' && !Array.isArray(receipt.payload)))
      || !(receipt.last_action_id === null || typeof receipt.last_action_id === 'string')
      || !(receipt.last_expected_revision === null || Number.isSafeInteger(receipt.last_expected_revision))
      || !(receipt.updated_at === null || typeof receipt.updated_at === 'string') || !current()) throw failure()
    try { validateSnapshot({ schema: 1, childId, scope, acknowledged: receipt, latest: receipt.payload, pending: null }, childId, scope) }
    catch { throw failure() }
    return { profileId }
  }
}

type Coordinator = {
  prepare(senderId: number, childId: string, scope: ParentDraftRecoveryScope, token: string, assertCurrent?:()=>void): Promise<ParentDraftRecoveryPrepared>
  read(senderId: number, childId: string, scope: ParentDraftRecoveryScope, token: string, revision: number, assertCurrent?:()=>void): Promise<ParentDraftRecoveryRecovered>
  checkpoint(senderId: number, leaseId: string, sequence: number, snapshot: ParentDraftRecoverySnapshot): ParentDraftRecoveryCheckpoint
  discard(senderId: number, childId: string, scope: ParentDraftRecoveryScope, token: string, revision: number, explicit: true, assertCurrent?:()=>void): Promise<ParentDraftRecoveryPrepared>
  revokeSender(senderId: number): void
}

/** The current primary MAIN frame is checked independently at every IPC boundary. */
export function registerParentDraftRecoveryIpc(ipc: Pick<IpcMain, 'handle' | 'on'>, owner: () => Owner | null, coordinator: Coordinator): void {
  const sender = (event: IpcMainEvent | IpcMainInvokeEvent): number => {
    const active = owner()
    if (!active || active.contents.isDestroyed() || active.frame.isDestroyed()
      || event.sender !== active.contents || event.senderFrame !== active.frame
      || active.contents.mainFrame !== active.frame) throw failure()
    return active.contents.id
  }
  ipc.handle('parent-draft-recovery:prepare', (event, child: string, scope: ParentDraftRecoveryScope, token: string) => {const id=sender(event),frame=event.senderFrame;const current=()=>{if(sender(event)!==id||event.senderFrame!==frame)throw failure()};return coordinator.prepare(id, child, scope, token, current)})
  ipc.handle('parent-draft-recovery:read', (event, child: string, scope: ParentDraftRecoveryScope, token: string, revision: number) => {const id=sender(event),frame=event.senderFrame;const current=()=>{if(sender(event)!==id||event.senderFrame!==frame)throw failure()};return coordinator.read(id, child, scope, token, revision, current)})
  ipc.handle('parent-draft-recovery:discard', (event, child: string, scope: ParentDraftRecoveryScope, token: string, revision: number, explicit: true) => {const id=sender(event),frame=event.senderFrame;const current=()=>{if(sender(event)!==id||event.senderFrame!==frame)throw failure()};return coordinator.discard(id, child, scope, token, revision, explicit, current)})
  ipc.on('parent-draft-recovery:checkpoint', (event, leaseId: string, sequence: number, snapshot: ParentDraftRecoverySnapshot) => {
    try { event.returnValue = { ok: true, value: coordinator.checkpoint(sender(event), leaseId, sequence, snapshot) } }
    catch { event.returnValue = { ok: false, error: 'PARENT_DRAFT_RECOVERY_UNCONFIRMED' } }
  })
  ipc.on('parent-draft-recovery:revoke', event => {
    try { coordinator.revokeSender(sender(event)); event.returnValue = { ok: true } }
    catch { event.returnValue = { ok: false, error: 'PARENT_DRAFT_RECOVERY_UNCONFIRMED' } }
  })
}
