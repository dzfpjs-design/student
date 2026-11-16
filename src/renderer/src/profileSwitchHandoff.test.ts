import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { privacyStopKey } from './deviceCommandReceipt'
import { acknowledgeProfileSwitch, markProfileSwitchActivated, prepareProfileSwitch, PROFILE_SWITCH_HANDOFF_KEY, readProfileSwitchHandoff, recordProfileSwitchDestination, resolveProfileSwitchDestination } from './profileSwitchHandoff'
import type { AgentTurn, SessionWorkspaceState } from './types'

const input = { turnId: 'source-turn', sourceSessionId: 'source-session', sourceChildId: 'child-a', targetChildId: 'child-b', clientActionId: 'source-turn-switch' }
const output = { profile_switched: true as const, target_child_id: 'child-b', target_session_id: 'target-session', previous_session_id: 'source-session' }
const target = { session: { session_id: 'target-session', child_id: 'child-b' } } as SessionWorkspaceState
const completed = { agent_turn_id: input.turnId, tool_status: 'completed' } as AgentTurn

describe('durable profile switch handoff', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); localStorage.clear() })

  it('journals the stable switch identity before any request and refuses a second action', async () => {
    const switchApi = vi.spyOn(localApi, 'switchSessionChild').mockImplementation(async (_source, _child, id) => {
      expect(readProfileSwitchHandoff()).toMatchObject({ ...input, clientActionId: id })
      return target.session
    })
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(target)
    const handoff = prepareProfileSwitch(input)
    expect(() => prepareProfileSwitch({ ...input, clientActionId: 'different-action' })).toThrow()
    await resolveProfileSwitchDestination(handoff)
    expect(switchApi).toHaveBeenCalledExactlyOnceWith(input.sourceSessionId, input.targetChildId, input.clientActionId)
  })

  it('does not begin switching when the pre-request journal cannot be saved', () => {
    const switchApi = vi.spyOn(localApi, 'switchSessionChild')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk full') })
    expect(() => prepareProfileSwitch(input)).toThrow('disk full')
    expect(switchApi).not.toHaveBeenCalled()
  })

  it('replays a lost switch response with the same identity after reload, without another session', async () => {
    const sessions = new Map<string, typeof target.session>()
    const switchApi = vi.spyOn(localApi, 'switchSessionChild').mockImplementation(async (_source, _child, id) => {
      const existed = sessions.has(id!)
      sessions.set(id!, target.session)
      if (!existed) throw new Error('response lost after commit')
      return sessions.get(id!)!
    })
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(target)
    await expect(resolveProfileSwitchDestination(prepareProfileSwitch(input))).rejects.toThrow('response lost')
    await expect(resolveProfileSwitchDestination(readProfileSwitchHandoff()!)).resolves.toMatchObject({ workspace: target })
    expect(switchApi.mock.calls.map((call) => call[2])).toEqual([input.clientActionId, input.clientActionId])
    expect(sessions.size).toBe(1)
  })

  it('recovers a saved destination after workspace loading fails without invoking switch again', async () => {
    const switchApi = vi.spyOn(localApi, 'switchSessionChild').mockResolvedValue(target.session)
    vi.spyOn(localApi, 'sessionWorkspace').mockRejectedValueOnce(new Error('offline')).mockResolvedValue(target)
    await expect(resolveProfileSwitchDestination(prepareProfileSwitch(input))).rejects.toThrow('offline')
    const saved = readProfileSwitchHandoff()!
    expect(saved.receipt?.output).toEqual(output)
    const restored = await resolveProfileSwitchDestination(saved)
    expect(restored.handoff.receipt).toEqual(saved.receipt)
    expect(switchApi).toHaveBeenCalledTimes(1)
  })

  it('keeps the exact completed payload across ack loss, UI activation and later retry', async () => {
    const ack = vi.spyOn(localApi, 'completeAgentTurn').mockRejectedValueOnce(new Error('ack lost')).mockResolvedValue(completed)
    const handoff = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    expect(await acknowledgeProfileSwitch(handoff)).toBe(false)
    markProfileSwitchActivated(handoff)
    expect(readProfileSwitchHandoff()?.activated).toBe(true)
    expect(await acknowledgeProfileSwitch(readProfileSwitchHandoff()!)).toBe(true)
    expect(ack.mock.calls[0]).toEqual(ack.mock.calls[1])
    expect(ack.mock.calls.every((call) => call[2] === 'completed')).toBe(true)
    expect(readProfileSwitchHandoff()).toBeNull()
  })

  it('retains the destination if acknowledgement wins the race before UI activation', async () => {
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(completed)
    const handoff = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    expect(await acknowledgeProfileSwitch(handoff)).toBe(true)
    expect(readProfileSwitchHandoff()).toMatchObject({ acknowledged: true, receipt: handoff.receipt })
    markProfileSwitchActivated(handoff)
    expect(readProfileSwitchHandoff()).toBeNull()
  })

  it('bounds hung acknowledgement, retries identically and tolerates its late return', async () => {
    vi.useFakeTimers()
    let finish!: (turn: AgentTurn) => void
    const ack = vi.spyOn(localApi, 'completeAgentTurn').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve })).mockResolvedValue(completed)
    const handoff = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    markProfileSwitchActivated(handoff)
    const first = acknowledgeProfileSwitch(handoff)
    expect(acknowledgeProfileSwitch(handoff)).toBe(first)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(await first).toBe(false)
    expect(await acknowledgeProfileSwitch(readProfileSwitchHandoff()!)).toBe(true)
    const next = prepareProfileSwitch({ ...input, turnId: 'new-turn', clientActionId: 'new-turn-switch' })
    finish(completed)
    await Promise.resolve()
    expect(readProfileSwitchHandoff()).toEqual(next)
    expect(ack.mock.calls[0]).toEqual(ack.mock.calls[1])
  })

  it('does not clear pending evidence for a wrong or nonterminal ack', async () => {
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({ ...completed, tool_status: 'pending' })
    const handoff = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    markProfileSwitchActivated(handoff)
    expect(await acknowledgeProfileSwitch(handoff)).toBe(false)
    expect(readProfileSwitchHandoff()?.receipt).toEqual(handoff.receipt)
  })

  it('leaves a hung switch unknown and ignores its late result until the same operation is recovered', async () => {
    vi.useFakeTimers()
    let finish!: (session: typeof target.session) => void
    const switchApi = vi.spyOn(localApi, 'switchSessionChild').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve })).mockResolvedValue(target.session)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(target)
    const attempt = resolveProfileSwitchDestination(prepareProfileSwitch(input))
    const rejected = expect(attempt).rejects.toThrow('档案切换结果正在核对')
    await vi.advanceTimersByTimeAsync(15_000)
    await rejected
    expect(readProfileSwitchHandoff()?.receipt).toBeUndefined()
    finish(target.session)
    await Promise.resolve()
    expect(readProfileSwitchHandoff()?.receipt).toBeUndefined()
    await resolveProfileSwitchDestination(readProfileSwitchHandoff()!)
    expect(switchApi.mock.calls[0]).toEqual(switchApi.mock.calls[1])
    expect(readProfileSwitchHandoff()?.receipt?.output).toEqual(output)
  })

  it('keeps the stable identity if storing the committed destination fails', async () => {
    const switchApi = vi.spyOn(localApi, 'switchSessionChild').mockResolvedValue(target.session)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(target)
    const handoff = prepareProfileSwitch(input)
    const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => { throw new Error('disk full') })
    await expect(resolveProfileSwitchDestination(handoff)).rejects.toThrow('disk full')
    expect(readProfileSwitchHandoff()).toEqual(handoff)
    storage.mockRestore()
    await resolveProfileSwitchDestination(readProfileSwitchHandoff()!)
    expect(switchApi.mock.calls[0]).toEqual(switchApi.mock.calls[1])
  })

  it('can finish an acknowledged receipt after local cleanup fails, without changing the payload', async () => {
    const ack = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(completed)
    const handoff = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    markProfileSwitchActivated(handoff)
    const storage = vi.spyOn(Storage.prototype, 'removeItem').mockImplementationOnce(() => { throw new Error('storage unavailable') })
    expect(await acknowledgeProfileSwitch(handoff)).toBe(false)
    expect(readProfileSwitchHandoff()?.receipt).toEqual(handoff.receipt)
    storage.mockRestore()
    expect(await acknowledgeProfileSwitch(readProfileSwitchHandoff()!)).toBe(true)
    expect(ack.mock.calls[0]).toEqual(ack.mock.calls[1])
    expect(readProfileSwitchHandoff()).toBeNull()
  })

  it('rejects mismatched child evidence without recording success', async () => {
    vi.spyOn(localApi, 'switchSessionChild').mockResolvedValue({ ...target.session, child_id: 'child-c' })
    const ack = vi.spyOn(localApi, 'completeAgentTurn')
    await expect(resolveProfileSwitchDestination(prepareProfileSwitch(input))).rejects.toThrow('PROFILE_DESTINATION_MISMATCH')
    expect(readProfileSwitchHandoff()?.receipt).toBeUndefined()
    expect(ack).not.toHaveBeenCalled()
  })

  it('preserves a privacy stop received after loading but before activation', async () => {
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(target)
    const saved = recordProfileSwitchDestination(prepareProfileSwitch(input), output)
    const restored = await resolveProfileSwitchDestination(saved)
    localStorage.setItem(privacyStopKey(input.sourceSessionId), 'stop')
    markProfileSwitchActivated(restored.handoff)
    expect(localStorage.getItem(privacyStopKey(output.target_session_id))).not.toBeNull()
  })

  it('retains malformed handoffs instead of silently creating another switch', () => {
    localStorage.setItem(PROFILE_SWITCH_HANDOFF_KEY, '{bad json')
    expect(() => prepareProfileSwitch(input)).toThrow()
    expect(localStorage.getItem(PROFILE_SWITCH_HANDOFF_KEY)).toBe('{bad json')
  })
})
