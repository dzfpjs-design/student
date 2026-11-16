// @vitest-environment jsdom
/** Connection I/O is replaced; React rerenders, the retained session options,
 * capture coordinator and component ownership/consent lifecycle are real.
 * Synthetic bridge receipts test selection only, not a physical UI/backend effect.
 */
import {useState} from 'react'
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import {claimVoiceCapture,getVoiceCaptureOwner,releaseVoiceCapture,resetVoiceCaptureCoordinatorForTests,subscribeVoiceCapture} from './voiceCaptureCoordinator'
import type {QwenNativeRuntimeBridge,QwenNativeCapabilityProposal,QwenNativeToolReceipt} from './qwenNativeProtocol'
import type {QwenRealtimeInputCapture} from './qwenRealtimeVoice'
const h=vi.hoisted(()=>({instances:[] as any[],bridges:[] as QwenNativeRuntimeBridge[],register:vi.fn(),release:vi.fn(),renew:vi.fn(),close:vi.fn()}))
vi.mock('./qwenRealtimeVoice',async original=>{const actual=await original<typeof import('./qwenRealtimeVoice')>();return {...actual,QwenRealtimeVoiceSession:class extends actual.QwenRealtimeVoiceSession{
 currentOptions:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]
 root={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true}),stop:vi.fn()}
 constructor(options:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]){super(options);this.currentOptions=options;h.instances.push(this)}
 async connect(){const state=this as any;state.localSourceTrack=this.root;state.stream={getTracks:()=>[this.root]};state.audioTrack={...this.root,enabled:false,stop:vi.fn()};state.nativeHostConnectionId='host-'+h.instances.length;state.nativeSocketOpen=true;state.sessionReady=true;state.microphoneReleased=true
  await this.currentOptions.callbacks.onMicrophoneOpen?.(this.root as unknown as MediaStreamTrack)
  this.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'listening',speechActive:false,pendingUtterances:0,recoverable:true})
 }
}}})
vi.mock('./mediaDevices',()=>({mediaDeviceRegistration:vi.fn(async()=>({systemIdHash:'public-synthetic-mic',capabilities:{}}))}))
vi.mock('./api',()=>({localApi:{registerDevice:h.register,releaseDeviceLease:h.release,renewDeviceLease:h.renew}}))
vi.mock('./companionVoice',async original=>({...await original<typeof import('./companionVoice')>(),getCompanionVoiceState:()=> 'idle',getCompanionPlaybackLocalOnly:()=>true,subscribeCompanionVoice:()=>()=>{},stopCompanionSpeech:vi.fn(),speakCompanion:vi.fn()}))
const result:QwenNativeToolReceipt={version:1,proposal_status:'admitted',execution:{status:'completed',location:'client_device_bridge'},public_result:{status:'synthetic_receipt_only'}}
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[]}
const context={version:1 as const,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
const capture:QwenRealtimeInputCapture={localInputId:'original-input',sessionId:'session',capturedAt:12345,source:'accepted_onset',reference:{child_id:'child'},carryoverConfirmationId:null}
const proposal=(call:string,destination='learning'):QwenNativeCapabilityProposal=>({version:1,host_connection_id:'host-1',voice_input_id:'original-voice',supplier_session_id:'supplier-original',input_item_id:'item-original',response_id:'response-original',call_id:call,capability_name:'workspace.navigate',args:{destination}})
function bridge():QwenNativeRuntimeBridge{return {loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>context),admit:vi.fn(async()=>({voice_input_id:'original-voice'})),admitHousehold:vi.fn(async()=>({voice_input_id:'household-voice',source_kind:'household_local_transcript' as const,raw_audio_available:false as const,entry_state:'unprocessed' as const})),propose:vi.fn(async()=>result),recordDialogue:vi.fn(async()=>undefined),readReceipt:vi.fn(async()=>result)}}
const props=(runtime:QwenNativeRuntimeBridge)=>({active:true,audioAllowed:true,sessionId:'session',childId:'child',childName:'Public synthetic',wakeWord:'小知',initialConversationActive:true,nativeRuntime:runtime,onTurn:vi.fn(),onStatus:vi.fn()})
const advance=async(ms=0)=>act(async()=>{await vi.advanceTimersByTimeAsync(ms)})
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(100000);h.instances=[];h.bridges=[];h.register.mockReset().mockResolvedValue({lease_id:'original-mic-lease'});h.release.mockReset().mockResolvedValue({});h.renew.mockReset().mockResolvedValue({});h.close.mockReset().mockResolvedValue(undefined);Object.defineProperty(window,'desktop',{configurable:true,value:{closeQwenNativeSocket:h.close,recordRendererDiagnostic:vi.fn(async()=>undefined)}});resetVoiceCaptureCoordinatorForTests()})
afterEach(()=>{cleanup();resetVoiceCaptureCoordinatorForTests();vi.useRealTimers();vi.restoreAllMocks()})

it('retained connection uses the current UI closure across several book/learning tool transitions without reopening its microphone',async()=>{
 function Harness(){const [open,setOpen]=useState(false);const runtime=bridge();runtime.propose=vi.fn(async p=>{if(p.args.destination==='mistake_book')setOpen(true);else if(open)setOpen(false);return result});h.bridges.push(runtime);return <><output data-testid="current-view">{open?'book':'learning'}</output><QwenRealtimeVoiceInput {...props(runtime)}/></>}
 render(<Harness/>);await advance();const session=h.instances[0],retained=session.currentOptions.nativeRuntime
 for(const [index,destination]of ['mistake_book','learning','mistake_book','learning'].entries()){
  await act(async()=>{await retained.propose(proposal('call-'+index,destination))});expect(screen.getByTestId('current-view')).toHaveTextContent(destination==='mistake_book'?'book':'learning')
 }
 expect(h.instances).toHaveLength(1);expect(h.register).toHaveBeenCalledTimes(1);expect(h.release).not.toHaveBeenCalled();expect(session.root.stop).not.toHaveBeenCalled()
})

it('every retained runtime method delegates the original arguments to the current bridge after a same-owner rerender',async()=>{
 const a=bridge(),b=bridge(),p=props(a),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance();const session=h.instances[0],retained=session.currentOptions.nativeRuntime as QwenNativeRuntimeBridge
 view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b}/>);await advance()
 const packet=proposal('after-render'),dialogue={...packet,text:'public synthetic',status:'unverified' as const,terminal:'completed' as const}
 const entry={entry_receipt:{version:1 as const,origin_scope:'unassigned_household' as const,storage_generation:'public-generation',receipt_id:'public-entry',client_action_id:'public-asr',issued_at_ms:12345,transcript_sha256:'a'.repeat(64),model_version:'public-local',source_kind:'household_local_transcript' as const},transcript:'公开合成原话'}
 await retained.loadCatalog('host-1');await retained.loadContext('host-1');await retained.admit('原始公开话语',capture,'host-1');await retained.propose(packet);await retained.recordDialogue(dialogue);await retained.readReceipt(packet);await retained.admitHousehold!(entry,'host-1')
 for(const key of ['loadCatalog','loadContext','admit','propose','recordDialogue','readReceipt','admitHousehold'] as const){expect(a[key]).not.toHaveBeenCalled();expect(b[key]).toHaveBeenCalledTimes(1)}
 expect(b.admit).toHaveBeenCalledWith('原始公开话语',capture,'host-1');expect(b.propose).toHaveBeenCalledWith(packet);expect(vi.mocked(b.propose).mock.calls[0][0]).toBe(packet);expect(vi.mocked(b.admit).mock.calls[0][1]).toBe(capture);expect(b.recordDialogue).toHaveBeenCalledWith(dialogue);expect(b.readReceipt).toHaveBeenCalledWith(packet);expect(b.admitHousehold).toHaveBeenCalledWith(entry,'host-1');expect(h.instances).toHaveLength(1)
})

it.each(['child','session','consent','active','capture_owner','connection','audio_device','runtime_removed'] as const)('obsolete retained adapter cannot reach a new bridge after %s changes',async change=>{
 const a=bridge(),b=bridge(),p=props(a),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance();const old=h.instances[0],retained=old.currentOptions.nativeRuntime as QwenNativeRuntimeBridge
 if(change==='runtime_removed'){view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={undefined}/>);await advance()}
 else if(change==='audio_device'){view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b} audioDeviceId='other-public-microphone'/>);await advance()}
 else if(change==='capture_owner')releaseVoiceCapture(getVoiceCaptureOwner()!)
 else if(change==='connection'){await act(async()=>old.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}));await advance(1001)}
 else{view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b} {...(change==='child'?{childId:'other-child'}:change==='session'?{sessionId:'other-session'}:change==='consent'?{audioAllowed:false}:{active:false})}/>);await advance()}
 await expect(retained.propose(proposal('obsolete'))).rejects.toThrow('OWNER_CHANGED');expect(a.propose).not.toHaveBeenCalled();expect(b.propose).not.toHaveBeenCalled()
})

it('an in-flight method remains bound to its original identity/bridge and is never retried on the new bridge',async()=>{
 let finish!:(value:QwenNativeToolReceipt)=>void;const a=bridge(),b=bridge();a.propose=vi.fn(()=>new Promise<QwenNativeToolReceipt>(resolve=>{finish=resolve}));const p=props(a),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance();const retained=h.instances[0].currentOptions.nativeRuntime as QwenNativeRuntimeBridge,original=proposal('in-flight')
 const pending=retained.propose(original);view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b}/>);await advance();finish(result);await expect(pending).resolves.toBe(result)
 expect(a.propose).toHaveBeenCalledExactlyOnceWith(original);expect(b.propose).not.toHaveBeenCalled();await retained.propose(proposal('next-call'));expect(b.propose).toHaveBeenCalledTimes(1);expect(a.readReceipt).not.toHaveBeenCalled();expect(b.readReceipt).not.toHaveBeenCalled();expect(h.instances).toHaveLength(1)
})

it('revocation during an in-flight request withholds its result without resubmitting its original identity',async()=>{
 let finish!:(value:QwenNativeToolReceipt)=>void;const a=bridge(),b=bridge();a.propose=vi.fn(()=>new Promise<QwenNativeToolReceipt>(resolve=>{finish=resolve}));const p=props(a),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance();const retained=h.instances[0].currentOptions.nativeRuntime as QwenNativeRuntimeBridge,original=proposal('revoked-in-flight')
 const pending=retained.propose(original);const denied=expect(pending).rejects.toThrow('OWNER_CHANGED');view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b} audioAllowed={false}/>);await advance();finish(result);await denied;expect(a.propose).toHaveBeenCalledExactlyOnceWith(original);expect(b.propose).not.toHaveBeenCalled();expect(b.readReceipt).not.toHaveBeenCalled()
})


it('a missing optional household admission remains unavailable rather than being advertised by the retained adapter',async()=>{
 const a=bridge(),b=bridge();b.admitHousehold=undefined;const p=props(a),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance();const retained=h.instances[0].currentOptions.nativeRuntime as QwenNativeRuntimeBridge
 expect(retained.admitHousehold).toBeTypeOf('function');view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={b}/>);await advance();expect(retained.admitHousehold).toBeUndefined();expect(h.instances).toHaveLength(1)
})

it('starts exactly once when another microphone task releases its capture',async()=>{
 claimVoiceCapture('other-task');const p=props(bridge());render(<QwenRealtimeVoiceInput {...p}/>);await advance()
 expect(h.instances).toHaveLength(0)
 await act(async()=>releaseVoiceCapture('other-task'));await advance()
 expect(h.instances).toHaveLength(1);expect(h.register).toHaveBeenCalledTimes(1)
})

it.each(['unmount','privacy','consent'] as const)('does not start after the waiting component loses %s authority',async reason=>{
 claimVoiceCapture('other-task');const p=props(bridge()),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance()
 if(reason==='unmount')view.unmount()
 if(reason==='privacy')await act(async()=>window.dispatchEvent(new Event('hsc-privacy-pause')))
 if(reason==='consent')view.rerender(<QwenRealtimeVoiceInput {...p} audioAllowed={false}/>);
 await act(async()=>releaseVoiceCapture('other-task'));await advance()
 expect(h.instances).toHaveLength(0);expect(getVoiceCaptureOwner()).toBeNull()
})

it('continues waiting when another task wins a capture release race',async()=>{
 claimVoiceCapture('first-task')
 let steal=true
 const unsubscribe=subscribeVoiceCapture(()=>{if(steal&&!getVoiceCaptureOwner()){steal=false;claimVoiceCapture('second-task')}})
 try{
  render(<QwenRealtimeVoiceInput {...props(bridge())}/>);await advance()
  await act(async()=>releaseVoiceCapture('first-task'));await advance()
  expect(getVoiceCaptureOwner()).toBe('second-task');expect(h.instances).toHaveLength(0)
  await act(async()=>releaseVoiceCapture('second-task'));await advance()
  expect(h.instances).toHaveLength(1);expect(h.register).toHaveBeenCalledTimes(1)
 }finally{unsubscribe()}
})

it('does not connect if privacy is paused synchronously while the microphone is claimed',async()=>{
 claimVoiceCapture('other-task')
 const unsubscribe=subscribeVoiceCapture(()=>{if(getVoiceCaptureOwner()?.startsWith('qwen-realtime-'))window.dispatchEvent(new Event('hsc-privacy-pause'))})
 try{
  render(<QwenRealtimeVoiceInput {...props(bridge())}/>);await advance()
  await act(async()=>releaseVoiceCapture('other-task'));await advance()
  expect(h.instances).toHaveLength(0);expect(getVoiceCaptureOwner()).toBeNull()
 }finally{unsubscribe()}
})

it('resumes waiting capture when an unavailable runtime becomes available after capture is released',async()=>{
 claimVoiceCapture('other-task');const p=props(bridge()),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance()
 view.rerender(<QwenRealtimeVoiceInput {...p} nativeRuntime={undefined}/>);await advance()
 await act(async()=>releaseVoiceCapture('other-task'));await advance()
 view.rerender(<QwenRealtimeVoiceInput {...p}/>);await advance()
 expect(h.instances).toHaveLength(1)
})

it('starts only the replacement child session when ownership changes while waiting',async()=>{
 claimVoiceCapture('other-task');const p=props(bridge()),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance()
 view.rerender(<QwenRealtimeVoiceInput {...p} childId='replacement-child' sessionId='replacement-session'/>);await advance()
 await act(async()=>releaseVoiceCapture('other-task'));await advance()
 expect(h.instances).toHaveLength(1)
 expect(h.instances[0].currentOptions.protectedPlaybackControl).toMatchObject({childId:'replacement-child',sessionId:'replacement-session'})
})

it('still retries connection errors after acquiring a previously occupied microphone',async()=>{
 claimVoiceCapture('other-task');render(<QwenRealtimeVoiceInput {...props(bridge())}/>);await advance()
 await act(async()=>releaseVoiceCapture('other-task'));await advance()
 await act(async()=>h.instances[0].currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}))
 await advance(1001);expect(h.instances).toHaveLength(2)
})

it('submits a held Space turn once, preserves focused controls, and cancels an unfinished turn on window blur', async () => {
  render(<>
    <input aria-label="编辑文字" />
    <a href="#details">详情</a>
    <select aria-label="选择学科"><option>化学</option></select>
    <QwenRealtimeVoiceInput {...props(bridge())} />
  </>)
  await advance()
  const transport = h.instances[0]
  const capture = transport.currentOptions.callbacks.onExplicitVoiceCapture
  const begin = vi.spyOn(transport, 'beginExplicitVoiceTurn').mockImplementation(() => { capture('ready'); return true })
  const finish = vi.spyOn(transport, 'finishExplicitVoiceTurn').mockImplementation(() => capture('idle'))
  const cancel = vi.spyOn(transport, 'cancelExplicitVoiceTurn').mockImplementation(() => capture('idle'))

  const space = { key: ' ', code: 'Space' }
  for (const element of [screen.getByRole('textbox'), screen.getByRole('link'), screen.getByRole('combobox'),
    screen.getByRole('switch'), screen.getByRole('button', { name: '开始说话' })]) {
    element.focus()
    expect(fireEvent.keyDown(element, space)).toBe(true)
    element.blur()
  }
  expect(fireEvent.keyDown(window, { ...space, isComposing: true })).toBe(true)
  expect(begin).not.toHaveBeenCalled()
  expect(fireEvent.keyDown(window, space)).toBe(false)
  expect(fireEvent.keyDown(window, { ...space, repeat: true })).toBe(false)
  expect(begin).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('status')).toHaveTextContent('松开空格发送')
  fireEvent.keyUp(window, { code: 'Space' })
  fireEvent.keyUp(window, space)
  expect(finish).toHaveBeenCalledTimes(1)

  fireEvent.keyDown(window, { key: ' ' })
  fireEvent(window, new Event('blur'))
  fireEvent.keyUp(window, space)
  expect(cancel).toHaveBeenCalledTimes(1)
  expect(finish).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('status')).toHaveTextContent('录音已取消')
})

it('supports tap twice, manual takeover in hands-free mode, and child-scoped mode persistence', async () => {
  const settingsKey = 'hsc-voice-input-mode:voice-mode-child'
  localStorage.removeItem(settingsKey)
  try {
    const scope = { ...props(bridge()), childId: 'voice-mode-child' }
    const view = render(<QwenRealtimeVoiceInput {...scope} />)
    await advance()
    const transport = h.instances[0]
    expect(transport.currentOptions.inputMode).toBe('manual')
    const capture = transport.currentOptions.callbacks.onExplicitVoiceCapture
    const begin = vi.spyOn(transport, 'beginExplicitVoiceTurn').mockImplementation(() => { capture('ready'); return true })
    const finish = vi.spyOn(transport, 'finishExplicitVoiceTurn').mockImplementation(() => capture('idle'))
    const mode = screen.getByRole('switch', { name: '免按监听' })
    mode.focus()
    fireEvent.click(mode, { detail: 1 })
    expect(mode).toHaveAttribute('aria-checked', 'true')
    expect(mode).not.toHaveFocus()
    const microphone = screen.getByRole('button', { name: '开始说话' })
    microphone.focus()
    fireEvent.click(microphone, { detail: 0 })
    expect(microphone).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: '结束并发送' }))
    expect(begin).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledTimes(1)
    expect(h.instances).toHaveLength(1)
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')

    view.unmount()
    const restored = render(<QwenRealtimeVoiceInput {...scope} />)
    await advance()
    expect(h.instances[1].currentOptions.inputMode).toBe('hands-free')
    restored.rerender(<QwenRealtimeVoiceInput {...scope} childId="voice-mode-other-child" />)
    await advance()
    expect(h.instances[2].currentOptions.inputMode).toBe('manual')
  } finally {
    localStorage.removeItem(settingsKey)
  }
})

it('starts Space with the companion collapsed and cancels rather than sending into another workspace', async () => {
  const reveal = vi.fn()
  const original = props(bridge())
  const view = render(<QwenRealtimeVoiceInput {...original} explicitControlsVisible={false} onKeyboardCaptureStart={reveal} />)
  await advance()
  const transport = h.instances[0]
  const state = transport.currentOptions.callbacks.onExplicitVoiceCapture
  const begin = vi.spyOn(transport, 'beginExplicitVoiceTurn').mockImplementation(() => { state('ready'); return true })
  const cancel = vi.spyOn(transport, 'cancelExplicitVoiceTurn').mockImplementation(() => state('idle'))
  const finish = vi.spyOn(transport, 'finishExplicitVoiceTurn').mockImplementation(() => state('idle'))
  const space = { key: ' ', code: 'Space' }
  expect(screen.queryByTestId('realtime-voice-controls')).toBeNull()
  expect(fireEvent.keyDown(window, space)).toBe(false)
  expect(begin).toHaveBeenCalledTimes(1)
  expect(reveal).toHaveBeenCalledTimes(1)

  view.rerender(<QwenRealtimeVoiceInput {...original} visibleLearningSource={false} onKeyboardCaptureStart={reveal} />)
  fireEvent.keyUp(window, space)
  expect(cancel).toHaveBeenCalledTimes(1)
  expect(finish).not.toHaveBeenCalled()

  fireEvent.keyDown(window, space)
  fireEvent.keyUp(window, space)
  expect(finish).toHaveBeenCalledTimes(1)
})

it('stops the current Tutor playback even while the voice transport is unavailable', async () => {
  const interruptPlayback = vi.fn(() => true)
  const reveal = vi.fn()
  claimVoiceCapture('other-task')
  render(<QwenRealtimeVoiceInput {...props(bridge())} onExplicitPlaybackInterrupt={interruptPlayback}
    onKeyboardCaptureStart={reveal} />)
  await advance()
  const space = { key: ' ', code: 'Space' }

  expect(fireEvent.keyDown(window, space)).toBe(false)
  fireEvent.keyDown(window, { ...space, repeat: true })
  fireEvent.keyUp(window, space)
  expect(interruptPlayback).toHaveBeenCalledTimes(1)
  expect(reveal).toHaveBeenCalledTimes(1)
  expect(screen.getByText('语音还在连接，请稍后再试。')).toBeTruthy()
  expect(h.instances).toHaveLength(0)
})
