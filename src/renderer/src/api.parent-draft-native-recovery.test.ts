import { afterEach,expect,it,vi } from 'vitest'
import { createParentDraftRecoveryPort,localApi,resetRuntimeCache } from './api'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import type { ScopedParentDraftRecoveryPort } from './parentDraftRecoveryPort'
import type { ParentDraftRecoveryPort,ParentDraftRecoverySnapshot } from '../../shared/parentDraftRecovery'
afterEach(()=>{delete window.desktop;resetRuntimeCache();vi.unstubAllGlobals()})
const prepared={leaseId:'opaque-test-lease',localRevision:4,hasRecovery:true,protection:'native_encrypted'as const}
const snapshot:ParentDraftRecoverySnapshot={schema:1,childId:'a',scope:'capture',acknowledged:null,latest:null,pending:null}
function fixture(){
 let serial=0
 const native={prepareParentDraftRecovery:vi.fn(async()=>prepared),readParentDraftRecovery:vi.fn(async()=>({...prepared,snapshot})),checkpointParentDraftRecovery:vi.fn((_lease:string,sequence:number)=>({stored:true as const,localRevision:5,sequence})),revokeParentDraftRecovery:vi.fn(),discardParentDraftRecovery:vi.fn(async()=>({...prepared,hasRecovery:false}))}
 window.desktop={...native,getRuntimeConfig:vi.fn(async()=>({status:'ready',localApiUrl:'http://127.0.0.1:19001',sessionToken:'SYNTHETIC-SESSION'}))}as unknown as NonNullable<Window['desktop']>
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>new Response(url.endsWith('/v1/parent/unlock')?JSON.stringify({parent_token:`SYNTHETIC-PARENT-${++serial}`}):'{}',{headers:{'content-type':'application/json'}})))
 return native
}
const port=()=>createParentDraftRecoveryPort('a','capture')as ScopedParentDraftRecoveryPort
it('passes the private current token only to native authorization methods and leaves checkpoint snapshot exact',async()=>{
 const native=fixture();await localApi.parentUnlock('2468',false);const p=port();await p.prepare();await p.read(4);p.checkpoint(prepared.leaseId,1,snapshot);await p.discard(4,true)
 expect(native.prepareParentDraftRecovery.mock.calls[0]).toEqual(['a','capture','SYNTHETIC-PARENT-1']);expect(native.readParentDraftRecovery.mock.calls[0]).toEqual(['a','capture','SYNTHETIC-PARENT-1',4]);expect(native.checkpointParentDraftRecovery.mock.calls[0]).toEqual([prepared.leaseId,1,snapshot]);expect(native.discardParentDraftRecovery.mock.calls[0]).toEqual(['a','capture','SYNTHETIC-PARENT-1',4,true])
})
it('refuses reads and prepares before a fresh PIN and has no memory durability fallback',async()=>{
 const native=fixture();await expect(port().prepare()).rejects.toThrow();await expect(port().read(4)).rejects.toThrow();expect(native.prepareParentDraftRecovery).not.toHaveBeenCalled();expect(native.readParentDraftRecovery).not.toHaveBeenCalled()
})
it('discards a late native prepare after authority changes without revoking a newer grant',async()=>{
 const native=fixture();await localApi.parentUnlock('2468',false);let finish!:(v:typeof prepared)=>void;native.prepareParentDraftRecovery.mockImplementation(()=>new Promise(resolve=>{finish=resolve}));const pending=port().prepare();changeParentDraftAuthority('locked');finish(prepared)
 await expect(pending).rejects.toThrow();expect(native.revokeParentDraftRecovery).not.toHaveBeenCalled()
})
it('discards a late raw reply after a new PIN without exposing it to the caller',async()=>{
 const native=fixture();await localApi.parentUnlock('2468',false);let finish!:(v:typeof prepared&{snapshot:ParentDraftRecoverySnapshot})=>void;native.readParentDraftRecovery.mockImplementation(()=>new Promise(resolve=>{finish=resolve}));const pending=port().read(4);await localApi.parentUnlock('1357',false);finish({...prepared,snapshot});await expect(pending).rejects.toThrow();expect(native.revokeParentDraftRecovery).not.toHaveBeenCalled()
})
it('allows only provided checkpoint bytes during the lock flush then invokes native revoke synchronously',async()=>{
 const native=fixture();await localApi.parentUnlock('2468',false);const p=port();changeParentDraftAuthority('locked');expect(p.checkpoint(prepared.leaseId,1,snapshot)).toEqual({stored:true,sequence:1,localRevision:5});p.revoke();expect(native.revokeParentDraftRecovery).toHaveBeenCalledTimes(1);await expect(p.read(4)).rejects.toThrow()
})
it('fails visibly when this product runtime has no complete native bridge',async()=>{
 fixture();await localApi.parentUnlock('2468',false);delete(window.desktop as unknown as Partial<ParentDraftRecoveryPort>).checkpointParentDraftRecovery;await expect(port().prepare()).rejects.toThrow('不支持加密草稿保护')
})
