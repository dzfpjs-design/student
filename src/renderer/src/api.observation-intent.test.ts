import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
const sid = 'synthetic-api-session', generation = 'a'.repeat(64)
const state = (revision = 0, status = 'paused') => ({ session_id: sid, child_id: 'synthetic-child', status: 'active', state: 'check_in', observation_status: status, observation_revision: revision, observation_generation: generation })
const response = (value: unknown) => new Response(JSON.stringify(value), {status:200, headers:{'Content-Type':'application/json'}})
const receipt = (body: Record<string, unknown>, effect='applied') => ({...state(2,'enabled'),observation_control:{...body,operation:'enabled',expected_revision:0,expected_generation:generation,result_revision:2,effect,applied:effect==='applied',fresh:effect==='applied'}})
let fetcher: ReturnType<typeof vi.fn>
beforeEach(() => { resetRuntimeCache(); Object.defineProperty(window, 'desktop', {value:undefined, configurable:true}); fetcher=vi.fn(); vi.stubGlobal('fetch',fetcher) })
afterEach(() => {resetRuntimeCache();vi.unstubAllGlobals()})

test('the actual API refuses a late enabled ACK after its synchronous privacy pause',async()=>{
 let release!: (value:Response)=>void, original!:Record<string,unknown>
 fetcher.mockImplementation((url:string, init?:RequestInit)=>{
  if(url.endsWith('/observation/resume')){original=JSON.parse(init!.body as string);return new Promise<Response>(r=>{release=r})}
  if(url.endsWith('/observation/pause'))return Promise.resolve(response(state(2,'paused')))
  return Promise.resolve(response({session:state()}))
 })
 await localApi.sessionWorkspace(sid)
 let captureOpens=0
 const originalResume=localApi.resumeObservation(sid).then(()=>{captureOpens++;return null},(error:unknown)=>error)
 await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
 await localApi.pauseObservation(sid)
 release(response(receipt(original)))
 const result=await originalResume
 expect(captureOpens).toBe(0)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_INTENT_CANCELLED'})
 expect(fetcher).toHaveBeenCalledTimes(3)
})

test('the actual API does not turn a duplicate immutable enabled receipt into a capture ACK',async()=>{
 fetcher.mockImplementation((url:string,init?:RequestInit)=>Promise.resolve(response(url.endsWith('/observation/resume')?receipt(JSON.parse(init!.body as string),'replayed'):{session:state()})))
 await localApi.sessionWorkspace(sid)
 let captureOpens=0
 const result=await localApi.resumeObservation(sid).then(()=>{captureOpens++;return null},(error:unknown)=>error)
 expect(captureOpens).toBe(0)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_ACK_NOT_FRESH'})
 expect(fetcher).toHaveBeenCalledTimes(2)
})

test('the actual API blocks unknown revision without a fetch or hidden GET',async()=>{
 fetcher.mockResolvedValue(response(state(1,'enabled')))
 const result=await localApi.resumeObservation(sid).catch((error:unknown)=>error)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_STATE_UNCONFIRMED'})
 expect(fetcher).not.toHaveBeenCalled()
})

test('the actual API binds a new action to the same authoritative workspace version',async()=>{
 fetcher.mockImplementation((url:string,init?:RequestInit)=>Promise.resolve(response(url.endsWith('/observation/resume')?receipt(JSON.parse(init!.body as string)):{session:state()})))
 await localApi.sessionWorkspace(sid)
 await expect(localApi.resumeObservation(sid)).resolves.toMatchObject({observation_status:'enabled'})
 const body=JSON.parse(fetcher.mock.calls[1][1].body)
 expect(body).toMatchObject({expected_revision:0,expected_generation:generation})
 expect(body.client_action_id).toBeTypeOf('string')
 expect(fetcher).toHaveBeenCalledTimes(2)
})

test('the public workspace API rejects an old enabled snapshot after a newer paused workspace was delivered',async()=>{
 let release!: (value:Response)=>void, gets=0
 fetcher.mockImplementation(()=>{
  gets++
  if(gets===1)return new Promise<Response>(r=>{release=r})
  return Promise.resolve(response({session:state(1,'paused')}))
 })
 let enabledRenders=0
 const old=localApi.sessionWorkspace(sid).then((value:any)=>{if(value.session.observation_status==='enabled')enabledRenders++;return null},(error:unknown)=>error)
 await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
 await localApi.sessionWorkspace(sid)
 release(response({session:state(0,'enabled')}))
 const result=await old
 expect(enabledRenders).toBe(0)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_WORKSPACE_SUPERSEDED'})
 expect(fetcher).toHaveBeenCalledTimes(2)
})

for(const kind of ['local','portable'])test(`${kind} restore invalidates a pre-restore public workspace before it can render enabled`,async()=>{
 let release!: (value:Response)=>void
 fetcher.mockImplementation((url:string)=>url.includes('/backups/')
   ?Promise.resolve(response({status:'restored',integrity_status:'ok',recovery_point_backup_id:'synthetic-recovery'}))
   :new Promise<Response>(r=>{release=r}))
 let enabledRenders=0
 const old=localApi.sessionWorkspace(sid).then((value:any)=>{if(value.session.observation_status==='enabled')enabledRenders++;return null},(error:unknown)=>error)
 await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
 if(kind==='local')await localApi.restoreBackup('synthetic-backup','确认恢复')
 else await localApi.restorePortableBackup('synthetic-path','synthetic-not-secret','确认迁移恢复')
 release(response({session:state(0,'enabled')}))
 const result=await old
 expect(enabledRenders).toBe(0)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_SERVICE_CHANGED'})
 expect(fetcher).toHaveBeenCalledTimes(2)
})

for(const terminal of ['completed','cancelled'])test(`a higher-ticket stale active GET cannot supersede observed ${terminal} in the same generation`,async()=>{
 const releases: Array<(value:Response)=>void>=[]
 fetcher.mockImplementation(()=>new Promise<Response>(r=>releases.push(r)))
 const first=localApi.sessionWorkspace(sid)
 await vi.waitFor(()=>expect(releases).toHaveLength(1))
 let activeRenders=0
 const later=localApi.sessionWorkspace(sid).then((value:any)=>{if(value.session.status==='active')activeRenders++;return null},(error:unknown)=>error)
 await vi.waitFor(()=>expect(releases).toHaveLength(2))
 releases[0](response({session:{...state(0,'paused'),status:terminal}}));await first
 releases[1](response({session:state(0,'enabled')}))
 const result=await later
 expect(activeRenders).toBe(0)
 expect(result).toMatchObject({code:'OBSERVATION_CONTROL_WORKSPACE_SUPERSEDED'})
 expect(fetcher).toHaveBeenCalledTimes(2)
})
