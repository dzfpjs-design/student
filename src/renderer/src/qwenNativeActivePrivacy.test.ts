// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import type {QwenNativeBoundRuntime} from './qwenNativeProtocol'
const sessions:any[]=[]
function fixture(){
 const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare goal',required_arguments:[],arguments_schema:{type:'object',properties:{}}}]}
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(),loadContext:vi.fn(),admit:vi.fn(async()=>({voice_input_id:'voice'})),propose:vi.fn(),readReceipt:vi.fn(),recordDialogue:vi.fn()}
 vi.stubGlobal('window',{desktop:{closeQwenNativeSocket:vi.fn(async()=>undefined)},setInterval,clearInterval,setTimeout,clearTimeout})
 const stop=vi.fn(),sent:any[]=[]
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',fullDuplex:true,nativeRuntime:runtime,nativeAudioAllowed:()=>true,
 nativeActivation:{isActive:()=>true,word:()=> '小知',onWake:vi.fn(),onPrivacyStop:stop,recognizeStandby:vi.fn()},callbacks:{onStatus:vi.fn(),captureInputContext:()=>({sessionId:'session',reference:{child_id:'child'},carryoverConfirmationId:null})}}) as any
 session.localSourceTrack={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true}),stop:vi.fn()};session.audioTrack={enabled:false,stop:vi.fn()}
 session.nativeSocketOpen=true;session.nativeSocketReady=true;session.nativeHostConnectionId='host';session.nativeCatalog=catalog;session.nativeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
 session.sessionReady=true;session.microphoneReleased=true;session.send=(event:unknown)=>sent.push(event);session.prepareNativeSession('supplier')
 for(let i=0;i<6;i++)session.manualAudio.processFrame(new Float32Array(1024).fill(.04),16000)
 for(let i=0;i<13;i++)session.manualAudio.processFrame(new Float32Array(1024),16000)
 session.receive(JSON.stringify({type:'input_audio_buffer.committed',item_id:'current-input'}));sessions.push(session)
 const complete=(text:string,item='current-input')=>session.receive(JSON.stringify({type:'conversation.item.input_audio_transcription.completed',item_id:item,transcript:text}))
 return {session,runtime,stop,sent,complete}
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('active current registered-name camera/privacy command preempts locally before canonical admission or model response',()=>{
 const f=fixture();f.complete('小智，请关闭摄像头一下')
 expect(f.stop).toHaveBeenCalledTimes(1);expect(f.session.closed).toBe(true);expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
 expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
})
it('active negated stops and explanatory privacy questions remain native ordinary input, not local privacy effects',async()=>{
 for(const text of ['别停止监听','为什么总说隐私模式']){const f=fixture();f.complete(text);await vi.waitFor(()=>expect(f.runtime.admit).toHaveBeenCalledTimes(1))
 expect(f.stop).not.toHaveBeenCalled();expect(f.session.closed).toBe(false);expect(vi.mocked(f.runtime.admit).mock.calls[0][0]).toBe(text)}
})
it('an unrelated provider item cannot preempt the current source; only its actual committed item can stop capture',()=>{
 const f=fixture();f.complete('隐私模式','unowned-item');expect(f.stop).not.toHaveBeenCalled();expect(f.session.closed).toBe(false);expect(f.runtime.admit).not.toHaveBeenCalled()
 f.complete('隐私模式');expect(f.stop).toHaveBeenCalledTimes(1);expect(f.session.closed).toBe(true);expect(f.runtime.admit).not.toHaveBeenCalled()
})
