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
let frames=0,time=0
const originalMedia=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
const originalQuality=Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype,'getVideoPlaybackQuality')

describe('observation analysis original-flight recovery',()=>{
 beforeEach(()=>{
  vi.useFakeTimers();tracks=[];frames=0;time=0;resetRuntimeCache()
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
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage:vi.fn(),getImageData:()=>({width:320,height:180,data})} as never)
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
  vi.spyOn(localApi,'recordRendererObservationQuality').mockResolvedValue({} as never)
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

 it.each(['headers','body'] as const)('bounds a permanently pending original HTTP %s and recovers through exact stream retirement',async(stage)=>{
  let firstSignal:AbortSignal|undefined
  const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>{
   if(!firstSignal){firstSignal=init?.signal??undefined
    if(stage==='headers')return await new Promise<Response>((_resolve,reject)=>{init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic original HTTP aborted','AbortError')),{once:true})})
    return {ok:true,json:()=>new Promise((_resolve,reject)=>{init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic original body aborted','AbortError')),{once:true})})} as Response
   }
   return {ok:true,json:async()=>success()} as Response
  })
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  expect(fetch).toHaveBeenCalledTimes(1)
  const first=tracks[0]
  await turn(11_000)
  expect(firstSignal?.aborted).toBe(true)
  expect(first.stop).toHaveBeenCalledTimes(1)
  expect(localApi.endObservationStream).toHaveBeenCalled()
  await turn(6_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(tracks.length).toBe(2)
  expect(screen.getByText(/synthetic fresh analysis/)).toBeTruthy()
 })
 it('a late old body cannot publish UI state or unlock the newer flight',async()=>{
  let oldBody!:(v:unknown)=>void, newBody!:(v:unknown)=>void
  const fetch=vi.fn().mockResolvedValueOnce({ok:true,json:()=>new Promise(resolve=>{oldBody=resolve})})
   .mockResolvedValueOnce({ok:true,json:()=>new Promise(resolve=>{newBody=resolve})})
   .mockResolvedValue({ok:true,json:async()=>success()})
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  for(let i=0;i<20&&fetch.mock.calls.length<2;i++)await turn(1_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(tracks.length).toBe(2)
  expect(tracks[1].readyState).toBe('live')
  await act(async()=>{oldBody(success('synthetic retired body'))})
  await turn(2_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(screen.queryByText(/synthetic retired body/)).toBeNull()
  await act(async()=>{newBody(success('synthetic new flight'))})
  await turn()
  expect(screen.getByText(/synthetic new flight/)).toBeTruthy()
 })

 it('session retirement aborts the original request and fences its late response',async()=>{
  let oldBody!:(v:unknown)=>void, oldSignal:AbortSignal|undefined
  const fetch=vi.fn().mockImplementationOnce(async(_url,init)=>{oldSignal=init?.signal;return {ok:true,json:()=>new Promise(resolve=>{oldBody=resolve})}})
   .mockResolvedValue({ok:true,json:async()=>success('synthetic current session')})
  vi.stubGlobal('fetch',fetch)
  const view=render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  view.rerender(<LocalObservation sessionId="analysis-session" active={false} enabled snoozeUntil={null}/>);await turn()
  expect(oldSignal?.aborted).toBe(true)
  await act(async()=>{oldBody(success('synthetic old child result'))})
  expect(screen.queryByText(/synthetic old child result/)).toBeNull()
  view.rerender(<LocalObservation sessionId="analysis-other-session" active enabled snoozeUntil={null}/>);await turn()
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(String(fetch.mock.calls[1][0])).toContain('/analysis-other-session/observation-frame')
  expect(screen.getByText(/synthetic current session/)).toBeTruthy()
 })

 it('a stale capture waits for the original stop ACK before starting a new epoch',async()=>{
  let stopAck!:(v:ReturnType<typeof receipt>)=>void
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(async(_s,epoch)=>await new Promise(resolve=>{stopAck=()=>resolve(receipt(epoch,'stop'))}))
  const fetch=vi.fn().mockResolvedValueOnce({ok:false,status:409,json:async()=>({code:'OBSERVATION_CAPTURE_STALE',message:'synthetic retired epoch'})})
   .mockResolvedValue({ok:true,json:async()=>success()})
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  await turn(1_000)
  expect(tracks.length).toBe(1)
  expect(fetch).toHaveBeenCalledTimes(1)
  await act(async()=>{stopAck(receipt('unused','stop'))})
  await turn(5_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  const first=JSON.parse(fetch.mock.calls[0][1].body),second=JSON.parse(fetch.mock.calls[1][1].body)
  expect(second.capture_sample.stream_epoch).not.toBe(first.capture_sample.stream_epoch)
  expect(second.capture_sample.sequence).toBe(1)
 })

 it('repeated pending analyses retain finite backoff across retired streams',async()=>{
  const starts:number[]=[]
  const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>{
   starts.push(Date.now())
   return await new Promise<Response>((_resolve,reject)=>{init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic original HTTP aborted','AbortError')),{once:true})})
  })
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  for(let i=0;i<72;i++)await turn(1_000)
  expect(starts.length).toBe(4)
  expect(starts[1]-starts[0]).toBeGreaterThanOrEqual(15_000)
  expect(starts[2]-starts[1]).toBeGreaterThanOrEqual(20_000)
  expect(starts[3]-starts[2]).toBeGreaterThanOrEqual(30_000)
  expect(localApi.endObservationStream).toHaveBeenCalledTimes(3)
  expect(tracks.slice(0,3).every(t=>t.stop.mock.calls.length===1)).toBe(true)
 })

 it.each([{status:403,code:'OBSERVATION_FORBIDDEN'},{status:409,code:'MONEY_UNKNOWN_PRICING'}])('does not automatically retry denied analysis $code',async({status,code})=>{
  const fetch=vi.fn().mockResolvedValue({ok:false,status,json:async()=>({code,message:'synthetic forbidden analysis'})})
  vi.stubGlobal('fetch',fetch)
  render(<LocalObservation sessionId="analysis-session" active enabled snoozeUntil={null}/>);await turn()
  await turn(60_000)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(tracks.length).toBe(1)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
 })

})
