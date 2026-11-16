import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CameraFrameClock, waitForCameraFrame } from './cameraDeviceRuntime'
function fixture() {
  let track = { readyState:'live', enabled:true, muted:false }
  let stream = { getVideoTracks:()=>[track] } as unknown as MediaStream, generation=4, next=0
  const callbacks = new Map<number,VideoFrameRequestCallback>()
  const video = { srcObject:stream, paused:false, ended:false, readyState:2, videoWidth:640, videoHeight:360, currentTime:1, frames:12,
    getVideoPlaybackQuality(){return {totalVideoFrames:this.frames}},
    requestVideoFrameCallback:vi.fn((fn:VideoFrameRequestCallback)=>{callbacks.set(++next,fn);return next}),
    cancelVideoFrameCallback:vi.fn((id:number)=>{callbacks.delete(id)}) }
  return {video,callbacks,get track(){return track},read:()=>({video:video as unknown as HTMLVideoElement,stream,leased:true,generation}),
    deliver(frames:number,time:number){const [id,fn]=[...callbacks][0]!;callbacks.delete(id);fn(performance.now(),{presentedFrames:frames,mediaTime:time}as VideoFrameCallbackMetadata)},
    replace(kind:string){if(kind==='generation')generation++;if(kind==='track')track={readyState:'live',enabled:true,muted:false};if(kind==='stream'){stream={getVideoTracks:()=>[track]}as unknown as MediaStream;video.srcObject=stream}} }
}
beforeEach(()=>vi.useFakeTimers());afterEach(()=>vi.useRealTimers())
describe('same-source freshness without interpreting scene movement',()=>{
 it.each(['missing','throws'])('does not treat a moving media clock as fresh source frames when the counter %s',mode=>{
  const f=fixture(),clock=new CameraFrameClock();f.video.getVideoPlaybackQuality=mode==='missing'?undefined as never:(()=>{throw new Error('unavailable')}) as never
  for(let i=0;i<6;i++){f.video.currentTime++;vi.advanceTimersByTime(1000);expect(clock.read('camera',f.read().video,f.read().stream,true,null).frame_confirmed).toBe(false)}
  expect(clock.frameAgeMs()).toBe(5000)
 })
 it.each(['camera','document_camera']as const)('rejects muted or disabled %s despite advancing counters',device=>{
  for(const flag of ['muted','enabled']as const){const f=fixture(),clock=new CameraFrameClock();expect(clock.read(device,f.read().video,f.read().stream,true,null).frame_confirmed).toBe(true);f.track[flag]=flag==='muted';for(let i=0;i<6;i++){f.video.frames++;f.video.currentTime++;vi.advanceTimersByTime(1000);expect(clock.read(device,f.read().video,f.read().stream,true,null)).toMatchObject({track_live:true,frame_confirmed:false,monitor_active:false})}expect(clock.frameAgeMs()).toBe(6000)}
 })
 it('short mute recovers freshness in the same stream before the five second retirement boundary',()=>{const f=fixture(),c=new CameraFrameClock();c.read('camera',f.read().video,f.read().stream,true,null);f.track.muted=true;vi.advanceTimersByTime(2000);f.video.frames++;expect(c.read('camera',f.read().video,f.read().stream,true,null).frame_confirmed).toBe(false);expect(c.frameAgeMs()).toBeLessThan(5000);f.track.muted=false;f.video.frames++;expect(c.read('camera',f.read().video,f.read().stream,true,null).frame_confirmed).toBe(true)})
 it('cached frame cannot acknowledge resume inside the original five seconds',async()=>{const f=fixture();let settled=false;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>{settled=true;return null},e=>{settled=true;return e as Error});await vi.advanceTimersByTimeAsync(4999);expect(settled).toBe(false);await vi.advanceTimersByTimeAsync(1);expect(await p).toMatchObject({message:'DEVICE_NO_CONFIRMED_FRAME'})})
 it('new source frames acknowledge a completely static detailed scene',async()=>{const f=fixture();let settled=false;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>{settled=true});await vi.advanceTimersByTimeAsync(100);expect(settled).toBe(false);f.video.frames++;await vi.advanceTimersByTimeAsync(50);await p;expect(settled).toBe(true)})
 it.each(['generation','track','stream'])('rejects late frame after %s replacement',async kind=>{const f=fixture();f.video.getVideoPlaybackQuality=undefined as never;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>null,e=>e as Error);const old=[...f.callbacks.values()][0]!;f.replace(kind);old(performance.now(),{presentedFrames:14,mediaTime:2}as VideoFrameCallbackMetadata);await vi.advanceTimersByTimeAsync(50);expect(await p).toMatchObject({name:'AbortError'})})
 it('without quality first callback is baseline and only strictly advancing second facts acknowledge',async()=>{const f=fixture();f.video.getVideoPlaybackQuality=undefined as never;let settled=false;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>{settled=true});await vi.advanceTimersByTimeAsync(50);expect(settled).toBe(false);f.deliver(12,1);await vi.advanceTimersByTimeAsync(50);expect(settled).toBe(false);f.deliver(12,1);await vi.advanceTimersByTimeAsync(50);expect(settled).toBe(false);f.deliver(13,1.1);await p;expect(settled).toBe(true)})
 it('media time without either frame source never acknowledges resume',async()=>{const f=fixture();f.video.getVideoPlaybackQuality=undefined as never;f.video.requestVideoFrameCallback=undefined as never;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>null,e=>e as Error);for(let i=0;i<5;i++){f.video.currentTime++;await vi.advanceTimersByTimeAsync(1000)}expect(await p).toMatchObject({message:'DEVICE_NO_CONFIRMED_FRAME'})})
 it('abort retires original callback and late callback cannot grant result',async()=>{const f=fixture();f.video.getVideoPlaybackQuality=undefined as never;const c=new AbortController(),p=waitForCameraFrame(f.read,c.signal).then(()=>null,e=>e as Error);const old=[...f.callbacks.values()][0]!;c.abort();old(performance.now(),{presentedFrames:14,mediaTime:2}as VideoFrameCallbackMetadata);expect(await p).toMatchObject({name:'AbortError'});expect(f.callbacks.size).toBe(0)})
 it('starts the original five second watchdog even when the first live stream is muted',()=>{const f=fixture(),c=new CameraFrameClock();f.track.muted=true;expect(c.read('camera',f.read().video,f.read().stream,true,null)).toMatchObject({frame_confirmed:false,last_frame_at:null});vi.advanceTimersByTime(5000);c.read('camera',f.read().video,f.read().stream,true,null);expect(c.frameAgeMs()).toBe(5000)})
 it('callback counter regression cannot replace the command baseline',async()=>{const f=fixture();f.video.getVideoPlaybackQuality=undefined as never;let settled=false;const p=waitForCameraFrame(f.read,new AbortController().signal).then(()=>{settled=true});f.deliver(12,1);f.deliver(2,0.2);f.deliver(3,0.3);await Promise.resolve();expect(settled).toBe(false);f.deliver(13,1.1);await p;expect(settled).toBe(true)})

 it('cancelled callback cannot consume the rearmed callback or supply its fresh baseline',async()=>{
  const f=fixture();f.video.getVideoPlaybackQuality=undefined as never
  const c=new AbortController();let settled=false
  const p=waitForCameraFrame(f.read,c.signal).then(()=>{settled=true;return null},e=>e as Error)
  const old=[...f.callbacks.values()][0]!
  f.track.muted=true;await vi.advanceTimersByTimeAsync(50)
  f.track.muted=false;await vi.advanceTimersByTimeAsync(50)
  const currentId=[...f.callbacks.keys()][0]!
  try{
   old(performance.now(),{presentedFrames:12,mediaTime:1}as VideoFrameCallbackMetadata)
   expect([...f.callbacks.keys()]).toEqual([currentId])
   f.deliver(13,1.1);await Promise.resolve();expect(settled).toBe(false)
   old(performance.now(),{presentedFrames:14,mediaTime:1.2}as VideoFrameCallbackMetadata)
   await Promise.resolve();expect(settled).toBe(false)
   f.deliver(14,1.2);expect(await p).toBeNull();expect(settled).toBe(true)
  }finally{c.abort();await p}
 })
 it('abort cancels the current callback handle after an older cancelled callback arrives',async()=>{
  const f=fixture();f.video.getVideoPlaybackQuality=undefined as never
  const c=new AbortController(),p=waitForCameraFrame(f.read,c.signal).then(()=>null,e=>e as Error)
  const old=[...f.callbacks.values()][0]!
  f.track.muted=true;await vi.advanceTimersByTimeAsync(50)
  f.track.muted=false;await vi.advanceTimersByTimeAsync(50)
  const currentId=[...f.callbacks.keys()][0]!
  old(performance.now(),{presentedFrames:12,mediaTime:1}as VideoFrameCallbackMetadata)
  c.abort();expect(await p).toMatchObject({name:'AbortError'})
  expect(f.video.cancelVideoFrameCallback).toHaveBeenCalledWith(currentId)
  expect(f.callbacks.size).toBe(0)
 })

})
