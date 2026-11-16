export type RealtimeAdmissionHandoff = {
  version: 1; sessionId: string; childId: string; inputId: string; createdAt: string;
  phase: 'admitting' | 'stored' | 'dispatching' | 'cancel_requested'; voiceInputId: string | null;
}
const prefix = 'hsc-realtime-admission-v1:'
const segment = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const inputSegment = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{8,128}$/.test(value)
const key = (value: Pick<RealtimeAdmissionHandoff, 'sessionId' | 'childId' | 'inputId'>) =>
  `${prefix}${value.sessionId}:${value.childId}:${value.inputId}`

export type RealtimeRecoveryIndex = {
  session_id: string; child_id: string; entries: { client_action_id: string; voice_input_id: string | null;
    status: 'prepared' | 'completed'; created_at: string }[]; has_more: boolean;
  local_only: true; cloud_transmission_allowed: false; replay_allowed: false; mastery_update_allowed: false;
}

export function restoreRealtimeAdmissionIdentities(sessionId: string, childId: string, index: RealtimeRecoveryIndex): number {
  if (!index || index.session_id !== sessionId || index.child_id !== childId || index.local_only !== true
    || index.cloud_transmission_allowed !== false || index.replay_allowed !== false || index.mastery_update_allowed !== false
    || typeof index.has_more !== 'boolean' || !Array.isArray(index.entries) || index.entries.length > 128) throw Error('Recovery index scope invalid')
  const existing = pendingRealtimeAdmissions(sessionId, childId), seen = new Set<string>()
  for (const row of index.entries) {
    if (!row || !inputSegment(row.client_action_id) || seen.has(row.client_action_id)
      || !['prepared', 'completed'].includes(row.status) || typeof row.created_at !== 'string'
      || !Number.isFinite(Date.parse(row.created_at))
      || (row.status === 'completed' ? !inputSegment(row.voice_input_id) : row.voice_input_id !== null)
      || Object.keys(row).sort().join(',') !== 'client_action_id,created_at,status,voice_input_id') throw Error('Recovery index entry invalid')
    seen.add(row.client_action_id)
    const old = existing.find(value => value.inputId === row.client_action_id)
    if (old?.voiceInputId && old.voiceInputId !== row.voice_input_id) throw Error('Recovery index identity changed')
  }
  const missing = index.entries.filter(row => !existing.some(value => value.inputId === row.client_action_id))
  if (existing.length + missing.length > 128) throw Error('Recovery journal capacity reached')
  for (const row of missing) persist({ version: 1, sessionId, childId, inputId: row.client_action_id,
    phase: row.status === 'completed' ? 'stored' : 'admitting', voiceInputId: row.voice_input_id, createdAt: row.created_at })
  return missing.length
}

// Identity and handoff state only. Canonical speech, source references and
// answers remain in the local service, never in this browser recovery journal.
export function pendingRealtimeAdmissions(sessionId: string, childId: string): RealtimeAdmissionHandoff[] {
  if (!segment(sessionId) || !segment(childId)) throw new Error('Invalid admission owner')
  const result: RealtimeAdmissionHandoff[] = []
  for (const name of Object.keys(localStorage)) {
    if (!name.startsWith(`${prefix}${sessionId}:${childId}:`)) continue
    const value = JSON.parse(localStorage.getItem(name)!) as RealtimeAdmissionHandoff
    if (!value || value.version !== 1 || value.sessionId !== sessionId || value.childId !== childId
      || !inputSegment(value.inputId) || name !== key(value)
      || !['admitting', 'stored', 'dispatching', 'cancel_requested'].includes(value.phase)
      || !(value.voiceInputId === null || inputSegment(value.voiceInputId))
      || (['stored', 'dispatching'].includes(value.phase) && value.voiceInputId === null)
      || typeof value.createdAt !== 'string' || !Number.isFinite(Date.parse(value.createdAt))
      || Object.keys(value).sort().join(',') !== 'childId,createdAt,inputId,phase,sessionId,version,voiceInputId') {
      throw new Error('Invalid admission journal')
    }
    result.push(value)
  }
  return result.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.inputId.localeCompare(b.inputId))
}

function persist(value: RealtimeAdmissionHandoff): RealtimeAdmissionHandoff {
  const serialized = JSON.stringify(value)
  localStorage.setItem(key(value), serialized)
  if (localStorage.getItem(key(value)) !== serialized) throw new Error('Admission journal not saved')
  return value
}

export function prepareRealtimeAdmission(sessionId: string, childId: string, inputId: string): RealtimeAdmissionHandoff {
  const pending = pendingRealtimeAdmissions(sessionId, childId)
  if (!inputSegment(inputId) || pending.length >= 128 || pending.some(value => value.inputId === inputId)) {
    throw new Error('Admission identity unavailable; check original receipt')
  }
  return persist({ version: 1, sessionId, childId, inputId, phase: 'admitting', voiceInputId: null, createdAt: new Date().toISOString() })
}

export function updateRealtimeAdmission(value: RealtimeAdmissionHandoff, phase: RealtimeAdmissionHandoff['phase'],
  voiceInputId: string | null = value.voiceInputId): RealtimeAdmissionHandoff {
  if (localStorage.getItem(key(value)) !== JSON.stringify(value)) throw new Error('Admission handoff changed')
  if (!(phase === 'cancel_requested' || (phase === 'stored' && value.phase === 'admitting')
    || (phase === 'dispatching' && value.phase === 'stored'))
    || (value.phase === 'cancel_requested' && phase !== 'cancel_requested')
    || (voiceInputId !== null && !inputSegment(voiceInputId))
    || (['stored', 'dispatching'].includes(phase) && voiceInputId === null)
    || (value.voiceInputId !== null && value.voiceInputId !== voiceInputId)) throw new Error('Invalid admission transition')
  return persist({ ...value, phase, voiceInputId })
}

export function acknowledgeRealtimeAdmission(value: RealtimeAdmissionHandoff): void {
  if (localStorage.getItem(key(value)) === JSON.stringify(value)) localStorage.removeItem(key(value))
}

export function restoredRealtimeAdmissionMessage(sessionId: string, childId: string): string | null {
  try {
    return pendingRealtimeAdmissions(sessionId, childId).length
      ? '上次有原话的保存或交接结果待核对，不会自动重做。可以说“核对刚才请求”。' : null
  } catch { return '原话的恢复记录无法读取，不会自动重做。请家长检查本机存储。' }
}
