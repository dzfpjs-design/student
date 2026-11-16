import { beforeAll, afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import { StrictMode, createElement } from 'react'
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { parse } from '@babel/parser'
import { stripTypeScriptTypes } from 'node:module'
import { createRuntimeAwake } from '../../main/runtime-awake'
import { useLearningSessionAwake, LEARNING_AWAKE_WARNING } from './learningSessionAwake'
import { createVerifiedQuit } from '../../main/verified-quit'

const root = resolve('.')
const read = (p: string) => readFileSync(resolve(root,p),'utf8')
let syncFunction: string
let qualificationFunction: string
let quitFunction: string
beforeAll(async () => {
  const preload = read('src/preload/index.ts')
  const node = parse(preload,{sourceType:'module',plugins:['typescript']}).program.body.find((n:any)=>n.type==='FunctionDeclaration'&&n.id.name==='awakeSync')!
  syncFunction = stripTypeScriptTypes(preload.slice(node.start!,node.end!))
  const parent = read('src/renderer/src/ParentCenter.tsx')
  const ast=parse(parent,{sourceType:'module',plugins:['typescript','jsx']})
  let found: any
  const walk=(n:any):void=>{if(!n||typeof n!=='object')return;if(n.type==='VariableDeclarator'&&n.id?.name==='startQualification')found=n.init;for(const [k,v]of Object.entries(n))if(k!=='loc'&&k!=='tokens'&&typeof v==='object'){if(Array.isArray(v))v.forEach(walk);else walk(v)}}
  walk(ast)
  qualificationFunction=stripTypeScriptTypes(`globalThis.startQualification=${parent.slice(found.start,found.end)}`)
  const main=read('src/main/index.ts');const mast=parse(main,{sourceType:'module',plugins:['typescript']});let confirmed:any,resume:any
  const scan=(n:any):void=>{if(!n||typeof n!=='object')return;if(n.type==='CallExpression'&&n.callee?.name==='createVerifiedQuit')confirmed=n.arguments[1];if(n.type==='FunctionDeclaration'&&n.id?.name==='resumeDraftPreparation')resume=n;for(const v of Object.values(n))if(v&&typeof v==='object'){if(Array.isArray(v))v.forEach(scan);else scan(v)}}
  scan(mast);expect(resume,'actual preparation resume closure').toBeDefined();quitFunction=stripTypeScriptTypes(`${main.slice(resume.start,resume.end)};globalThis.confirmed=${main.slice(confirmed.start,confirmed.end)}`)
})
afterEach(()=>{cleanup();delete window.desktop;vi.restoreAllMocks()})

function fixture() {
  const started=new Set<number>();let next=0
  const power={start:vi.fn(()=>{const id=++next;started.add(id);return id}),isStarted:vi.fn((id:number)=>started.has(id)),stop:vi.fn((id:number)=>{started.delete(id)})}
  const makeWindow=(id:number)=>({isDestroyed:()=>false,webContents:Object.assign(new EventEmitter(),{id,mainFrame:{id},isDestroyed:()=>false})})
  const first=makeWindow(1);let current=first
  const failure=vi.fn();const runtime=createRuntimeAwake(power,()=>current,failure)
  const requests:any[]=[]
  const send=(request:any,sender= current.webContents,frame:unknown=sender.mainFrame)=>{
    let delivered:any,writes=0;const event={sender,senderFrame:frame}
    Object.defineProperty(event,'returnValue',{set:(value:unknown)=>{writes++;if(writes===1)delivered=value}})
    runtime.handle(event,request);expect(writes).toBe(1);return delivered
  }
  const context:any={ipcRenderer:{sendSync:(channel:string,request:any)=>{expect(channel).toBe('runtime:set-awake-owner');requests.push(request);return send(request)}}}
  runInNewContext(syncFunction,context)
  const desktop={
    setLearningSessionAwake:(sessionId:string,leaseId:string,enabled:boolean)=>context.awakeSync('learning',leaseId,sessionId,enabled),
    setDeviceTestAwake:async(enabled:boolean,leaseId:string)=>context.awakeSync('qualification',leaseId,null,enabled),
  }
  return {power,started,first,makeWindow,setCurrent:(w:typeof first)=>{current=w},runtime,send,desktop,requests,failure}
}
const id=()=>crypto.randomUUID()
const request=(leaseId=id(),kind:'learning'|'qualification'='learning',enabled=true)=>({kind,leaseId,sessionId:kind==='learning'?'session-1':null,enabled})

describe('actual Main owner and synchronous preload path',()=>{
  it('holds both owners until the last exact nonce releases; duplicate acquire is idempotent',async()=>{
    const f=fixture(),learning=id(),qualification=id()
    expect(f.desktop.setLearningSessionAwake('session-1',learning,true)).toEqual({active:true,held:true})
    f.desktop.setLearningSessionAwake('session-1',learning,true)
    await f.desktop.setDeviceTestAwake(true,qualification)
    expect(f.power.start).toHaveBeenCalledTimes(1)
    expect(await f.desktop.setDeviceTestAwake(false,qualification)).toEqual({active:true,held:false})
    expect(f.power.stop).not.toHaveBeenCalled()
    f.desktop.setLearningSessionAwake('session-1',learning,false)
    expect(f.started.size).toBe(0)
  })
  it('old qualification finally cannot release a newer run',async()=>{
    const f=fixture(),old=id(),fresh=id()
    await f.desktop.setDeviceTestAwake(true,old);await f.desktop.setDeviceTestAwake(false,old)
    await f.desktop.setDeviceTestAwake(true,fresh);await f.desktop.setDeviceTestAwake(false,old)
    expect(f.started.size).toBe(1)
    await f.desktop.setDeviceTestAwake(false,fresh);expect(f.started.size).toBe(0)
  })
  it('rejects foreign windows, subframes and retired main frames before native calls',()=>{
    const f=fixture(),other=f.makeWindow(2),old=f.first.webContents.mainFrame
    expect(f.send(request(),other.webContents).ok).toBe(false)
    expect(f.send(request(),f.first.webContents,{}).ok).toBe(false)
    f.first.webContents.mainFrame={id:8}
    expect(f.send(request(),f.first.webContents,old).ok).toBe(false)
    expect(f.power.start).not.toHaveBeenCalled()
  })
  it.each([null,{},request().leaseId,{...request(),enabled:'true'},{...request(),extra:'private'}, {...request(),kind:'foreign'}, {...request(),sessionId:''}])('always returns a finite synchronous rejection for invalid payload %#',(raw)=>{
    const f=fixture();expect(f.send(raw)).toEqual({ok:false,code:'RUNTIME_AWAKE_UNCONFIRMED'});expect(f.started.size).toBe(0)
  })
  it('limits each window to eight nonces, while duplicate acquire and exact release remain available',()=>{
    const f=fixture(),requests=Array.from({length:8},()=>request())
    requests.forEach(r=>expect(f.send(r).ok).toBe(true));const ninth=request()
    expect(f.send(ninth)).toEqual({ok:false,code:'RUNTIME_AWAKE_UNCONFIRMED'})
    expect(f.send(requests[0]).ok).toBe(true);expect(f.started.size).toBe(1)
    expect(f.send({...requests[0],enabled:false}).ok).toBe(true)
    expect(f.send(ninth).ok).toBe(true)
    for(const r of [...requests.slice(1),ninth])expect(f.send({...r,enabled:false}).ok).toBe(true)
    expect(f.started.size).toBe(0)
  })
  it('power start exception responds synchronously, rolls back that owner and permits retry',()=>{
    const f=fixture(),r=request();f.power.start.mockImplementationOnce(()=>{throw Error('private OS detail')})
    expect(f.send(r)).toEqual({ok:false,code:'RUNTIME_AWAKE_UNCONFIRMED'})
    expect(f.send({...r,enabled:false}).value).toEqual({active:false,held:false})
    expect(f.send(r).ok).toBe(true)
  })
  it('isStarted failure after start rolls back and closes the newly acquired native blocker',()=>{
    const f=fixture();f.power.isStarted.mockImplementationOnce(()=>{throw Error('private')})
    expect(f.send(request()).ok).toBe(false);expect(f.started.size).toBe(0)
  })
  it('stop failure is not acknowledged as closed and the same release retries',()=>{
    const f=fixture(),r=request();f.send(r);f.power.stop.mockImplementationOnce(()=>{throw Error('private')})
    expect(f.send({...r,enabled:false})).toEqual({ok:false,code:'RUNTIME_AWAKE_UNCONFIRMED'})
    expect(f.started.size).toBe(1)
    expect(f.send({...r,enabled:false}).value).toEqual({active:false,held:false})
    expect(f.started.size).toBe(0)
  })
  it('native stop without actual release remains unconfirmed; lifecycle retry can clear it',()=>{
    const f=fixture(),r=request();f.send(r);f.power.stop.mockImplementationOnce(()=>{})
    expect(f.send({...r,enabled:false}).ok).toBe(false);expect(f.started.size).toBe(1)
    f.runtime.clear();expect(f.started.size).toBe(0)
  })
  it('preload rejects malformed replies with a finite error',()=>{
    const context:any={ipcRenderer:{sendSync:()=>({ok:true,value:{active:false,held:true,private:'data'}})}}
    runInNewContext(syncFunction,context)
    expect(()=>context.awakeSync('learning',id(),'session-1',true)).toThrow('RUNTIME_AWAKE_UNCONFIRMED')
  })
  it('changing a nonce scope cannot release the existing exact owner',()=>{
    const f=fixture(),r=request();f.send(r)
    expect(f.send({...r,sessionId:'other',enabled:false}).ok).toBe(false)
    expect(f.started.size).toBe(1);expect(f.send({...r,enabled:false}).ok).toBe(true)
  })
})

describe('actual registered lifecycle callbacks and verified quit callback',()=>{
  it('same-document and subframe navigation retain; committed full top-frame navigation releases',()=>{
    const f=fixture();f.runtime.watchWindow(f.first);f.send(request())
    f.first.webContents.emit('did-start-navigation',{},'public',true,true)
    f.first.webContents.emit('did-start-navigation',{},'public',false,false)
    expect(f.started.size).toBe(1)
    f.first.webContents.emit('did-start-navigation',{},'public',false,true)
    expect(f.started.size).toBe(1)
    f.first.webContents.emit('did-navigate',{},'public')
    expect(f.started.size).toBe(0)
  })
  it.each(['render-process-gone','destroyed'])('%s releases using the captured window ID',(event)=>{
    const f=fixture();f.runtime.watchWindow(f.first);f.send(request())
    const emitter=f.first.webContents
    Object.defineProperty(f.first,'webContents',{get:()=>{throw Error('destroyed object')}})
    expect(()=>emitter.emit(event)).not.toThrow();expect(f.started.size).toBe(0)
  })
  it('retiring frame cannot reacquire while pending; commit clears old owners and admits the new frame',()=>{
    const f=fixture();f.runtime.watchWindow(f.first);const old=request();f.send(old)
    f.first.webContents.emit('did-start-navigation',{},'public',false,true)
    expect(f.send(request()).ok).toBe(false)
    expect(f.send({...old,enabled:false}).ok).toBe(true)
    f.first.webContents.mainFrame={id:5};f.first.webContents.emit('did-navigate',{},'public')
    expect(f.started.size).toBe(0);expect(f.send(request()).ok).toBe(true)
  })
  it.each(['did-fail-load','did-fail-provisional-load'])('%s preserves still-active original owner and permits retry',(event)=>{
    const f=fixture();f.runtime.watchWindow(f.first);f.send(request())
    f.first.webContents.emit('did-start-navigation',{},'public',false,true)
    f.first.webContents.emit(event,{},-3,'synthetic failure','public',true,1,1)
    expect(f.started.size).toBe(1);expect(f.send(request()).ok).toBe(true)
    f.first.webContents.emit('did-navigate',{},'public');expect(f.started.size).toBe(0)
  })
  it('old window crash only clears its owners, not the new primary window',()=>{
    const f=fixture();f.runtime.watchWindow(f.first);f.send(request());const newer=f.makeWindow(2);f.setCurrent(newer);f.runtime.watchWindow(newer);f.send(request())
    f.first.webContents.emit('render-process-gone');expect(f.started.size).toBe(1)
    newer.webContents.emit('destroyed');expect(f.started.size).toBe(0)
  })
  it('verified quit only clears after controlled media confirmed; power failures do not block original service stop',async()=>{
    const f=fixture();f.send(request());const calls:string[]=[];const preparation={quiesce:vi.fn(async()=>{calls.push('draft-quiesce')}),resume:vi.fn()},recovery={resume:vi.fn()};const context:any={runtimeAwake:f.runtime,diagnosticRecorder:{record:()=>Promise.resolve(),flush:async()=>{calls.push('flush')}},pilotTestEmail:{stop:()=>calls.push('pilot')},localService:{stop:async()=>{calls.push('service')}},app:{quit:()=>calls.push('quit')},quitAfterServiceStop:false,draftQuitDraining:true,draftPreparation:preparation,draftRecovery:recovery}
    runInNewContext(quitFunction,context)
    let closed=false;const quit=createVerifiedQuit([async()=>({closed})],context.confirmed,async()=>{calls.push('blocked')})
    await quit();expect(f.started.size).toBe(1);expect(calls).toEqual(['blocked']);expect(preparation.quiesce).not.toHaveBeenCalled()
    closed=true;f.power.stop.mockImplementationOnce(()=>{throw Error('private')});await quit()
    expect(calls).toEqual(['blocked','pilot','draft-quiesce','service','flush','quit']);expect(preparation.quiesce).toHaveBeenCalledOnce();expect(preparation.resume).not.toHaveBeenCalled();expect(recovery.resume).not.toHaveBeenCalled();expect(context.quitAfterServiceStop).toBe(true);expect(f.failure).toHaveBeenCalled()
    f.runtime.clear();expect(f.started.size).toBe(0)
  })
})

describe('actual renderer learning hook and qualification function',()=>{
  const active={session_id:'session-1',status:'active',state:'classroom_recall'}
  it('quiet active learning and intermediate workflow completion retain; pause/end release; restore reacquires',()=>{
    const f=fixture();window.desktop=f.desktop as any
    const hook=renderHook(({session})=>useLearningSessionAwake(session),{initialProps:{session:active}})
    expect(f.started.size).toBe(1);const initial=f.requests[0].leaseId
    hook.rerender({session:{...active,state:'homework'}});expect(f.requests).toHaveLength(1)
    hook.rerender({session:{...active,status:'paused'}});expect(f.started.size).toBe(0)
    hook.rerender({session:active});expect(f.started.size).toBe(1);expect(f.requests[2].leaseId).not.toBe(initial)
    hook.rerender({session:{...active,state:'completed'}});expect(f.started.size).toBe(0)
    hook.rerender({session:{...active,status:'completed',state:'completed'}});expect(f.started.size).toBe(0)
  })
  it('StrictMode cleanup and a late old release cannot stop the current mount',()=>{
    const f=fixture();window.desktop=f.desktop as any
    const hook=renderHook(()=>useLearningSessionAwake(active),{reactStrictMode:true})
    expect(f.requests.map(r=>r.enabled)).toEqual([true,false,true])
    expect(f.requests[0].leaseId).not.toBe(f.requests[2].leaseId)
    f.send({...f.requests[0],enabled:false});expect(f.started.size).toBe(1)
    hook.unmount();expect(f.started.size).toBe(0)
  })
  it('parent/home unmount releases and active workspace remount reacquires',()=>{
    const f=fixture();window.desktop=f.desktop as any
    const first=renderHook(()=>useLearningSessionAwake(active));first.unmount();expect(f.started.size).toBe(0)
    const restored=renderHook(()=>useLearningSessionAwake(active));expect(f.started.size).toBe(1);restored.unmount();expect(f.started.size).toBe(0)
  })
  it('a power error gives a friendly finite warning and does not prevent learning UI mount',()=>{
    const f=fixture();window.desktop=f.desktop as any;f.power.start.mockImplementationOnce(()=>{throw Error('private native error with nonce')})
    const hook=renderHook(()=>useLearningSessionAwake(active));expect(hook.result.current).toBe(LEARNING_AWAKE_WARNING);expect(hook.result.current).not.toContain('IPC');hook.unmount()
  })
  it('UUID failure becomes a friendly warning without acquiring or releasing an unknown nonce',()=>{
    const f=fixture();window.desktop=f.desktop as any
    vi.spyOn(crypto,'randomUUID').mockImplementationOnce(()=>{throw Error('UUID unavailable')})
    const hook=renderHook(()=>useLearningSessionAwake(active))
    expect(hook.result.current).toBe(LEARNING_AWAKE_WARNING);hook.unmount();expect(f.requests).toHaveLength(0)
  })
  it('qualification UUID failure still clears busy state and never releases an unknown nonce',async()=>{
    const f=fixture(),qualifying=vi.fn(),message=vi.fn();const context:any={crypto:{randomUUID:()=>{throw Error('UUID unavailable')}},AbortController,window:{desktop:f.desktop},setQualifying:qualifying,setMessage:message,setProgress:vi.fn(),qualificationAbort:{current:null},qualificationMode:'quick_dual',bindings:{},localApi:{startDeviceQualification:vi.fn()},ApiError:class extends Error {}}
    runInNewContext(qualificationFunction,context);await context.startQualification()
    expect(qualifying.mock.calls.map(row=>row[0])).toEqual([true,false]);expect(context.qualificationAbort.current).toBeNull();expect(f.requests).toHaveLength(0);expect(context.localApi.startDeviceQualification).not.toHaveBeenCalled();expect(message).toHaveBeenLastCalledWith('设备验收未能启动或保存结果。')
  })
  it('actual qualification success/finally releases its own nonce while learning remains held',async()=>{
    const f=fixture();window.desktop=f.desktop as any;const learning=id();f.desktop.setLearningSessionAwake('session-1',learning,true)
    const context:any={crypto,AbortController,window:{desktop:f.desktop},setQualifying:vi.fn(),setMessage:vi.fn(),setProgress:vi.fn(),qualificationAbort:{current:null},qualificationMode:'quick_dual',bindings:{},localApi:{startDeviceQualification:async()=>({run_id:'run',expected_seconds:60,completion_token:'public-synthetic'}),finishDeviceQualification:async()=>({status:'cancelled'}),deviceQualification:async()=>({})},runVideoQualification:async()=>({outcome:'cancelled',metrics:{}}),setQualification:vi.fn(),mergeQualificationResult:vi.fn(),ApiError:class extends Error {}}
    runInNewContext(qualificationFunction,context);await context.startQualification()
    const q=f.requests.filter(r=>r.kind==='qualification');expect(q.map(r=>r.enabled)).toEqual([true,false]);expect(q[0].leaseId).toBe(q[1].leaseId);expect(f.started.size).toBe(1)
    f.desktop.setLearningSessionAwake('session-1',learning,false);expect(f.started.size).toBe(0)
  })
  it('actual qualification exception still releases without exposing native details to the parent',async()=>{
    const f=fixture();const message=vi.fn();const context:any={crypto,AbortController,window:{desktop:f.desktop},setQualifying:vi.fn(),setMessage:message,setProgress:vi.fn(),qualificationAbort:{current:null},qualificationMode:'quick_dual',bindings:{},localApi:{startDeviceQualification:async()=>{throw Error('private')}},runVideoQualification:vi.fn(),setQualification:vi.fn(),mergeQualificationResult:vi.fn(),ApiError:class extends Error {}}
    runInNewContext(qualificationFunction,context);await context.startQualification()
    expect(f.started.size).toBe(0);expect(message).toHaveBeenLastCalledWith('设备验收未能启动或保存结果。')
  })
  it('real source wires the checked handler, primary window watch, session hook and finite visible warning',()=>{
    const main=read('src/main/index.ts'),workspace=read('src/renderer/src/SessionWorkspace.tsx')
    expect(main).toContain("ipcMain.on('runtime:set-awake-owner', (event, request: unknown) => runtimeAwake.handle(event, request))")
    expect(main).toContain('runtimeAwake.watchWindow(mainWindow)');expect(main).not.toContain('deviceQualificationPowerBlocker')
    expect(workspace).toContain('useLearningSessionAwake(workspace.session)');expect(workspace).toContain('role="status">{awakeWarning}')
  })
})
