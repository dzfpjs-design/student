// @vitest-environment jsdom
import {act,cleanup,render} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import {setVoiceQuestionCapture,clearVoiceCaptureContext} from './voiceCaptureContext'
import {getVoiceCaptureOwner,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
const mock=vi.hoisted(()=>({instances:[] as any[],provideTap:true,register:vi.fn(),release:vi.fn(),renew:vi.fn(),closeHost:vi.fn()}))
// Only connection I/O is replaced. The inherited recovery method and the
// component's ownsConnection/control.isCurrent/coordinator are real.
vi.mock('./qwenRealtimeVoice',async original=>{
 const actual=await original<typeof import('./qwenRealtimeVoice')>()
 return {...actual,QwenRealtimeVoiceSession:class extends actual.QwenRealtimeVoiceSession{
  currentOptions:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]
  root={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true}),stop:vi.fn()}
  tap={dispose:vi.fn()}
  constructor(options:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]){super(options);this.currentOptions=options;mock.instances.push(this)}
  async connect(){
   const state=this as any
   state.localSourceTrack=this.root;state.stream={getTracks:()=>[this.root]};state.audioTrack={...this.root,enabled:false,stop:vi.fn()}
   state.nativeHostConnectionId='host-'+mock.instances.length;state.nativeSocketOpen=true;state.microphoneReleased=true;state.sessionReady=true
   state.protectedControl=mock.provideTap?this.tap:null;state.standbyWake={dispose:vi.fn(),resumeAfterTransportRetired:vi.fn(()=>true)}
   await this.currentOptions.callbacks.onMicrophoneOpen?.(this.root as unknown as MediaStreamTrack)
   this.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'listening',speechActive:false,pendingUtterances:0,recoverable:true})
  }
 }}
})
vi.mock('./mediaDevices',()=>({mediaDeviceRegistration:vi.fn(async()=>({systemIdHash:'synthetic',capabilities:{}}))}))
vi.mock('./api',()=>({localApi:{registerDevice:mock.register,releaseDeviceLease:mock.release,renewDeviceLease:mock.renew}}))
vi.mock('./companionVoice',async original=>({...await original<typeof import('./companionVoice')>(),getCompanionVoiceState:()=> 'idle',getCompanionPlaybackLocalOnly:()=>true,subscribeCompanionVoice:()=>()=>{},stopCompanionSpeech:vi.fn(),speakCompanion:vi.fn()}))
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(100000);mock.instances=[];mock.provideTap=true;mock.register.mockReset().mockImplementation(async()=>({lease_id:'lease-'+mock.instances.length}));mock.release.mockReset().mockResolvedValue({});mock.renew.mockReset().mockResolvedValue({});mock.closeHost.mockReset().mockResolvedValue(undefined)
 Object.defineProperty(window,'desktop',{configurable:true,value:{closeQwenNativeSocket:mock.closeHost,recordRendererDiagnostic:vi.fn(async()=>undefined)}});resetVoiceCaptureCoordinatorForTests()})
afterEach(()=>{cleanup();clearVoiceCaptureContext();resetVoiceCaptureCoordinatorForTests();vi.useRealTimers();vi.restoreAllMocks()})
const advance=async(ms:number)=>act(async()=>{await vi.advanceTimersByTimeAsync(ms)})
const props=()=>({active:true,audioAllowed:true,sessionId:'session',childId:'child',childName:'Synthetic',wakeWord:'小知',initialConversationActive:true,nativeRuntime:{} as any,onTurn:vi.fn(),onStatus:vi.fn(),onWake:vi.fn()})
async function exhaust(){
 const p=props(),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 for(const [index,delay] of [1000,3000,8000].entries()){
  await act(async()=>mock.instances[index].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}));await advance(delay+1)
 }
 const last=mock.instances[3];expect(last.currentOptions.protectedPlaybackControl.isCurrent()).toBe(true)
 await act(async()=>last.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}))
 return {p,view,last}
}

it('the real current onset callback gives book browsing only an independent child source; returning views cannot mutate that captured source',async()=>{
 setVoiceQuestionCapture('session','child',{kind:'diagnostic',binding:{prompt_id:'hidden-original',item_version:'version-original'}})
 const p=props(),view=render(<QwenRealtimeVoiceInput {...p} visibleLearningSource={false}/>);await advance(0)
 const session=mock.instances[0]
 const captured=session.currentOptions.callbacks.captureInputContext()
 expect(captured).toEqual({sessionId:'session',reference:{child_id:'child'},carryoverConfirmationId:null})
 view.rerender(<QwenRealtimeVoiceInput {...p} visibleLearningSource={true}/>);await advance(0)
 const next=session.currentOptions.callbacks.captureInputContext()
 expect(next.reference.question_reference).toEqual({kind:'diagnostic',binding:{prompt_id:'hidden-original',item_version:'version-original'}})
 expect(captured.reference).toEqual({child_id:'child'})
 expect(mock.instances).toHaveLength(1)
})
