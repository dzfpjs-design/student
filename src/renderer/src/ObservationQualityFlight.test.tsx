import {act, cleanup, render, screen} from '@testing-library/react'
import {createHash} from 'node:crypto'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {LocalObservation} from './SessionWorkspace'
import {localApi, resetRuntimeCache} from './api'
import * as media from './mediaDevices'
import {reconcileObservationRegistration} from './observationCameraStartup'
import {reconcileObservationCoverage} from './observationCoverageStartup'

vi.mock('./QwenRealtimeVoiceInput',()=>({QwenRealtimeVoiceInput:()=>null}))
const receipt=(epoch:string,boundary:string)=>({status:'recorded',event_id:`coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}`})
const success=(reason='synthetic fresh analysis')=>({state:'writing',observation_ready:true,scene_quality:{status:'ready',message:reason},person_baseline:{status:'established',message:'synthetic baseline'},reason,intervention:null})
const turn=async(ms=0)=>{await act(async()=>{await vi.advanceTimersByTimeAsync(ms)})}
let tracks:Array<{readyState:string;stop:ReturnType<typeof vi.fn>;addEventListener:ReturnType<typeof vi.fn>}>=[]
let frames=0,time=0,dark=true
const originalMedia=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
const originalQuality=Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype,'getVideoPlaybackQuality')

describe('observation quality original-flight recovery',()=>{
 beforeEach(()=>{
  vi.useFakeTimers();tracks=[];frames=0;time=0;dark=true;resetRuntimeCache()
  vi.spyOn(crypto.subtle,'digest').mockImplementation(async(_a,data)=>Uint8Array.from(createHash('sha256').update(Buffer.from(data as ArrayBuffer)).digest()).buffer)
  vi.spyOn(HTMLMediaElement.prototype,'play').mockResolvedValue()
  vi.spyOn(HTMLMediaElement.prototype,'paused','get').mockReturnValue(false)
  vi.spyOn(HTMLMediaElement.prototype,'ended','get').mockReturnValue(false)
  vi.spyOn(HTMLMediaElement.prototype,'readyState','get').mockReturnValue(2)
  vi.spyOn(HTMLMediaElement.prototype,'currentTime','get').mockImplementation(()=>++time/10)
  vi.spyOn(HTMLVideoElement.prototype,'videoWidth','get').mockReturnValue(640)
  vi.spyOn(HTMLVideoElement.prototype,'videoHeight','get').mockReturnValue(360)
  Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',{configurable:true,value:()=>({totalVideoFrames:++frames,droppedVideoFrames:0})})
  const data=new Uint8ClampedArray(320*180*4)
  for(let i=0;i<data.length;i+=4){data[i]=data[i+1]=data[i+2]=(i/4)%2?180:60;data[i+3]=255}
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage:vi.fn(),getImageData:()=>({width:320,height:180,data:dark?new Uint8ClampedArray(data.length):data})} as never)
  vi.spyOn(HTMLCanvasElement.prototype,'toDataURL').mockReturnValue('data:image/jpeg;base64,c3ludGhldGlj')
  Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{enumerateDevices:vi.fn().mockResolvedValue([]),getUserMedia:vi.fn(async()=>{const track={readyState:'live',enabled:true,muted:false,stop:vi.fn(()=>{track.readyState='ended'}),addEventListener:vi.fn()};tracks.push(track);return {getTracks:()=>[track],getVideoTracks:()=>[track]}})}})
  vi.spyOn(media,'resolveBoundVideoDevice').mockResolvedValue({device:{deviceId:'synthetic-bound-camera',label:'synthetic camera'} as MediaDeviceInfo,method:'system_id_hash'})
  vi.spyOn(media,'mediaDeviceRegistration').mockResolvedValue({systemIdHash:'analysis-camera-hash',capabilities:{}})
  vi.spyOn(localApi,'deviceBindings').mockResolvedValue([{device_type:'camera',system_id_hash:'analysis-camera-hash',label:'synthetic camera'}] as never)
  vi.spyOn(localApi,'registerDevice').mockImplementation(async()=>({lease_id:'synthetic-lease-'+tracks.length,status:'active',expires_at:'later'}))
  vi.spyOn(localApi,'releaseDeviceLease').mockImplementation(async id=>({lease_id:id,status:'released'}))
  vi.spyOn(localApi,'startObservationQualification').mockResolvedValue({} as never)
  vi.spyOn(localApi,'confirmDeviceFrame').mockResolvedValue({} as never)
  vi.spyOn(localApi,'beginObservationStream').mockImplementation(async(_s,epoch)=>receipt(epoch,'start'))
  vi.spyOn(localApi,'endObservationStream').mockImplementation(async(_s,epoch)=>receipt(epoch,'stop'))
  vi.spyOn(localApi,'recordDeviceIncident').mockResolvedValue({} as never)
  vi.spyOn(localApi,'pendingSessionBehaviorIntervention').mockResolvedValue({status:'none'} as never)
  vi.stubGlobal('desktop',{getRuntimeConfig:async()=>({status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'synthetic-analysis-test',dataScope:'trial'})})
 })
 afterEach(async()=>{
  cleanup();await turn(5100)
  await act(async()=>{await reconcileObservationRegistration('analysis-camera-hash');await reconcileObservationCoverage('analysis-session');await reconcileObservationCoverage('analysis-other-session')})
  vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers()
  if(originalMedia)Object.defineProperty(navigator,'mediaDevices',originalMedia)
  if(originalQuality)Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',originalQuality)
  else delete(HTMLVideoElement.prototype as unknown as {getVideoPlaybackQuality?:unknown}).getVideoPlaybackQuality
 })


 it.each(['headers','body'] as const)('bounds bad-frame quality HTTP %s and recovers with the same original stream ACK rules',async(stage)=>{
  let firstSignal:AbortSignal|undefined
  const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>{
   if(!firstSignal){firstSignal=init?.signal??undefined
    if(stage==='headers')return await new Promise<Response>((_resolve,reject)=>{init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic quality HTTP aborted','AbortError')),{once:true})})
    return {ok:true,json:()=>new Promise((_resolve,reject)=>{init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic quality body aborted','AbortError')),{once:true})})} as Response
   }
   return {ok:true,json:async()=>success('synthetic recovered healthy capture')} as Response
  })
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(String(fetch.mock.calls[0][0])).toContain('/observation/qualification/unavailable')
  const request=JSON.parse(String(fetch.mock.calls[0][1]?.body))
  expect(request.capture_sample).toMatchObject({stream_epoch:expect.any(String),sequence:expect.any(Number),capture_monotonic_ms:expect.any(Number)})
  const first=tracks[0]
  await turn(8_000)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(firstSignal?.aborted).toBe(false)
  dark=false
  await turn(3_000)
  expect(firstSignal?.aborted).toBe(true)
  expect(first.stop).toHaveBeenCalledTimes(1)
  expect(localApi.endObservationStream).toHaveBeenCalled()
  await turn(6_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(tracks.length).toBe(2)
  expect(screen.getByText(/synthetic recovered healthy capture/)).toBeTruthy()
 })

 it('unmounted session quality rejection cannot overwrite a newly mounted session or unlock its pending analysis',async()=>{
  let rejectOld!:(error:Error)=>void,resolveNew!:(value:unknown)=>void
  let oldSignal:AbortSignal|undefined
  const fetch=vi.fn().mockImplementationOnce(async(_url:unknown,init?:RequestInit)=>{oldSignal=init?.signal??undefined;return {ok:true,json:()=>new Promise((_resolve,reject)=>{rejectOld=reject})}})
   .mockResolvedValueOnce({ok:true,json:()=>new Promise(resolve=>{resolveNew=resolve})})
   .mockResolvedValue({ok:true,json:async()=>success()})
  vi.stubGlobal('fetch',fetch)
  const view=render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  expect(fetch).toHaveBeenCalledTimes(1)
  view.unmount();await turn(5_100)
  dark=false
  render(<LocalObservation sessionId="analysis-other-session" active enabled snoozeUntil={null}/>);await turn()
  expect(oldSignal?.aborted).toBe(true)
  expect(fetch).toHaveBeenCalledTimes(2)
  await act(async()=>{rejectOld(new Error('public retired quality failure'))})
  await turn(2_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(screen.queryByText(/取景状态暂时没有保存/)).toBeNull()
  await act(async()=>{resolveNew(success('synthetic new-child current capture'))})
  expect(screen.getByText(/synthetic new-child current capture/)).toBeTruthy()
 })

 it('quality authorization denial retires its stream without automatic permission retries',async()=>{
  const fetch=vi.fn().mockResolvedValue({ok:false,status:403,json:async()=>({code:'MONEY_OWNER_CLOSED',message:'public synthetic permission denied'})})
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  await turn(20_000)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  expect(tracks.length).toBe(1)
  expect(localApi.endObservationStream).toHaveBeenCalled()
 })
})
