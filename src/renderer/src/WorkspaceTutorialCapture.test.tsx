import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import {act,cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import type {ComponentProps} from 'react'
import {SessionWorkspace} from './SessionWorkspace'
import * as inputs from './LocalInputs'
import * as voice from './companionVoice'
import {localApi,requestVoiceSynthesis} from './api'
import * as api from './api'
import {listenReadyPending,cancelListenReady} from './listenReadyCue'
import {onboardingResponseGrant,revokeOnboardingResponses} from './onboardingResponseGrant'
import {getVoiceCaptureOwner,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import {clearVoiceCaptureContext,trackDiagnosticCapture} from './voiceCaptureContext'
import type {SessionWorkspaceState} from './types'
const disposers:Array<()=>void>=[]
let props:ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs',async original=>{const actual=await original<typeof inputs>();return {...actual,
 HandsFreeVoiceInput:(p:typeof props)=>{props=p;return <actual.HandsFreeVoiceInput {...p}/>},VoiceInputButton:()=>null}})
vi.mock('./VoiceCalibrationPanel',async()=>{const {useEffect}=await import('react');return {VoiceCalibrationPanel:({onMicrophoneBindingReadyChange}:any)=>{
 useEffect(()=>{onMicrophoneBindingReadyChange(true)},[onMicrophoneBindingReadyChange]);return null}}})
const media=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
afterEach(()=>{cleanup();disposers.splice(0).forEach(fn=>fn());cancelListenReady();revokeOnboardingResponses();voice.stopCompanionSpeech();clearVoiceCaptureContext();resetVoiceCaptureCoordinatorForTests();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();if(media)Object.defineProperty(navigator,'mediaDevices',media);else Reflect.deleteProperty(navigator,'mediaDevices')})
const workspace:SessionWorkspaceState={session:{session_id:'public-session',child_id:'public-child',status:'active',state:'check_in',observation_status:'paused',snooze_until:null},planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0},family_rule:{observation_mode:'shadow'},recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],plan:null,plan_items:[],drafts:{},latest_tutor_turn:null,sensor_release:{opened_leases:0,active_leases:0,released_leases:0,by_device:{}},daily_report_ready:false}as unknown as SessionWorkspaceState
async function mount(step='wake_practice',saveDelay=0,playbackAmplitude=0,sampleRate=16000,partialManaged=false,fallbackAvailable=true,echoMode:'browser-selected'|'unknown'='browser-selected'){
 vi.useFakeTimers()
 const processors:Array<any>=[],utterances:Array<any>=[],tones:Array<any>=[],playbackKinds:Array<string>=[]
 class Audio{
  state='running';sampleRate=sampleRate;destination={};get currentTime(){return Date.now()/1000}
  createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()})
  createScriptProcessor=(samples:number)=>{const p={samples,connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null};processors.push(p);return p}
  createBuffer=(_c:number,n:number,rate:number)=>({duration:n/rate,getChannelData:()=>new Float32Array(n)})
  createBufferSource=()=>{const listeners=new Set<()=>void>();let timer:any;const node:any={buffer:null,onended:null,connect:vi.fn(),disconnect:vi.fn(),addEventListener:(_type:string,fn:()=>void)=>listeners.add(fn),stop:()=>{clearTimeout(timer);listeners.forEach(fn=>fn())},start:(at?:number)=>{if(at===undefined)tones.push(node);timer=setTimeout(()=>{listeners.forEach(fn=>fn());node.onended?.({isTrusted:true})},Math.max(0,(at??this.currentTime)-this.currentTime)*1000+node.buffer.duration*1000)}};return node}
  resume=async()=>{this.state='running'};close=async()=>{this.state='closed'}
 }
 const track={readyState:'live',enabled:true,stop:vi.fn(),getSettings:()=>({deviceId:'public-mic',echoCancellation:echoMode==='unknown'?undefined:true}),getCapabilities:()=>({echoCancellation:[true,false]}),addEventListener:vi.fn(),removeEventListener:vi.fn()}
 vi.stubGlobal('AudioContext',Audio)
 Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:vi.fn(async()=>({getAudioTracks:()=>[track],getTracks:()=>[track]})),enumerateDevices:vi.fn(async()=>[])}})
 class Utterance{onstart:any;onend:any;onerror:any;voice:any;lang='';rate=1;constructor(public text:string){}}
 const synthesis={getVoices:()=>fallbackAvailable?[{name:'public-local',lang:'zh-CN',localService:true}]:[],speak:(u:Utterance)=>{utterances.push(u);setTimeout(()=>u.onstart?.({isTrusted:true}),0)},cancel:vi.fn(),speaking:false,pending:false,paused:false}
 vi.stubGlobal('speechSynthesis',synthesis);vi.stubGlobal('SpeechSynthesisUtterance',Utterance)
 let progress:any={childId:'public-child',version:1,step,updatedAt:'2026-10-10T12:00:00Z'}
 const desktop={getWakeWordSettings:vi.fn(async()=>({childId:'public-child',wakeWord:'小知',enabled:true,inactivitySeconds:60,updatedAt:'2026-10-10T12:00:00Z'})),saveWakeWordSettings:vi.fn(async x=>x),getVoiceOnboarding:vi.fn(async()=>progress),saveVoiceOnboarding:vi.fn(async x=>{if(saveDelay)await new Promise(r=>setTimeout(r,saveDelay));progress=x;return x})}
 vi.stubGlobal('desktop',ownedVoiceRuntimePolicyDesktop(desktop));vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
 for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
   vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
     handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
 }
 vi.spyOn(api,'requestVoiceSynthesis').mockImplementation(async()=>{
   if(!partialManaged)throw new Error('MANAGED_ROUTE_UNAVAILABLE')
   const pcm=new Uint8Array(8+9600),view=new DataView(pcm.buffer);view.setUint32(0,16000,true);view.setUint32(4,9600,true)
   for(let i=0;i<4800;i++)view.setInt16(8+i*2,Math.trunc(2000*Math.sin(i*2*Math.PI*180/16000)),true)
   return new Response(pcm,{status:200,headers:{'Content-Type':'application/x-hsc-pcm16-frame-v2'}})
  })
 disposers.push(voice.subscribeCompanionVoice(state=>{if(state==='speaking')playbackKinds.push(voice.getCompanionPlaybackLease()?.kind??'absent')}))
 vi.spyOn(localApi,'agentExecutionRecoveryIndex').mockImplementation(async(session,child)=>({session_id:session,child_id:child,local_only:true,cloud_transmission_allowed:false,replay_allowed:false,has_more:false,entries:[]}))
 vi.spyOn(localApi,'registerDevice').mockResolvedValue({lease_id:'public-lease',status:'active',expires_at:'later'})
 vi.spyOn(localApi,'releaseDeviceLease').mockResolvedValue({lease_id:'public-lease',status:'released'})
 vi.spyOn(localApi,'voiceInteraction').mockResolvedValue({route:'onboarding',action:'advance',source:'cloud',response_text:'公开合成回复'}as never)
 vi.spyOn(localApi,'agentTurn').mockRejectedValue(new Error('PRACTICE_MUST_NOT_REACH_AGENT'))
 const asr=vi.spyOn(localApi,'transcribeStandby').mockResolvedValue({client_action_id:'wake-public',transcript:'小知',duration_ms:320,model_version:'public-local',status:'completed',persisted:false,cloud_used:false})
 const transcribe=vi.spyOn(localApi,'transcribe').mockResolvedValue({status:'completed',cloud_used:false,local_only:true,transcript:'今天数学学了分数作业是做三道题',model_version:'sherpa-onnx-zipformer-zh-en-2023-11-22'}as never)
 let view:any
 await act(async()=>{view=render(<SessionWorkspace initial={workspace} childName='公开学生' childGrade='初三' onBack={vi.fn()}/>);await vi.advanceTimersByTimeAsync(1)})
 for(let i=0;i<100&&!processors.at(-1)?.onaudioprocess;i++)await act(async()=>{await vi.advanceTimersByTimeAsync(1)})
 expect(processors.at(-1)?.onaudioprocess).toBeTypeOf('function')
 const frame=async(amplitude=0)=>{await act(async()=>{const samples=processors.at(-1).samples;await vi.advanceTimersByTimeAsync(samples/sampleRate*1000);processors.at(-1).onaudioprocess({inputBuffer:{getChannelData:()=>Float32Array.from({length:samples},(_,i)=>amplitude*Math.sin((i+1)*2*Math.PI*180/sampleRate))}})})}
 const frames=async(n:number,amplitude=0)=>{for(let i=0;i<n;i++)await frame(amplitude)}
 const end=async()=>{await act(async()=>{await vi.advanceTimersByTimeAsync(1);utterances.at(-1)?.onend?.({isTrusted:true});await vi.advanceTimersByTimeAsync(1)})}
 return {view,frame,frames,end,playbackAmplitude,playbackKinds,utterances,tones,desktop,asr,transcribe,progress:()=>progress,processors}
}
it.each([{saveDelay:0,sampleRate:16000,playbackAmplitude:0,partialManaged:false,echoMode:'browser-selected' as const},{saveDelay:78,sampleRate:48000,playbackAmplitude:.08,partialManaged:false,echoMode:'browser-selected' as const},{saveDelay:78,sampleRate:48000,playbackAmplitude:.08,partialManaged:true,echoMode:'browser-selected' as const},{saveDelay:78,sampleRate:48000,playbackAmplitude:.08,partialManaged:true,echoMode:'unknown' as const}])('binds mounted study through saved IPC progress $saveDelay and native $sampleRate frames, partial=$partialManaged with cue',async({saveDelay,sampleRate,playbackAmplitude,partialManaged,echoMode})=>{
 const h=await mount('wake_practice',saveDelay,playbackAmplitude,sampleRate,partialManaged,true,echoMode)
 expect(props.purpose).toBe('wake_word_setup')
 await h.frames(20)
 await h.end()
 expect(props.wakeWord).toBe('小知')
 await h.frames(sampleRate===48000?110:60)
 await h.frames(10,.03);await h.frames(sampleRate===48000?65:38)
 await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})
 expect(h.asr).toHaveBeenCalledOnce()
 expect(h.progress().step).toBe('study_practice')
 expect(props.purpose).toBe('wake_word_setup')
 expect(props.wakeWord).toBeUndefined()
 expect(h.utterances.at(-1).text).toContain('今天数学学了分数');if(saveDelay)expect(h.processors).toHaveLength(2);if(partialManaged)expect(h.playbackKinds).toContain('managed')
 expect(onboardingResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 await h.frames(sampleRate===48000?620:400,playbackAmplitude)
 expect(h.transcribe).not.toHaveBeenCalled()
 await h.end()
 expect(onboardingResponseGrant('public-session','public-child')).not.toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true)
 await h.frames(sampleRate===48000?110:60)
 expect(h.utterances.at(-1).text).toContain('听到提示音完全结束')
 await h.end();await h.frames(sampleRate===48000?150:100)
 expect(h.tones).toHaveLength(1)
 await h.frames(40)
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 expect(h.transcribe).not.toHaveBeenCalled()
 await h.frames(sampleRate===48000?15:10,.03);await h.frames(sampleRate===48000?65:38)
 await act(async()=>{await vi.advanceTimersByTimeAsync(200)})
 expect(h.transcribe).toHaveBeenCalledOnce()
 expect(h.transcribe.mock.calls[0][1]).toBe('wake_word_setup')
 expect(h.transcribe.mock.calls[0][6]).toBeUndefined()
 expect(h.progress().step).toBe('document_practice')
 expect(localApi.agentTurn).not.toHaveBeenCalled()
})

it('never grants a managed prefix when the full local fallback is unavailable',async()=>{
 const h=await mount('study_practice',0,0,48000,true,false)
 await h.frames(30)
 expect(h.playbackKinds).toContain('managed')
 expect(h.utterances).toHaveLength(0)
 expect(onboardingResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 expect(h.tones).toHaveLength(0);expect(h.transcribe).not.toHaveBeenCalled()
})
it.each(['interrupted','unmounted','privacy'])('rejects a stale fallback natural end after %s',async(reason)=>{
 const h=await mount('study_practice',0,0,48000,true)
 await h.frames(30,.08)
 expect(h.playbackKinds).toContain('managed');expect(h.playbackKinds).toContain('local_sapi')
 expect(onboardingResponseGrant('public-session','public-child')).toBeNull()
 if(reason==='unmounted')h.view.unmount()
 else await act(async()=>{if(reason==='privacy')window.dispatchEvent(new Event('hsc-privacy-pause'));else voice.stopCompanionSpeech()})
 await h.end()
 expect(onboardingResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 expect(h.tones).toHaveLength(0);expect(h.transcribe).not.toHaveBeenCalled()
})
it('does not replace a dedicated diagnostic binding with a tutorial capture lane',async()=>{
 await trackDiagnosticCapture('public-session',async()=>({status:'waiting_answer',prompt:{prompt_id:'public-question',item_version:'public-v1',child_id:'public-child',origin_session_id:'public-session',item:{track:'pet',answer_type:'speaking',assessment_mode:'pet_reference_rubric'}}})as never)
 const h=await mount('study_practice',0,0,48000,true)
 await h.frames(100,.08);await h.end()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 expect(h.tones).toHaveLength(0);expect(h.transcribe).not.toHaveBeenCalled()
})
