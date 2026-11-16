import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {setTimeout as realDelay} from 'node:timers/promises'
import {HandsFreeVoiceInput} from './LocalInputs'
import {localApi,resetRuntimeCache} from './api'
import * as voice from './companionVoice'
import {resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import {clearVoiceCaptureContext} from './voiceCaptureContext'

const mediaDescriptor=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
const tracks:Array<EventTarget&{readyState:string;stop:ReturnType<typeof vi.fn>}>=[]
const contexts:Array<{state:string;close:ReturnType<typeof vi.fn>}>=[]
let mode:'ok'|'pending'|'forbidden'|'expired'|'wrong-lease'='ok'
const requests:Array<{url:string;signal:AbortSignal|undefined;resolve:(v:Response)=>void}>=[]
const advance=async(ms:number)=>{await act(async()=>{await vi.advanceTimersByTimeAsync(ms)})}
function response(lease:string,status='active',seconds=180){return new Response(JSON.stringify({lease_id:lease,status,expires_at:new Date(Date.now()+seconds*1000).toISOString()}),{status:200,headers:{'Content-Type':'application/json'}})}
beforeEach(()=>{
 vi.useFakeTimers();vi.setSystemTime(100000);resetRuntimeCache();resetVoiceCaptureCoordinatorForTests();tracks.length=0;contexts.length=0;requests.length=0;mode='ok'
 vi.spyOn(voice,'getCompanionVoiceState').mockReturnValue('idle');vi.spyOn(voice,'subscribeCompanionVoice').mockImplementation(()=>()=>{})
 vi.stubGlobal('AudioContext',class{
  sampleRate=16000;state='running';close=vi.fn(async()=>{this.state='closed'})
  constructor(){contexts.push(this)}
  createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()})
  createScriptProcessor=()=>({connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null})
 })
 Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:vi.fn(async()=>{
  const track=Object.assign(new EventTarget(),{readyState:'live',enabled:true,label:'public synthetic microphone',stop:vi.fn(),getSettings:()=>({deviceId:'public-mic'}),getCapabilities:()=>({})});track.stop.mockImplementation(()=>{track.readyState='ended'});tracks.push(track);return{getTracks:()=>[track],getAudioTracks:()=>[track]}
 }),enumerateDevices:vi.fn(async()=>[])}})
 let registration=0
 vi.spyOn(localApi,'registerDevice').mockImplementation(async()=>({lease_id:'lease-'+(++registration),status:'active',expires_at:new Date(Date.now()+180000).toISOString()}))
 vi.spyOn(localApi,'releaseDeviceLease').mockImplementation(async lease=>({lease_id:lease,status:'released'}))
 vi.spyOn(localApi,'renewDeviceLease') // Call through the actual API and request() implementation.
 vi.spyOn(localApi,'voicePlayback').mockResolvedValue({}as never)
 vi.spyOn(localApi,'transcribe').mockRejectedValue(Error('NO_PROVIDER_ALLOWED'))
 vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL,init?:RequestInit)=>{
  const url=new URL(String(input));expect(url.origin).toBe('http://127.0.0.1:8765');expect(init?.method).toBe('POST');expect(url.pathname).toMatch(/^\/v1\/device-leases\/lease-\d+\/renew$/)
  const lease=url.pathname.split('/')[3]
  if(mode==='pending')return new Promise<Response>(resolve=>requests.push({url:url.href,signal:init?.signal??undefined,resolve}))
  requests.push({url:url.href,signal:init?.signal??undefined,resolve:()=>{}})
  if(mode==='forbidden')return Promise.resolve(new Response(JSON.stringify({detail:{code:'DEVICE_PERMISSION_DENIED',message:'public denial'}}),{status:403,headers:{'Content-Type':'application/json'}}))
  return Promise.resolve(response(mode==='wrong-lease'?'foreign-lease':lease,'active',mode==='expired'?-1:180))
 }))
})
afterEach(()=>{cleanup();clearVoiceCaptureContext();resetVoiceCaptureCoordinatorForTests();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();resetRuntimeCache();if(mediaDescriptor)Object.defineProperty(navigator,'mediaDevices',mediaDescriptor);else Reflect.deleteProperty(navigator,'mediaDevices')})
async function mount(){
 const props={sessionId:'public-session-a',childId:'public-child-a',audioDeviceId:'public-mic-a',purpose:'agent_command',autoStart:true,onTranscript:vi.fn()}
 const view=render(<HandsFreeVoiceInput {...props}/>);await advance(20)
 await act(async()=>{for(let n=0;n<400&&!vi.mocked(localApi.registerDevice).mock.calls.length;n++)await realDelay(5)})
 expect(localApi.registerDevice).toHaveBeenCalledOnce();return{view,props}
}
it('renews the same original live lease through the real API beyond the180s backend TTL',async()=>{
 await mount();await advance(181000)
 expect(localApi.registerDevice).toHaveBeenCalledOnce();expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();expect(localApi.renewDeviceLease).toHaveBeenCalledTimes(6)
 expect(requests).toHaveLength(6);expect(requests.every(r=>r.url.endsWith('/lease-1/renew'))).toBe(true);expect(tracks[0].readyState).toBe('live');expect(contexts[0].state).toBe('running');expect(localApi.releaseDeviceLease).not.toHaveBeenCalled();expect(localApi.transcribe).not.toHaveBeenCalled()
})
it('normal stop aborts the original pending renewal and cancels all future old-lease renewals',async()=>{
 mode='pending';await mount();await advance(30000);expect(requests).toHaveLength(1);expect(requests[0].signal?.aborted).toBe(false)
 fireEvent.click(screen.getByRole('button',{name:/正在听/}));expect(requests[0].signal?.aborted).toBe(true);expect(tracks[0].readyState).toBe('ended');await advance(90000)
 expect(requests).toHaveLength(1);expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1);expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-1','hands_free_stopped');expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
})
it('a hung renewal reaches the10s deadline and uses original bounded automatic recovery',async()=>{
 mode='pending';await mount();await advance(30000);expect(requests).toHaveLength(1);await advance(10000)
 expect(requests[0].signal?.aborted).toBe(true);expect(tracks[0].readyState).toBe('ended');await advance(100);expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-1','hands_free_capture_ended');expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
 mode='ok';await advance(1000);await act(async()=>{for(let n=0;n<400&&vi.mocked(localApi.registerDevice).mock.calls.length<2;n++)await realDelay(5)});expect(localApi.registerDevice).toHaveBeenCalledTimes(2);expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
})
it.each(['forbidden','expired','wrong-lease']as const)('rejects %s original renewal evidence and retires only the original lease',async reason=>{
 mode=reason;await mount();await advance(30000);await advance(100);expect(tracks[0].readyState).toBe('ended');expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('lease-1','hands_free_capture_ended');expect(requests).toHaveLength(1);expect(localApi.transcribe).not.toHaveBeenCalled()
})
it('session/child/device rebind aborts old flight; its late denial cannot stop the new original owner',async()=>{
 mode='pending';const{view,props}=await mount();await advance(30000);const old=requests[0]
 mode='ok';view.rerender(<HandsFreeVoiceInput {...props} sessionId="public-session-b" childId="public-child-b" audioDeviceId="public-mic-b"/>);await advance(20)
 await act(async()=>{for(let n=0;n<400&&vi.mocked(localApi.registerDevice).mock.calls.length<2;n++)await realDelay(5)});expect(old.signal?.aborted).toBe(true);expect(localApi.registerDevice).toHaveBeenCalledTimes(2)
 await act(async()=>old.resolve(new Response(JSON.stringify({detail:{code:'DEVICE_PERMISSION_DENIED'}}),{status:403})));await advance(30000)
 expect(tracks[1].readyState).toBe('live');expect(requests.at(-1)?.url).toContain('/lease-2/renew');expect(localApi.releaseDeviceLease).not.toHaveBeenCalledWith('lease-2',expect.anything());expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
})
it('unmount clears the timer and aborts its pending request without reacquiring a microphone',async()=>{
 mode='pending';const{view}=await mount();await advance(30000);view.unmount();expect(requests[0].signal?.aborted).toBe(true);await advance(90000);expect(requests).toHaveLength(1);expect(tracks[0].readyState).toBe('ended');expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
})
it('a registration arriving after normal cancellation is released once without starting any lease renewal',async()=>{
 let finish!:(v:Awaited<ReturnType<typeof localApi.registerDevice>>)=>void
 vi.mocked(localApi.registerDevice).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 await mount();fireEvent.click(screen.getByRole('button',{name:'取消打开麦克风'}))
 await act(async()=>finish({lease_id:'late-original',status:'active',expires_at:new Date(Date.now()+180000).toISOString()}));await advance(181000)
 expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1);expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('late-original','hands_free_open_cancelled_after_registration');expect(localApi.renewDeviceLease).not.toHaveBeenCalled();expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();expect(tracks[0].readyState).toBe('ended')
})
