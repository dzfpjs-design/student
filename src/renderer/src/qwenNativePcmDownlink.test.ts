// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenNativePcmDownlink} from './qwenNativePcmDownlink'
const players:QwenNativePcmDownlink[]=[]
function fixture(){
  const sources:any[]=[],peers:any[]=[],buffers:Float32Array[]=[]
  const track=Object.assign(new EventTarget(),{kind:'audio',stop:vi.fn()})
  const stream={getAudioTracks:()=>[track],getTracks:()=>[track]}
  class Peer extends EventTarget{
    connectionState='connected';iceGatheringState='complete';localDescription:RTCSessionDescriptionInit|null=null
    constructor(readonly config:unknown){super();peers.push(this)}
    addTrack=vi.fn();createOffer=async()=>({type:'offer',sdp:'LOCAL_ONLY'})
    createAnswer=async()=>({type:'answer',sdp:'LOCAL_ONLY'})
    setLocalDescription=async(value:RTCSessionDescriptionInit)=>{this.localDescription=value}
    setRemoteDescription=async()=>{const event=new Event('track');Object.assign(event,{track,streams:[stream]});this.dispatchEvent(event)}
    close=vi.fn(()=>{this.connectionState='closed';this.dispatchEvent(new Event('connectionstatechange'))})
  }
  const destination={stream},physicalDestination={FORBIDDEN_PHYSICAL_SINK:true}
  class Context{
    state='running';currentTime=10;destination=physicalDestination;resume=async()=>undefined;close=vi.fn(async()=>undefined)
    createMediaStreamDestination=()=>destination
    createBuffer=(_channels:number,length:number,rate:number)=>({length,sampleRate:rate,copyToChannel:(samples:Float32Array)=>{buffers.push(samples.slice())}})
    createBufferSource=()=>{const source={buffer:null,onended:null as (()=>void)|null,connect:vi.fn(),disconnect:vi.fn(),start:vi.fn(),stop:vi.fn()};sources.push(source);return source}
  }
  vi.stubGlobal('AudioContext',Context);vi.stubGlobal('RTCPeerConnection',Peer)
  const failed=vi.fn();let current=true
  const player=new QwenNativePcmDownlink({current:()=>current,failed});players.push(player)
  const pcm=(n:number,value=16384)=>{const bytes=new Uint8Array(n*2),view=new DataView(bytes.buffer);for(let i=0;i<n;i++)view.setInt16(i*2,value,true);return btoa(String.fromCharCode(...bytes))}
  return {player,failed,sources,peers,buffers,destination,stream,physicalDestination,track,pcm,retire:()=>{current=false}}
}
afterEach(()=>{for(const p of players.splice(0))p.dispose();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('actual helper creates only a no-STUN local output pair and returns a receiver stream; scheduled PCM never connects to a physical WebAudio sink',async()=>{
  const f=fixture();expect(await f.player.start()).toBe(f.stream)
  expect(f.peers).toHaveLength(2);for(const p of f.peers)expect(p.config).toEqual({iceServers:[]})
  expect(f.peers[0].addTrack).toHaveBeenCalledExactlyOnceWith(f.track,f.stream);expect(f.peers[1].addTrack).not.toHaveBeenCalled()
  f.player.begin('response-one');expect(f.player.append('response-one',f.pcm(240))).toBe(true)
  expect(f.buffers[0]).toEqual(new Float32Array(240).fill(.5));expect(f.sources[0].connect).toHaveBeenCalledExactlyOnceWith(f.destination)
  expect(f.sources[0].connect).not.toHaveBeenCalledWith(f.physicalDestination);expect(f.sources[0].start).toHaveBeenCalledWith(10.02)
})
it('same-input continuation preserves generated preface, while new input/stop physically stops all pending sources and rejects late chunks',async()=>{
  const f=fixture();await f.player.start();f.player.begin('preface');f.player.append('preface',f.pcm(240))
  f.player.begin('continuation',true);expect(f.sources[0].stop).not.toHaveBeenCalled();expect(f.player.append('preface',f.pcm(240))).toBe(false)
  f.player.append('continuation',f.pcm(240));expect(f.sources[1].start.mock.calls[0][0]).toBeGreaterThan(f.sources[0].start.mock.calls[0][0])
  f.player.cancel();for(const s of f.sources){expect(s.stop).toHaveBeenCalledTimes(1);expect(s.disconnect).toHaveBeenCalledTimes(1)}
  expect(f.player.append('continuation',f.pcm(240))).toBe(false)
  f.player.begin('new-response');f.player.append('new-response',f.pcm(240));f.retire();expect(f.player.append('new-response',f.pcm(240))).toBe(false)
})
it('aggregate queued audio is bounded and overflow aborts every old source rather than leaving residual playback',async()=>{
  const f=fixture();await f.player.start();f.player.begin('long-response')
  const packet=f.pcm(60000)
  for(let i=0;i<36;i++)expect(f.player.append('long-response',packet)).toBe(true)
  expect(f.player.append('long-response',packet)).toBe(false);expect(f.failed).toHaveBeenCalledExactlyOnceWith('QWEN_NATIVE_AUDIO_QUEUE_INVALID')
  for(const s of f.sources)expect(s.stop).toHaveBeenCalledTimes(1)
  expect(f.player.append('long-response',packet)).toBe(false)
})
it('real output track end or peer failure is a fault and disposal closes both peers and cancels scheduled PCM',async()=>{
  const f=fixture();await f.player.start();f.player.begin('response');f.player.append('response',f.pcm(240))
  f.track.dispatchEvent(new Event('ended'));expect(f.failed).toHaveBeenCalledWith('QWEN_NATIVE_DOWNLINK_FAILED')
  f.peers[1].connectionState='failed';f.peers[1].dispatchEvent(new Event('connectionstatechange'));expect(f.failed).toHaveBeenCalledWith('QWEN_NATIVE_DOWNLINK_FAILED')
  f.failed.mockClear();f.player.dispose();expect(f.failed).not.toHaveBeenCalled();expect(f.sources[0].stop).toHaveBeenCalledTimes(1)
  for(const p of f.peers)expect(p.close).toHaveBeenCalledTimes(1)
})
