import { cancelListenReady, LISTEN_READY_GUIDANCE } from './listenReadyCue'
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
  cancelListenReady(); revokeOrdinaryResponses();disposeSession?.(); cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
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
  const tones:Array<{buffer:AudioBuffer|null;start:ReturnType<typeof vi.fn>;stop:ReturnType<typeof vi.fn>;disconnect:ReturnType<typeof vi.fn>;onended:((e:Event)=>void)|null}>=[],contexts:Array<{state:string;close:ReturnType<typeof vi.fn>}>=[]
  const guidance:Array<{text:string;started:(()=>void)|undefined;options:voice.CompanionSpeechOptions}>=[]
  const tracks=[track]
  let currentTrack=track
  vi.stubGlobal('AudioContext', class {
    sampleRate = sampleRate;state='running';destination={};born=Date.now()
    constructor(){contexts.push(this)}
    get currentTime(){return (Date.now()-this.born)/1000}
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = (samples: number) => { blockSamples = samples;processor={connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null}; return processor }
    createBuffer = (_channels:number,frames:number,rate:number)=>{const data=new Float32Array(frames);return{duration:frames/rate,length:frames,sampleRate:rate,getChannelData:()=>data}as unknown as AudioBuffer}
    createBufferSource = ()=>{
      let timer:ReturnType<typeof setTimeout>|undefined
      const source={buffer:null as AudioBuffer|null,onended:null as ((e:Event)=>void)|null,connect:vi.fn(),disconnect:vi.fn(),
        start:vi.fn(()=>{timer=setTimeout(()=>source.onended?.({isTrusted:true}as Event),121)}),
        stop:vi.fn(()=>{clearTimeout(timer);source.onended?.({isTrusted:true}as Event)})}
      tones.push(source);return source
    }
    resume=vi.fn(async()=>{this.state='running'})
    close = vi.fn(async()=>{this.state='closed'})
  })
  vi.spyOn(voice,'speakCompanion').mockImplementation(async(text,_provider,_cache,started,options={})=>{
    guidance.push({text,started,options});state='preparing';for(const fn of listeners)fn(state)
    options.onPreparationStart?.();return 'system_offline'
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn(async()=>{
      if(opens++>0){const next={...track,readyState:'live',stop:vi.fn(()=>{next.readyState='ended'}),getSettings:()=>({deviceId:'replacement-mic',echoCancellation:echoMode})};tracks.push(next);currentTrack=next}
      const opened=currentTrack;return {getTracks:()=>[opened],getAudioTracks:()=>[opened]}
    }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'voicePlayback').mockImplementation(async (_session,body)=>({playback_id:body.playback_id}))
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
    guidance,tones,contexts,track, tracks, ended:()=>{track.readyState='ended';for(const fn of [...trackListeners])fn()},opens:()=>opens, blockSamples, nativeFrame: (amplitude: number) => frame(amplitude, blockSamples / sampleRate * 1000, blockSamples) }
}

/** Complete the independently guarded low-AEC notice and tone using actual PCM quiet.
 * These recorder tests retain their original no-tail / current-track assertions. */
async function completeReadinessNotice(input: Awaited<ReturnType<typeof capture>>) {
  expect(input.guidance).toHaveLength(1)
  const notice=input.guidance[0]
  expect(notice.text).toContain(LISTEN_READY_GUIDANCE)
  expect(notice.options.localOnly).toBe(true)
  expect(notice.started).toBeTypeOf('function')
  expect(notice.options.onPlaybackEnd).toBeTypeOf('function')
  input.lease({id:'readiness-notice',generation:2,sessionId:'s',kind:'local_sapi',text:notice.text} as never)
  input.state('speaking');notice.started?.();await input.nativeFrame(.8);await input.silence(250)
  input.lease(null);input.state('idle');notice.options.onPlaybackEnd?.();await input.silence(1)
  for(let i=0;i<62;i++)await input.nativeFrame(0)
  expect(input.tones).toHaveLength(1)
  await input.silence(121)
  for(let i=0;i<5;i++)await input.nativeFrame(0)
  expect(input.transcribe).not.toHaveBeenCalled()
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

it('ordinary320ms retains entire short response with local-only and exact original references',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input)
 expect(input.transcribe).not.toHaveBeenCalled();await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.bytes().length).toBe(44+10*input.blockSamples*2)
 expect(input.transcribe.mock.calls[0][6]).toBe(true);expect(input.transcribe.mock.calls[0][7]).toEqual({version:'ordinary_prompt_response_v1',session_state:'study',source_agent_turn_id:'completed-agent'})
 expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('ordinary1088ms retains first768ms and complete long response',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,24);await speech(input,10);await input.silence()
 expect(input.bytes().length).toBe(44+34*input.blockSamples*2);expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('ordinary without explicit natural-end grant leaves the original850ms barrier',async()=>{
 const input=await capture('plain',true);await startReply(input);await speech(input);await input.silence();expect(input.transcribe).not.toHaveBeenCalled()
})
it.each([false,true])('unsupported/downgraded AEC whole1088ms cannot leak truncated ordinary/cloud tail (%s)',async(all)=>{
 const input=await capture('plain',all);await authorizedReply(input)
 if(all)input.track.getSettings=()=>({deviceId:'original-mic',echoCancellation:true})
 await speech(input,34);await input.silence();expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled()
})
it('8s expiry cannot release the old rejected long utterance into ordinary ASR',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,280);await input.silence();expect(input.transcribe).not.toHaveBeenCalled()
})
it('actual ended→newGUM does not migrate PCM or release whole-utterance rejection',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,10);input.ended()
 for(let i=0;i<30&&input.opens()<2;i++)await input.silence(100)
 expect(input.opens()).toBe(2);await speech(input,40);await input.silence();expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled()
})
it('new ordinary late answer remains possible after1050ms actual quiet',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,34)
 for(let i=0;i<40;i++)await input.nativeFrame(0)
 // Finish the guarded recovery notice/tone before a new late answer. Wall time alone is insufficient.
 await completeReadinessNotice(input);await speech(input,34);await input.silence();expect(input.transcribe).toHaveBeenCalledOnce()
 expect(input.transcribe.mock.calls[0][6]).not.toBe(true)
})
it('changed source object epoch destroys retained PCM before queue',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,4)
 setVoiceQuestionCapture('s','a',{kind:'mistake',question_id:'new',version:'v',source_ref:'m'}as never)
 await speech(input,6);await input.silence();expect(input.transcribe).not.toHaveBeenCalled()
})
it.each(['pending','recovery'])('no new response grant can bypass %s authority',async(kind)=>{
 const input=await capture('plain',true)
 if(kind==='pending')setVoicePendingEvaluationCapture('s','a','evaluation')
 else setVoiceLocalRecoveryCapture('s','a',true)
 await authorizedReply(input);await speech(input);await input.silence();expect(input.transcribe).not.toHaveBeenCalled()
})
it.each(['cloud','marker','reference','raw'])('ordinary saved provenance rejects %s mismatch',async(kind)=>{
 const input=await capture('plain',true);await authorizedReply(input)
 input.transcribe.mockImplementation(async(...args)=>({status:'completed',cloud_used:kind==='cloud',local_only:true,transcript:'公开回答',voice_input_id:'v',capture_binding:{status:'completed',raw_audio_available:kind==='raw',ordinary_response_binding:kind==='marker'?{version:'unknown'}:args[7],question_reference:kind==='reference'?{kind:'mistake'}:null}})as never)
 await speech(input);await input.silence();expect(input.onTranscript).not.toHaveBeenCalled()
})
it('same child/session source revoked during ASR suppresses late delivery',async()=>{
 const input=await capture('plain',true);const scope=await authorizedReply(input)
 let finish:(value:never)=>void=()=>{};input.transcribe.mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
 await speech(input);await input.silence();scope.changeOwner()
 finish({status:'completed',cloud_used:false,local_only:true,transcript:'公开回答',voice_input_id:'v',capture_binding:{status:'completed',raw_audio_available:false}}as never)
 await input.silence(1);expect(input.onTranscript).not.toHaveBeenCalled()
})
it('frames while the prompt is still speaking cannot claim natural-end permission',async()=>{
 const input=await capture('plain',true);disposeSession=registerOrdinarySession({sessionId:'s',childId:'a',state:'study'})
 const grant=beginOrdinaryResponse({sessionId:'s',childId:'a',state:'study',prompt:{},text:'公开合成提示',isCurrent:()=>true})
 input.lease({id:'actual',generation:1,sessionId:'s',kind:'local_sapi',text:'公开合成提示'}as never);input.state('speaking');grant.started()
 await speech(input,34);await input.silence();expect(input.transcribe).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it.each([
 {kind:'diagnostic',binding:{prompt_id:'p',item_version:'v'}},
 {kind:'diagnostic_help',binding:{prompt_id:'p',item_version:'v'}},
 {kind:'exam',binding:{run_id:'run',position:1,item_id:'item',source_version:'v'}},
 {kind:'vocabulary',binding:{prompt_session_id:'ps',card_version:'v'}},
 {kind:'mistake',binding:{session_id:'s',child_id:'a',phase:'retest',item_id:'i',source_version:'v',mistake_id:'m'}},
 {kind:'document',binding:{revision_id:'r',artifact_id:'a',content_sha256:'c',page_sha256:'p'}}
])('ordinary lane preserves the full original %s capture reference',async(reference)=>{
 const input=await capture('plain',true);setVoiceQuestionCapture('s','a',reference as never);await authorizedReply(input);await speech(input);await input.silence()
 expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual(reference);expect(input.onTranscript).toHaveBeenCalledOnce()
})
