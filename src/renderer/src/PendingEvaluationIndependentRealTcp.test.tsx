import { ownedVoiceRuntimePolicyDesktop, ownedVoiceRuntimeConfig } from './test/ownedVoiceRuntimePolicy'

import { spawn, execFileSync, type ChildProcess } from 'node:child_process'
import { webcrypto } from 'node:crypto'
import { once } from 'node:events'
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import type { ComponentProps } from 'react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import * as inputs from './LocalInputs'
import { localApi, resetRuntimeCache } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { clearVoiceCaptureContext } from './voiceCaptureContext'
import { readPendingEvaluation, savePendingEvaluation } from './pendingEvaluationReceipt'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

it.each(['resources', 'tutor'] as const)('joins mounted hands-free %s UI to real pending-evaluation TCP and durable read-only replay after SIGKILL', async mode => {
  const parent = await mkdtemp(join(tmpdir(), 'pytest-pending-dialogue-ui-'))
  const root = join(parent, 'test_source')
  await mkdir(root)
  const python = join(process.cwd(), '.venv/bin/python')
  const fixture = 'services/local_agent/tests/fixtures/pending_dialogue_ui.py'
  const env = { PATH: process.env.PATH, HOME: process.env.HOME,
    PYTHONPATH: join(process.cwd(), 'services/local_agent/src'), PYTHONDONTWRITEBYTECODE: '1' }
  let child: ChildProcess | undefined, base = '', runtimeEpoch = 0
  const sid = 'session-runtime-test', cid = 'child-runtime'
  const quote = mode === 'tutor' ? '数学里两个负数相乘为什么得到正数？' : '等评价的时候，你能告诉我有没有PET学习资料吗？'
  const stop = async (): Promise<void> => {
    if (!child || child.exitCode !== null || child.signalCode !== null) { child = undefined; return }
    const end = once(child, 'exit'); child.kill('SIGKILL'); await end; child = undefined
  }
  const start = async (): Promise<void> => {
    child = spawn(python, [fixture, '--data-root', root, ...(mode === 'tutor' ? ['--tutor'] : [])], { cwd: process.cwd(), env, stdio: ['ignore', 'pipe', 'pipe'] })
    let errors = ''
    child.stderr!.on('data', data => { errors = (errors + String(data)).slice(-1600) })
    const lines = createInterface({ input: child.stdout! })
    const first = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('startup timeout: ' + errors)), 12000)
      lines.once('line', line => { clearTimeout(timer); resolve(line) })
      child!.once('exit', code => { clearTimeout(timer); reject(new Error('fixture exit ' + code + ': ' + errors)) })
    })
    lines.close()
    const ready = JSON.parse(first) as { port: number; synthetic_models: boolean; import_isolated: boolean }
    expect(ready.synthetic_models).toBe(true)
    expect(ready.import_isolated).toBe(true)
    base = `http://127.0.0.1:${ready.port}`
    runtimeEpoch += 1
    for (let i = 0; i < 50; i++) {
      try { if ((await fetch(base + '/openapi.json', { headers: { 'X-HSC-Session': 'public-independent-ui-fixture' } })).ok) return } catch { /* socket startup */ }
      await new Promise(resolve => setTimeout(resolve, 50))
    }
    throw new Error('service did not start: ' + errors)
  }
  const post = async (path: string, body: unknown): Promise<Record<string, unknown>> => {
    const response = await fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': 'public-independent-ui-fixture' }, body: JSON.stringify(body) })
    const value = await response.json()
    if (!response.ok) throw new Error(JSON.stringify(value))
    return value
  }
  const facts = (): Record<string, unknown> => JSON.parse(execFileSync(python,
    [fixture, '--data-root', root, '--inspect'], { cwd: process.cwd(), env }).toString())
  try {
    await start()
    vi.stubGlobal('crypto', webcrypto)
    const ownedRuntime = () => ownedVoiceRuntimeConfig(base, 'public-independent-ui-fixture', runtimeEpoch)
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getRuntimeConfig: async () => ownedRuntime(),
      getWakeWordSettings: async () => ({ childId: cid, wakeWord: '小知', enabled: true, inactivitySeconds: 60 }),
      getVoiceOnboarding: async () => ({ childId: cid, version: 1, step: 'completed' }) }, ownedRuntime, globalThis.fetch))
    const speech = vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    vi.spyOn(inputs, 'cancelLocalSpeech').mockImplementation(() => undefined)
    const nativeFetch = globalThis.fetch
    let actionId = ''
    vi.stubGlobal('fetch', (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).endsWith('/agent-turn') && init?.method === 'POST') {
        actionId = JSON.parse(String(init.body)).client_action_id
      }
      return nativeFetch(url, init)
    })
    resetRuntimeCache()
    const before = facts()
    savePendingEvaluation({ version: 1, sessionId: sid, childId: cid, turnId: 'eval', status: 'in_progress', silent: false })
    expect(await window.desktop?.getVoiceRuntimePolicy?.()).toEqual({ version: 1, capture: 'configured', runtimeEpoch })
    const initial = await localApi.sessionWorkspace(sid)
    const planned = vi.spyOn(localApi, 'agentTurn')
    const executed = vi.spyOn(localApi, 'executeAgentTurn')
    const completed = vi.spyOn(localApi, 'completeAgentTurn')
    const taught = vi.spyOn(localApi, 'askTutor')
    await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="初三" onBack={() => undefined} />) })
    await post(`/v1/sessions/${sid}/voice/realtime/begin`, { client_action_id: 'ui-pet-voice',
      model_label: 'synthetic-asr', captured_at_ms: Date.now(), capture_reference: { child_id: cid, pending_evaluation_turn_id: 'eval' } })
    const voice = await post(`/v1/sessions/${sid}/voice/realtime/complete`, { client_action_id: 'ui-pet-voice', transcript: quote })
    await act(async () => { await microphone.onTranscript(quote, undefined, voice.voice_input_id as string, 'eval') })
    expect(planned).toHaveBeenCalledTimes(1)
    expect(executed).toHaveBeenCalledTimes(mode === 'resources' ? 1 : 0)
    expect(completed).toHaveBeenCalledTimes(mode === 'tutor' ? 1 : 0)
    const turn = await planned.mock.results[0].value
    const receipt = mode === 'resources' ? await executed.mock.results[0].value : undefined
    if (mode === 'resources') {
      expect(turn.action.type).toBe('inspect_learning_system')
      expect(receipt!.execution.capability).toBe('system.inspect_learning')
      expect(receipt!.execution.result.cloud_transmission_allowed).toBe(false)
      expect(speech.mock.calls.some(([text]) => text === receipt!.execution.response_text)).toBe(true)
    } else {
      expect(turn.action.type).toBe('ask_tutor')
      expect(taught).toHaveBeenCalledTimes(1)
      const tutor = await taught.mock.results[0].value
      expect(tutor.question).toBe(quote)
      expect(speech.mock.calls.some(([text]) => text.includes('分配律'))).toBe(true)
      expect(completed.mock.calls[0][0]).toBe(turn.agent_turn_id)
    }
    expect(readPendingEvaluation(sid, cid)).toMatchObject({ turnId: 'eval', silent: true })
    const after = facts()
    for (const key of Object.keys(before).filter(key => !key.startsWith('independent_'))) expect(after[key]).toEqual(before[key])
    expect(after.independent_turns).toEqual([[turn.agent_turn_id]])
    expect((after.independent_receipts as unknown[]).length).toBe(1)
    expect((after.independent_tutor_turns as unknown[]).length).toBe(mode === 'tutor' ? 1 : 0)
    const callsBeforeRestart = JSON.parse(await readFile(join(root, 'model-calls.json'), 'utf8'))
    expect(callsBeforeRestart).toEqual(['voice_interaction', 'agent_plan', ...(mode === 'tutor' ? ['tutor_hint_level_6'] : [])])
    cleanup(); await stop(); await start(); resetRuntimeCache(); speech.mockClear()
    const restored = await localApi.sessionWorkspace(sid)
    await act(async () => { render(<SessionWorkspace initial={restored} childName="合成孩子" childGrade="初三" onBack={() => undefined} />) })
    // Recovery reads the original delivery identity, never creates a new one.
    const replay = await localApi.agentRequestReceipt(sid, actionId)
    expect(replay.status).toBe('recorded_completed')
    expect(replay.replay_allowed).toBe(false)
    // This endpoint is evaluation-only; the original evaluator still owns it.
    const recovered = await localApi.agentTurnExecutionStatus('eval')
    expect(['in_progress', 'unknown']).toContain(recovered.status)
    expect(recovered.execution).toBeUndefined()
    expect(planned).toHaveBeenCalledTimes(1)
    expect(executed).toHaveBeenCalledTimes(mode === 'resources' ? 1 : 0)
    expect(completed).toHaveBeenCalledTimes(mode === 'tutor' ? 1 : 0)
    await waitFor(() => expect(readPendingEvaluation(sid, cid)).toMatchObject({ turnId: 'eval', silent: true }))
    expect(speech).not.toHaveBeenCalled()
    expect(facts()).toEqual(after)
    expect(JSON.parse(await readFile(join(root, 'model-calls.json'), 'utf8'))).toEqual(callsBeforeRestart)
  } finally {
    cleanup(); await stop(); await rm(parent, { recursive: true, force: true }); resetRuntimeCache()
    localStorage.clear(); clearVoiceCaptureContext(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  }
}, 45000)
