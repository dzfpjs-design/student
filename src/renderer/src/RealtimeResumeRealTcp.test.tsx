import fs from 'node:fs'
import { ownedVoiceRuntimeConfig, ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SessionWorkspace } from './SessionWorkspace'
import { localApi, resetRuntimeCache } from './api'
import { pendingRealtimeAdmissions, prepareRealtimeAdmission, updateRealtimeAdmission } from './realtimeAdmissionJournal'
import { voiceCaptureReference } from './voiceCaptureContext'
import { readPendingEvaluation, savePendingEvaluation } from './pendingEvaluationReceipt'
import * as inputs from './LocalInputs'
import type { QwenRealtimeInputCapture, QwenRealtimeToolResult } from './qwenRealtimeVoice'

const harness = vi.hoisted(() => ({ onTurn: null as null | ((words: string, capture: QwenRealtimeInputCapture) => Promise<QwenRealtimeToolResult>),
  onInputCompleted: null as null | ((words: string, capture: QwenRealtimeInputCapture) => void) }))
vi.mock('./QwenRealtimeVoiceInput', () => ({ QwenRealtimeVoiceInput: (props: { onTurn: typeof harness.onTurn; onInputCompleted: typeof harness.onInputCompleted }) => {
  harness.onTurn = props.onTurn; harness.onInputCompleted = props.onInputCompleted; return null
} }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
afterEach(() => { cleanup(); resetRuntimeCache(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.skipIf(!process.env.HSC_REALTIME_RESUME_TCP_CASE)('continues canonical saved speech through mounted child workspace, actual TCP and Runtime', async () => {
  const config = JSON.parse(fs.readFileSync(process.env.HSC_REALTIME_RESUME_TCP_CASE!, 'utf8'))
  const nativeFetch = globalThis.fetch.bind(globalThis)
  const snapshot = config.runtime_snapshot
  expect(snapshot.localApiUrl).toBe(config.base_url)
  expect(Number.isSafeInteger(snapshot.runtimeEpoch) && snapshot.runtimeEpoch > 0).toBe(true)
  expect(snapshot.sessionToken).toBe('public-realtime-resume-tcp-session')
  const runtime = ownedVoiceRuntimeConfig(snapshot.localApiUrl, snapshot.sessionToken, snapshot.runtimeEpoch)
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getQwenRealtimeSettings: async () => ({ configured: true, enabled: true, model: 'synthetic-only', endpoint: 'synthetic-invalid' }),
    getVoiceOnboarding: async () => ({ childId: config.child_id, version: 1, step: 'completed' }),
    getWakeWordSettings: async () => ({ childId: config.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60 }),
  }, runtime, nativeFetch))
  const wrong = ownedVoiceRuntimeConfig(snapshot.localApiUrl, snapshot.sessionToken + '-wrong', snapshot.runtimeEpoch)
  expect(await ownedVoiceRuntimePolicyDesktop({}, wrong, nativeFetch).getVoiceRuntimePolicy()).toEqual({ version: 1, capture: 'blocked', runtimeEpoch: snapshot.runtimeEpoch })
  expect(await window.desktop?.getVoiceRuntimePolicy?.()).toEqual({ version: 1, capture: 'configured', runtimeEpoch: snapshot.runtimeEpoch })
  vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: async () => [] } })
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
  vi.spyOn(inputs, 'cancelLocalSpeech').mockImplementation(() => undefined)
  resetRuntimeCache()
  const initial = await localApi.sessionWorkspace(config.session_id)
  if (config.pending_evaluation_turn_id) savePendingEvaluation({ version: 1, sessionId: config.session_id,
    childId: config.child_id, turnId: config.current_pending_evaluation_turn_id ?? config.pending_evaluation_turn_id,
    status: 'unknown', silent: true })
  updateRealtimeAdmission(prepareRealtimeAdmission(config.session_id, config.child_id, config.input_id), 'stored', config.voice_input_id)
  const resume = vi.spyOn(localApi, 'resumeRealtimeVoice')
  const preview = vi.spyOn(localApi, 'realtimeResumePreview')
  const rephrase = vi.spyOn(localApi, 'agentTurn')
  const calibration = vi.spyOn(localApi, 'sessionVoiceCalibrationSummary')
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={() => undefined} />) })
  // The RTC child is mounted only after the real calibration summary settles.
  // Observe the existing requests; do not create a second request or change the callback deadline.
  expect(calibration).toHaveBeenCalled()
  await act(async () => { await Promise.allSettled(calibration.mock.results.map(result => result.value)) })
  await waitFor(() => expect(harness.onTurn).toEqual(expect.any(Function)))
  await act(async () => { await harness.onTurn!('继续刚才未处理的原话', {
    sessionId: config.session_id, localInputId: 'new-synthetic-resume-control', capturedAt: Date.now(),
    reference: voiceCaptureReference(config.session_id, config.child_id), carryoverConfirmationId: null, source: 'accepted_onset',
  }) })
  expect(resume).toHaveBeenCalledTimes(1)
  if (config.planned_turn_id) {
    expect(await preview.mock.results[0].value).toMatchObject({ resume_kind: 'execution', agent_turn_id: config.planned_turn_id })
    expect(await resume.mock.results[0].value).toMatchObject({ agent_turn_id: config.planned_turn_id })
  }
  expect(rephrase).not.toHaveBeenCalled()
  if (config.pending_evaluation_turn_id) {
    expect(await preview.mock.results[0].value).toMatchObject({ pending_evaluation_turn_id: config.pending_evaluation_turn_id })
    expect(await resume.mock.results[0].value).toMatchObject({ action: {
      pending_evaluation_control: { evaluation_turn_id: config.pending_evaluation_turn_id } } })
    expect(readPendingEvaluation(config.session_id, config.child_id)?.turnId)
      .toBe(config.current_pending_evaluation_turn_id ?? config.pending_evaluation_turn_id)
  }
  expect(pendingRealtimeAdmissions(config.session_id, config.child_id)).toEqual([])
  const receipt = await localApi.agentRequestReceipt(config.session_id, 'realtime-' + config.voice_input_id)
  expect(receipt.status).toBe('recorded_completed')
  fs.writeFileSync(config.result, JSON.stringify({ resumed_posts: 1, rephrased_posts: 0,
    original_receipt: receipt.status, recovery_journal_empty: true,
    planned_turn_preserved: config.planned_turn_id ?? null, microphone_audio: 'synthetic-not-verified' }))
}, 60000)

it.skipIf(!process.env.HSC_REALTIME_RECOVERY_INDEX_TCP_CASE)('restores lost identities in mounted production workspace without automatically dispatching saved originals', async () => {
  const config = JSON.parse(fs.readFileSync(process.env.HSC_REALTIME_RECOVERY_INDEX_TCP_CASE!, 'utf8'))
  localStorage.clear()
  vi.stubGlobal('desktop', {
    getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: config.base_url, sessionToken: 'synthetic-output-session-token' }),
    getQwenRealtimeSettings: async () => ({ configured: true, enabled: true, model: 'synthetic-only', endpoint: 'synthetic-invalid' }),
    getVoiceOnboarding: async () => ({ childId: config.child_id, version: 1, step: 'completed' }),
    getWakeWordSettings: async () => ({ childId: config.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60 }),
  })
  vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: async () => [] } })
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
  vi.spyOn(inputs, 'cancelLocalSpeech').mockImplementation(() => undefined)
  resetRuntimeCache()
  const initial = await localApi.sessionWorkspace(config.session_id)
  const resumed = vi.spyOn(localApi, 'resumeRealtimeVoice'), planned = vi.spyOn(localApi, 'agentTurn')
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={() => undefined} />) })
  await waitFor(() => expect(pendingRealtimeAdmissions(config.session_id, config.child_id)).toHaveLength(2))
  expect(pendingRealtimeAdmissions(config.session_id, config.child_id).map(row => row.inputId)).toEqual(config.input_ids)
  expect(resumed).not.toHaveBeenCalled(); expect(planned).not.toHaveBeenCalled()
  expect(JSON.stringify({ ...localStorage })).not.toContain(config.words[0])
  expect(JSON.stringify({ ...localStorage })).not.toContain(config.words[1])
  fs.writeFileSync(config.result, JSON.stringify({ workspace_mounted: true, restored_entries: 2, auto_runtime_dispatches: 0,
    video_and_voice_component_boundaries_stubbed: true, physical_devices_tested: false }))
}, 60000)
