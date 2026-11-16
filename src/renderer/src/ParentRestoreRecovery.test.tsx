import {mkdtemp,readFile,realpath,rm,writeFile} from 'node:fs/promises'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {ParentRestoreRecovery} from './ParentRestoreRecovery'
import {resetRuntimeCache} from './api'
import {ControlledLaunchIntents} from '../../main/controlled-launch-intent'
import {LocalDraftPrivacy} from '../../main/local-draft-privacy'
import {NativePrivacyCoordinator} from '../../main/native-privacy'
import type {NativePrivacyMetadata} from '../../shared/nativePrivacy'

type Kind='restore'|'portable_restore'
const roots:string[]=[]
afterEach(async()=>{cleanup();resetRuntimeCache();localStorage.clear();delete window.desktop;vi.restoreAllMocks();vi.unstubAllGlobals();for(const root of roots.splice(0))await rm(root,{recursive:true,force:true})})
async function fixture(kind:Kind='portable_restore',applied=true){
  resetRuntimeCache();localStorage.clear()
  const root=await realpath(await mkdtemp(join(tmpdir(),'hsc-portable-recovery-ui-')));roots.push(root)
  const humanWork=join(root,'synthetic-human-work');await writeFile(humanWork,'ORIGINAL-SYNTHETIC-WORK')
  const later='hsc-writing-authorization:child-b:later';localStorage.setItem(later,'KEEP-LATER-WORK')
  const drivers={read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true}
  const journal=new ControlledLaunchIntents(root,drivers)
  const metadata:NativePrivacyMetadata={nonce:'original-native-nonce',request_id:'original-restore-request',kind,child_id:null,scope:null,expected_generation:'original-generation',cleanup_manifest_sha256:'a'.repeat(64),command_sha256:'b'.repeat(64)}
  journal.beginPrivacy(metadata)
  // The existing native coordinator owns the actual durable draft barrier. The
  // synthetic callback below is its original cleanup boundary, not an erase by UI.
  const eraseAll=vi.fn(()=>{localStorage.removeItem('synthetic-native-draft')})
  localStorage.setItem('synthetic-native-draft','KEEP-UNTIL-NATIVE-ACK')
  const drafts=new LocalDraftPrivacy(root,{eraseAll,revokeAll:vi.fn(),eraseChild:vi.fn(),revokeChild:vi.fn()})
  const binding={schema:1 as const,kind,child_id:null,scope:null,request_id:metadata.request_id,expected_generation:metadata.expected_generation,cleanup_manifest_sha256:metadata.cleanup_manifest_sha256,command_sha256:metadata.command_sha256,deletion_id:null}
  drafts.begin(binding);drafts.markDispatched(binding)
  let settled=false,statusKind:string=kind,statusRequest=metadata.request_id,statusState=applied?'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS':'UNAPPLIED',expired=false
  let receiptPatch:Record<string,unknown>={},held:Promise<void>|null=null,release:()=>void=()=>{}
  const receipt=()=>({request_id:metadata.request_id,kind,child_id:null,scope:null,status:applied?'committed':'failed',origin_generation:metadata.expected_generation,current_generation:applied?'new-generation':metadata.expected_generation,result_generation:applied?'new-generation':null,cleanup_manifest_sha256:metadata.cleanup_manifest_sha256,cleanup_allowed:applied,reference:applied?{type:'audit_event',id:'original-audit'}:null,local_only:true,cloud_transmission_allowed:false,...receiptPatch})
  const call=vi.fn(async(path:string,body:unknown,token:string)=>{expect(token).toBe('original-parent-token');expect(body).toBeUndefined();expect(path).toBe('/v1/parent/privacy-operations/'+metadata.request_id);return {status:200,body:receipt()}})
  const permit=vi.fn(async(original:NativePrivacyMetadata,token:string,fence:boolean)=>{expect(original).toEqual(metadata);expect(token).toBe('original-parent-token');if(fence){if(held)await held;settled=true}return {...original,status:settled?(applied?'finished':'failed'):'claimed'}})
  const coordinator=new NativePrivacyCoordinator(journal,{call,permit,closeLegacy:vi.fn(async()=>undefined),end:vi.fn(async()=>undefined)},drafts)
  const recoverNativePrivacy=vi.fn((requestId:string,token:string,explicit:boolean)=>coordinator.recover(requestId,token,explicit))
  const runtime={localApiUrl:'http://127.0.0.1:24317',sessionToken:'synthetic-session',status:'ready' as const}
  const restartLocalService=vi.fn(async()=>{expect(journal.activePrivacy()).toBeNull();expect(drafts.active()).toBeNull();expect(eraseAll).toHaveBeenCalledTimes(applied?1:0);return runtime})
  window.desktop={getRuntimeConfig:vi.fn(async()=>runtime),restartLocalService,recoverNativePrivacy}as never
  const requests:Array<{path:string;method:string}>=[]
  vi.stubGlobal('fetch',vi.fn(async(url:string,options:RequestInit={})=>{
    const path=new URL(url).pathname,method=options.method??'GET';requests.push({path,method})
    expect(new Headers(options.headers).get('X-HSC-Session')).toBe(runtime.sessionToken)
    let body:unknown,status=200
    if(path==='/v1/parent/status')body={configured:true}
    else if(path==='/v1/parent/unlock'){expect(JSON.parse(String(options.body))).toEqual({pin:'4286'});body={parent_token:'original-parent-token'}}
    else if(path==='/v1/parent/recovery-status'){
      expect(new Headers(options.headers).get('X-HSC-Parent')).toBe('original-parent-token')
      if(expired){status=401;body={code:'PARENT_SESSION_REQUIRED',message:'家长解锁已过期，请重新解锁。'}}
      else body={schema:1,state:statusState,request_id:statusRequest,kind:statusKind,normal_business_ready:false,restart_required:settled}
    }else throw Error('UNEXPECTED_SYNTHETIC_REQUEST')
    return new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})
  }))
  const onRestarted=vi.fn(async()=>undefined)
  render(<ParentRestoreRecovery onRestarted={onRestarted}/>)
  return {root,humanWork,later,journal,drafts,metadata,eraseAll,recoverNativePrivacy,restartLocalService,requests,call,permit,onRestarted,
    setKind:(v:string)=>{statusKind=v},setRequest:(v:string)=>{statusRequest=v},setState:(v:string)=>{statusState=v},expire:()=>{expired=true},patchReceipt:(v:Record<string,unknown>)=>{receiptPatch=v},
    holdCleanup:()=>{held=new Promise<void>(resolve=>{release=resolve})},releaseCleanup:()=>release()}
}
async function unlock(){await screen.findByLabelText('家长 PIN');fireEvent.change(screen.getByLabelText('家长 PIN'),{target:{value:'4286'}});fireEvent.click(screen.getByRole('button',{name:'解锁并读取原恢复'}))}
async function review(){await unlock();await screen.findByRole('button',{name:'核对原恢复并继续'})}
function resume(){fireEvent.click(screen.getByRole('button',{name:'核对原恢复并继续'}))}
function noResend(f:Awaited<ReturnType<typeof fixture>>){expect(f.requests.filter(r=>r.method!=='GET')).toEqual([{path:'/v1/parent/unlock',method:'POST'}]);expect(f.call.mock.calls.every(c=>c[0]===`/v1/parent/privacy-operations/${f.metadata.request_id}`)).toBe(true);expect(localStorage.getItem(f.later)).toBe('KEEP-LATER-WORK')}

it.each<Kind>(['portable_restore','restore'])('committed %s uses the original native cleanup before offering restart',async kind=>{
  const f=await fixture(kind);f.holdCleanup();await review();expect(f.recoverNativePrivacy).not.toHaveBeenCalled();resume()
  await waitFor(()=>expect(f.permit).toHaveBeenCalledTimes(2));expect(screen.queryByRole('button',{name:'重启本地服务并继续'})).not.toBeInTheDocument()
  expect(f.journal.activePrivacy()).toEqual(f.metadata);expect(f.drafts.active()?.kind).toBe(kind);expect(f.eraseAll).not.toHaveBeenCalled()
  f.releaseCleanup();await screen.findByText('已恢复，原操作核对完成')
  expect(f.recoverNativePrivacy).toHaveBeenCalledExactlyOnceWith(f.metadata.request_id,'original-parent-token',true)
  expect(f.journal.activePrivacy()).toBeNull();expect(f.drafts.active()).toBeNull();expect(f.eraseAll).toHaveBeenCalledOnce()
  expect(f.restartLocalService).not.toHaveBeenCalled();fireEvent.click(screen.getByRole('button',{name:'重启本地服务并继续'}));await waitFor(()=>expect(f.onRestarted).toHaveBeenCalledOnce())
  expect(f.restartLocalService).toHaveBeenCalledOnce();expect(await readFile(f.humanWork,'utf8')).toBe('ORIGINAL-SYNTHETIC-WORK');noResend(f)
})
it.each<Kind>(['portable_restore','restore'])('failed %s releases only the original fence, preserving data',async kind=>{
  const f=await fixture(kind,false);await review();resume();await screen.findByText('原恢复未应用，原数据未替换')
  expect(f.eraseAll).not.toHaveBeenCalled();expect(localStorage.getItem('synthetic-native-draft')).toBe('KEEP-UNTIL-NATIVE-ACK');expect(await readFile(f.humanWork,'utf8')).toBe('ORIGINAL-SYNTHETIC-WORK')
  expect(f.journal.activePrivacy()).toBeNull();expect(f.drafts.active()).toBeNull();fireEvent.click(screen.getByRole('button',{name:'重启本地服务并继续'}));await waitFor(()=>expect(f.onRestarted).toHaveBeenCalledOnce());noResend(f)
})
it.each(['delete','foreign_kind'])('rejects initial status kind %s without invoking native recovery',async kind=>{
  const f=await fixture();f.setKind(kind);await unlock();await screen.findByRole('alert');expect(screen.queryByRole('button',{name:'核对原恢复并继续'})).not.toBeInTheDocument();expect(f.recoverNativePrivacy).not.toHaveBeenCalled();noResend(f)
})
it.each<Kind>(['restore','portable_restore'])('rejects same-request status changed away from original %s',async kind=>{
  const f=await fixture(kind);await review();f.setKind(kind==='restore'?'portable_restore':'restore');resume();await screen.findByRole('alert')
  expect(f.recoverNativePrivacy).not.toHaveBeenCalled();expect(f.journal.activePrivacy()).toEqual(f.metadata);expect(f.restartLocalService).not.toHaveBeenCalled();noResend(f)
})
it.each([{kind:'restore'},{kind:'delete'},{kind:'foreign_kind'},{request_id:'foreign-request'},{status:'unknown'},{origin_generation:''},{current_generation:''},{result_generation:'original-generation'},{cleanup_manifest_sha256:'bad'},{cleanup_allowed:false},{reference:{type:'deletion_job',id:'wrong-kind'}},{local_only:false},{cloud_transmission_allowed:true},{child_id:'other-child'},{scope:'all_history'}])('rejects a mismatched or incomplete original portable receipt %j',async patch=>{
  const f=await fixture();await review();f.patchReceipt(patch);resume();await screen.findByRole('alert');expect(screen.queryByRole('button',{name:'重启本地服务并继续'})).not.toBeInTheDocument();expect(f.restartLocalService).not.toHaveBeenCalled();noResend(f)
})
it('rejects a fresh other request before native recovery',async()=>{const f=await fixture();await review();f.setRequest('other-request');resume();await screen.findByRole('alert');expect(f.recoverNativePrivacy).not.toHaveBeenCalled();noResend(f)})
it('keeps unknown original status closed',async()=>{const f=await fixture();f.setState('UNKNOWN');await unlock();await screen.findByRole('alert');expect(f.recoverNativePrivacy).not.toHaveBeenCalled();noResend(f)})
it('expired parent status never invokes native recovery or restart',async()=>{const f=await fixture();await review();f.expire();resume();expect(await screen.findByRole('alert')).toHaveTextContent('家长解锁已过期');expect(f.recoverNativePrivacy).not.toHaveBeenCalled();expect(f.restartLocalService).not.toHaveBeenCalled();noResend(f)})
it('session-only recovery does not replace missing Electron native authority',async()=>{const f=await fixture();await review();delete window.desktop;resume();await screen.findByRole('alert');expect(f.recoverNativePrivacy).not.toHaveBeenCalled();expect(f.journal.activePrivacy()).toEqual(f.metadata);noResend(f)})
