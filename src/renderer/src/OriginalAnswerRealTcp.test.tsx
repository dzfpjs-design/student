import fs from 'node:fs'
import { createHash } from 'node:crypto'
import { setTimeout as realDelay } from 'node:timers/promises'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { HandsFreeVoiceInput } from './LocalInputs'
import * as voice from './companionVoice'
import { clearVoiceCaptureContext, trackDiagnosticCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'

const mediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
afterEach(() => {
  cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests(); resetRuntimeCache()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear()
  if (mediaDevices) Object.defineProperty(navigator, 'mediaDevices', mediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

it.skipIf(!process.env.HSC_ORIGINAL_ANSWER_TCP_CASE)('captures original PCM through actual ASR TCP, Runtime and persisted answer', async () => {
  const config = JSON.parse(fs.readFileSync(process.env.HSC_ORIGINAL_ANSWER_TCP_CASE!, 'utf8'))
  const captureMode = config.capture_mode ?? 'prefix'
  const realWord = captureMode === 'real_short_word' || captureMode === 'real_dialogue_marker'
  vi.stubGlobal('desktop', { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: config.base_url, sessionToken: '' }) })
  resetRuntimeCache()
  let state: ReturnType<typeof voice.getCompanionVoiceState> = 'idle'
  vi.spyOn(voice, 'getCompanionVoiceState').mockImplementation(() => state)
  const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as null | ((event: AudioProcessingEvent) => void) }
  const track = { stop: vi.fn(), getSettings: () => ({ deviceId: 'synthetic-original-mic' }), getCapabilities: () => ({}) }
  const sampleRate = captureMode === 'short_48k' ? 48000 : 16000
  let blockSamples = 0
  vi.stubGlobal('AudioContext', class {
    sampleRate = sampleRate
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = (samples: number) => { blockSamples = samples; return processor }
    close = vi.fn().mockResolvedValue(undefined)
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    enumerateDevices: async () => [],
    getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }),
  } })
  await trackDiagnosticCapture(config.session_id, () => localApi.diagnosticPrompt(config.session_id))
  vi.useFakeTimers()
  const asr = vi.spyOn(localApi, 'transcribe')
  const onTranscript = vi.fn()
  const status = vi.fn()
  render(<HandsFreeVoiceInput sessionId={config.session_id} childId={config.child_id} purpose="agent_command"
    autoStart initialConversationActive onTranscript={onTranscript} onRuntimeStatusChange={status} />)
  for (let i = 0; i < 100 && status.mock.lastCall?.[0].phase !== 'listening'; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(20); await realDelay(20) })
  }
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  const frame = async (amplitude: number, samples = 4096) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(samples / sampleRate * 1000)
      const chunk = Float32Array.from({ length: samples }, (_, i) => amplitude * Math.sin((i + 1) * 2 * Math.PI * 180 / sampleRate))
      processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
    })
  }
  const short = captureMode !== 'prefix'
  let expectedSamples = 6 * 4096, firstAmplitude = .03
  let expectedFirstSample: number | undefined
  if (realWord) {
    const wav = fs.readFileSync(config.wav_fixture)
    // Generated fixture includes metadata chunks; locate PCM rather than assuming offset 44.
    let offset = 12
    while (wav.toString('ascii', offset, offset + 4) !== 'data') {
      offset += 8 + wav.readUInt32LE(offset + 4) + (wav.readUInt32LE(offset + 4) % 2)
      expect(offset).toBeLessThan(wav.length)
    }
    const length = wav.readUInt32LE(offset + 4)
    const pcm = wav.subarray(offset + 8, offset + 8 + length)
    expectedSamples = Math.ceil(pcm.length / 2 / blockSamples) * blockSamples
    expectedFirstSample = Math.trunc(pcm.readInt16LE(0) / 32768 * 0x7fff)
    for (let start = 0; start < expectedSamples; start += blockSamples) {
      const chunk = Float32Array.from({ length: blockSamples }, (_, i) =>
        (start + i) * 2 < pcm.length ? pcm.readInt16LE((start + i) * 2) / 32768 : 0)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(blockSamples / sampleRate * 1000)
        processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
      })
    }
  } else if (short) {
    expect(blockSamples / sampleRate * 1000).toBeLessThanOrEqual(32)
    if (captureMode === 'preroll_short') {
      for (let i = 0; i < Math.ceil(sampleRate * 3 / blockSamples); i += 1) await frame(0, blockSamples)
    }
    const blocks = Math.ceil(sampleRate * .32 / blockSamples)
    for (let i = 0; i < blocks; i += 1) await frame(.1, blockSamples)
    expectedSamples = Math.round((blocks * blockSamples + (captureMode === 'preroll_short' ? sampleRate * 1.4 : 0)) * 16000 / sampleRate)
    firstAmplitude = captureMode === 'preroll_short' ? 0 : .1
  } else {
    state = 'speaking'; await frame(0.8)
    state = 'idle'; for (let i = 0; i < 3; i += 1) await frame(0.03)
    for (let i = 0; i < 3; i += 1) await frame(0.1)
  }
  await act(async () => { await vi.advanceTimersByTimeAsync(1200) })
  vi.useRealTimers()
  for (let i = 0; i < 100 && !onTranscript.mock.calls.length; i += 1) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
  }
  expect(asr).toHaveBeenCalledTimes(1)
  expect(onTranscript).toHaveBeenCalledTimes(1)
  const data = Buffer.from(asr.mock.calls[0][2], 'base64')
  expect(data.length).toBe(44 + expectedSamples * 2)
  expect(data.readInt16LE(44)).toBe(expectedFirstSample ?? Math.trunc(firstAmplitude * Math.sin(2 * Math.PI * 180 / sampleRate) * 0x7fff))
  const captured = await asr.mock.results[0].value
  expect(captured.capture_binding).toMatchObject({ prompt_id: config.prompt_id, item_version: config.item_version })
  const turn = await localApi.agentTurn(config.session_id, captured.transcript, undefined, captured.voice_input_id,
    undefined, undefined, 'original-tcp-turn')
  if (realWord) {
    // The controlled port returns a Chinese acknowledgement to an English PET
    // prompt: recognition is real VAD, not evidence of an English answer/skill.
    expect(captured.transcript).toBe(captureMode === 'real_dialogue_marker' ? '嗯' : '对')
    expect(turn.action.type).toBe('none')
    expect(turn.action.voice_input_id).toBe(captured.voice_input_id)
    fs.writeFileSync(config.result, JSON.stringify({ sha256: createHash('sha256').update(data).digest('hex'),
      byte_length: data.length, client_action_id: asr.mock.calls[0][4], voice_input_id: captured.voice_input_id,
      agent_turn_id: turn.agent_turn_id, raw_audio_available: captured.capture_binding?.raw_audio_available,
      prompt_id: config.prompt_id, item_version: config.item_version,
      capture_mode: captureMode, duration_ms: expectedSamples / 16000 * 1000 }))
    return
  }
  expect(turn.action.source_voice_input_id).toBe(captured.voice_input_id)
  expect(turn.action.type).toBe('submit_diagnostic_answer')
  const workspace = await localApi.sessionWorkspace(config.session_id)
  const executed = await runDiagnosticVoice(workspace, { ...turn.action, type: 'submit_diagnostic_answer' },
    turn.agent_turn_id, () => true)
  expect(executed.result).toMatchObject({ status: 'answered' })
  const receipt = { capability: turn.runtime_capability ?? turn.action.capability,
    execution_location: turn.runtime_execution_location ?? turn.action.execution_location,
    action_type: turn.action.type, status: 'completed', completed_at: new Date().toISOString(),
    output: executed.result }
  await localApi.completeAgentTurn(turn.agent_turn_id, executed.response, 'completed', undefined,
    { execution_receipt: receipt, client_bridge_receipt: receipt })
  fs.writeFileSync(config.result, JSON.stringify({ sha256: createHash('sha256').update(data).digest('hex'),
    byte_length: data.length, client_action_id: asr.mock.calls[0][4], voice_input_id: captured.voice_input_id,
    agent_turn_id: turn.agent_turn_id, raw_audio_available: captured.capture_binding?.raw_audio_available,
    prompt_id: config.prompt_id, item_version: config.item_version,
    capture_mode: captureMode, duration_ms: expectedSamples / 16000 * 1000 }))
}, 45000)
