import {act, cleanup, render, screen} from '@testing-library/react'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {createVoiceRuntimePolicyHandler} from '../../main/voice-runtime-policy'
import type {VoiceRuntimePolicy} from '../../shared/voiceRuntimePolicy'
import {useVoiceRuntimePolicy} from './useVoiceRuntimePolicy'
import {selectVoiceCaptureMode} from './voiceCaptureMode'

function Probe({child='child-a',session='session-a',refresh=false}:{child?:string;session?:string;refresh?:boolean}) {
  const policy=useVoiceRuntimePolicy(child,session,refresh)
  const mode=selectVoiceCaptureMode({runtimePolicy:policy,runtimeEpoch:policy?.runtimeEpoch??null,
    realtimeSettingsLoaded:true,realtimeConfigured:true,realtimeEnabled:true,realtimeFailed:false,
    onboardingSupported:true,onboardingPhase:'naming',privacyStopped:false,microphoneReady:true,
    wakeSettingsLoaded:true,editingWakeWord:false})
  return <><output data-testid="policy">{policy?.capture??'null'}</output><output data-testid="mode">{mode}</output></>
}
const runtime=(epoch=1):{status:'ready'|'starting';localApiUrl:string;sessionToken:string;runtimeEpoch:number}=>({status:'ready',localApiUrl:'http://127.0.0.1:40293',sessionToken:'synthetic-owned-policy-token',runtimeEpoch:epoch})
const policy=(capture:VoiceRuntimePolicy['capture']='configured',epoch=1):VoiceRuntimePolicy=>({version:1,capture,runtimeEpoch:epoch})
function deferred<T>() { let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return {promise,resolve} }
const advance=async(ms:number)=>{await act(async()=>{await vi.advanceTimersByTimeAsync(ms)})}
async function mount(host:object,props:{child?:string;session?:string;refresh?:boolean}={}) {
  vi.stubGlobal('desktop',host)
  let view!:ReturnType<typeof render>
  await act(async()=>{view=render(<Probe {...props}/>);await Promise.resolve()})
  return view
}
function simpleHost(read:()=>Promise<unknown>,readRuntime=async()=>runtime()) {
  return {getRuntimeConfig:vi.fn(readRuntime),getVoiceRuntimePolicy:vi.fn(read)}
}
beforeEach(()=>{vi.useFakeTimers();vi.stubGlobal('fetch',vi.fn().mockRejectedValue(Error('UNEXPECTED_NETWORK')))})
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()})

describe('owned voice policy recovery',()=>{
  it.each(['configured','bounded_segmented'] as const)('accepts one fresh %s read and never adds a poll or grant',async capture=>{
    const host=simpleHost(async()=>policy(capture))
    await mount(host)
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    await advance(60_000)
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
    expect(host.getRuntimeConfig).toHaveBeenCalledTimes(2)
  })
  it.each(['timeout','owner-admission-busy','revision-race'] as const)('recovers after original Main handler %s without a restart',async failure=>{
    const child={},event={sender:{id:17}};let revision=0,first=true
    const current=()=>{if(failure==='owner-admission-busy'&&first){first=false;throw Error('CLOUD_PROVIDER_REFRESH_BUSY')};return {...runtime(),child,revision}}
    const physical=vi.fn(async()=>{
      if(first){first=false;if(failure==='timeout')throw new DOMException('synthetic bounded timeout','TimeoutError');if(failure==='revision-race')revision++}
      return new Response(JSON.stringify({version:1,capture:'configured'}),{status:200})
    })
    const original=createVoiceRuntimePolicyHandler({allowedSender:()=>true,current,fetch:physical as typeof fetch})
    const host=simpleHost(()=>original(event))
    await mount(host)
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    await advance(1000)
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
    await advance(7000)
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
  })
  it('uses only the 1s/2s/4s retries and keeps a persistent backend blocked response blocked',async()=>{
    const host=simpleHost(async()=>policy('blocked'))
    await mount(host)
    await advance(999);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
    await advance(1);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
    await advance(1999);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
    await advance(1);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(3)
    await advance(3999);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(3)
    await advance(1);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(4)
    await advance(60_000);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
  })
  it.each([
    ['null',null],['old epoch',policy('configured',2)],['extra field',{...policy(),unexpected:true}],
    ['unknown capture',{...policy(),capture:'segmented'}],['wrong version',{...policy(),version:2}],
  ])('never accepts persistent invalid policy %s',async(_label,value)=>{
    const host=simpleHost(async()=>value)
    await mount(host);await advance(1000);await advance(2000);await advance(4000);await advance(60_000)
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(4)
  })
  it.each(['epoch','url','token','not-ready'] as const)('rejects a changed fresh after-read %s and recovers only with the next whole read',async changed=>{
    let reads=0
    const host=simpleHost(async()=>policy(),async()=>{
      reads++
      if(reads===2){const result=runtime();if(changed==='epoch')result.runtimeEpoch=2;if(changed==='url')result.localApiUrl='http://127.0.0.1:40294';if(changed==='token')result.sessionToken='different-synthetic-token';if(changed==='not-ready')return {...result,status:'starting'} as ReturnType<typeof runtime>;return result}
      return runtime()
    })
    await mount(host);expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    await advance(1000);expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    expect(host.getRuntimeConfig).toHaveBeenCalledTimes(4)
  })
  it('does not call the policy endpoint for an unready initial owner and retries a fresh ready owner',async()=>{
    let ready=false
    const host=simpleHost(async()=>policy(),async()=>ready?runtime():({...runtime(),status:'starting'} as ReturnType<typeof runtime>))
    await mount(host);expect(host.getVoiceRuntimePolicy).not.toHaveBeenCalled()
    ready=true;await advance(1000)
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
  })
  it('cancels the old retry timer when the real runtime-restarted event requests a fresh read',async()=>{
    let healthy=false
    const host=simpleHost(async()=>policy(healthy?'configured':'blocked'))
    await mount(host);healthy=true
    await act(async()=>{window.dispatchEvent(new Event('hsc-runtime-restarted'))})
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    await advance(60_000);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
  })
  it('serializes repeated restarts behind the original IPC and discards its old epoch late reply',async()=>{
    const old=deferred<VoiceRuntimePolicy>(),fresh=deferred<VoiceRuntimePolicy>();let epoch=1,active=0,maxActive=0
    const host=simpleHost(()=>{active++;maxActive=Math.max(maxActive,active);return (epoch===1?old.promise:fresh.promise).finally(()=>{active--})},async()=>runtime(epoch))
    await mount(host);epoch=2
    await act(async()=>{window.dispatchEvent(new Event('hsc-runtime-restarted'));window.dispatchEvent(new Event('hsc-runtime-restarted'))})
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
    await act(async()=>{old.resolve(policy());await Promise.resolve()})
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
    await act(async()=>{fresh.resolve(policy('configured',2));await Promise.resolve()})
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    expect(maxActive).toBe(1)
    await advance(60_000);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
  })
  it('does not expose the previous child policy while a rebound child waits for its own fresh reply',async()=>{
    const fresh=deferred<VoiceRuntimePolicy>();let count=0
    const host=simpleHost(()=>++count===1?Promise.resolve(policy()):fresh.promise)
    const view=await mount(host);expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    await act(async()=>{view.rerender(<Probe child="child-b" session="session-b"/>)})
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    await act(async()=>{fresh.resolve(policy());await Promise.resolve()})
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
  })
  it('serializes a child rebind behind a late read and never publishes the retired child reply',async()=>{
    const old=deferred<VoiceRuntimePolicy>(),fresh=deferred<VoiceRuntimePolicy>();let count=0,active=0,maxActive=0
    const host=simpleHost(()=>{active++;maxActive=Math.max(maxActive,active);return (++count===1?old.promise:fresh.promise).finally(()=>{active--})})
    const view=await mount(host)
    await act(async()=>{view.rerender(<Probe child="child-b" session="session-b"/>)})
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
    await act(async()=>{old.resolve(policy());await Promise.resolve()})
    expect(screen.getByTestId('mode')).toHaveTextContent('blocked')
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(2)
    await act(async()=>{fresh.resolve(policy());await Promise.resolve()})
    expect(screen.getByTestId('mode')).toHaveTextContent('segmented');expect(maxActive).toBe(1)
  })
  it('removes a pending retry on unmount',async()=>{
    const host=simpleHost(async()=>policy('blocked'))
    const view=await mount(host);view.unmount();await advance(60_000)
    expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
  })
  it('removes a queued restart on unmount and ignores the unresolved original IPC late result',async()=>{
    const old=deferred<VoiceRuntimePolicy>()
    const host=simpleHost(()=>old.promise)
    const view=await mount(host)
    await act(async()=>{window.dispatchEvent(new Event('hsc-runtime-restarted'))})
    view.unmount()
    await act(async()=>{old.resolve(policy());await Promise.resolve()})
    await advance(60_000);expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(1)
  })
})


it('refreshes a live native policy without briefly retiring healthy capture, and observes revocation',async()=>{
  let allow=true
  const second=deferred<VoiceRuntimePolicy>();let reads=0
  const host=simpleHost(()=>++reads===2?second.promise:Promise.resolve(policy(allow?'configured':'bounded_segmented')))
  await mount(host,{refresh:true});expect(screen.getByTestId('policy')).toHaveTextContent('configured')
  await advance(2000);expect(screen.getByTestId('policy')).toHaveTextContent('configured')
  await act(async()=>{second.resolve(policy());await Promise.resolve()})
  allow=false;await advance(2000)
  expect(screen.getByTestId('policy')).toHaveTextContent('bounded_segmented')
  expect(host.getVoiceRuntimePolicy).toHaveBeenCalledTimes(3)
})
