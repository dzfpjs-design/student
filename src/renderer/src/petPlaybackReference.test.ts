import { it, expect, vi } from 'vitest'
import { isPetPcmControlReference } from './petPlaybackReference'
import { playLocalPcm, getCompanionPlaybackLease, stopCompanionSpeech } from './companionVoice'
const reference=()=>({version:'pet_full_audio_control_reference_v1' as const,child_id:'child',session_id:'session',media_id:'media',prompt_id:'canonical',item_version:'version',diagnostic_item_id:'leader',source_version:'source',audio_sha256:'a'.repeat(64),spoken_text_sha256:'b'.repeat(64),source_review_event_id:'approval',generation:2,local_only:true as const,cloud_transmission_allowed:false as const})
it.each([['extra',{text:'must not expose script'}],['generation',{generation:true}],['private',{cloud_transmission_allowed:true}],['unknown',{version:'unknown'}]])('rejects malformed %s capsule',(_kind,delta)=>expect(isPetPcmControlReference({...reference(),...delta})).toBe(false))
it('actual PCM engine retains only its immutable reviewed capsule at native scheduling and cancellation retires it',async()=>{
 const stop=vi.fn(),close=vi.fn(async()=>{})
 class Context {state='running';currentTime=0;destination={};close=close
  createBuffer=(_channels:number,length:number,rate:number)=>({duration:length/rate,getChannelData:()=>new Float32Array(length)})
  createBufferSource=()=>Object.assign(new EventTarget(),{buffer:null,connect:vi.fn(),start:vi.fn(),stop})
 }
 vi.stubGlobal('AudioContext',Context)
 const input=reference();let observed:ReturnType<typeof getCompanionPlaybackLease>=null
 let started!:()=>void;const start=new Promise<void>(resolve=>{started=resolve})
 try{
  const playback=playLocalPcm(new Int16Array(8000).buffer,16000,()=>{observed=getCompanionPlaybackLease();started()},{localOnly:true,localPlaybackSessionId:'session',pcmControlReference:input,isCurrent:()=>true})
  await start;expect(observed).toMatchObject({kind:'local_pcm',text:null,pcmReference:reference()});expect(Object.isFrozen(observed!.pcmReference)).toBe(true)
  input.source_review_event_id='changed caller object';expect(observed!.pcmReference!.source_review_event_id).toBe('approval')
  stopCompanionSpeech();expect(await playback).toBe('cancelled');expect(getCompanionPlaybackLease()).toBeNull();expect(stop).toHaveBeenCalledTimes(1);expect(close).toHaveBeenCalledTimes(1)
 }finally{stopCompanionSpeech();vi.unstubAllGlobals()}
})
