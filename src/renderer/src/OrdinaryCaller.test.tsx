import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { ordinaryResponseGrant, revokeOrdinaryResponses } from './ordinaryResponseGrant'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import * as voice from './companionVoice'
import type { AgentTurn, SessionWorkspaceState, VoiceInteractionResponse } from './types'
import type { VoiceOnboardingProgress } from './voiceOnboardingRuntime'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { saveAgentRequest } from './agentRequestJournal'

let microphone: ComponentProps<typeof LocalInputs.HandsFreeVoiceInput>
let realMicrophone = false
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
vi.mock('./LocalInputs', async (original) => {
  const actual = await original<typeof import('./LocalInputs')>()
  return { ...actual, HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return realMicrophone ? <actual.HandsFreeVoiceInput {...props} /> : null }, VoiceInputButton: () => null }
})
vi.mock('./VoiceCalibrationPanel', async () => {
  const { useEffect } = await import('react')
  return { VoiceCalibrationPanel: ({ onMicrophoneBindingReadyChange }: { onMicrophoneBindingReadyChange: (ready: boolean) => void }) => {
    useEffect(() => { onMicrophoneBindingReadyChange(true) }, [onMicrophoneBindingReadyChange])
    return null
  } }
})

function workspace(childId = 'child-a'): SessionWorkspaceState {
  return {
    session: { session_id: `session-${childId}`, child_id: childId, status: 'active', state: 'check_in', observation_status: 'paused', snooze_until: null },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0 },
    family_rule: { observation_mode: 'shadow' }, recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [],
    plan: null, plan_items: [], drafts: {}, latest_tutor_turn: null,
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, by_device: {} }, daily_report_ready: false,
  } as unknown as SessionWorkspaceState
}
const progress = (step: VoiceOnboardingProgress['step'], childId = 'child-a'): VoiceOnboardingProgress => ({ childId, version: 1, step, updatedAt: '2026-09-05T12:00:00Z' })
const cloudReply = (action: VoiceInteractionResponse['action'] = 'none', values: Partial<VoiceInteractionResponse> = {}): VoiceInteractionResponse => ({
  route: action === 'none' ? 'clarify' : 'onboarding', action, source: 'cloud', response_text: '我们慢慢来。', ...values,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('voice onboarding in the workspace', () => {
  let names: Map<string, DesktopWakeWordSettings>
  let records: Map<string, VoiceOnboardingProgress>
  let desktop: { getWakeWordSettings: ReturnType<typeof vi.fn>; saveWakeWordSettings: ReturnType<typeof vi.fn>; getVoiceOnboarding: ReturnType<typeof vi.fn>; saveVoiceOnboarding: ReturnType<typeof vi.fn> }
  const setName = (childId = 'child-a') => names.set(childId, { childId, wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: '2026-09-05T12:00:00Z' })
  const say = async (text: string) => { await act(async () => { await microphone.onTranscript(text) }) }
  const wake = async () => { await act(async () => { microphone.onWake?.() }) }
  const respond = (action: VoiceInteractionResponse['action'], values: Partial<VoiceInteractionResponse> = {}) => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValueOnce(cloudReply(action, values))
  }
  const candidate = (name = '小知') => respond('name_candidate', { candidate: name, response_text: `我听到的唤醒名字是“${name}”。这样叫我好吗？` })
  const resumeTutorial = () => {
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce({
      agent_turn_id: 'resume-tutorial', response_text: '云端规划已选择重新练习。', intent: 'resume_voice_tutorial',
      runtime_capability: 'onboarding.resume', runtime_execution_location: 'client_device_bridge',
      action: { type: 'resume_voice_tutorial', capability: 'onboarding.resume', execution_location: 'client_device_bridge', source: 'agent_runtime_cloud' },
    } as AgentTurn)
  }
  async function mount(props: Partial<ComponentProps<typeof SessionWorkspace>> = {}) {
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="初三" onBack={vi.fn()} {...props} />) })
    return view
  }

  beforeEach(() => {
    vi.spyOn(localApi, 'agentExecutionRecoveryIndex').mockImplementation(async (session, child) => ({
      session_id: session, child_id: child, local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, has_more: false, entries: [] }))
    realMicrophone = false
    names = new Map()
    records = new Map();records.set('child-a',progress('completed'));setName()
    desktop = {
      getWakeWordSettings: vi.fn(async (id: string) => names.get(id) ?? null),
      saveWakeWordSettings: vi.fn(async (value: DesktopWakeWordSettings) => { names.set(value.childId, value); return value }),
      getVoiceOnboarding: vi.fn(async (id: string) => records.get(id) ?? null),
      saveVoiceOnboarding: vi.fn(async (value: VoiceOnboardingProgress) => { records.set(value.childId, value); return value }),
    }
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop(desktop))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
    for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
      vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
        handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
    }
    vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('PRACTICE_MUST_NOT_REACH_AGENT'))
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'voiceInteraction').mockResolvedValue(cloudReply())
    vi.spyOn(localApi, 'askTutor')
    vi.spyOn(localApi, 'addHomework')
    vi.spyOn(localApi, 'startDocumentMonitor')
    vi.spyOn(localApi, 'registerDevice')
    vi.spyOn(localApi, 'saveSessionDraft')
  })
  afterEach(() => {
    revokeOrdinaryResponses(); cleanup(); localStorage.clear(); resetVoiceCaptureCoordinatorForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
  })


  function actualPlayback() {
    const call=vi.mocked(LocalInputs.speakLocally).mock.lastCall!
    expect(call[3]).toBeTypeOf('function')
    vi.spyOn(voice,'getCompanionPlaybackLease').mockReturnValue({id:'real-start-lease',generation:1,
      sessionId:call[1],kind:'local_sapi',text:call[0]} as never)
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    call[3]?.()
    return {call,end:()=>{vi.mocked(voice.getCompanionPlaybackLease).mockReturnValue(null)
      vi.mocked(voice.getCompanionVoiceState).mockReturnValue('idle');call[4]?.onPlaybackEnd?.()}}
  }
  it('actual wake caller arms only the same actual natural-end attempt',async()=>{
    vi.mocked(LocalInputs.speakLocally).mockImplementation(()=>undefined);await mount();await wake();const playback=actualPlayback();expect(ordinaryResponseGrant('session-child-a','child-a')).toBeNull()
    playback.end();expect(ordinaryResponseGrant('session-child-a','child-a')).not.toBeNull()
  })
  it('text_only/idle and error do not arm an ordinary reply',async()=>{
    await mount();await wake();const call=vi.mocked(LocalInputs.speakLocally).mock.lastCall!
    call[2]?.('text_only');expect(ordinaryResponseGrant('session-child-a','child-a')).toBeNull()
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('error');call[4]?.onPlaybackEnd?.()
    expect(ordinaryResponseGrant('session-child-a','child-a')).toBeNull()
  })
  it('unmount invalidates a late same-playback natural-end callback',async()=>{
    vi.mocked(LocalInputs.speakLocally).mockImplementation(()=>undefined);const view=await mount();await wake();const playback=actualPlayback();view.unmount();playback.end()
    expect(ordinaryResponseGrant('session-child-a','child-a')).toBeNull()
  })
})
