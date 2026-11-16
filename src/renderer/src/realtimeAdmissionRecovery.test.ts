import { afterEach, expect, it, vi } from 'vitest'
import { acknowledgeRealtimeAdmission, pendingRealtimeAdmissions, prepareRealtimeAdmission,
  restoredRealtimeAdmissionMessage, updateRealtimeAdmission } from './realtimeAdmissionJournal'
import { checkRealtimeAdmissions, recoverRealtimeCancellations, type RealtimeAdmissionReceipt } from './realtimeAdmissionRecovery'
import type { AgentRequestReceipt } from './agentRequestRecovery'

afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })
const prepare = (child = 'synthetic-child', key = 'synthetic-input') => prepareRealtimeAdmission('synthetic-session', child, key)
const receipt = (status: RealtimeAdmissionReceipt['status']): RealtimeAdmissionReceipt => ({ session_id: 'synthetic-session',
  child_id: 'synthetic-child', client_action_id: 'synthetic-input', status,
  voice_input_id: status === 'completed' ? 'canonical-synthetic-voice' : null,
  local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
const agent = (status: AgentRequestReceipt['status']): AgentRequestReceipt => ({ session_id: 'synthetic-session',
  client_action_id: 'realtime-canonical-synthetic-voice', status, response_text: '合成原请求状态，不是重新执行。',
  local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })

it('retains only delivery identities across reload and strictly separates children', () => {
  const first = prepare(), other = prepare('other-child')
  expect(pendingRealtimeAdmissions(first.sessionId, first.childId)).toEqual([first])
  expect(pendingRealtimeAdmissions(other.sessionId, other.childId)).toEqual([other])
  expect(restoredRealtimeAdmissionMessage(first.sessionId, first.childId)).toContain('不会自动重做')
  expect(Object.keys(first).sort()).toEqual(['childId', 'createdAt', 'inputId', 'phase', 'sessionId', 'version', 'voiceInputId'])
  expect(() => prepare()).toThrow(/unavailable/)
})

it('rejects failed storage, corrupted metadata and revival after privacy cancellation', () => {
  const original = prepare(), stopped = updateRealtimeAdmission(original, 'cancel_requested')
  expect(() => updateRealtimeAdmission(original, 'stored', 'canonical-synthetic-voice')).toThrow(/changed/)
  expect(() => updateRealtimeAdmission(stopped, 'stored', 'canonical-synthetic-voice')).toThrow(/transition/)
  acknowledgeRealtimeAdmission(original)
  expect(pendingRealtimeAdmissions(original.sessionId, original.childId)).toEqual([stopped])
  localStorage.setItem(Object.keys(localStorage)[0], JSON.stringify({ ...stopped, transcript: 'must not be stored' }))
  expect(() => pendingRealtimeAdmissions(original.sessionId, original.childId)).toThrow(/Invalid/)
  expect(restoredRealtimeAdmissionMessage(original.sessionId, original.childId)).toContain('无法读取')
  localStorage.clear()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {})
  expect(() => prepare()).toThrow(/not saved/)
})

it.each(['not_found', 'prepared', 'completed', 'cancelled'] as const)('reads a %s canonical receipt without replaying a command', async status => {
  prepare()
  const read = vi.fn().mockResolvedValue(receipt(status)), readAgent = vi.fn().mockResolvedValue(agent('unconfirmed'))
  const message = await checkRealtimeAdmissions('synthetic-session', 'synthetic-child', read, readAgent, () => true)
  expect(read).toHaveBeenCalledExactlyOnceWith('synthetic-session', 'synthetic-input', expect.any(AbortSignal))
  expect(readAgent).toHaveBeenCalledTimes(status === 'completed' ? 1 : 0)
  expect(message).toMatch(status === 'completed' ? /合成原请求状态/ : /没有|不能|尚未/)
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(status === 'cancelled' ? 0 : 1)
})

it('retains a stored input with unknown handoff across repeated authoritative readonly checks and reload', async () => {
  const stored = updateRealtimeAdmission(prepare(), 'stored', 'canonical-synthetic-voice')
  const readAgent = vi.fn().mockResolvedValue(agent('unconfirmed'))
  for (let index = 0; index < 2; index++) {
    expect(await checkRealtimeAdmissions(stored.sessionId, stored.childId, async () => receipt('completed'), readAgent, () => true))
      .toBe(agent('unconfirmed').response_text)
    expect(pendingRealtimeAdmissions(stored.sessionId, stored.childId)).toEqual([stored])
  }
  expect(readAgent).toHaveBeenCalledTimes(2)
  expect(restoredRealtimeAdmissionMessage(stored.sessionId, stored.childId)).toContain('不会自动重做')
})

it.each(['admitting', 'stored'] as const)('does not infer Runtime execution from local %s phase', async phase => {
  const initial = prepare()
  if (phase === 'stored') updateRealtimeAdmission(initial, 'stored', 'canonical-synthetic-voice')
  const readAgent = vi.fn().mockResolvedValueOnce(agent('interpreted')).mockResolvedValueOnce(agent('recorded_completed'))
  expect(await checkRealtimeAdmissions(initial.sessionId, initial.childId, async () => receipt('completed'), readAgent, () => true))
    .toBe(agent('interpreted').response_text)
  expect(pendingRealtimeAdmissions(initial.sessionId, initial.childId)).toHaveLength(1)
  await checkRealtimeAdmissions(initial.sessionId, initial.childId, async () => receipt('completed'), readAgent, () => true)
  expect(pendingRealtimeAdmissions(initial.sessionId, initial.childId)).toEqual([])
  expect(readAgent.mock.calls.map(call => call.slice(0, 2))).toEqual([
    [initial.sessionId, 'realtime-canonical-synthetic-voice'], [initial.sessionId, 'realtime-canonical-synthetic-voice'],
  ])
})

it.each(['recorded_completed', 'recorded_failed', 'interpreted', 'received', 'unconfirmed'] as const)(
  'checks handed-off %s work by its original stable Agent key, never claiming it is still running', async status => {
    const stored = updateRealtimeAdmission(prepare(), 'stored', 'canonical-synthetic-voice')
    updateRealtimeAdmission(stored, 'dispatching')
    const readAgent = vi.fn().mockResolvedValue(agent(status))
    expect(await checkRealtimeAdmissions('synthetic-session', 'synthetic-child', async () => receipt('completed'), readAgent, () => true))
      .toBe(agent(status).response_text)
    expect(readAgent).toHaveBeenCalledExactlyOnceWith('synthetic-session', 'realtime-canonical-synthetic-voice', expect.any(AbortSignal))
    expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child'))
      .toHaveLength(['recorded_completed', 'recorded_failed'].includes(status) ? 0 : 1)
  })

it('retains unconfirmed cancellation and rejects another owner or changed canonical voice', async () => {
  const stored = updateRealtimeAdmission(prepare(), 'stored', 'canonical-synthetic-voice')
  updateRealtimeAdmission(stored, 'cancel_requested')
  const readAgent = vi.fn()
  expect(await checkRealtimeAdmissions('synthetic-session', 'synthetic-child', async () => receipt('completed'), readAgent, () => true))
    .toContain('撤回尚未确认')
  await expect(checkRealtimeAdmissions('synthetic-session', 'synthetic-child', async () => ({ ...receipt('completed'), child_id: 'other-child' }), readAgent, () => true))
    .rejects.toThrow(/scope mismatch/)
  expect(readAgent).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
})

it('leaves the durable notice untouched if privacy changes during the readonly request', async () => {
  prepare()
  let current = true
  expect(await checkRealtimeAdmissions('synthetic-session', 'synthetic-child', async () => {
    current = false; return receipt('completed')
  }, vi.fn(), () => current)).toBeNull()
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
})

it('continues a bounded readonly backlog rather than starving later inputs behind unknown results', async () => {
  for (let index = 0; index < 10; index++) prepare('backlog-child', `backlog-input-${index}`)
  const read = vi.fn(async (_session: string, input: string) => ({ ...receipt('prepared'),
    child_id: 'backlog-child', client_action_id: input }))
  const check = () => checkRealtimeAdmissions('synthetic-session', 'backlog-child', read, vi.fn(), () => true)
  await check(); expect(read).toHaveBeenCalledTimes(8)
  await check()
  expect(new Set(read.mock.calls.map(call => call[1])).size).toBe(10)
  expect(pendingRealtimeAdmissions('synthetic-session', 'backlog-child')).toHaveLength(10)
})

it('recovers a durable cancellation only after a scoped cancelled receipt, never from the POST response', async () => {
  updateRealtimeAdmission(prepare(), 'cancel_requested')
  const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' })
  const read = vi.fn().mockResolvedValueOnce(receipt('completed')).mockResolvedValueOnce(receipt('cancelled'))
  const attempts = new Map<string, number>()
  const run = () => recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, attempts, new AbortController().signal)
  expect(await run()).toMatchObject({ confirmed: 0, retryable: true })
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
  expect(await run()).toMatchObject({ confirmed: 1, retryable: false })
  expect(cancel).toHaveBeenCalledTimes(2)
  expect(read).toHaveBeenCalledTimes(2)
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toEqual([])
})

it('keeps failed cancellations durable with three attempts per recovery cycle and resumes from the same identity', async () => {
  updateRealtimeAdmission(prepare(), 'cancel_requested')
  const cancel = vi.fn().mockRejectedValue(new Error('offline')), read = vi.fn()
  const attempts = new Map<string, number>()
  const run = () => recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, attempts, new AbortController().signal)
  await run(); await run()
  expect(await run()).toMatchObject({ confirmed: 0, retryable: false })
  await run()
  expect(cancel).toHaveBeenCalledTimes(3)
  expect(read).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
  cancel.mockResolvedValueOnce(undefined); read.mockResolvedValueOnce(receipt('cancelled'))
  expect(await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, new Map(), new AbortController().signal))
    .toMatchObject({ confirmed: 1 })
  expect(cancel.mock.calls[3].slice(0, 2)).toEqual(['synthetic-session', 'synthetic-input'])
})

it('does not acknowledge another owner, a changed journal or a cancelled recovery owner', async () => {
  updateRealtimeAdmission(prepare(), 'cancel_requested')
  const cancel = vi.fn().mockResolvedValue(undefined)
  const read = vi.fn().mockResolvedValue({ ...receipt('cancelled'), child_id: 'other-child' })
  await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, new Map(), new AbortController().signal)
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
  let current = true
  cancel.mockImplementationOnce(async () => { current = false })
  read.mockClear()
  await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => current, new Map(), new AbortController().signal)
  expect(read).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
  const signal = AbortSignal.abort()
  cancel.mockClear()
  await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, new Map(), signal)
  expect(cancel).not.toHaveBeenCalled()
})

it('processes only cancellation records, eight at a time, without starving the rest', async () => {
  for (let index = 0; index < 10; index++) updateRealtimeAdmission(prepare('backlog-child', `cancel-backlog-${index}`), 'cancel_requested')
  prepare('backlog-child', 'untouched-admitting')
  const cancel = vi.fn().mockResolvedValue(undefined)
  const read = vi.fn(async (_session: string, input: string) => ({ ...receipt('cancelled'), child_id: 'backlog-child', client_action_id: input }))
  const run = () => recoverRealtimeCancellations('synthetic-session', 'backlog-child', cancel, read, () => true, new Map(), new AbortController().signal)
  expect(await run()).toMatchObject({ confirmed: 8, retryable: true })
  expect(await run()).toMatchObject({ confirmed: 2, retryable: false })
  expect(cancel).toHaveBeenCalledTimes(10)
  expect(pendingRealtimeAdmissions('synthetic-session', 'backlog-child').map(item => item.inputId)).toEqual(['untouched-admitting'])
})

it('releases a hung cancellation after ten seconds and ignores a late receipt', async () => {
  vi.useFakeTimers()
  updateRealtimeAdmission(prepare(), 'cancel_requested')
  let finish!: () => void
  const cancel = vi.fn(() => new Promise<void>(resolve => { finish = resolve })), read = vi.fn().mockResolvedValue(receipt('cancelled'))
  const outcome = recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, new Map(), new AbortController().signal)
  await vi.advanceTimersByTimeAsync(10_000)
  expect(await outcome).toMatchObject({ confirmed: 0, failures: 1, retryable: true })
  finish(); await vi.advanceTimersByTimeAsync(1)
  expect(read).not.toHaveBeenCalled()
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toHaveLength(1)
  vi.useRealTimers()
})

it('does not remove changed recovery metadata or trust a replay-enabled cancellation receipt', async () => {
  const item = updateRealtimeAdmission(prepare(), 'cancel_requested')
  const cancel = vi.fn().mockResolvedValue(undefined)
  const read = vi.fn(async () => {
    updateRealtimeAdmission(item, 'cancel_requested', 'new-canonical-input')
    return receipt('cancelled')
  })
  expect(await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, read, () => true, new Map(), new AbortController().signal))
    .toMatchObject({ confirmed: 0 })
  const changed = pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')[0]
  expect(changed.voiceInputId).toBe('new-canonical-input')
  const unsafe = vi.fn(async () => ({ ...receipt('cancelled'), replay_allowed: true } as unknown as RealtimeAdmissionReceipt))
  await recoverRealtimeCancellations('synthetic-session', 'synthetic-child', cancel, unsafe, () => true, new Map(), new AbortController().signal)
  expect(pendingRealtimeAdmissions('synthetic-session', 'synthetic-child')).toEqual([changed])
})
