import { localApi } from './api'
import { privacyStopKey, privacyStopPending } from './deviceCommandReceipt'
import type { SessionWorkspaceState } from './types'

export type ProfileSwitchOutput = { profile_switched: true; target_child_id: string; target_session_id: string; previous_session_id: string }
type Receipt = { capability: 'profile.switch'; execution_location: 'client_device_bridge'; action_type: 'switch_child_profile'; status: 'completed'; completed_at: string; output: ProfileSwitchOutput }
export type ProfileSwitchHandoff = {
  version: 1
  turnId: string
  sourceSessionId: string
  sourceChildId: string
  targetChildId: string
  clientActionId: string
  receipt?: Receipt
  activated?: boolean
  acknowledged?: boolean
}
export const PROFILE_SWITCH_HANDOFF_KEY = 'hsc-profile-switch-handoff-v1'
const FINAL_RESPONSE = '已切换到对应孩子的独立学习档案。'
const acknowledgements = new Map<string, Promise<boolean>>()

export class ProfileSwitchPendingError extends Error {
  constructor() { super('档案切换结果正在核对，已保留原操作，不会创建第二个会话。'); this.name = 'ProfileSwitchPendingError' }
}

export function readProfileSwitchHandoff(): ProfileSwitchHandoff | null {
  const raw = localStorage.getItem(PROFILE_SWITCH_HANDOFF_KEY)
  if (raw === null) return null
  const item = JSON.parse(raw) as ProfileSwitchHandoff
  if (item?.version !== 1 || [item.turnId, item.sourceSessionId, item.sourceChildId, item.targetChildId, item.clientActionId]
    .some((id) => typeof id !== 'string' || !id || id.length > 160)) throw new Error('PROFILE_HANDOFF_INVALID')
  if ([item.activated, item.acknowledged].some((flag) => flag !== undefined && typeof flag !== 'boolean')
    || ((item.activated || item.acknowledged) && !item.receipt)) throw new Error('PROFILE_HANDOFF_INVALID')
  if (item.receipt) {
    const { receipt } = item
    const output = receipt.output
    if (receipt.capability !== 'profile.switch' || receipt.action_type !== 'switch_child_profile' || receipt.execution_location !== 'client_device_bridge'
      || receipt.status !== 'completed' || !Number.isFinite(Date.parse(receipt.completed_at)) || output?.profile_switched !== true
      || output.previous_session_id !== item.sourceSessionId || output.target_child_id !== item.targetChildId
      || typeof output.target_session_id !== 'string' || !output.target_session_id || output.target_session_id.length > 160) throw new Error('PROFILE_HANDOFF_INVALID')
  }
  return item
}

function save(item: ProfileSwitchHandoff): void {
  localStorage.setItem(PROFILE_SWITCH_HANDOFF_KEY, JSON.stringify(item))
}

export function prepareProfileSwitch(input: Omit<ProfileSwitchHandoff, 'version' | 'receipt' | 'activated' | 'acknowledged'>): ProfileSwitchHandoff {
  const pending = readProfileSwitchHandoff()
  if (pending) {
    if (pending.clientActionId !== input.clientActionId || pending.turnId !== input.turnId || pending.sourceSessionId !== input.sourceSessionId || pending.sourceChildId !== input.sourceChildId || pending.targetChildId !== input.targetChildId) throw new ProfileSwitchPendingError()
    return pending
  }
  const item: ProfileSwitchHandoff = { version: 1, ...input }
  save(item)
  return item
}

export function recordProfileSwitchDestination(item: ProfileSwitchHandoff, output: ProfileSwitchOutput): ProfileSwitchHandoff {
  const saved = readProfileSwitchHandoff()
  if (!saved || saved.clientActionId !== item.clientActionId) throw new ProfileSwitchPendingError()
  item = saved
  if (output.profile_switched !== true || output.target_child_id !== item.targetChildId || output.previous_session_id !== item.sourceSessionId
    || !output.target_session_id || (item.receipt && item.receipt.output.target_session_id !== output.target_session_id)) throw new Error('PROFILE_DESTINATION_MISMATCH')
  if (item.receipt) return item
  const next: ProfileSwitchHandoff = { ...item, receipt: { capability: 'profile.switch', execution_location: 'client_device_bridge', action_type: 'switch_child_profile',
    status: 'completed', completed_at: new Date().toISOString(), output } }
  save(next)
  return next
}

export async function resolveProfileSwitchDestination(item: ProfileSwitchHandoff): Promise<{ handoff: ProfileSwitchHandoff; workspace: SessionWorkspaceState }> {
  let next = item
  if (!next.receipt) {
    // The server resolves this stable action ID before checking the closed
    // source session, so even a lost switch response cannot create a new one.
    const switched = await withDeadline(localApi.switchSessionChild(next.sourceSessionId, next.targetChildId, next.clientActionId))
    if (switched.child_id !== next.targetChildId) throw new Error('PROFILE_DESTINATION_MISMATCH')
    next = recordProfileSwitchDestination(next, { profile_switched: true, target_child_id: next.targetChildId, target_session_id: switched.session_id, previous_session_id: next.sourceSessionId })
  }
  const workspace = await withDeadline(localApi.sessionWorkspace(next.receipt!.output.target_session_id))
  if (workspace.session.child_id !== next.targetChildId || workspace.session.session_id !== next.receipt!.output.target_session_id) throw new Error('PROFILE_DESTINATION_MISMATCH')
  if (privacyStopPending(next.sourceSessionId)) localStorage.setItem(privacyStopKey(workspace.session.session_id), 'profile-switch-privacy-preserved')
  return { handoff: next, workspace }
}

async function withDeadline<T>(request: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([request, new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new ProfileSwitchPendingError()), 15_000) })])
  } finally { clearTimeout(timer) }
}

export function markProfileSwitchActivated(item: ProfileSwitchHandoff): void {
  const saved = readProfileSwitchHandoff()
  if (!saved || saved.clientActionId !== item.clientActionId || !saved.receipt) throw new ProfileSwitchPendingError()
  if (privacyStopPending(saved.sourceSessionId)) localStorage.setItem(privacyStopKey(saved.receipt.output.target_session_id), 'profile-switch-privacy-preserved')
  if (saved.acknowledged) localStorage.removeItem(PROFILE_SWITCH_HANDOFF_KEY)
  else save({ ...saved, activated: true })
}

export function acknowledgeProfileSwitch(item: ProfileSwitchHandoff): Promise<boolean> {
  if (!item.receipt) return Promise.resolve(false)
  if (item.acknowledged) return Promise.resolve(true)
  const current = acknowledgements.get(item.clientActionId)
  if (current) return current
  let timer: ReturnType<typeof setTimeout> | undefined
  const request = (async () => {
    try {
      const completed = await localApi.completeAgentTurn(item.turnId, FINAL_RESPONSE, 'completed', undefined, { execution_receipt: item.receipt!, client_bridge_receipt: item.receipt! })
      if (completed.agent_turn_id !== item.turnId || completed.tool_status !== 'completed') return false
      const saved = readProfileSwitchHandoff()
      if (saved?.clientActionId === item.clientActionId) {
        if (saved.activated) localStorage.removeItem(PROFILE_SWITCH_HANDOFF_KEY)
        else save({ ...saved, acknowledged: true })
      }
      return true
    } catch { return false }
  })()
  // A hung acknowledgement must not hold the destination UI or prevent later
  // idempotent acknowledgement with the exact same persisted receipt.
  const work = Promise.race([request, new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(false), 5_000) })])
    .finally(() => { clearTimeout(timer); if (acknowledgements.get(item.clientActionId) === work) acknowledgements.delete(item.clientActionId) })
  acknowledgements.set(item.clientActionId, work)
  return work
}
