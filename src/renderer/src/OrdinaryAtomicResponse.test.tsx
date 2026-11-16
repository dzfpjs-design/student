import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { AdaptiveVoiceActivityDetector, HandsFreeVoiceInput, companionPlaybackCaptureCooldownMs, pcmCaptureBlockSamples } from './LocalInputs'
import { beginOrdinaryResponse, revokeOrdinaryResponses, registerOrdinarySession } from './ordinaryResponseGrant'
import { getVoiceCaptureOwner, claimVoiceCapture } from './voiceCaptureCoordinator'
import * as voice from './companionVoice'
import { clearVoiceCaptureContext, trackDiagnosticCapture, setVoicePendingEvaluationCapture,setVoiceLocalRecoveryCapture,setVoiceQuestionCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import type { DiagnosticPromptResult } from './types'

let disposeSession:(()=>void)|undefined
const actualTranscribe=localApi.transcribe
const mediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const question = (id = 'original', oral = true) => ({ status: 'waiting_answer', prompt: {
  prompt_id: id, item_version: 'v-' + id, child_id: 'a', origin_session_id: 's',
  item: { track: 'pet', answer_type: oral ? 'speaking' : 'single_choice', assessment_mode: 'pet_reference_rubric' },
} }) as DiagnosticPromptResult
afterEach(() => {
  revokeOrdinaryResponses();disposeSession?.(); cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear()
  if (mediaDevices) Object.defineProperty(navigator, 'mediaDevices', mediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

async function capture(binding: 'plain' | 'original' = 'plain', systemAec = false, sampleRate = 16_000, standby = false) {
  vi.useFakeTimers()
  let state: ReturnType<typeof voice.getCompanionVoiceState> = 'idle'
  vi.spyOn(voice, 'getCompanionVoiceState').mockImplementation(() => state)
  const listeners = new Set<(next: typeof state) => void>()
  vi.spyOn(voice, 'subscribeCompanionVoice').mockImplementation(callback => { listeners.add(callback); return () => listeners.delete(callback) })
  let processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as null | ((event: AudioProcessingEvent) => void) }
  let echoMode: unknown = true
  const applyConstraints = vi.fn(async () => { echoMode = 'all' })
  const trackListeners=new Set<()=>void>()
  const track = { addEventListener:(_name:string,fn:()=>void)=>trackListeners.add(fn),removeEventListener:(_name:string,fn:()=>void)=>trackListeners.delete(fn),readyState: 'live', enabled: true, stop: vi.fn(() => { track.readyState = 'ended' }), getSettings: () => ({ deviceId: 'original-mic', echoCancellation: echoMode }),
    getCapabilities: () => ({ echoCancellation: systemAec ? ['all', true, false] : [true, false] }), applyConstraints }
  let blockSamples = 0,opens=0
  const tracks=[track]
  let currentTrack=track
  vi.stubGlobal('AudioContext', class {
    sampleRate = sampleRate; state = 'running'
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = (samples: number) => { blockSamples = samples;processor={connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null}; return processor }
    close = vi.fn().mockResolvedValue(undefined)
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn(async()=>{
      if(opens++>0){const next={...track,readyState:'live',stop:vi.fn(()=>{next.readyState='ended'}),getSettings:()=>({deviceId:'replacement-mic',echoCancellation:echoMode})};tracks.push(next);currentTrack=next}
      const opened=currentTrack;return {getTracks:()=>[opened],getAudioTracks:()=>[opened]}
    }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'prefix', status: 'active', expires_at: 'later' })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'prefix', status: 'released' })
  const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(async(...args)=>({status:'completed',cloud_used:false,local_only:true,
    model_version:'sherpa-onnx-zipformer-zh-en-2023-11-22',transcript:'公开合成回答',voice_input_id:'v',capture_binding:{status:'completed',raw_audio_available:false,
      ordinary_response_binding:args[7],question_reference:args[5]?.question_reference??null,activity_reference:args[5]?.activity_reference,
      task_choice_reference:args[5]?.task_choice_reference,question_validation:args[5]?.question_reference?'valid':undefined,
      activity_validation:args[5]?.activity_reference?'valid':undefined}})as never)
  const transcribeStandby = vi.spyOn(localApi, 'transcribeStandby').mockResolvedValue({ status: 'completed',
    cloud_used: true, transcript: '嗯', persisted: false } as never)
  if (binding === 'original') await trackDiagnosticCapture('s', async () => question('original', true))
  let lease = null as ReturnType<typeof voice.getCompanionPlaybackLease>
  vi.spyOn(voice, 'getCompanionPlaybackLease').mockImplementation(() => lease)
  const status = vi.fn(), onTranscript = vi.fn()
  const view = render(<HandsFreeVoiceInput sessionId="s" childId="a" purpose='agent_command'
    autoStart initialConversationActive={!standby} wakeWord={standby ? '小知' : undefined}
    onTranscript={onTranscript} onRuntimeStatusChange={status} />)
  const readyPhase = standby ? 'wake_standby' : 'listening'
  for (let i = 0; i < 100 && status.mock.lastCall?.[0].phase !== readyPhase; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  }
  expect(processor.onaudioprocess).toBeTypeOf('function')
  expect(status.mock.lastCall?.[0].phase).toBe(readyPhase)
  const frame = async (amplitude = 0.1, milliseconds = 256, samples = 4096) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(milliseconds)
      const chunk = Float32Array.from({ length: samples }, (_, i) => amplitude * Math.sin((i + 1) * 2 * Math.PI * 180 / sampleRate))
      processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
    })
  }
  const silence = async (milliseconds = 1200) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }
  const bytes = () => Uint8Array.from(atob(transcribe.mock.calls[0][2]), character => character.charCodeAt(0))
  return { lease: (value: typeof lease) => { lease = value }, state: (value: typeof state) => { state = value; for (const callback of listeners) callback(value) }, frame, silence, transcribe, transcribeStandby, status, onTranscript, view, bytes, stop: track.stop, applyConstraints,
    track, tracks, ended:()=>{track.readyState='ended';for(const fn of [...trackListeners])fn()},opens:()=>opens, blockSamples, nativeFrame: (amplitude: number) => frame(amplitude, blockSamples / sampleRate * 1000, blockSamples) }
}

async function startReply(input: Awaited<ReturnType<typeof capture>>) {
  input.state('speaking')
  await input.nativeFrame(.8)
  input.state('idle') // Actual observer receives the natural end, extending the original 850ms gate.
  await input.silence(84 - input.blockSamples / 16000 * 1000)
}


async function authorizedReply(input: Awaited<ReturnType<typeof capture>>, childId='a') {
  let current=true
  disposeSession=registerOrdinarySession({sessionId:'s',childId:'a',state:'study'})
  const grant=beginOrdinaryResponse({sessionId:'s',childId,state:'study',
    prompt:{},text:'公开合成提示',sourceAgentTurnId:'completed-agent',isCurrent:()=>current})
  input.lease({id:'actual-playback',generation:1,sessionId:'s',kind:'local_sapi',text:'公开合成提示'} as never)
  input.state('speaking');grant.started()
  await input.nativeFrame(.8)
  input.lease(null);input.state('idle');grant.naturalEnd()
  await input.silence(84-input.blockSamples/16000*1000)
  return {grant,changeOwner:()=>{current=false}}
}
const speech=async(input:Awaited<ReturnType<typeof capture>>,n=10)=>{for(let i=0;i<n;i++)await input.nativeFrame(.03)}


it('claimed response keeps all8640ms PCM in one local request',async()=>{
 const input=await capture('plain',true);await authorizedReply(input)
 await speech(input,270);for(let i=0;i<38;i++)await input.nativeFrame(0)
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.transcribe.mock.calls[0][6]).toBe(true)
 expect(input.transcribeStandby.mock.calls.length).toBe(0);expect(input.onTranscript).toHaveBeenCalledOnce()
 const wav=input.bytes(),view=new DataView(wav.buffer,wav.byteOffset,wav.byteLength)
 expect((wav.length-44)/2).toBeGreaterThanOrEqual(270*input.blockSamples)
 for(let i=0;i<270*input.blockSamples;i++){
  const sample=Math.fround(.03*Math.sin((i%input.blockSamples+1)*2*Math.PI*180/16000))
  expect(view.getInt16(44+i*2,true)).toBe(Math.trunc(sample*0x7fff))
 }
})
it('over12s response refuses entire utterance and tail until actual quiet then permits a new late answer',async()=>{
 const input=await capture('plain',true);await authorizedReply(input)
 await speech(input,440);await input.silence(1500);await speech(input,20)
 expect(input.transcribe.mock.calls.length).toBe(0);expect(input.transcribeStandby.mock.calls.length).toBe(0);expect(input.onTranscript).not.toHaveBeenCalled()
 for(let i=0;i<34;i++)await input.nativeFrame(0)
 await speech(input,10);for(let i=0;i<38;i++)await input.nativeFrame(0)
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.onTranscript).toHaveBeenCalledOnce()
 expect(input.transcribe.mock.calls[0][6]).toBeUndefined()
})

it('response hard limit refusal survives actual sameowner track ended and newGUM until new continuous quiet',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,400)
 await act(async()=>{input.ended();await vi.advanceTimersByTimeAsync(1500)})
 expect(input.opens()).toBe(2);expect(input.tracks[0].readyState).toBe('ended');expect(input.tracks[1].readyState).toBe('live')
 await speech(input,30);await input.silence(1200)
 expect(input.transcribe.mock.calls.length).toBe(0);expect(input.transcribeStandby.mock.calls.length).toBe(0);expect(input.onTranscript.mock.calls.length).toBe(0)
 for(let i=0;i<34;i++)await input.nativeFrame(0)
 await speech(input,10);for(let i=0;i<38;i++)await input.nativeFrame(0)
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('ordinary late unclaimed speech retains the existing8s segment routing',async()=>{
 const input=await capture('plain',true);await speech(input,270);for(let i=0;i<38;i++)await input.nativeFrame(0)
 expect(input.transcribe.mock.calls.length).toBe(2)
 expect(input.transcribe.mock.calls.map(args=>args[6])).toEqual([undefined,undefined])
})
