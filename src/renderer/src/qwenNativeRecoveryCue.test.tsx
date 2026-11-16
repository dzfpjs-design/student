// @vitest-environment jsdom
import {act,cleanup,fireEvent,render} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {PLAYBACK_TAIL_RECOVERY_GUIDANCE,QwenRealtimeVoiceInput,resetPermanentQwenRecoveryForTests} from './QwenRealtimeVoiceInput'
import {getVoiceCaptureOwner,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import {privacyStopKey} from './deviceCommandReceipt'
import {PLAYBACK_TAIL_FAILURE,protectedPlaybackBarrier} from './protectedPlaybackBarrier'
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
beforeEach(()=>{localStorage.clear();resetPermanentQwenRecoveryForTests();vi.useFakeTimers();vi.setSystemTime(100000);mock.instances=[];mock.provideTap=true;mock.register.mockReset().mockImplementation(async()=>({lease_id:'lease-'+mock.instances.length}));mock.release.mockReset().mockResolvedValue({});mock.renew.mockReset().mockResolvedValue({});mock.closeHost.mockReset().mockResolvedValue(undefined)
 Object.defineProperty(window,'desktop',{configurable:true,value:{closeQwenNativeSocket:mock.closeHost,recordRendererDiagnostic:vi.fn(async()=>undefined)}});resetVoiceCaptureCoordinatorForTests()})
afterEach(()=>{cleanup();resetVoiceCaptureCoordinatorForTests();vi.useRealTimers();vi.restoreAllMocks()})
const advance=async(ms:number)=>act(async()=>{await vi.advanceTimersByTimeAsync(ms)})
const props=()=>({active:true,audioAllowed:true,sessionId:'session',childId:'child',childName:'Synthetic',wakeWord:'小知',initialConversationActive:true,nativeRuntime:{} as any,onTurn:vi.fn(),onStatus:vi.fn(),onWake:vi.fn()})

it('the native failed-transcription branch requests one owned re-speaking cue after recovery',async()=>{
 const cue=vi.fn();const p={...props(),onRepeatRequired:cue};render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 const retired=mock.instances[0]
 await act(async()=>retired.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'这句话的云端转写未完成，没有执行请求；恢复后请重说。'}))
 expect(cue).not.toHaveBeenCalled();await advance(1001)
 expect(mock.instances).toHaveLength(2);expect(cue).toHaveBeenCalledExactlyOnceWith({sessionId:'session',childId:'child'})
 expect(retired.currentOptions.protectedPlaybackControl.isCurrent()).toBe(false)
})
it('does not turn an unrelated unknown failure into a false lost-input cue',async()=>{
 const cue=vi.fn();render(<QwenRealtimeVoiceInput {...props()}onRepeatRequired={cue}/>);await advance(0)
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'PRIVATE_UNRECOGNIZED_FAILURE'}))
 await advance(1001);expect(mock.instances).toHaveLength(2);expect(cue).not.toHaveBeenCalled()
})
it('a revoked privacy owner cannot emit the failed-transcription guidance or reconnect',async()=>{
 const cue=vi.fn();render(<QwenRealtimeVoiceInput {...props()}onRepeatRequired={cue}/>);await advance(0)
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'这句话的云端转写未完成，没有执行请求；恢复后请重说。'}))
 await act(async()=>window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
 await advance(1001);expect(mock.instances).toHaveLength(1);expect(cue).not.toHaveBeenCalled()
})

it('requires explicit recovery after playback-tail attempts run out, then still requires observed silence', async () => {
 const p = props(), scope = { sessionId: p.sessionId, childId: p.childId }
 const view = render(<QwenRealtimeVoiceInput {...p}/>); await advance(0)
 const now = performance.now()
 const token = protectedPlaybackBarrier.begin(scope, 'synthetic-lease', 1, now - 20_000)!
 try {
  expect(protectedPlaybackBarrier.reserveOpen(scope, now - 11_000)).toBe(true)
  expect(protectedPlaybackBarrier.reserveOpen(scope, now - 5_500)).toBe(true)
  await act(async () => mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:false,detail:PLAYBACK_TAIL_FAILURE}))
  expect(p.onStatus).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'error', detail: PLAYBACK_TAIL_RECOVERY_GUIDANCE }))
  expect(getVoiceCaptureOwner()).toBeNull()
  expect(protectedPlaybackBarrier.mayOpen(scope, performance.now())).toBe(false)
  await advance(600_000)
  expect(mock.instances).toHaveLength(1)
  view.unmount()
  const reopened = render(<QwenRealtimeVoiceInput {...p}/>); await advance(0)
  expect(mock.instances).toHaveLength(1)
  const retry = reopened.getByRole('button', { name: '重试麦克风' })
  expect(retry.classList.contains('agent-playback-retry')).toBe(true)
  fireEvent.click(retry); await advance(0)
  expect(mock.instances).toHaveLength(2)
  expect(reopened.queryByRole('button', { name: '重试麦克风' })).toBeNull()
  expect(protectedPlaybackBarrier.held()).toBe(true)
  const reopenedAt = performance.now(), epoch = Symbol()
  expect(protectedPlaybackBarrier.reserveOpen(scope, reopenedAt)).toBe(true)
  expect(protectedPlaybackBarrier.claim(scope, epoch, reopenedAt)).toBe(true)
  expect(protectedPlaybackBarrier.observe(scope, epoch, false, 50, reopenedAt)).toBe('held')
  expect(protectedPlaybackBarrier.observe(scope, epoch, false, 800, reopenedAt + 800)).toBe('held')
  expect(protectedPlaybackBarrier.observe(scope, epoch, false, 50, reopenedAt + 851)).toBe('released')
 } finally { protectedPlaybackBarrier.rollback(token) }
})

for (const [code, guidance] of [
 ['QWEN_NATIVE_CREDENTIAL_REJECTED','检查语音设置中的密钥'],
 ['QWEN_NATIVE_ACCESS_DENIED','检查模型权限'],
 ['QWEN_NATIVE_ACCOUNT_BALANCE','检查百炼账户'],
]) it(`stops retrying ${code} until the user explicitly retries`,async()=>{
 const onPermanentFailure=vi.fn(), p={...props(),onPermanentFailure}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:code}))
 expect(onPermanentFailure).toHaveBeenCalledOnce()
 expect(onPermanentFailure.mock.calls[0][0]).toContain(guidance)
 expect(onPermanentFailure.mock.calls[0][0]).not.toContain(code)
 await advance(600_000);expect(mock.instances).toHaveLength(1)
 fireEvent.click(view.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(mock.instances).toHaveLength(2)
 expect(view.queryByRole('button',{name:'已检查设置，重试云语音'})).toBeNull()
})

it('does not retry or replay an original whose durable handoff is unconfirmed',async()=>{
 const handoff=vi.fn(async()=>false), p={...props(),onPendingInputHandoff:handoff}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}))
 await advance(0);expect(handoff).toHaveBeenCalledOnce()
 fireEvent.click(view.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(1000)
 expect(mock.instances).toHaveLength(1)
 expect(p.onStatus.mock.calls.at(-1)?.[0].detail).toContain('仍在核对')
})

it('explicit retry first confirms retained originals and keeps the actionable failure visible',async()=>{
 const handoff=vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true), onPermanentFailure=vi.fn()
 const p={...props(),onPendingInputHandoff:handoff,onPermanentFailure},view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}))
 await advance(0);expect(mock.instances).toHaveLength(1)
 fireEvent.click(view.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(handoff).toHaveBeenCalledTimes(2);expect(handoff.mock.calls[1][2]).toBe(true)
 expect(onPermanentFailure.mock.calls.at(-1)?.[0]).toContain('检查语音设置中的密钥')
 expect(onPermanentFailure.mock.calls.at(-1)?.[0]).toContain('原话已保存')
 expect(mock.instances).toHaveLength(2)
})

it('ignores a timed-out or privacy-stopped late handoff and does not reconnect',async()=>{
 let resolve!:(value:boolean)=>void
 const handoff=vi.fn(()=>new Promise<boolean>(done=>{resolve=done})),p={...props(),onPendingInputHandoff:handoff}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}))
 await advance(20_001)
 localStorage.setItem(privacyStopKey('session'),'stopped')
 await act(async()=>window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
 await act(async()=>resolve(true));fireEvent.click(view.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(mock.instances).toHaveLength(1);expect(handoff).toHaveBeenCalledTimes(1)
 view.unmount();const reopened=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 expect(reopened.queryByRole('button',{name:'已检查设置，重试云语音'})).toBeNull()
 expect(mock.instances).toHaveLength(1);expect(handoff).toHaveBeenCalledTimes(1)
 localStorage.removeItem(privacyStopKey('session'));reopened.unmount()
 const resumed=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 fireEvent.click(resumed.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(handoff).toHaveBeenCalledTimes(1);expect(mock.instances).toHaveLength(2)
})

it('keeps a permanent failure and pending original through effect change and remount without automatic retry',async()=>{
 const handoff=vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true),p={...props(),onPendingInputHandoff:handoff}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}))
 await advance(0);view.rerender(<QwenRealtimeVoiceInput {...p} childName="Changed"/>);await advance(0)
 expect(mock.instances).toHaveLength(1);view.unmount()
 const reopened=render(<QwenRealtimeVoiceInput {...p}/>);await advance(600_000)
 expect(mock.instances).toHaveLength(1)
 fireEvent.click(reopened.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(handoff.mock.calls[1][0]).toEqual([{utterance:'合成原话'}])
 expect(handoff.mock.calls[1][2]).toBe(true);expect(mock.instances).toHaveLength(2)
})

it('does not retry a retained original while consent is withdrawn or in another child session',async()=>{
 const handoff=vi.fn().mockResolvedValue(false),p={...props(),onPendingInputHandoff:handoff}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}));await advance(0)
 view.rerender(<QwenRealtimeVoiceInput {...p} audioAllowed={false}/>);await advance(0)
 expect(handoff).toHaveBeenCalledTimes(1);expect(mock.instances).toHaveLength(1)
 view.rerender(<QwenRealtimeVoiceInput {...p} sessionId="other-session" childId="other-child"/>);await advance(0)
 expect(mock.instances).toHaveLength(2);expect(handoff).toHaveBeenCalledTimes(1)
 view.rerender(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 expect(mock.instances).toHaveLength(2);expect(handoff).toHaveBeenCalledTimes(1)
 fireEvent.click(view.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(handoff).toHaveBeenCalledTimes(1)
})

it('drops retained originals if permission is withdrawn while the component is unmounted',async()=>{
 const handoff=vi.fn().mockResolvedValue(false),p={...props(),onPendingInputHandoff:handoff}
 const view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 mock.instances[0].takePendingInputsForReconnect=vi.fn().mockReturnValueOnce([{utterance:'合成原话'}]).mockReturnValue([])
 await act(async()=>mock.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'QWEN_NATIVE_CREDENTIAL_REJECTED'}));await advance(0)
 view.unmount();const disabled=render(<QwenRealtimeVoiceInput {...p} active={false} audioAllowed={false}/>);await advance(0);disabled.unmount()
 const resumed=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 fireEvent.click(resumed.getByRole('button',{name:'已检查设置，重试云语音'}));await advance(0)
 expect(handoff).toHaveBeenCalledTimes(1);expect(mock.instances).toHaveLength(2)
})
