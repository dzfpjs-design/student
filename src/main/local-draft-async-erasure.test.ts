// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {mkdtempSync,realpathSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {LocalDraftPrivacy} from './local-draft-privacy'
import {ControlledLaunchIntents} from './controlled-launch-intent'
import {NativePrivacyCoordinator,canonical} from './native-privacy'
import {createHash,randomUUID} from 'node:crypto'
import type {NativePrivacyResponse} from '../shared/nativePrivacy'
const roots:string[]=[]
afterEach(()=>{for(const p of roots.splice(0))rmSync(p,{recursive:true,force:true})})
function fixture(scope='all_history'){
 const root=realpathSync(mkdtempSync(join(tmpdir(),'public-privacy-ready-')));roots.push(root)
 let release!:()=>void;const ready=new Promise<void>(r=>{release=r}),warm=vi.fn(()=>ready),erase=vi.fn()
 const local=new LocalDraftPrivacy(root,{revokeChild:vi.fn(),revokeAll:vi.fn(),eraseChild:erase,eraseAll:erase},undefined,warm)
 const journal=new ControlledLaunchIntents(root,{read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true})
 let status='missing',generation='original-generation',auth=true
 const operation={request_id:'original-request',expected_generation:generation,cleanup_manifest_sha256:'a'.repeat(64)}
 const command={path:'/v1/parent/children/child/delete',body:{scope,operation,confirmation:'public synthetic'}}
 const receipt=()=>({request_id:operation.request_id,kind:'delete',child_id:'child',scope,origin_generation:operation.expected_generation,current_generation:generation,result_generation:status==='committed'?generation:null,cleanup_manifest_sha256:operation.cleanup_manifest_sha256,status,cleanup_allowed:status==='committed'})
 const transport={call:vi.fn(async(p:string):Promise<NativePrivacyResponse>=>{
  if(!auth)return {status:403,body:{}}
  if(p.endsWith('/generation'))return {status:200,body:{generation}}
  if(p.endsWith('/native-context'))return {status:200,body:{child_id:'child',scope,native_required:scope==='all_history',operation}}
  if(p.startsWith('/v1/parent/privacy-operations/'))return {status:status==='missing'?404:200,body:receipt()}
  status='committed';return {status:200,body:{}}
 }),permit:vi.fn(async(m:any)=>({...m,status:'finished'})),end:vi.fn(async()=>{}),closeLegacy:vi.fn(async()=>{})}
 const c=new NativePrivacyCoordinator(journal,transport,local)
 return {local,journal,c,command,warm,erase,release,transport,setGeneration:(v:string)=>{generation=v},deny:()=>{auth=false}}
}
it('cold committed erasure waits inside its original durable fence, then erases and finishes once',async()=>{
 const f=fixture(),p=f.c.execute(f.command,'public-parent');await vi.waitFor(()=>expect(f.warm).toHaveBeenCalledTimes(1))
 expect(f.local.active()).not.toBeNull();expect(f.erase).not.toHaveBeenCalled();f.release();await p
 expect(f.erase).toHaveBeenCalledTimes(1);expect(f.local.active()).toBeNull();expect(f.journal.activePrivacy()).toBeNull()
 expect(f.transport.call.mock.calls.filter(([p])=>p===f.command.path)).toHaveLength(1)
})
it.each(['same-command-new-nonce','revoked-parent'])('READY cannot erase after %s',async reason=>{
 const f=fixture(),p=f.c.execute(f.command,'public-parent'),rejected=expect(p).rejects.toThrow();await vi.waitFor(()=>expect(f.warm).toHaveBeenCalledTimes(1))
 if(reason==='same-command-new-nonce'){const old=f.journal.activePrivacy()!;f.journal.finishPrivacy(old,false);f.journal.beginPrivacy({...old,nonce:crypto.randomUUID()})}else f.deny()
 f.release();await rejected;expect(f.erase).not.toHaveBeenCalled();expect(f.local.active()).not.toBeNull()
 expect(f.transport.call.mock.calls.filter(([p])=>p===f.command.path)).toHaveLength(1)
})
it('a changed original local fence during readiness is rejected before erasure',async()=>{
 const f=fixture(),binding={schema:1 as const,kind:'delete' as const,child_id:'child',scope:'knowledge' as const,request_id:'request',expected_generation:'generation',cleanup_manifest_sha256:'a'.repeat(64),deletion_id:null,command_sha256:'b'.repeat(64)}
 f.local.begin(binding);const p=f.local.prepareErase(binding),rejected=expect(p).rejects.toThrow('SCOPE_CHANGED')
 f.local.finish(binding);f.local.begin({...binding,request_id:'different-request'});f.release();await rejected;expect(f.erase).not.toHaveBeenCalled()
})
it.each(['null-to-new-claim','old-to-new-nonce'])('transport waiting cannot silently adopt %s before erasure',async reason=>{
 const f=fixture(reason==='null-to-new-claim'?'knowledge':'all_history')
 const original=f.transport.call.getMockImplementation()!;let entered!:()=>void,release!:()=>void
 const waiting=new Promise<void>(r=>{entered=r}),barrier=new Promise<void>(r=>{release=r});let first=true
 f.transport.call.mockImplementation(async p=>{if(first&&p.endsWith('/generation')){first=false;entered();await barrier}return original(p)})
 const p=f.c.execute(f.command,'public-parent'),rejected=expect(p).rejects.toThrow();await waiting
 const old=f.journal.activePrivacy()
 if(old){f.journal.finishPrivacy(old,false);f.journal.beginPrivacy({...old,nonce:crypto.randomUUID()})}
 else f.journal.beginPrivacy({nonce:crypto.randomUUID(),kind:'restore',child_id:null,scope:null,request_id:'new-native-request',expected_generation:'original-generation',cleanup_manifest_sha256:'c'.repeat(64),command_sha256:'d'.repeat(64)})
 release();f.release();await rejected;expect(f.erase).not.toHaveBeenCalled();expect(f.local.active()).not.toBeNull()
})
it('legacy job-only retry refreshes the original native context and generation after cold readiness',async()=>{
 const f=fixture('knowledge'),original=f.transport.call.getMockImplementation()!
 f.transport.call.mockImplementation(async p=>p.endsWith('/retry')?{status:200,body:{deletion_id:'job',child_id:'child',scope:'knowledge',target_type:'child_scope',status:'completed'}}:original(p))
 const p=f.c.execute({path:'/v1/parent/deletions/job/retry',body:{confirmation:'public synthetic'}},'public-parent'),rejected=expect(p).rejects.toThrow('GENERATION_CHANGED')
 await vi.waitFor(()=>expect(f.warm).toHaveBeenCalledTimes(1));f.setGeneration('replacement-generation');f.release();await rejected
 expect(f.erase).not.toHaveBeenCalled();expect(f.local.active()).not.toBeNull();expect(f.transport.call.mock.calls.filter(([p])=>p.endsWith('/retry'))).toHaveLength(1)
})

it.each(['null-to-new-claim','old-to-new-nonce'])('native retry context waiting cannot adopt %s as its entry claim',async reason=>{
 const f=fixture(),command={path:'/v1/parent/deletions/job/retry',body:{confirmation:'public synthetic'}}
 const claim={nonce:randomUUID(),kind:'delete' as const,child_id:'child',scope:'all_history',request_id:'original-request',expected_generation:'original-generation',cleanup_manifest_sha256:'a'.repeat(64),command_sha256:createHash('sha256').update(canonical([command.path,command.body])).digest('hex')}
 if(reason==='old-to-new-nonce')f.journal.beginPrivacy(claim)
 const original=f.transport.call.getMockImplementation()!;let entered!:()=>void,release!:()=>void
 const waiting=new Promise<void>(r=>{entered=r}),barrier=new Promise<void>(r=>{release=r})
 f.transport.call.mockImplementation(async p=>{if(p.endsWith('/native-context')){entered();await barrier}return original(p)})
 const pending=f.c.execute(command,'public-parent'),rejected=expect(pending).rejects.toThrow('NATIVE_PRIVACY_SCOPE_CHANGED');await waiting
 if(reason==='old-to-new-nonce')f.journal.finishPrivacy(claim,false)
 f.journal.beginPrivacy({...claim,nonce:randomUUID()});release();await rejected
 expect(f.transport.permit).not.toHaveBeenCalled();expect(f.warm).not.toHaveBeenCalled();expect(f.erase).not.toHaveBeenCalled()
 expect(f.transport.call.mock.calls.filter(([p])=>p===command.path)).toHaveLength(0)
})
