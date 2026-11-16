import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { cancelLocalSpeech, speakLocally } from './LocalInputs'
import * as voice from './companionVoice'
import type { SessionWorkspaceState } from './types'

vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: () => null, VoiceInputButton: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

const initial = { session: { session_id: 'reminder-session-a', child_id: 'reminder-child-a',
  status: 'active', state: 'check_in', observation_status: 'paused' }, plan_items: [], recalls: [],
  homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {},
  family_rule: {}, sensor_release: { active_leases: 0, by_device: {} } } as unknown as SessionWorkspaceState
const message = '你愿意试试我们约好的这一步吗？先自己整理桌面。'
const receipt = (current: unknown = true, promptId = 'habit-prompt') =>
  new Response(JSON.stringify({ prompt_id: promptId, current }), { status: 200 })
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }

describe.each(['behavior_intervention', 'oral_supply_ready', 'oral_daily_supply_ready', 'due_commitment', 'task_ready'])('mounted %s reminder playback source gate', sourceType => {
  let heard: string[]
  let ends: Map<string, () => void>
  let checks: string[]
  let current: boolean
  let read: () => Promise<Response>
  beforeEach(() => {
    resetRuntimeCache(); cancelLocalSpeech()
    heard = []; ends = new Map(); checks = []; current = true
    read = async () => receipt(current)
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url.endsWith('/agent/proactive-prompts/habit-prompt/current')) { checks.push(url); return read() }
      throw new Error('UNEXPECTED_LOCAL_REQUEST')
    }))
    vi.spyOn(voice, 'speakCompanion').mockImplementation(async (text, _provider, _cache, start, options) => {
      heard.push(text); start?.(); ends.set(text, () => options?.onPlaybackEnd?.()); return 'system_offline'
    })
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'acknowledgeProactiveAgentPrompt').mockResolvedValue({ prompt_id: 'habit-prompt', status: 'acknowledged' })
  })
  afterEach(() => { cleanup(); cancelLocalSpeech(); localStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); resetRuntimeCache() })
  const mount = async () => {
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={initial} childName="合成甲" childGrade="四年级" onBack={vi.fn()} />) })
    await act(async () => { for (const end of ends.values()) end() })
    heard = []; ends.clear()
    return view
  }
  const emit = async (overrides: Record<string, unknown> = {}) => {
    await act(async () => { window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: {
      message, promptId: 'habit-prompt', sourceType,
      sessionId: initial.session.session_id, childId: initial.session.child_id, ...overrides,
    } })) })
  }
  const release = async (text: string) => { await act(async () => { ends.get(text)?.() }) }

  it.each(['dialogue', 'lesson'] as const)('aborts a hung reminder GET and immediately speaks the new %s; late true cannot replay it', async priority => {
    await mount()
    const pending = deferred<Response>(), stop = vi.spyOn(voice, 'stopCompanionSpeech')
    read = () => pending.promise
    await emit()
    expect(checks).toHaveLength(1)
    const request = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/current'))!
    const signal = request[1]?.signal
    expect(signal?.aborted).toBe(false)
    await act(async () => {
      speakLocally('孩子当前问题的回复', initial.session.session_id, undefined, undefined, { localOnly: true, priority })
    })
    expect(heard).toEqual(['孩子当前问题的回复'])
    expect(signal?.aborted).toBe(true)
    expect(stop).not.toHaveBeenCalled()
    await act(async () => { pending.resolve(receipt(true)) })
    expect(heard).toEqual(['孩子当前问题的回复'])
    expect(screen.queryByText(message)).toBeNull()
    expect(stop).not.toHaveBeenCalled()
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
    await release('孩子当前问题的回复')
    expect(heard).toEqual(['孩子当前问题的回复'])
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
  })

  it.each(['parent_stop', 'child_stop'])('does not play or acknowledge a queued instruction after %s', async () => {
    await mount()
    const previousReply = document.querySelector('.agent-dialogue > span')?.textContent
    expect(previousReply).toBeTruthy()
    speakLocally('正在讲解', initial.session.session_id, undefined, undefined, { localOnly: true })
    await emit()
    expect(checks).toHaveLength(0)
    current = false
    await release('正在讲解')
    expect(checks).toHaveLength(1)
    expect(heard).not.toContain(message)
    expect(screen.queryByText(message)).toBeNull()
    expect(document.querySelector('.agent-dialogue > span')?.textContent).toBe(previousReply)
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
  })

  it('reads at the real dequeue boundary and acknowledges only rendered completion', async () => {
    await mount()
    speakLocally('正在讲解', initial.session.session_id, undefined, undefined, { localOnly: true })
    await emit(); expect(checks).toHaveLength(0)
    await release('正在讲解')
    expect(checks).toEqual(['http://127.0.0.1:8765/v1/sessions/reminder-session-a/agent/proactive-prompts/habit-prompt/current'])
    expect(heard).toContain(message)
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
    await release(message)
    expect(localApi.acknowledgeProactiveAgentPrompt).toHaveBeenCalledExactlyOnceWith('reminder-session-a', 'habit-prompt')
  })

  it.each(['network', 'wrong_receipt', 'truthy_not_boolean'])('fails closed on %s and permits an explicit same-key fresh retry', async kind => {
    await mount()
    read = kind === 'network' ? async () => { throw new Error('offline') }
      : async () => kind === 'wrong_receipt' ? receipt(true, 'different-prompt') : receipt('true')
    await emit()
    expect(heard).not.toContain(message)
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
    read = async () => receipt(true)
    await emit()
    expect(heard.filter(text => text === message)).toHaveLength(1)
    expect(checks).toHaveLength(2)
  })

  it('rejects foreign or unowned behavior events before requesting or queueing', async () => {
    await mount()
    await emit({ childId: 'other-child' }); await emit({ sessionId: 'other-session' })
    await emit({ childId: undefined }); await emit({ promptId: undefined })
    expect(heard).not.toContain(message); expect(checks).toHaveLength(0)
  })

  it('ignores a successful old-child read after switching owner without cancelling new speech', async () => {
    const view = await mount(), pending = deferred<Response>()
    read = () => pending.promise
    await emit()
    expect(checks).toHaveLength(1)
    const other = { ...initial, session: { ...initial.session, session_id: 'reminder-session-b', child_id: 'reminder-child-b' } }
    await act(async () => { view.rerender(<SessionWorkspace initial={other} childName="合成乙" childGrade="初三" onBack={vi.fn()} />) })
    speakLocally('乙的当前回复', other.session.session_id, undefined, undefined, { localOnly: true })
    await act(async () => { pending.resolve(receipt(true)) })
    expect(heard).not.toContain(message); expect(heard).toContain('乙的当前回复')
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
  })

  it('keeps unscoped non-behavior guidance compatible without calling the new endpoint', async () => {
    await mount()
    await emit({ sourceType: undefined, childId: undefined, sessionId: undefined })
    expect(heard).toContain(message); expect(checks).toHaveLength(0)
    await release(message)
    expect(localApi.acknowledgeProactiveAgentPrompt).toHaveBeenCalledTimes(1)
  })

  it('carries exact scope through the real proactive polling event into the playback gate', async () => {
    vi.useFakeTimers()
    const poll = vi.spyOn(localApi, 'proactiveAgentPrompt').mockResolvedValue({ status: 'delivered',
      prompt: { prompt_id: 'habit-prompt', source_type: sourceType, message,
        expires_at: new Date(Date.now() + 120_000).toISOString() } })
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(poll).toHaveBeenCalledWith('reminder-session-a')
    expect(checks).toHaveLength(1); expect(heard).toContain(message)
    const fetchCall = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/current'))!
    expect(fetchCall[1]).toMatchObject({ cache: 'no-store', signal: expect.any(AbortSignal) })
    expect(fetchCall[1]?.method).toBeUndefined()
  })

  it('deduplicates active and delivered prompt IDs even if ACK response is lost', async () => {
    await mount()
    const pending = deferred<Response>()
    read = () => pending.promise
    await emit(); await emit()
    expect(checks).toHaveLength(1)
    await act(async () => { pending.resolve(receipt(true)) })
    vi.mocked(localApi.acknowledgeProactiveAgentPrompt).mockRejectedValue(new Error('lost ACK'))
    await release(message); await emit()
    expect(heard.filter(text => text === message)).toHaveLength(1)
    expect(checks).toHaveLength(1)
    expect(localApi.acknowledgeProactiveAgentPrompt).toHaveBeenCalledTimes(1)
  })

  it('rejects expired and HTTP-404 sources without changing the question or submitting any command', async () => {
    await mount()
    const turn = vi.spyOn(localApi, 'agentTurn'), execute = vi.spyOn(localApi, 'executeAgentTurn')
    await emit({ expiresAt: new Date(Date.now() - 1).toISOString() })
    expect(checks).toHaveLength(0)
    read = async () => new Response('{}', { status: 404 })
    await emit()
    expect(checks).toHaveLength(1); expect(heard).not.toContain(message)
    expect(turn).not.toHaveBeenCalled(); expect(execute).not.toHaveBeenCalled()
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
  })

  it('ignores an old read after privacy stop and never acknowledges it', async () => {
    await mount()
    const pending = deferred<Response>()
    read = () => pending.promise
    await emit()
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')); pending.resolve(receipt(true)) })
    expect(heard).not.toContain(message)
    expect(localApi.acknowledgeProactiveAgentPrompt).not.toHaveBeenCalled()
  })
})
