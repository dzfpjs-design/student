import {afterEach,beforeEach,describe,it,expect,vi} from 'vitest'
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {HandsFreeVoiceInput,cancelLocalSpeech,speakLocally} from './LocalInputs'
import {localApi} from './api'
import {getCompanionVoiceState} from './companionVoice'
import {getVoiceCaptureOwner,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import {protectedPlaybackBarrier} from './protectedPlaybackBarrier'

describe('real segmented recorder protected control fanout',()=>{
 let processors:Array<{onaudioprocess:((event:AudioProcessingEvent)=>void)|null}>=[]
 let track:any,getUserMedia:ReturnType<typeof vi.fn>,contexts:number,onTranscript:ReturnType<typeof vi.fn<(text:string)=>void>>,onWake:ReturnType<typeof vi.fn<()=>void>>,sampleRate:number
 const originalMedia=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
 beforeEach(()=>{
  vi.useFakeTimers();vi.setSystemTime(10000);processors=[];contexts=0;onTranscript=vi.fn<(text:string)=>void>();onWake=vi.fn<()=>void>();sampleRate=16000
  vi.stubGlobal('AudioContext',class {sampleRate=sampleRate;state='running';constructor(){contexts++}createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()});createScriptProcessor=()=>{const p={connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null as any};processors.push(p);return p};close=vi.fn(async()=>{})})
  vi.stubGlobal('SpeechSynthesisUtterance',class {constructor(public text:string){}})
  vi.stubGlobal('speechSynthesis',{getVoices:()=>[{name:'public-synthetic-local',lang:'zh-CN',localService:true}],speak:(u:SpeechSynthesisUtterance)=>u.onstart?.(new Event('start') as SpeechSynthesisEvent),cancel:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn()})
  vi.stubGlobal('fetch',vi.fn(async()=>{throw new Error('Unmocked network forbidden')}))
  track={enabled:true,readyState:'live',stop:vi.fn(),addEventListener:vi.fn(),getSettings:()=>({deviceId:'public-fixture',echoCancellation:true}),getCapabilities:()=>({echoCancellation:['all']}),applyConstraints:vi.fn(async()=>{throw new DOMException('Public synthetic rejected driver mode','OverconstrainedError')})}
  let opens=0;getUserMedia=vi.fn(async()=>{const source=opens++===0?track:{...track,enabled:true,readyState:'live',stop:vi.fn()};return {getTracks:()=>[source],getAudioTracks:()=>[source]}});Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia,enumerateDevices:vi.fn(async()=>[])}})
  vi.spyOn(localApi,'registerDevice').mockResolvedValue({lease_id:'public-owned-mic',status:'active',expires_at:'later'});vi.spyOn(localApi,'releaseDeviceLease').mockResolvedValue({lease_id:'public-owned-mic',status:'released'})
  vi.spyOn(localApi,'transcribe').mockRejectedValue(new Error('Normal/cloud ASR forbidden for control'));vi.spyOn(localApi,'transcribeStandby').mockRejectedValue(new Error('Standby ASR forbidden for control'))
  vi.spyOn(localApi,'voicePlayback').mockImplementation(async(_s,p)=>({playback_id:p.playback_id}))
  vi.spyOn(localApi,'localPlaybackControl').mockImplementation(async(_s,p)=>({status:'recognized',control:'stop_playback',playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false}))
 })
 afterEach(()=>{cleanup();cancelLocalSpeech();const token=protectedPlaybackBarrier.token();if(token)protectedPlaybackBarrier.rollback(token);resetVoiceCaptureCoordinatorForTests();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();if(originalMedia)Object.defineProperty(navigator,'mediaDevices',originalMedia);else Reflect.deleteProperty(navigator,'mediaDevices')})
 async function mount(wakeWord?:string){let view:any;await act(async()=>{view=render(<HandsFreeVoiceInput sessionId="public-session" childId="public-child" purpose="agent_command" autoStart initialConversationActive wakeWord={wakeWord} onWake={onWake} onTranscript={onTranscript}/>);await vi.advanceTimersByTimeAsync(1)})
  await act(async()=>{await vi.waitFor(()=>expect(localApi.registerDevice).toHaveBeenCalled());await Promise.resolve()});return view}
 it('48k continuous voiced protected SAPI accepts local attention without any quiet or normal/cloud ASR',async()=>{
  sampleRate=48000
  vi.mocked(localApi.localPlaybackControl).mockImplementation(async(_s,p)=>({status:'recognized',control:'wake_companion',playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false}))
  await mount('小知');await prompt();await frames(true,250)
  expect(localApi.localPlaybackControl).toHaveBeenCalledOnce()
  expect(vi.mocked(localApi.localPlaybackControl).mock.calls[0][1]).toMatchObject({attention_wake_word:'小知',source_kind:'local_sapi',child_id:'public-child'})
  expect(onWake).toHaveBeenCalledOnce();expect(protectedPlaybackBarrier.held()).toBe(true)
  expect(getUserMedia).toHaveBeenCalledTimes(1);expect(localApi.transcribe).not.toHaveBeenCalled();expect(localApi.transcribeStandby).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
 async function frames(voiced:boolean,count:number){for(let i=0;i<count;i++)await act(async()=>{processors.at(-1)?.onaudioprocess?.({inputBuffer:{getChannelData:()=>new Float32Array(512).fill(voiced?.05:0)}} as unknown as AudioProcessingEvent);await vi.advanceTimersByTimeAsync(512/sampleRate*1000)})}
 async function prompt(){await act(async()=>{speakLocally('现在核对当前保存的公开学习内容。','public-session',undefined,undefined,{localOnly:true});await Promise.resolve()});expect(getCompanionVoiceState()).toBe('speaking')}
 it('same existing microphone/processor stops actual SAPI and restores normal listening only after real quiet, with no normal transcript',async()=>{
  await mount();await prompt();await frames(true,4);await frames(false,9)
  expect(localApi.localPlaybackControl).toHaveBeenCalledTimes(1);expect(getUserMedia).toHaveBeenCalledTimes(1);expect(contexts).toBe(1);expect(track.applyConstraints).toHaveBeenCalled();expect(getCompanionVoiceState()).toBe('interrupted');expect(protectedPlaybackBarrier.held()).toBe(true)
  expect(screen.getByRole('button').textContent).toContain('确认播放尾部静音');expect(localApi.transcribe).not.toHaveBeenCalled();expect(localApi.transcribeStandby).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
  await frames(false,14);expect(protectedPlaybackBarrier.held()).toBe(true);await frames(false,16);expect(protectedPlaybackBarrier.held()).toBe(false);expect(screen.getByRole('button').textContent).toContain('正在听');expect(onTranscript).not.toHaveBeenCalled()
 })
 it('held-tail registered segmented capture with no processor frames expires and releases the actual track without normal submission',async()=>{
  protectedPlaybackBarrier.begin({sessionId:'public-session',childId:'public-child'},'prior-accepted-cancellation',1,performance.now());await mount()
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000)});expect(track.stop).toHaveBeenCalled();expect(getVoiceCaptureOwner()).toBeNull();expect(protectedPlaybackBarrier.held()).toBe(true);expect(screen.getByText(/播放尾部静音尚未确认/)).toBeTruthy();expect(localApi.localPlaybackControl).not.toHaveBeenCalled();expect(localApi.transcribe).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
 it('held-tail expiry during pending registration disposes source and releases the late real registration lease',async()=>{
  let resolve:(x:any)=>void=()=>{};vi.mocked(localApi.registerDevice).mockImplementation(()=>new Promise(r=>{resolve=r}))
  protectedPlaybackBarrier.begin({sessionId:'public-session',childId:'public-child'},'prior-accepted-cancellation',1,performance.now());await mount()
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000)});expect(track.stop).toHaveBeenCalled();expect(protectedPlaybackBarrier.held()).toBe(true)
  await act(async()=>{resolve({lease_id:'public-late-real-lease',status:'active',expires_at:'later'});await Promise.resolve()});expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('public-late-real-lease','hands_free_open_cancelled_after_registration');expect(localApi.transcribe).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
 it('actual zero-frame recorder recovery consumes only two automatic windows and cannot open a fourth source',async()=>{
  protectedPlaybackBarrier.begin({sessionId:'public-session',childId:'public-child'},'prior-accepted-cancellation',1,performance.now());await mount()
  for(let i=0;i<8;i++)await act(async()=>{await vi.advanceTimersByTimeAsync(4000)})
  expect(getUserMedia).toHaveBeenCalledTimes(3);expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(3);expect(getVoiceCaptureOwner()).toBeNull();expect(protectedPlaybackBarrier.held()).toBe(true);expect(screen.getByText(/家长可用现有麦克风控件/)).toBeTruthy();expect(localApi.localPlaybackControl).not.toHaveBeenCalled();expect(localApi.transcribe).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
 it('changed child/session retires the real recorder and rejects late local control without canceling speech',async()=>{
  let resolve:(x:any)=>void=()=>{};vi.mocked(localApi.localPlaybackControl).mockImplementation((_s,p)=>new Promise(r=>{resolve=x=>r({...x,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch})}))
  const view=await mount();await prompt();await frames(true,4);await frames(false,9);expect(localApi.localPlaybackControl).toHaveBeenCalledTimes(1)
  await act(async()=>{view.rerender(<HandsFreeVoiceInput sessionId="other-session" childId="other-child" purpose="agent_command" onTranscript={onTranscript}/>);await Promise.resolve()})
  await act(async()=>{resolve({status:'recognized',control:'stop_playback',cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve()})
  expect(track.stop).toHaveBeenCalled();expect(getCompanionVoiceState()).toBe('speaking');expect(protectedPlaybackBarrier.held()).toBe(false);expect(onTranscript).not.toHaveBeenCalled()
 })
 it.each([false,undefined])('disabled/unknown effective AEC=%s grants neither control ASR nor ordinary submission during protected playback',async(mode)=>{
  track.getSettings=()=>({deviceId:'public-fixture',echoCancellation:mode});await mount();await prompt();await frames(true,4);await frames(false,9);expect(localApi.localPlaybackControl).not.toHaveBeenCalled();expect(localApi.transcribe).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled();expect(getCompanionVoiceState()).toBe('speaking')
 })
 it('accepted cancellation stays global across a real child/session switch and only the new recorder quiet can release it',async()=>{
  const view=await mount();await prompt();await frames(true,4);await frames(false,9);expect(getCompanionVoiceState()).toBe('interrupted');await frames(false,10)
  expect(protectedPlaybackBarrier.held()).toBe(true)
  await act(async()=>{view.rerender(<HandsFreeVoiceInput sessionId="other-session" childId="other-child" purpose="agent_command" autoStart onTranscript={onTranscript}/>);await vi.advanceTimersByTimeAsync(1)})
  await act(async()=>{await vi.waitFor(()=>expect(localApi.registerDevice).toHaveBeenCalledTimes(2));await Promise.resolve()})
  expect(getUserMedia).toHaveBeenCalledTimes(2);expect(protectedPlaybackBarrier.held()).toBe(true)
  await frames(false,20);expect(protectedPlaybackBarrier.held()).toBe(true);expect(screen.getByRole('button').textContent).toContain('确认播放尾部静音')
  await frames(false,10);expect(protectedPlaybackBarrier.held()).toBe(false);expect(screen.getByRole('button').textContent).toContain('正在听')
  expect(localApi.localPlaybackControl).toHaveBeenCalledTimes(1);expect(localApi.transcribe).not.toHaveBeenCalled();expect(localApi.transcribeStandby).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
 it('exhausted quiet recovery blocks automatic GUM, exposes retry, and explicit existing-button retry only opens a bounded quiet-only observation',async()=>{
  const scope={sessionId:'public-session',childId:'public-child'};protectedPlaybackBarrier.begin(scope,'previous-actual-lease',1,0);protectedPlaybackBarrier.tick(5000);protectedPlaybackBarrier.claim(scope,Symbol(),5000);protectedPlaybackBarrier.tick(10000);protectedPlaybackBarrier.claim(scope,Symbol(),10000);protectedPlaybackBarrier.tick(15000)
  await act(async()=>{render(<HandsFreeVoiceInput sessionId="public-session" childId="public-child" purpose="agent_command" autoStart onTranscript={onTranscript}/>);await vi.advanceTimersByTimeAsync(1)})
  expect(getUserMedia).not.toHaveBeenCalled();expect(screen.getByText(/家长可用现有麦克风控件/)).toBeTruthy();await act(async()=>{fireEvent.click(screen.getByRole('button'));await vi.advanceTimersByTimeAsync(1)});await act(async()=>{await vi.waitFor(()=>expect(localApi.registerDevice).toHaveBeenCalled());await Promise.resolve()})
  expect(getUserMedia).toHaveBeenCalledTimes(1);expect(protectedPlaybackBarrier.held()).toBe(true);await frames(false,30);expect(protectedPlaybackBarrier.held()).toBe(false);expect(localApi.localPlaybackControl).not.toHaveBeenCalled();expect(localApi.transcribe).not.toHaveBeenCalled();expect(onTranscript).not.toHaveBeenCalled()
 })
})
