import {act, cleanup, render, screen} from '@testing-library/react'
import {createHash} from 'node:crypto'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {LocalObservation} from './SessionWorkspace'
import {ApiError, localApi, resetRuntimeCache} from './api'
import * as media from './mediaDevices'
import {reconcileObservationRegistration} from './observationCameraStartup'
import {reconcileObservationCoverage} from './observationCoverageStartup'

import {requestCameraDevice} from './cameraDeviceRuntime'
vi.mock('./QwenRealtimeVoiceInput',()=>({QwenRealtimeVoiceInput:()=>null}))
const receipt=(epoch:string,boundary:string)=>({status:'recorded',event_id:`coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}`})
const success=(reason='synthetic fresh analysis')=>({state:'writing',observation_ready:true,scene_quality:{status:'ready',message:reason},person_baseline:{status:'established',message:'synthetic baseline'},reason,intervention:null})
const turn=async(ms=0)=>{await act(async()=>{await vi.advanceTimersByTimeAsync(ms)})}
let tracks:Array<{readyState:string;stop:ReturnType<typeof vi.fn>;addEventListener:ReturnType<typeof vi.fn>}>=[]
let frames=0,time=0,serial=0
let sid='',binding=''
const deferred=<T,>()=>{let resolve!:(v:T)=>void,reject!:(e:unknown)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}}
const mount=()=>render(<LocalObservation sessionId={sid} active enabled snoozeUntil={null} onAuthorityRefresh={async current=>{if(!current())throw Error('retired authority')}}/>)
const request=(signal?:AbortSignal)=>requestCameraDevice(sid,'camera','resume',signal)
const originalMedia=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
const originalQuality=Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype,'getVideoPlaybackQuality')

describe('fresh explicit camera resume coverage authority',()=>{
 beforeEach(()=>{
  vi.useFakeTimers();tracks=[];frames=0;time=0;resetRuntimeCache();sid='resume-current-session-'+(++serial);binding='resume-current-binding-'+serial
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
  vi.spyOn(media,'mediaDeviceRegistration').mockResolvedValue({systemIdHash:binding,capabilities:{}})
  vi.spyOn(localApi,'deviceBindings').mockResolvedValue([{device_type:'camera',system_id_hash:binding,label:'synthetic camera'}] as never)
  vi.spyOn(localApi,'registerDevice').mockImplementation(async()=>({lease_id:'synthetic-lease-'+tracks.length,status:'active',expires_at:'later'}))
  vi.spyOn(localApi,'releaseDeviceLease').mockImplementation(async id=>({lease_id:id,status:'released'}))
  vi.spyOn(localApi,'startObservationQualification').mockResolvedValue({} as never)
  vi.spyOn(localApi,'confirmDeviceFrame').mockResolvedValue({} as never)
  vi.spyOn(localApi,'beginObservationStream').mockImplementation(async(_s,epoch)=>receipt(epoch,'start'))
  vi.spyOn(localApi,'endObservationStream').mockImplementation(async(_s,epoch)=>receipt(epoch,'stop'))
  vi.spyOn(localApi,'recordDeviceIncident').mockResolvedValue({} as never)
  vi.spyOn(localApi,'pendingSessionBehaviorIntervention').mockResolvedValue({status:'none'} as never)
  vi.spyOn(localApi,'resumeObservation').mockResolvedValue({observation_status:'enabled'} as never)
  vi.spyOn(localApi,'analyzeObservation').mockResolvedValue(success() as never)
  vi.spyOn(localApi,'recordRendererObservationQuality').mockResolvedValue({} as never)
  vi.stubGlobal('desktop',{getRuntimeConfig:async()=>({status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'synthetic-analysis-test',dataScope:'trial'})})
 })
 afterEach(async()=>{
  cleanup();await turn(5100)
  await act(async()=>{await reconcileObservationRegistration(binding);await reconcileObservationCoverage(sid)})
  vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers()
  if(originalMedia)Object.defineProperty(navigator,'mediaDevices',originalMedia)
  if(originalQuality)Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',originalQuality)
  else delete(HTMLVideoElement.prototype as unknown as {getVideoPlaybackQuality?:unknown}).getVideoPlaybackQuality
 })


 it('rebinds a fresh acknowledged epoch while preserving the original healthy stream',async()=>{
  let revision=2
  const openings=new Map<string,number>()
  vi.mocked(localApi.beginObservationStream).mockImplementation(async(_s,epoch)=>{openings.set(epoch,revision);return receipt(epoch,'start')})
  vi.mocked(localApi.resumeObservation).mockImplementation(async()=>{revision+=2;return {observation_status:'enabled'} as never})
  vi.mocked(localApi.analyzeObservation).mockImplementation(async(_s,_image,sample)=>{if(openings.get(sample!.stream_epoch)!==revision)throw new ApiError('synthetic old coverage',409,'OBSERVATION_CAPTURE_STALE');return success() as never})
  mount();await turn(100)
  const video=screen.getByLabelText('观察摄像头实时画面') as HTMLVideoElement,stream=video.srcObject
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1)
  const result=request();await turn(200);await expect(result).resolves.toMatchObject({device_status:'active'})
  await turn(2200)
  expect(tracks[0].stop).not.toHaveBeenCalled()
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  const oldEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1],newEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[1][1]
  expect(newEpoch).not.toBe(oldEpoch)
  expect(localApi.endObservationStream).toHaveBeenCalledWith(sid,oldEpoch,1,'context_changed',expect.any(AbortSignal))
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(2)
  expect(vi.mocked(localApi.analyzeObservation).mock.calls[1][2]).toMatchObject({stream_epoch:newEpoch,sequence:1})
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled();expect(video.srcObject).toBe(stream)
 })
 it('does not publish a new sampler before original stop and original new begin ACKs',async()=>{
  const stop=deferred<ReturnType<typeof receipt>>(),begin=deferred<ReturnType<typeof receipt>>(),old=deferred<never>()
  let oldSignal:AbortSignal|undefined
  vi.mocked(localApi.analyzeObservation).mockImplementationOnce((_s,_image,_sample,signal)=>{oldSignal=signal;return old.promise})
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(()=>stop.promise)
  vi.mocked(localApi.beginObservationStream).mockImplementationOnce(async(_s,epoch)=>receipt(epoch,'start')).mockImplementationOnce(()=>begin.promise)
  mount();await turn(100)
  const oldEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
  let ack=false;const result=request().then(value=>{ack=true;return value});await turn(100)
  expect(oldSignal?.aborted).toBe(true)
  await turn(2000)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1);expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1);expect(ack).toBe(false)
  await act(async()=>{stop.resolve(receipt(oldEpoch,'stop'))});await turn(100)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  const newEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[1][1]
  await turn(1500);expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1);expect(ack).toBe(false)
  await act(async()=>{begin.resolve(receipt(newEpoch,'start'))});await turn(100);await result
  await turn(2200)
  const freshCalls=vi.mocked(localApi.analyzeObservation).mock.calls.slice(1)
  expect(freshCalls.length).toBeGreaterThanOrEqual(1)
  expect(freshCalls.every(call=>call[2]?.stream_epoch===newEpoch)).toBe(true)
  expect(freshCalls.map(call=>call[2]?.sequence)).toEqual(freshCalls.map((_call,i)=>i+1))
  await act(async()=>{old.reject(new ApiError('synthetic old flight',409,'OBSERVATION_CAPTURE_STALE'))});await turn(100)
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled();expect(ack).toBe(true)
 })
 it.each(['cancel','inactive','unmount'] as const)('cannot start a replacement epoch after %s while the old stop is pending',async boundary=>{
  const stop=deferred<ReturnType<typeof receipt>>()
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(()=>stop.promise)
  const view=mount();await turn(100)
  const oldEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1],controller=new AbortController()
  let ack=false;const result=request(controller.signal).then(()=>{ack=true},error=>error);await turn(100)
  if(boundary==='cancel')await act(async()=>controller.abort())
  else if(boundary==='inactive')view.rerender(<LocalObservation sessionId={sid} active={false} enabled snoozeUntil={null}/>)
  else view.unmount()
  await turn(100);await act(async()=>stop.resolve(receipt(oldEpoch,'stop')));await turn(100)
  const failure=await result
  if(boundary==='cancel')expect(failure).toHaveProperty('name','AbortError');else expect(failure).toBeInstanceOf(Error)
  expect(ack).toBe(false)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1);expect(tracks).toHaveLength(1);expect(tracks[0].stop).toHaveBeenCalledTimes(1)
 })
 it('a failed old stop cannot grant a new sampler or successful resume',async()=>{
  vi.mocked(localApi.endObservationStream).mockResolvedValueOnce({status:'not_recorded'})
  mount();await turn(100);let ack=false;const result=request().then(()=>{ack=true},error=>error);await turn(100)
  expect(await result).toBeInstanceOf(Error);expect(ack).toBe(false);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1);expect(tracks[0].stop).toHaveBeenCalledTimes(1)
 })
 it('a mismatched new begin receipt never grants analysis or resume credit',async()=>{
  vi.mocked(localApi.beginObservationStream).mockImplementationOnce(async(_s,epoch)=>receipt(epoch,'start')).mockResolvedValueOnce({status:'recorded',event_id:'synthetic-wrong-ACK'})
  mount();await turn(100);let ack=false;const result=request().then(()=>{ack=true},error=>error);await turn(100)
  expect(await result).toBeInstanceOf(Error);expect(ack).toBe(false);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2);expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1)
 })
 it('retires a pending new begin after cancellation and consumes its original late ACK before closing',async()=>{
  const begin=deferred<ReturnType<typeof receipt>>()
  vi.mocked(localApi.beginObservationStream).mockImplementationOnce(async(_s,epoch)=>receipt(epoch,'start')).mockImplementationOnce(()=>begin.promise)
  mount();await turn(100);const controller=new AbortController();let ack=false
  const result=request(controller.signal).then(()=>{ack=true},error=>error);await turn(100)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  const lateEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[1][1]
  await act(async()=>controller.abort());await turn(100)
  expect(await result).toHaveProperty('name','AbortError');expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  await act(async()=>begin.resolve(receipt(lateEpoch,'start')));await turn(100)
  expect(localApi.endObservationStream).toHaveBeenCalledWith(sid,lateEpoch,0,'unavailable',expect.any(AbortSignal))
  expect(ack).toBe(false);expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1);expect(tracks).toHaveLength(1)
 })
 it('joins the original opening before applying fresh permission then rotates its first accepted coverage',async()=>{
  const registration=deferred<Awaited<ReturnType<typeof localApi.registerDevice>>>()
  vi.mocked(localApi.registerDevice).mockReturnValueOnce(registration.promise)
  mount();await turn(100);expect(localApi.beginObservationStream).not.toHaveBeenCalled()
  const result=request();await turn(100);expect(localApi.resumeObservation).not.toHaveBeenCalled()
  await act(async()=>registration.resolve({lease_id:'original-opening-lease',status:'active',expires_at:'later'}));await turn(200)
  await expect(result).resolves.toMatchObject({device_status:'active'})
  expect(localApi.resumeObservation).toHaveBeenCalledTimes(1);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled()
 })

 it('keeps the accepted replacement epoch silent until an actual post-command frame advances',async()=>{
  let advancing=true,quality=20,mediaTime=1
  Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',{configurable:true,value:()=>({totalVideoFrames:advancing?++quality:quality,droppedVideoFrames:0})})
  vi.mocked(Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'currentTime')!.get!).mockImplementation(()=>advancing?++mediaTime/10:mediaTime/10)
  const refresh=vi.fn(async(current:()=>boolean)=>{if(!current())throw Error('retired authority')})
  render(<LocalObservation sessionId={sid} active enabled snoozeUntil={null} onAuthorityRefresh={refresh}/>);await turn(100)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1)
  const video=screen.getByLabelText('观察摄像头实时画面') as HTMLVideoElement,originalStream=video.srcObject
  advancing=false
  let resolved=false;const result=request().then(value=>{resolved=true;return value},error=>error)
  await turn(100)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  const newEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[1][1]
  // Both old stop and new begin have actually ACKed. The unchanged cached
  // quality/media time must still not acquire a sequence or reach analysis.
  await turn(2100)
  expect(resolved).toBe(false);expect(refresh).not.toHaveBeenCalled()
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(1)
  expect(video.srcObject).toBe(originalStream);expect(tracks[0].stop).not.toHaveBeenCalled()
  advancing=true
  await turn(100);await expect(result).resolves.toMatchObject({frame_confirmed:true,device_status:'active'})
  expect(refresh).toHaveBeenCalledTimes(1)
  await turn(2100)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(2)
  expect(vi.mocked(localApi.analyzeObservation).mock.calls[1][2]).toMatchObject({stream_epoch:newEpoch,sequence:1})
  expect(video.srcObject).toBe(originalStream);expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled()
 })

})
