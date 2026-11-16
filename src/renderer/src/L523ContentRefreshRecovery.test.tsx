import {act, cleanup, render, screen} from '@testing-library/react'
import {createHash} from 'node:crypto'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {LocalObservation} from './SessionWorkspace'
import {ApiError, localApi, resetRuntimeCache} from './api'
import * as media from './mediaDevices'
import {reconcileObservationRegistration} from './observationCameraStartup'
import {reconcileObservationCoverage} from './observationCoverageStartup'

import {requestCameraDevice,queryCameraDevices} from './cameraDeviceRuntime'
vi.mock('./QwenRealtimeVoiceInput',()=>({QwenRealtimeVoiceInput:()=>null}))
const receipt=(epoch:string,boundary:string)=>({status:'recorded',event_id:`coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}`})
const success=(reason='synthetic fresh analysis')=>({state:'writing',observation_ready:true,scene_quality:{status:'ready',message:reason},person_baseline:{status:'established',message:'synthetic baseline'},reason,intervention:null})
const turn=async(ms=0)=>{await act(async()=>{await vi.advanceTimersByTimeAsync(ms)})}
let tracks:Array<{readyState:string;stop:ReturnType<typeof vi.fn>;addEventListener:ReturnType<typeof vi.fn>}>=[]
let frames=0,time=0,serial=0,contentShift=0
let sid='',binding=''
const deferred=<T,>()=>{let resolve!:(v:T)=>void,reject!:(e:unknown)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}}
const mount=()=>render(<LocalObservation sessionId={sid} active enabled snoozeUntil={null} onAuthorityRefresh={async current=>{if(!current())throw Error('retired authority')}}/>)
const request=(signal?:AbortSignal)=>requestCameraDevice(sid,'camera','resume',signal)
const originalMedia=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
const originalQuality=Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype,'getVideoPlaybackQuality')

describe('L523 repeated-content connection recheck',()=>{
 beforeEach(()=>{
  vi.useFakeTimers();tracks=[];frames=0;time=0;contentShift=0;resetRuntimeCache();sid='resume-current-session-'+(++serial);binding='resume-current-binding-'+serial
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
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage:vi.fn(),getImageData:()=>{const next=data.slice();next[0]+=contentShift;return {width:320,height:180,data:next}}} as never)
  vi.spyOn(HTMLCanvasElement.prototype,'toDataURL').mockReturnValue('data:image/jpeg;base64,c3ludGhldGlj')
  Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{enumerateDevices:vi.fn().mockResolvedValue([]),getUserMedia:vi.fn(async()=>{const track={readyState:'live',enabled:true,muted:false,stop:vi.fn(()=>{track.readyState='ended'}),addEventListener:vi.fn()};tracks.push(track);return {getTracks:()=>[track],getVideoTracks:()=>[track]}})}})
  vi.spyOn(media,'resolveBoundVideoDevice').mockResolvedValue({device:{deviceId:'synthetic-bound-camera',label:'L523 (1d6b:0102)'} as MediaDeviceInfo,method:'system_id_hash'})
  vi.spyOn(media,'mediaDeviceRegistration').mockResolvedValue({systemIdHash:binding,capabilities:{}})
  vi.spyOn(localApi,'deviceBindings').mockResolvedValue([{device_type:'camera',system_id_hash:binding,label:'L523 (1d6b:0102)'}] as never)
  vi.spyOn(localApi,'registerDevice').mockImplementation(async()=>({lease_id:'synthetic-lease-'+tracks.length,status:'active',expires_at:'later'}))
  vi.spyOn(localApi,'renewDeviceLease').mockImplementation(async id=>({lease_id:id,status:'active',expires_at:'later'}))
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



 it('bounds a repeated detailed L523 frame to one original release and fresh-source check',async()=>{
  const stop=deferred<ReturnType<typeof receipt>>()
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(()=>stop.promise)
  mount();await turn(100)
  expect(localApi.analyzeObservation).toHaveBeenCalled()
  const firstEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
  await turn(31_000)
  expect(tracks).toHaveLength(1);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1)
  const whileStopping=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(500);expect(localApi.analyzeObservation).toHaveBeenCalledTimes(whileStopping)
  await act(async()=>stop.resolve(receipt(firstEpoch,'stop')));await turn(2200);await turn(100)
  expect(tracks).toHaveLength(2)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  expect(localApi.endObservationStream).toHaveBeenCalledWith(sid,firstEpoch,expect.any(Number),'unavailable',expect.any(AbortSignal))
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/画面内容仍未变化/)).toBeTruthy()
  expect(queryCameraDevices(sid,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})
  const calls=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(60_000)
  expect(tracks).toHaveLength(2)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls)
  expect(tracks[1].stop).not.toHaveBeenCalled()
  contentShift=1;await turn(2200)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls+1)
  expect(tracks).toHaveLength(2)
  // Same authorization: changed content restored analysis, not a second budget.
  await turn(34_000);await turn(2200)
  expect(tracks).toHaveLength(2);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  expect(tracks[1].stop).not.toHaveBeenCalled()
  expect(queryCameraDevices(sid,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})
 })

 it('keeps exact-content sampling independent of six-second successful analysis backpressure',async()=>{
  // Original successful analysis remains inside its ten-second deadline.
  vi.mocked(localApi.analyzeObservation).mockImplementation(async()=>{
   await new Promise(resolve=>setTimeout(resolve,6000));return success() as never
  })
  mount();await turn(100);await turn(80_000)
  expect(frames).toBeGreaterThan(20);expect(time).toBeGreaterThan(20)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  // Commit the retirement render, then allow its original reconnect timer.
  await turn(2200);await turn(100)
  expect(tracks).toHaveLength(2)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  expect(tracks[1].stop).not.toHaveBeenCalled()
  expect(queryCameraDevices(sid,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})
  expect(screen.getByText(/画面内容仍未变化/)).toBeTruthy()
  const calls=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(14_000)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  expect(tracks).toHaveLength(2)
 })

 it('keeps local exact-content checks during analysis retry backoff without a second source retry',async()=>{
  vi.mocked(localApi.analyzeObservation).mockRejectedValue(new Error('synthetic local analysis unavailable'))
  mount();await turn(100);await turn(80_000)
  expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  await turn(2200);await turn(100)
  expect(tracks).toHaveLength(2)
  expect(queryCameraDevices(sid,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})
  expect(screen.getByText(/画面内容仍未变化/)).toBeTruthy()
  const calls=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(20_000)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls)
  expect(tracks).toHaveLength(2)
 })

 it('does not publish an earlier analysis success after repeated content exhausts its one source check',async()=>{
  mount();await turn(100);await turn(34_000);await turn(2200);await turn(100)
  expect(tracks).toHaveLength(2)
  contentShift=1;await turn(2200)
  await turn(24_000)
  vi.mocked(localApi.analyzeObservation).mockImplementation(async()=>{
   await new Promise(resolve=>setTimeout(resolve,6000));return success('late original success') as never
  })
  await turn(2000);await turn(7000)
  expect(tracks).toHaveLength(2);expect(tracks[1].stop).not.toHaveBeenCalled()
  expect(screen.getByText(/画面内容仍未变化/)).toBeTruthy()
  expect(screen.queryByText(/late original success/)).toBeNull()
  expect(queryCameraDevices(sid,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})
  const calls=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(10_000)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls)
  expect(tracks).toHaveLength(2)
 })

 it('keeps genuine content changes in the original L523 stream without reconnecting',async()=>{
  mount();await turn(100)
  for(let n=1;n<=25;n++){contentShift=n;await turn(2000)}
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled()
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(26)
 })
 it('does not apply the L523 repeat policy to another observation camera',async()=>{
  vi.mocked(media.resolveBoundVideoDevice).mockResolvedValue({device:{deviceId:'synthetic-bound-camera',label:'Other Camera'} as MediaDeviceInfo,method:'system_id_hash'})
  mount();await turn(100);await turn(100_000)
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).not.toHaveBeenCalled()
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(51)
 })
 it('permits normal explicit device retry after unchanged content without inventing movement',async()=>{
  mount();await turn(100);await turn(34_000);await turn(2200);await turn(100)
  const before=vi.mocked(localApi.analyzeObservation).mock.calls.length
  expect(screen.getByText(/画面内容仍未变化/)).toBeTruthy()
  const result=request();await turn(200);await expect(result).resolves.toMatchObject({device_status:'active'})
  await turn(2200)
  expect(localApi.analyzeObservation).toHaveBeenCalledTimes(before+1)
  expect(tracks).toHaveLength(2)
  expect(localApi.resumeObservation).toHaveBeenCalledTimes(1)
 })
 it.each(['privacy_pause','inactive','disabled','session','unmount'] as const)('cannot reopen retired original authority after %s during old end ACK',async boundary=>{
  const stop=deferred<ReturnType<typeof receipt>>()
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(()=>stop.promise)
  const view=mount();await turn(100);await turn(31_000)
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  const oldEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
  if(boundary==='privacy_pause')await act(async()=>window.dispatchEvent(new Event('hsc-privacy-pause')))
  else if(boundary==='unmount')view.unmount()
  else view.rerender(<LocalObservation sessionId={boundary==='session'?sid+'-other':sid} active={boundary!=='inactive'} enabled={boundary!=='disabled'} snoozeUntil={null}/>)
  await turn(100);await act(async()=>stop.resolve(receipt(oldEpoch,'stop')));await turn(35_000)
  expect(tracks).toHaveLength(1);expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1)
 })
 it('holds closed when the original end ACK is not confirmed',async()=>{
  vi.mocked(localApi.endObservationStream).mockResolvedValueOnce({status:'not_recorded'})
  mount();await turn(100);await turn(34_000);await turn(2200);await turn(100)
  expect(tracks).toHaveLength(1);expect(tracks[0].stop).toHaveBeenCalledTimes(1)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(1)
  expect(screen.getByText(/旧连接的关闭状态还未确认/)).toBeTruthy()
 })
 it('requires strict advancing frames in the replacement and never loops a failed source check',async()=>{
  Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',{configurable:true,value:()=>({totalVideoFrames:tracks.length<2?++frames:100,droppedVideoFrames:0})})
  mount();await turn(100);await turn(34_000);await turn(2200);await turn(6000);await turn(100)
  expect(tracks).toHaveLength(2);expect(tracks[1].stop).toHaveBeenCalledTimes(1)
  expect(screen.getByText(/新连接尚未确认到新帧/)).toBeTruthy()
  const calls=vi.mocked(localApi.analyzeObservation).mock.calls.length
  await turn(60_000)
  expect(tracks).toHaveLength(2);expect(localApi.analyzeObservation).toHaveBeenCalledTimes(calls)
 })
})
