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
import { VoiceMistakeDialogue } from './VoiceMistakeDialogue'
import { clearVoiceCaptureContext, voiceCaptureReference } from './voiceCaptureContext'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

it.each([['candidate', 'paper'], ['answer', 'paper'], ['confirm', 'paper'],
  ['diagnosis', 'paper'], ['remediation', 'paper'], ['diagnosis', 'diagnostic'], ['remediation', 'diagnostic']])('joins actual %s/%s dialogue reference, hands-free workspace and TCP Tutor then SIGKILL recovery', async (phase, source) => {
  const parent = await mkdtemp(join(tmpdir(), 'pytest-mistake-stage-ui-'))
  const root = join(parent, 'test_source')
  await mkdir(root)
  const python = join(process.cwd(), '.venv/bin/python')
  const fixture = 'services/local_agent/tests/fixtures/mistake_stage_ui.py'
  const env = { PATH: process.env.PATH, HOME: process.env.HOME,
    PYTHONPATH: join(process.cwd(), 'services/local_agent/src'), PYTHONDONTWRITEBYTECODE: '1' }
  let child: ChildProcess | undefined, base = '', sid = '', cid = '', runtimeEpoch = 0
  const quote = '数学里两个负数相乘为什么得到正数？'
  const stop = async (): Promise<void> => {
    if (!child || child.exitCode !== null || child.signalCode !== null) { child = undefined; return }
    const end = once(child, 'exit'); child.kill('SIGKILL'); await end; child = undefined
  }
  const start = async (): Promise<void> => {
    child = spawn(python, [fixture, '--data-root', root, '--phase', phase, '--source', source], { cwd: process.cwd(), env, stdio: ['ignore', 'pipe', 'pipe'] })
    let errors = ''
    child.stderr!.on('data', data => { errors = (errors + String(data)).slice(-1600) })
    const lines = createInterface({ input: child.stdout! })
    const first = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('startup timeout: ' + errors)), 18000)
      lines.once('line', line => { clearTimeout(timer); resolve(line) })
      child!.once('exit', code => { clearTimeout(timer); reject(new Error('fixture exit ' + code + ': ' + errors)) })
    })
    lines.close()
    const ready = JSON.parse(first)
    expect(ready.synthetic_models).toBe(true)
    expect(ready.import_isolated).toBe(true)
    sid = ready.session_id; cid = ready.child_id
    base = `http://127.0.0.1:${ready.port}`
    runtimeEpoch += 1
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(base + '/openapi.json', { headers: { 'X-HSC-Session': 'public-independent-ui-fixture' } })).ok) return } catch { /* Socket is starting. */ }
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
    expect(await window.desktop?.getVoiceRuntimePolicy?.()).toEqual({ version: 1, capture: 'configured', runtimeEpoch })
    const initial = await localApi.sessionWorkspace(sid)
    const planned = vi.spyOn(localApi, 'agentTurn')
    const taught = vi.spyOn(localApi, 'askTutor')
    const completed = vi.spyOn(localApi, 'completeAgentTurn')
    let finishOpening!: (handled: boolean) => void
    const opening = new Promise<boolean>(resolve => { finishOpening = resolve })
    await act(async () => { render(<><SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={() => undefined} />
      {source === 'paper' && <VoiceMistakeDialogue sessionId={sid} childId={cid} artifactId="voice-photo" onOpened={finishOpening} />}</>) })
    if (source === 'paper') await act(async () => { expect(await opening).toBe(true) })
    await waitFor(() => expect(voiceCaptureReference(sid, cid).question_reference).toMatchObject({ kind: 'mistake', binding: { phase, item_id: 'source-dialogue' } }))
    // Opening the saved dialogue legitimately refreshes its timestamp. Freeze
    // the exact persisted source only after that opening has completed.
    const before = facts()
    const reference = voiceCaptureReference(sid, cid)
    await post(`/v1/sessions/${sid}/voice/realtime/begin`, { client_action_id: 'ui-stage-voice', model_label: 'synthetic-asr',
      captured_at_ms: Date.now(), capture_reference: reference })
    const voice = await post(`/v1/sessions/${sid}/voice/realtime/complete`, { client_action_id: 'ui-stage-voice', transcript: quote })
    await act(async () => { await microphone.onTranscript(quote, undefined, voice.voice_input_id as string) })
    expect(planned).toHaveBeenCalledTimes(1)
    expect(taught).toHaveBeenCalledTimes(1)
    expect(completed).toHaveBeenCalledTimes(1)
    const turn = await planned.mock.results[0].value
    const tutor = await taught.mock.results[0].value
    expect(turn.action.type).toBe('ask_tutor')
    expect(tutor.question).toBe(quote)
    expect(speech.mock.calls.some(([text]) => text.includes('分配律'))).toBe(true)
    const after = facts()
    for (const key of Object.keys(before).filter(key => key !== 'independent_tutor_turns')) expect(after[key]).toEqual(before[key])
    expect((after.independent_tutor_turns as unknown[]).length).toBe(1)
    const calls = JSON.parse(await readFile(join(root, 'model-calls.json'), 'utf8'))
    expect(calls).toEqual(['voice_interaction', 'agent_plan', 'tutor_hint_level_6'])
    cleanup(); await stop(); await start(); resetRuntimeCache(); speech.mockClear()
    expect(actionId).toBeTruthy()
    const recovered = await localApi.agentRequestReceipt(sid, actionId)
    expect(recovered.status).toBe('recorded_completed')
    expect(recovered.replay_allowed).toBe(false)
    const restored = await localApi.mistakeDialogue(sid)
    expect(restored.dialogue?.dialogue_binding).toEqual(reference.question_reference!.binding)
    const workspace = await localApi.sessionWorkspace(sid)
    await act(async () => { render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={() => undefined} />) })
    expect(taught).toHaveBeenCalledTimes(1)
    expect(completed).toHaveBeenCalledTimes(1)
    expect(facts()).toEqual(after)
    expect(JSON.parse(await readFile(join(root, 'model-calls.json'), 'utf8'))).toEqual(calls)
    expect(speech).not.toHaveBeenCalled()
  } finally {
    cleanup(); await stop(); await rm(parent, { recursive: true, force: true }); resetRuntimeCache()
    localStorage.clear(); clearVoiceCaptureContext(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  }
}, 60000)
