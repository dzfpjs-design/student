import { cancelListenReady, LISTEN_READY_GUIDANCE } from './listenReadyCue'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { AdaptiveVoiceActivityDetector, HandsFreeVoiceInput, companionPlaybackCaptureCooldownMs, pcmCaptureBlockSamples } from './LocalInputs'
import { beginOnboardingResponse, revokeOnboardingResponses } from './onboardingResponseGrant'
import { getVoiceCaptureOwner, claimVoiceCapture } from './voiceCaptureCoordinator'
import * as voice from './companionVoice'
import { clearVoiceCaptureContext, trackDiagnosticCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import type { DiagnosticPromptResult } from './types'

const actualTranscribe=localApi.transcribe
const mediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const question = (id = 'original', oral = true) => ({ status: 'waiting_answer', prompt: {
  prompt_id: id, item_version: 'v-' + id, child_id: 'a', origin_session_id: 's',
  item: { track: 'pet', answer_type: oral ? 'speaking' : 'single_choice', assessment_mode: 'pet_reference_rubric' },
} }) as DiagnosticPromptResult
afterEach(() => {
  cancelListenReady(); revokeOnboardingResponses(); cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
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
  const transcribe = vi.spyOn(localApi, 'transcribe').mockResolvedValue({ status: 'completed', cloud_used: false, local_only: true, model_version: 'sherpa-onnx-zipformer-zh-en-2023-11-22',
    transcript: '今天数学学了分数。', voice_input_id: 'original-voice', capture_binding: { status: 'completed' } } as never)
  const transcribeStandby = vi.spyOn(localApi, 'transcribeStandby').mockResolvedValue({ status: 'completed',
    cloud_used: true, transcript: '嗯', persisted: false } as never)
  if (binding === 'original') await trackDiagnosticCapture('s', async () => question('original', true))
  let lease = null as ReturnType<typeof voice.getCompanionPlaybackLease>
  vi.spyOn(voice, 'getCompanionPlaybackLease').mockImplementation(() => lease)
  const status = vi.fn(), onTranscript = vi.fn()
  const view = render(<HandsFreeVoiceInput sessionId="s" childId="a" purpose={binding === 'plain' ? 'wake_word_setup' : 'agent_command'}
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
  const grant=beginOnboardingResponse({sessionId:'s',childId,phase:'active',step:'study_practice',
    prompt:{},promptText:'公开合成提示',epoch:1,isCurrent:()=>current})
  input.lease({id:'actual-playback',generation:1,sessionId:'s',kind:'local_sapi',text:'公开合成提示'} as never)
  input.state('speaking');grant.started()
  await input.nativeFrame(.8)
  input.lease(null);input.state('idle');grant.naturalEnd()
  await input.silence(84-input.blockSamples/16000*1000)
  return {grant,changeOwner:()=>{current=false}}
}
const speech=async(input:Awaited<ReturnType<typeof capture>>,n=10)=>{for(let i=0;i<n;i++)await input.nativeFrame(.03)}
it('retains every frame of a 320ms immediate reply and requests only local plain ASR after the original tail',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input)
 expect(input.transcribe).not.toHaveBeenCalled()
 await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.bytes().length).toBe(44+10*input.blockSamples*2)
 expect(input.transcribe.mock.calls[0][6]).toBe(true)
 expect(input.transcribe.mock.calls[0][5]).toMatchObject({child_id:'a',question_reference:null})
 expect(input.transcribe.mock.calls[0][5]?.local_recovery_only).not.toBe(true)
 expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('retains the first768ms and entire continuing1088ms from the actual recorder',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,24)
 for(let i=0;i<10;i++)await input.nativeFrame(.1)
 await input.silence()
 expect(input.bytes().length).toBe(44+34*input.blockSamples*2)
 expect(new DataView(input.bytes().buffer).getInt16(44,true)).toBe(Math.trunc(.03*Math.sin(2*Math.PI*180/16000)*0x7fff))
 expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('ordinary plain input without the separate natural-end permission keeps original850ms protection',async()=>{
 const input=await capture('plain',true);await startReply(input);await speech(input);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it.each([false,true])('unknown or changed systemAEC does not silently submit early reply (initialall=%s)',async(all)=>{
 const input=await capture('plain',all);await authorizedReply(input)
 if(all)input.track.getSettings=()=>({deviceId:'original-mic',echoCancellation:true}) as never
 await speech(input);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
 expect(input.view.container.textContent).toContain('请先停一下，再说一遍')
})
it('another child prompt cannot authorize this native recorder',async()=>{
 const input=await capture('plain',true);await authorizedReply(input,'b');await speech(input);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled()
})
it.each(['prompt','replay','error','ended','privacy'])('revokes all retained early frames on%s',async(kind)=>{
 const input=await capture('plain',true),scope=await authorizedReply(input);await speech(input,4)
 if(kind==='prompt')scope.changeOwner()
 if(kind==='replay'){input.lease({id:'replacement',generation:2}as never);input.state('speaking')}
 if(kind==='error')input.state('error')
 if(kind==='ended')input.track.readyState='ended'
 if(kind==='privacy')window.dispatchEvent(new Event('hsc-privacy-pause'))
 await speech(input,6);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it('late ASR completion after actual capture ownership is revoked cannot dispatch',async()=>{
 const input=await capture('plain',true);await authorizedReply(input)
 let finish!:(value:never)=>void
 input.transcribe.mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
 vi.spyOn(localApi,'cancelTranscriptionAction').mockResolvedValue({status:'cancelled'}as never)
 vi.spyOn(localApi,'transcriptionAction').mockResolvedValue({status:'ignored'}as never)
 await speech(input);await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce()
 input.view.unmount()
 await act(async()=>finish({status:'completed',local_only:true,cloud_used:false,transcript:'公开回答',voice_input_id:'v',capture_binding:{status:'completed'}}as never))
 expect(input.onTranscript).not.toHaveBeenCalled()
})
it('unexpected cloud receipt is rejected even if adapter labels it completed',async()=>{
 const input=await capture('plain',true);await authorizedReply(input)
 input.transcribe.mockResolvedValue({status:'completed',cloud_used:true,local_only:true,transcript:'公开回答',voice_input_id:'v',capture_binding:{status:'completed'}}as never)
 await speech(input);await input.silence()
 expect(input.onTranscript).not.toHaveBeenCalled();expect(input.view.container.textContent).toContain('本机采集凭据')
})
it('cancelLocalSpeech invalidates an already ended grant rather than reusing idle state',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,4)
 const {cancelLocalSpeech}=await import('./LocalInputs');cancelLocalSpeech()
 await speech(input,6);await input.silence();expect(input.transcribe).not.toHaveBeenCalled()
})
it('expired8s permission cannot be queued as an early answer',async()=>{
 const input=await capture('plain',true);const scope=await authorizedReply(input)
 await input.silence(8000)
 expect(scope.grant.responsePending('s','a')).toBe(false)
})
it('playback echo before genuine natural completion retains no permitted answer',async()=>{
 const input=await capture('plain',true)
 const grant=beginOnboardingResponse({sessionId:'s',childId:'a',phase:'active',step:'study_practice',prompt:{},promptText:'公开提示',epoch:1,isCurrent:()=>true})
 input.lease({id:'speech',generation:1,sessionId:'s',kind:'local_sapi',text:'公开提示'}as never);input.state('speaking');grant.started()
 await speech(input);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it('new recorder registration after native end cannot borrow the old frozen track epoch',async()=>{
 const input=await capture('plain',true),response=await authorizedReply(input)
 await speech(input,4)
 const {registerOnboardingCapture}=await import('./onboardingResponseGrant')
 const release=registerOnboardingCapture('s','a',{owner:getVoiceCaptureOwner()!,track:input.track as unknown as MediaStreamTrack,
  epoch:Symbol('replacement'),echoPolicy:{mode:'all',negotiation:'accepted',acoustic_quality_verified:false}},()=>true)
 await speech(input,6);await input.silence()
 expect(response.grant.responsePending('s','a')).toBe(false);expect(input.transcribe).not.toHaveBeenCalled();release()
})
// These run the real recorder and ordinary/local API entry points. No native ASR is invoked.
it.each([true, undefined, 'remote-only'])('blocks the entire1088ms early reply in unsupported actual echo mode%s',async(mode)=>{
 const input=await capture('plain',false);input.track.getSettings=()=>({deviceId:'original-mic',echoCancellation:mode})as never
 await authorizedReply(input);await speech(input,34);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
 expect(input.view.container.textContent).toContain('请先停一下，再说一遍')
})
it('AEC downgrade during an early1088ms reply never queues its ordinary/cloud tail',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,4)
 input.track.getSettings=()=>({deviceId:'original-mic',echoCancellation:true})as never
 await speech(input,30);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it('unsupported early speech remains blocked beyond8s expiry until actual silence frames',async()=>{
 const input=await capture('plain',false),scope=await authorizedReply(input);await speech(input,270);await input.silence()
 expect(scope.grant.responsePending('s','a')).toBe(false)
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled();expect(input.onTranscript).not.toHaveBeenCalled()
})
it('claimed8640ms reply stays one complete local utterance without a default ASR tail',async()=>{
 const input=await capture('plain',true),scope=await authorizedReply(input);await speech(input,270);await input.silence()
 expect(scope.grant.responsePending('s','a')).toBe(false)
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.transcribe.mock.calls[0][6]).toBe(true)
 expect(input.transcribeStandby).not.toHaveBeenCalled();expect(input.onTranscript).toHaveBeenCalledOnce()
 expect(input.bytes().length).toBe(44+270*input.blockSamples*2)
 const wav=input.bytes(),view=new DataView(wav.buffer,wav.byteOffset,wav.byteLength)
 for(let i=0;i<270*input.blockSamples;i++){
  const sample=Math.fround(.03*Math.sin((i%input.blockSamples+1)*2*Math.PI*180/16000))
  expect(view.getInt16(44+i*2,true)).toBe(Math.trunc(sample*0x7fff))
 }
})
it('wall-clock silence alone does not release a rejected early utterance; observed natural silence permits a fresh late answer',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,34);await input.silence(8000)
 await speech(input,10);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled()
 // Original conversation silence1050ms, measured from real received PCM duration; no synthetic ended event.
 for(let i=0;i<33;i++)await input.nativeFrame(0)
 await completeReadinessNotice(input)
 await speech(input,10);await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.transcribe.mock.calls[0][6]).toBeUndefined()
 expect(input.onTranscript).toHaveBeenCalledOnce()
})
it('a genuinely late answer after the low-AEC readiness cue retains the original default route',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await input.silence(900)
 for(let i=0;i<33;i++)await input.nativeFrame(0)
 await completeReadinessNotice(input)
 await speech(input,10);await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.transcribe.mock.calls[0][6]).toBeUndefined()
})

it('interrupted natural silence cannot release the rejected utterance',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,34)
 for(let i=0;i<31;i++)await input.nativeFrame(0)
 await input.nativeFrame(.03)
 for(let i=0;i<2;i++)await input.nativeFrame(0)
 await speech(input,10);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled()
})
it('a missing-frame gap cannot join two quiet fragments into natural silence',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,34)
 for(let i=0;i<20;i++)await input.nativeFrame(0)
 await input.silence(1000)
 for(let i=0;i<13;i++)await input.nativeFrame(0)
 await speech(input,10);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled()
})
it('RTC-fallback remote-only early reply emits zero real API transport requests; quiet recovery keeps default late routing',async()=>{
 const input=await capture('plain',false)
 input.track.getSettings=()=>({deviceId:'original-mic',echoCancellation:'remote-only'})as never
 const requests:{local_only?:boolean}[]=[]
 const fetch=vi.fn(async(_url:unknown,options?:RequestInit)=>{
   requests.push(JSON.parse(String(options?.body)))
   return {ok:true,json:async()=>({status:'completed',cloud_used:true,transcript:'今天数学学了分数。',voice_input_id:'fake-cloud-result',capture_binding:{status:'completed'}})}as Response
 })
 vi.stubGlobal('fetch',fetch);input.transcribe.mockImplementation(actualTranscribe)
 await authorizedReply(input);await speech(input,34);await input.silence()
 expect(fetch).not.toHaveBeenCalled();expect(requests).toEqual([])
 for(let i=0;i<33;i++)await input.nativeFrame(0)
 await completeReadinessNotice(input)
 await speech(input,10);await input.silence()
 expect(fetch).toHaveBeenCalledOnce();expect(requests[0].local_only).toBeUndefined()
})

it('same-owner automatic native track reconnection cannot submit a rejected early tail; new real quiet permits a full new answer',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,10)
 await act(async()=>{input.ended();await vi.advanceTimersByTimeAsync(1500)})
 expect(input.opens()).toBe(2);expect(input.tracks[0].readyState).toBe('ended');expect(input.tracks[1]).not.toBe(input.tracks[0])
 expect(input.tracks[1].readyState).toBe('live')
 await speech(input,24);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled()
 expect(input.view.container.textContent).toContain('请先停一下，再说一遍')
 for(let i=0;i<33;i++)await input.nativeFrame(0)
 await completeReadinessNotice(input)
 await speech(input,10);await input.silence()
 expect(input.transcribe).toHaveBeenCalledOnce();expect(input.transcribe.mock.calls[0][6]).toBeUndefined()
})
it('track loss during an eligible retained reply cannot authorize a new-track ordinary tail',async()=>{
 const input=await capture('plain',true);await authorizedReply(input);await speech(input,4)
 await act(async()=>{input.ended();await vi.advanceTimersByTimeAsync(1500)})
 expect(input.opens()).toBe(2);await speech(input,30);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled();expect(input.transcribeStandby).not.toHaveBeenCalled()
})
it('a reconnected track cannot borrow silence accumulated on the previous track',async()=>{
 const input=await capture('plain',false);await authorizedReply(input);await speech(input,10)
 for(let i=0;i<20;i++)await input.nativeFrame(0)
 await act(async()=>{input.ended();await vi.advanceTimersByTimeAsync(1500)})
 expect(input.opens()).toBe(2)
 for(let i=0;i<13;i++)await input.nativeFrame(0)
 await speech(input,24);await input.silence()
 expect(input.transcribe).not.toHaveBeenCalled()
})
