import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
const old = { status:'ready' as const, localApiUrl:'http://127.0.0.1:19021', sessionToken:'PUBLIC-SESSION-A' }
const fresh = { status:'ready' as const, localApiUrl:'http://127.0.0.1:19022', sessionToken:'PUBLIC-SESSION-B' }
let current: { status:'ready'|'starting'|'error'; localApiUrl:string;sessionToken:string }
let desktop: { getRuntimeConfig: ReturnType<typeof vi.fn> }, events: CustomEvent[], listen: (e: Event)=>void
const json = (value:unknown={}, status=200) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}})
beforeEach(()=>{
 resetRuntimeCache();current={...old};desktop={getRuntimeConfig:vi.fn(async()=>current)};window.desktop=desktop as unknown as NonNullable<Window['desktop']>
 events=[];listen=e=>events.push(e as CustomEvent);window.addEventListener('hsc-runtime-restarted',listen)
})
afterEach(()=>{window.removeEventListener('hsc-runtime-restarted',listen);delete window.desktop;resetRuntimeCache();vi.unstubAllGlobals()})
async function prime(){vi.stubGlobal('fetch',vi.fn(async()=>json()));await localApi.bootstrap()}
it('recovers the next new request after backend-only port/token rotation, without replaying the failed write',async()=>{
 await prime();current={...fresh};const fetcher=vi.fn(async(url:string)=>{if(url.startsWith(old.localApiUrl))throw new TypeError('public offline');return json({fresh:true})});vi.stubGlobal('fetch',fetcher)
 await expect(localApi.startSession('child-a')).rejects.toMatchObject({code:'LOCAL_SERVICE_OFFLINE'})
 expect(fetcher).toHaveBeenCalledTimes(1);expect(fetcher.mock.calls[0][0]).toBe(old.localApiUrl+'/v1/sessions');expect(events).toHaveLength(1)
 await expect(localApi.bootstrap()).resolves.toEqual({fresh:true});expect(fetcher.mock.lastCall?.[0]).toBe(fresh.localApiUrl+'/v1/bootstrap')
 const options=fetcher.mock.lastCall as unknown as [string,RequestInit];expect(new Headers(options[1].headers).get('X-HSC-Session')).toBe(fresh.sessionToken)
 expect(fetcher.mock.calls.filter(([url])=>url.endsWith('/v1/sessions'))).toHaveLength(1)
})
it('shares one IPC and recovery event across concurrent failures from the same captured epoch',async()=>{
 await prime();let finish!:(value:typeof fresh)=>void;desktop.getRuntimeConfig.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}));vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}))
 const a=localApi.bootstrap().catch(e=>e),b=localApi.startSession('child-a').catch(e=>e)
 await vi.waitFor(()=>expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(2));finish(fresh)
 expect((await a).code).toBe('LOCAL_SERVICE_OFFLINE');expect((await b).code).toBe('LOCAL_SERVICE_OFFLINE');expect(events).toHaveLength(1);expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(2)
})
it('a late old failure cannot clear a newer recovered identity or issue another IPC',async()=>{
 await prime();let fail!:()=>void;let calls=0;const fetcher=vi.fn(async(url:string)=>{
  if(url.startsWith(old.localApiUrl)){if(++calls===1)return new Promise<Response>((_resolve,reject)=>{fail=()=>reject(new TypeError('public late'))});throw new TypeError('public offline')}
  return json({fresh:true})
 });vi.stubGlobal('fetch',fetcher);const late=localApi.bootstrap().catch(e=>e);await vi.waitFor(()=>expect(fail).toBeTypeOf('function'));current={...fresh};await localApi.startSession('child-a').catch(()=>undefined);await localApi.bootstrap();const reads=desktop.getRuntimeConfig.mock.calls.length;fail();await late;await localApi.bootstrap()
 expect(events).toHaveLength(1);expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(reads);expect(fetcher.mock.lastCall?.[0]).toBe(fresh.localApiUrl+'/v1/bootstrap')
})
it('a late successful old JSON body is rejected after the runtime fence changes',async()=>{
 await prime();let release!:(v:unknown)=>void;let n=0;vi.stubGlobal('fetch',vi.fn(async(url:string)=>{
  if(url.startsWith(old.localApiUrl)){if(++n===1)return {ok:true,json:()=>new Promise(resolve=>{release=resolve})} as Response;throw new TypeError('public offline')}
  return json()
 }));const late=localApi.bootstrap().catch(e=>e);await vi.waitFor(()=>expect(release).toBeTypeOf('function'));current={...fresh};await localApi.startSession('child-a').catch(()=>undefined);release({stale:true});expect(await late).toMatchObject({code:'LOCAL_SERVICE_CHANGED'})
})
it('checks only exact local-session rejection for same-port token rotation and never replays it',async()=>{
 await prime();current={...fresh,localApiUrl:old.localApiUrl};let first=true;const fetcher=vi.fn(async()=>{if(first){first=false;return json({code:'LOCAL_SESSION_REQUIRED',message:'public local session'},401)}return json({fresh:true})});vi.stubGlobal('fetch',fetcher)
 await expect(localApi.startSession('child-a')).rejects.toMatchObject({code:'LOCAL_SESSION_REQUIRED',status:401});expect(fetcher).toHaveBeenCalledTimes(1);expect(events).toHaveLength(1);await localApi.bootstrap();const call=fetcher.mock.lastCall as unknown as [string,RequestInit];expect(new Headers(call[1].headers).get('X-HSC-Session')).toBe(fresh.sessionToken)
})
it.each([401,409,503])('ordinary HTTP%s business failure does not query IPC or reset runtime',async status=>{
 await prime();current={...fresh};vi.stubGlobal('fetch',vi.fn(async()=>json({code:'PUBLIC_BUSINESS_REJECTION',message:'public error'},status)));await expect(localApi.bootstrap()).rejects.toMatchObject({status,code:'PUBLIC_BUSINESS_REJECTION'});expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(1);expect(events).toHaveLength(0)
})
it('AbortError is preserved without native IPC or a recovery event',async()=>{
 await prime();vi.stubGlobal('fetch',vi.fn(async()=>{throw new DOMException('public cancel','AbortError')}));await expect(localApi.bootstrap()).rejects.toMatchObject({name:'AbortError'});expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(1);expect(events).toHaveLength(0)
})
it('sameidentity transient offline failure retains the cache and its original error',async()=>{
 await prime();const fetcher=vi.fn(async()=>{throw new TypeError('public offline')});vi.stubGlobal('fetch',fetcher);await expect(localApi.bootstrap()).rejects.toMatchObject({code:'LOCAL_SERVICE_OFFLINE'});expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(2);expect(events).toHaveLength(0);vi.stubGlobal('fetch',vi.fn(async()=>json()));await localApi.bootstrap();expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(2)
})
it('native IPC unavailability does not overwrite the original transport failure',async()=>{
 await prime();desktop.getRuntimeConfig.mockRejectedValueOnce(Error('PUBLIC_NATIVE_UNAVAILABLE'));vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));await expect(localApi.startSession('child-a')).rejects.toMatchObject({code:'LOCAL_SERVICE_OFFLINE'});expect(events).toHaveLength(0)
})
it('observed starting runtime uses existing App recovery event rather than replaying the request',async()=>{
 await prime();current={...old,status:'starting'};vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));await expect(localApi.bootstrap()).rejects.toMatchObject({code:'LOCAL_SERVICE_OFFLINE'});expect(events).toHaveLength(1);expect(events[0].detail.error).toBeTypeOf('string');current={...fresh};vi.stubGlobal('fetch',vi.fn(async()=>json()));await localApi.bootstrap();expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(3)
})
it('a late IPC result cannot retire a newer explicit runtime reset and ready identity',async()=>{
 await prime();let finish!:(v:typeof fresh)=>void;desktop.getRuntimeConfig.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}));vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));const pending=localApi.bootstrap().catch(e=>e);await vi.waitFor(()=>expect(finish).toBeTypeOf('function'))
 resetRuntimeCache();current={...fresh};vi.stubGlobal('fetch',vi.fn(async()=>json({fresh:true})));await localApi.bootstrap();const reads=desktop.getRuntimeConfig.mock.calls.length;finish({...old});await pending;await localApi.bootstrap();expect(events).toHaveLength(0);expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(reads)
})
it('identity retirement clears the old parent grant through the existing authorization fence',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>json({parent_token:'PUBLIC-PARENT-A'})));await localApi.parentUnlock('2468',false);current={...fresh};vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));await localApi.bootstrap().catch(()=>undefined)
 const fetcher=vi.fn(async()=>json());vi.stubGlobal('fetch',fetcher);await localApi.parentToday();const call=fetcher.mock.lastCall as unknown as [string,RequestInit];expect(new Headers(call[1].headers).get('X-HSC-Parent')).toBe('');expect(new Headers(call[1].headers).get('X-HSC-Session')).toBe(fresh.sessionToken);expect(events).toHaveLength(1)
})
it('initial startup failure remains owned by the existing App recovery loop',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));await expect(localApi.bootstrap()).rejects.toMatchObject({code:'LOCAL_SERVICE_OFFLINE'});expect(desktop.getRuntimeConfig).toHaveBeenCalledTimes(1);expect(events).toHaveLength(0)
})
it('an unresponsive native identity check is bounded and its late result cannot trigger recovery',async()=>{
 await prime();vi.useFakeTimers();let finish!:(v:typeof fresh)=>void;desktop.getRuntimeConfig.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}));vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('public offline')}));const pending=localApi.startSession('child-a').catch(e=>e)
 await vi.advanceTimersByTimeAsync(2000);expect(await pending).toMatchObject({code:'LOCAL_SERVICE_OFFLINE'});finish(fresh);await Promise.resolve();expect(events).toHaveLength(0);vi.useRealTimers()
})
