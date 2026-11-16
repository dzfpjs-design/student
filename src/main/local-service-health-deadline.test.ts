import {EventEmitter} from 'node:events'
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest'
const fixture=vi.hoisted(()=>({children:[] as any[],ports:[] as Array<()=>void>,holdPort:false}))
vi.mock('electron',()=>({app:{getVersion:()=> '0.1.4',getAppPath:()=>process.cwd(),getPath:()=>'/public-synthetic-service',isPackaged:false}}))
vi.mock('node:net',()=>{const createServer=()=>{
  const server={once(){return server},listen(_port:number,_host:string,ready:()=>void){
    fixture.holdPort?fixture.ports.push(ready):ready();return server
  },address:()=>({port:32145}),close(done?:(error?:Error)=>void){done?.();return server}}
  return server
};return {createServer,default:{createServer}}})
vi.mock('node:child_process',()=>{const spawn=vi.fn((_file:string,_args:string[],options:{env:NodeJS.ProcessEnv})=>{
  const child=Object.assign(new EventEmitter(),{sessionToken:options.env.HSC_SESSION_TOKEN,exitCode:null as number|null,stdout:new EventEmitter(),stderr:new EventEmitter(),
    kill:vi.fn(()=>{child.exitCode=0;child.emit('exit', 0, null);child.emit('close', 0, null);return true})})
  fixture.children.push(child);return child
});return {spawn,default:{spawn}}})
import {LocalServiceManager,LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS,LOCAL_SERVICE_HEALTH_TIMEOUT_MS,LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS} from './local-service'
const managers:LocalServiceManager[]=[]
const flush=async()=>{for(let i=0;i<100;i++)await Promise.resolve()}
function manager(){const events:any[]=[];const value=new LocalServiceManager(event=>events.push({...event,at:performance.now()}));
  value.setDataEncryptionKey('public-synthetic-data-key');managers.push(value);return {value,events}}
function result(promise:Promise<void>){let state:any={status:'pending'};void promise.then(()=>{state={status:'resolved'}},error=>{state={status:'rejected',error}});return ()=>state}
function mockFetch(health:(init:RequestInit|undefined)=>Promise<Response>){return vi.spyOn(globalThis,'fetch').mockImplementation((url,init)=>{
  if(String(url).endsWith('/v1/runtime/shutdown')){const child=fixture.children.at(-1);if(child && child.exitCode===null){child.exitCode=0;child.emit('exit', 0, null);child.emit('close', 0, null)}return Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}))}
  return health(init)
})}
beforeEach(()=>{vi.useFakeTimers({toFake:['setTimeout','clearTimeout','Date','performance']});vi.stubEnv('HSC_LOCAL_AGENT_PYTHON',process.execPath);vi.stubEnv('HSC_RUNTIME_MODE','formal');vi.stubEnv('HSC_LOCAL_API_URL','');vi.stubEnv('HSC_SESSION_TOKEN','');fixture.children.length=0;fixture.ports.length=0;fixture.holdPort=false})
afterEach(async()=>{for(const value of managers.splice(0)){const stopping=value.stop();for(const ready of fixture.ports.splice(0))ready();await flush();await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+1000);await stopping}
  vi.restoreAllMocks();vi.unstubAllEnvs();vi.useRealTimers()})
describe('fixed local-service health deadline and startup ownership',()=>{
  it.each([
    { version: '0.1.0', status: 'ok' },
    { build_version: '0.1.3', build_component: 'home-study-local-agent' },
    { build_version: '0.1.4', build_component: 'different-component' },
    null,
  ])('does not mark an old, mismatched or malformed backend ready',async body=>{
    mockFetch(()=>Promise.resolve(new Response(JSON.stringify(body),{status:200})))
    const {value,events}=manager(),outcome=result(value.start());await flush()
    expect(outcome().status).toBe('rejected')
    expect(outcome().error.message).toContain('版本不一致')
    expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0)
  })
  it('accepts a same-version recovery-only backend so its restricted surface remains reachable',async()=>{
    mockFetch(()=>Promise.resolve(new Response(JSON.stringify({status:'recovery_only',normal_business_ready:false,
      build_version:'0.1.4',build_component:'home-study-local-agent'}),{status:200})))
    const {value}=manager(),outcome=result(value.start());await flush()
    expect(outcome().status).toBe('resolved')
  })
  it('keeps a stalled health body within the existing total deadline',async()=>{
    mockFetch(()=>Promise.resolve({ok:true,json:()=>new Promise(()=>{})} as unknown as Response))
    const {value,events}=manager(),outcome=result(value.start());await flush()
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_HEALTH_TIMEOUT_MS);await flush()
    expect(outcome().status).toBe('rejected')
    expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0)
  })
  it('bounds a fetch that ignores abort and preserves a recoverable fixed timeout',async()=>{
    const signals:AbortSignal[]=[];const elapsed:number[]=[];mockFetch(init=>{const signal=init!.signal as AbortSignal;signals.push(signal);const start=performance.now();signal?.addEventListener('abort',()=>elapsed.push(performance.now()-start),{once:true});return new Promise(()=>{})})
    const {value,events}=manager();const outcome=result(value.start());await flush();expect(fixture.children).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS)
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_HEALTH_TIMEOUT_MS-LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS);await flush()
    expect(outcome()).toMatchObject({status:'rejected',error:expect.any(Error)})
    expect(outcome().error.message).toBe('本地学习服务在60秒内没有就绪')
    expect(signals.every(signal=>signal?.aborted===true)).toBe(true)
    expect(elapsed.length).toBe(signals.length);expect(elapsed.every(ms=>ms<=5000)).toBe(true)
    expect(events.find(e=>e.event==='startup_failed').at).toBe(LOCAL_SERVICE_HEALTH_TIMEOUT_MS)
    expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0)
    expect(value.getRuntime()).toMatchObject({status:'starting',recoveryAttempt:1})
    expect(fixture.children[0].exitCode).toBe(0)
  })
  it('keeps slow failed requests inside the same monotonic sixty seconds despite a wall-clock rollback',async()=>{
    mockFetch(()=>new Promise(resolve=>setTimeout(()=>resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:503})),200)))
    const {value,events}=manager();const outcome=result(value.start());await flush();await vi.advanceTimersByTimeAsync(59000)
    vi.setSystemTime(Date.now()-86400000);await vi.advanceTimersByTimeAsync(1000);await flush()
    expect(outcome().status).toBe('rejected');expect(events.find(e=>e.event==='startup_failed').at).toBe(60000)
    expect(fixture.children[0].exitCode).toBe(0)
  })
  it('revokes pending health on stop and rejects a later successful response',async()=>{
    let resolveHealth!:(r:Response)=>void;let signal!:AbortSignal
    mockFetch(init=>{signal=init!.signal as AbortSignal;return new Promise(resolve=>{resolveHealth=resolve})})
    const {value,events}=manager();const outcome=result(value.start());await flush();await value.stop();await flush()
    resolveHealth(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}));await flush()
    expect(outcome().status).toBe('rejected');expect(value.getRuntime().status).toBe('stopped')
    expect(signal?.aborted).toBe(true)
    expect(events.filter(e=>e.event==='service_ready'||e.event==='recovery_scheduled')).toHaveLength(0)
  })
  it('does not mark an exited child ready and allows its actual recovery child to become ready',async()=>{
    let resolveOld!:(r:Response)=>void;let calls=0
    mockFetch(()=>++calls===1?new Promise(resolve=>{resolveOld=resolve}):Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})))
    const {value,events}=manager();const outcome=result(value.start());await flush();const old=fixture.children[0]
    old.exitCode=7;old.emit('exit', 7, null);old.emit('close', 7, null);resolveOld(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}));await flush()
    expect(outcome().status).toBe('rejected');expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1000);await flush();expect(fixture.children).toHaveLength(2)
    expect(value.getRuntime().status).toBe('ready');expect(events.filter(e=>e.event==='service_ready')).toHaveLength(1)
    old.emit('exit', 7, null);old.emit('close', 7, null);expect(value.getRuntime().status).toBe('ready')
    await value.stop();expect(fixture.children[1].exitCode).toBe(0)
  })
  it('rejects port allocation that finishes after stop without spawning an orphan',async()=>{
    fixture.holdPort=true;mockFetch(()=>Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})))
    const {value}=manager();const outcome=result(value.start());await flush();const stopping=value.stop()
    fixture.ports.shift()!();await stopping;await flush()
    expect(outcome().status).toBe('rejected');expect(fixture.children).toHaveLength(0);expect(value.getRuntime().status).toBe('stopped')
  })
  it('retains successful cold starts and a fresh restart after cancelling the old generation',async()=>{
    let resolveOld!:(r:Response)=>void;let calls=0
    mockFetch(()=>++calls===1?new Promise(resolve=>{resolveOld=resolve}):Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})))
    const {value,events}=manager();const old=result(value.start());await flush();await value.restart();await flush()
    resolveOld(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}));await flush()
    expect(old().status).toBe('rejected');expect(fixture.children).toHaveLength(2)
    expect(value.getRuntime().status).toBe('ready');expect(events.filter(e=>e.event==='service_ready')).toHaveLength(1)
    expect(JSON.stringify(events)).not.toContain(fixture.children[0].sessionToken)
  })
  it('permits a healthy response during the unchanged measured cold-start window',async()=>{
    mockFetch(()=>Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:performance.now()>=59500?200:503})))
    const {value,events}=manager();const outcome=result(value.start());await flush();await vi.advanceTimersByTimeAsync(59750)
    expect(outcome().status).toBe('resolved');expect(value.getRuntime().status).toBe('ready')
    expect(events.find(e=>e.event==='service_ready').at).toBe(59500)
  })
  it('cancels an external-runtime health request without leaking its header token or restoring ready',async()=>{
    vi.stubEnv('HSC_LOCAL_API_URL','http://127.0.0.1:32145');vi.stubEnv('HSC_SESSION_TOKEN','synthetic-header-secret')
    let resolveHealth!:(r:Response)=>void;mockFetch(()=>new Promise(resolve=>{resolveHealth=resolve}))
    const {value,events}=manager();const outcome=result(value.start());await flush();await value.stop();resolveHealth(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}));await flush()
    expect(outcome().status).toBe('rejected');expect(value.getRuntime().status).toBe('stopped')
    expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0);expect(JSON.stringify(events)).not.toContain('synthetic-header-secret')
  })
  it('keeps synchronous transport exceptions private in the existing owned-child recovery path',async()=>{
    mockFetch(()=>{throw Error('private transport synthetic-header-secret')})
    const {value,events}=manager();const outcome=result(value.start());await flush();await vi.advanceTimersByTimeAsync(60000)
    expect(outcome().status).toBe('rejected');expect(outcome().error.message).toBe('本地学习服务在60秒内没有就绪')
    expect(value.getRuntime()).toMatchObject({status:'starting',recoveryAttempt:1})
    expect(JSON.stringify(events)).not.toContain('synthetic-header-secret');expect(fixture.children[0].exitCode).toBe(0)
  })
  it('accepts an actual health response taking 750ms without confusing the poll interval with a request limit',async()=>{
    mockFetch(()=>new Promise(resolve=>setTimeout(()=>resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})),750)))
    const {value,events}=manager();const outcome=result(value.start());await flush();await vi.advanceTimersByTimeAsync(750)
    expect(outcome().status).toBe('resolved');expect(value.getRuntime().status).toBe('ready')
    expect(events.find(e=>e.event==='service_ready').at).toBe(750)
  })
  it('rejects a 750ms response that would exceed the remaining total deadline and never marks its late success ready',async()=>{
    let lateSignal!:AbortSignal
    mockFetch(init=>{
      if(performance.now()<59500)return Promise.resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:503}))
      lateSignal=init!.signal as AbortSignal
      return new Promise(resolve=>setTimeout(()=>resolve(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})),750))
    })
    const {value,events}=manager();const outcome=result(value.start());await flush();await vi.advanceTimersByTimeAsync(60000)
    expect(outcome().status).toBe('rejected');expect(outcome().error.message).toBe('本地学习服务在60秒内没有就绪')
    expect(events.find(e=>e.event==='startup_failed').at).toBe(60000);expect(lateSignal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(250)
    expect(events.filter(e=>e.event==='service_ready')).toHaveLength(0);expect(fixture.children[0].exitCode).toBe(0)
  })
})

describe('bounded graceful closure of the same original child',()=>{
  function liveOriginal(){
    const requests=vi.spyOn(globalThis,'fetch').mockImplementation(async()=>new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}))
    const actual=manager();return {...actual,requests}
  }
  function shutdownCalls(requests:any){return requests.mock.calls.filter((call:any[])=>String(call[0]).endsWith('/v1/runtime/shutdown'))}
  it('allows a normal 3.3s close without force or a second shutdown request',async()=>{
    const {value,events,requests}=liveOriginal();await value.start();const child=fixture.children[0]
    const first=result(value.stop()),second=result(value.stop());await flush()
    setTimeout(()=>{child.exitCode=0;child.emit('exit',0,null);child.emit('close',0,null)},3300)
    await vi.advanceTimersByTimeAsync(3299);expect(first().status).toBe('pending');expect(child.kill).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1);await flush()
    expect(first().status).toBe('resolved');expect(second().status).toBe('resolved');expect(shutdownCalls(requests)).toHaveLength(1)
    expect(child.kill).not.toHaveBeenCalled();expect(events.filter(e=>e.event==='force_stop')).toHaveLength(0);expect(value.getRuntime().status).toBe('stopped')
  })
  it('forces at the finite grace boundary once but still waits for original stdio close',async()=>{
    const {value,events,requests}=liveOriginal();await value.start();const child=fixture.children[0]
    child.kill.mockImplementation(()=>{child.exitCode=null;child.emit('exit',null,'SIGKILL');return true})
    const first=result(value.stop()),second=result(value.stop());await flush()
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS-1)
    expect(child.kill).not.toHaveBeenCalled();expect(first().status).toBe('pending')
    await vi.advanceTimersByTimeAsync(1);await flush()
    expect(child.kill).toHaveBeenCalledTimes(1);expect(child.kill).toHaveBeenCalledWith('SIGKILL');expect(first().status).toBe('pending')
    expect(value.getRuntime().status).not.toBe('stopped');expect(events.filter(e=>e.event==='force_stop')).toHaveLength(1)
    child.emit('close',null,'SIGKILL');await flush()
    expect(first().status).toBe('resolved');expect(second().status).toBe('resolved');expect(shutdownCalls(requests)).toHaveLength(1)
    expect(fixture.children).toHaveLength(1);expect(value.getRuntime().status).toBe('stopped')
  })
  it('retains an unknown original child across retries without resending or force repetition',async()=>{
    const {value,events,requests}=liveOriginal();await value.start();const child=fixture.children[0]
    child.kill.mockImplementation(()=>true)
    const first=result(value.stop());await flush()
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+1000);await flush()
    expect(first()).toMatchObject({status:'rejected',error:expect.any(Error)});expect(first().error.message).toBe('尚未确认后台保存并退出，请稍后重试。')
    expect(value.getRuntime().status).toBe('error');expect((value as any).child).toBe(child)
    const retry=result(value.stop());await flush();await vi.advanceTimersByTimeAsync(1000);await flush()
    expect(retry().status).toBe('rejected');expect(shutdownCalls(requests)).toHaveLength(1);expect(child.kill).toHaveBeenCalledTimes(1)
    child.exitCode=0;child.emit('exit',0,null)
    const final=result(value.stop());await flush();expect(final().status).toBe('pending');expect(value.getRuntime().status).not.toBe('stopped')
    child.emit('close',0,null);await flush()
    expect(final().status).toBe('resolved');expect(fixture.children).toHaveLength(1);expect(events.filter(e=>e.event==='force_stop')).toHaveLength(1)
    expect(value.getRuntime().status).toBe('stopped')
  })
})
