// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
const sessions:any[]=[]
function fixture(aec:unknown){
 vi.useFakeTimers();vi.stubGlobal('window',{desktop:{},setInterval,clearInterval,setTimeout,clearTimeout})
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',fullDuplex:true,nativeRuntime:{} as any,nativeAudioAllowed:()=>true,callbacks:{onStatus:vi.fn(),onError:vi.fn()}}) as any
 session.localSourceTrack={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:aec})};session.audioTrack={enabled:false}
 session.playbackAuthorized=true;session.activeResponseId='response';session.responseActive=false;session.nativeAudioDeltaSeen=true
 session.nativeDownlink={cancel:vi.fn(),dispose:vi.fn()};session.remoteAudio={muted:false,play:vi.fn(async()=>undefined),pause:vi.fn(),remove:vi.fn()}
 sessions.push(session);return session
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.useRealTimers();vi.unstubAllGlobals()})
it('unknown AEC and a missing observer fails into recovery instead of reopening append over an active audio queue',async()=>{
 const s=fixture(false);s.scheduleNativeQuiet('response');await vi.advanceTimersByTimeAsync(1201)
 expect(s.failureReported).toBe(true);expect(s.playbackAuthorized).toBe(false);expect(s.nativeDownlink.cancel).toHaveBeenCalled()
})
it('first observer unavailability in conservative AEC fails into recovery and cannot masquerade as quiet',async()=>{
 const s=fixture(undefined);s.remotePlaybackObservation={arm:(_observed:()=>void,unavailable:()=>void)=>unavailable(),disarm:vi.fn(),dispose:vi.fn()}
 s.observeRemotePlayback();expect(s.failureReported).toBe(true);expect(s.playbackAuthorized).toBe(false)
})
it('healthy observed PCM can later reopen conservative input after quiet, while browser-selected AEC observer failure never hard-mutes long audio',async()=>{
 const s=fixture(false);s.remotePlaybackObservation={disarm:vi.fn(),dispose:vi.fn()};s.playbackStarted=true
 s.scheduleNativeQuiet('response');await vi.advanceTimersByTimeAsync(1201)
 expect(s.failureReported).toBe(false);expect(s.nativeRemoteMediaActive).toBe(false);expect(s.playbackAuthorized).toBe(true)
 const duplex=fixture(true);duplex.remotePlaybackObservation={arm:(_observed:()=>void,unavailable:()=>void)=>unavailable(),disarm:vi.fn(),dispose:vi.fn()}
 duplex.observeRemotePlayback();await vi.advanceTimersByTimeAsync(2000)
 expect(duplex.failureReported).toBe(false);expect(duplex.playbackAuthorized).toBe(true);expect(duplex.nativeDownlink.cancel).not.toHaveBeenCalled()
})
