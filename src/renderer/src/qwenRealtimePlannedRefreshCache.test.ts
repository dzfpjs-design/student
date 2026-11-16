import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'

type Health={reset():void;snapshot(at:number):{known:boolean;fresh:boolean;pending:boolean}}
type Session=Pick<QwenRealtimeVoiceSession,'close'|'readPlannedRefreshState'> & {
 receive(raw:string):void;audioHealthEpoch:number;audioHealthTimer:number|null;
 audioActivityHealth:Health;pendingToolCalls:unknown[];peer:unknown
}
beforeEach(()=>vi.useFakeTimers({toFake:['Date','performance','setTimeout','clearTimeout','setInterval','clearInterval']}))
afterEach(()=>{vi.restoreAllMocks();vi.useRealTimers()})
async function transport(){
 let energy=0
 const track={id:'public-owned-track',enabled:false,muted:false,readyState:'live',getSettings:()=>({}),stop:vi.fn()}
 const root={...track,enabled:true},error=vi.fn()
 const peer={connectionState:'connected',close:vi.fn(),getStats:vi.fn(async()=>new Map([['public-source',{
  id:'public-source',type:'media-source',kind:'audio',trackIdentifier:track.id,
  totalAudioEnergy:energy,totalSamplesDuration:performance.now()/1000,
 }]]))}
 const session=Object.assign(new QwenRealtimeVoiceSession({childName:'public test',callbacks:{
  onStatus:vi.fn(),onError:error,onCommittedTurn:vi.fn().mockResolvedValue(undefined),
  captureInputContext:()=>({sessionId:'public-session',reference:{child_id:'public-child'},carryoverConfirmationId:null}),
 }}),{peer,sender:{track,replaceTrack:vi.fn().mockResolvedValue(undefined)},audioTrack:track,localSourceTrack:root,
 stream:{getTracks:()=>[root,track]}}) as unknown as Session
 session.receive(JSON.stringify({type:'session.updated'}));await Promise.resolve()
 return {session,track,root,peer,error,setEnergy(x:number){energy=x}}
}
const advance=async(ms:number)=>{await vi.advanceTimersByTimeAsync(ms)}
const state=(t:Awaited<ReturnType<typeof transport>>)=>t.session.audioActivityHealth.snapshot(performance.now())

it('keeps an already current fresh quiet interval at1000.3ms without another getStats',async()=>{
 const t=await transport();await advance(1000);expect(state(t)).toEqual({known:true,fresh:true,pending:false})
 const calls=t.peer.getStats.mock.calls.length;vi.spyOn(performance,'now').mockReturnValue(1000.3)
 expect(await t.session.readPlannedRefreshState()).toEqual({current:true,known:true,fresh:true,pending:false})
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls);t.session.close()
})
it('does not promote a fresh unknown first measurement or force a neighboring stats request',async()=>{
 const t=await transport();await advance(500);expect(state(t)).toEqual({known:false,fresh:true,pending:false})
 const calls=t.peer.getStats.mock.calls.length
 expect(await t.session.readPlannedRefreshState()).toEqual({current:true,known:false,fresh:true,pending:false})
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls);t.session.close()
})
it('preserves actual fresh unconfirmed activity pending and outstanding native work',async()=>{
 const t=await transport();await advance(1000);t.setEnergy(.1);await advance(500)
 expect(state(t)).toEqual({known:true,fresh:true,pending:true});const calls=t.peer.getStats.mock.calls.length
 expect((await t.session.readPlannedRefreshState()).pending).toBe(true)
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls);t.session.audioActivityHealth.reset()
 await advance(1000);t.session.pendingToolCalls.push({utterance:'public pending'})
 expect((await t.session.readPlannedRefreshState()).pending).toBe(true);t.session.close()
})
it('refreshes an expired snapshot using the original bounded poll and existing epoch',async()=>{
 const t=await transport();await advance(1000);window.clearInterval(t.session.audioHealthTimer!)
 await advance(2001);expect(state(t).fresh).toBe(false);const calls=t.peer.getStats.mock.calls.length
 expect(await t.session.readPlannedRefreshState()).toEqual({current:true,known:true,fresh:true,pending:false})
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls+1);t.session.close()
})
it('an expired failed stats poll cannot inherit the old quiet proof',async()=>{
 const t=await transport();await advance(1000);window.clearInterval(t.session.audioHealthTimer!)
 await advance(2001);t.peer.getStats.mockRejectedValueOnce(Error('public stats failure'))
 const reading=await t.session.readPlannedRefreshState();expect(reading.current).toBe(true)
 expect(reading.known).toBe(false);expect(reading.fresh).toBe(false);expect(t.error).not.toHaveBeenCalled();t.session.close()
})
it('joins an existing original poll without starting a concurrent request',async()=>{
 const t=await transport();await advance(1000);let resolve!:(x:Awaited<ReturnType<typeof t.peer.getStats>>)=>void
 t.peer.getStats.mockImplementationOnce(()=>new Promise(yes=>{resolve=yes}));await advance(500)
 const calls=t.peer.getStats.mock.calls.length,reading=t.session.readPlannedRefreshState();await Promise.resolve()
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls)
 resolve(new Map([['public-source',{id:'public-source',type:'media-source',kind:'audio',trackIdentifier:t.track.id,
 totalAudioEnergy:0,totalSamplesDuration:performance.now()/1000}]]))
 expect(await reading).toEqual({current:true,known:true,fresh:true,pending:false})
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls);t.session.close()
})
it.each(['epoch','peer','close'] as const)('original in-flight read cannot revive after %s',async change=>{
 const t=await transport();await advance(1000);let resolve!:(x:Awaited<ReturnType<typeof t.peer.getStats>>)=>void
 t.peer.getStats.mockImplementationOnce(()=>new Promise(yes=>{resolve=yes}));await advance(500)
 const reading=t.session.readPlannedRefreshState();await Promise.resolve()
 if(change==='epoch'){t.session.audioHealthEpoch++;t.session.audioActivityHealth.reset()}
 if(change==='peer')t.session.peer={...t.peer}
 if(change==='close')t.session.close()
 resolve(new Map([['public-source',{id:'public-source',type:'media-source',kind:'audio',trackIdentifier:t.track.id,
 totalAudioEnergy:0,totalSamplesDuration:performance.now()/1000}]]))
 const result=await reading;expect(!result.current||!result.known||!result.fresh).toBe(true)
 t.session.close();expect(t.error).not.toHaveBeenCalled()
})
it('a never-settling expired read keeps its original2s cancellation and does not reenter',async()=>{
 const t=await transport();await advance(1000);window.clearInterval(t.session.audioHealthTimer!)
 await advance(2001);let resolve!:(x:Awaited<ReturnType<typeof t.peer.getStats>>)=>void
 t.peer.getStats.mockImplementationOnce(()=>new Promise(yes=>{resolve=yes}));const calls=t.peer.getStats.mock.calls.length
 const first=t.session.readPlannedRefreshState();await Promise.resolve();const second=t.session.readPlannedRefreshState()
 await advance(2000);for(const r of await Promise.all([first,second]))expect(!r.known&&!r.fresh).toBe(true)
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls+1)
 resolve(new Map([['public-source',{id:'public-source',type:'media-source',kind:'audio',trackIdentifier:t.track.id,
 totalAudioEnergy:0,totalSamplesDuration:performance.now()/1000}]]));await Promise.resolve()
 expect(state(t)).toEqual({known:false,fresh:false,pending:false});t.session.close()
})
it.each(['ended','disabled','muted'] as const)('a cached quiet proof cannot bypass current root %s',async change=>{
 const t=await transport();await advance(1000);const calls=t.peer.getStats.mock.calls.length
 if(change==='ended')t.root.readyState='ended';if(change==='disabled')t.root.enabled=false;if(change==='muted')t.root.muted=true
 expect((await t.session.readPlannedRefreshState()).current).toBe(false)
 expect(t.peer.getStats).toHaveBeenCalledTimes(calls);t.session.close()
})
