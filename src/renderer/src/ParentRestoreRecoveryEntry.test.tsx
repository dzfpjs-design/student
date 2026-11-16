import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {webcrypto} from 'node:crypto'
import {PrivacyOperations} from './ParentCenter'
import {ApiError,localApi} from './api'
import {preparePrivacyIntent,readPrivacyIntent} from './privacyOperationRecovery'
import {changeParentDraftAuthority} from './parentDraftAuthority'
import type {PrivacyPreferences,SystemHealth} from './types'
const label='核对原恢复，保留当前草稿'
const health={database_integrity:'ok',database_size_bytes:0,disk_free_bytes:100000,pending_report_jobs:0,pending_agent_turns:0,pending_deletion_jobs:0,model_failures_last_7_days:0,active_sensor_leases:0,last_backup:null,maintenance:{daily_backup:null,retention_cleanup:null},recent_maintenance_failures:[],devices:[],device_incidents:[],recent_model_failures:[],recent_report_failures:[],recent_agent_failures:[],recent_deletion_failures:[]}as unknown as SystemHealth
const preferences={cloud_model_text_allowed:false,cloud_audio_allowed:false,raw_media_storage_allowed:false,model_improvement_allowed:false,model_call_retention_days:90,tutor_turn_retention_days:180,report_retention_days:730,updated_at:'2026-10-22T00:00:00Z'}as PrivacyPreferences
const key='hsc-writing-authorization:child-a:original'
function mount(childId='child-a'){return render(<PrivacyOperations childId={childId}initial={preferences}health={health}onHealthChange={vi.fn()}/>)}
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(reason:unknown)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject}}
async function setup(kind:'restore'|'portable_restore'|'delete'='portable_restore'){
 vi.stubGlobal('crypto',webcrypto);localStorage.setItem(key,'PUBLIC_SYNTHETIC_ORIGINAL_WORK')
 const intent=await preparePrivacyIntent(kind,kind==='delete'?'child-a':null,kind==='delete'?'media':null,'synthetic-generation')
 vi.spyOn(localApi,'listBackups').mockResolvedValue([]);vi.spyOn(localApi,'captureArtifacts').mockResolvedValue([]);vi.spyOn(localApi,'systemHealth').mockResolvedValue(health)
 vi.spyOn(localApi,'privacyOperationReceipt').mockResolvedValue({request_id:intent.requestId,kind,child_id:intent.childId,scope:intent.scope,origin_generation:intent.generation,current_generation:intent.generation,result_generation:null,cleanup_manifest_sha256:intent.manifestSha256,status:'failed',cleanup_allowed:false,reference:null,local_only:true,cloud_transmission_allowed:false}as never)
 const restore=vi.spyOn(localApi,'restoreBackup'),portable=vi.spyOn(localApi,'restorePortableBackup'),native=vi.spyOn(localApi,'recoverNativePrivacyOperation')
 // The generated Location wrapper calls this jsdom implementation. Replace only
 // the synthetic navigation boundary; the real component still calls reload().
 const symbol=Object.getOwnPropertySymbols(window.location).find(s=>s.description==='impl')!
 const reload=vi.spyOn((window.location as unknown as Record<symbol,{reload:()=>void}>)[symbol],'reload').mockImplementation(()=>{})
 return{intent,restore,portable,native,reload}
}
afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals();delete window.desktop})
it.each(['restore','portable_restore']as const)('original %s failed cleanup exposes an explicit verified recovery entry without consuming intent or draft',async kind=>{
 const f=await setup(kind),pending=deferred<{status:string;normal_business_ready?:boolean}>();const check=vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockReturnValue(pending.promise)
 mount();const button=await screen.findByRole('button',{name:label});await waitFor(()=>expect(button).toBeEnabled());fireEvent.click(button)
 expect(f.reload).not.toHaveBeenCalled();expect(readPrivacyIntent()).toEqual(f.intent);expect(localStorage.getItem(key)).toBe('PUBLIC_SYNTHETIC_ORIGINAL_WORK')
 pending.resolve({status:'recovery_only',normal_business_ready:false});await waitFor(()=>expect(f.reload).toHaveBeenCalledOnce())
 expect(check).toHaveBeenCalledOnce();expect(readPrivacyIntent()).toEqual(f.intent);expect(localStorage.getItem(key)).toBe('PUBLIC_SYNTHETIC_ORIGINAL_WORK');expect(f.restore).not.toHaveBeenCalled();expect(f.portable).not.toHaveBeenCalled();expect(f.native).not.toHaveBeenCalled()
})
it.each(['unknown','normal']as const)('health %s keeps the page and intent, and the same button permits an explicit fresh retry',async mode=>{
 const f=await setup();const check=vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockImplementationOnce(()=>mode==='unknown'?Promise.reject(Error('PRIVATE_RAW_ERROR_MUST_NOT_SHOW')):Promise.resolve({status:'ok',normal_business_ready:true})).mockResolvedValue({status:'recovery_only',normal_business_ready:false})
 mount();const b=await screen.findByRole('button',{name:label});await waitFor(()=>expect(b).toBeEnabled());fireEvent.click(b)
 await screen.findByText('尚未确认原恢复核对页面，当前资料与草稿均保留。请重试核对，不要再次恢复备份。');expect(f.reload).not.toHaveBeenCalled();expect(screen.queryByText('PRIVATE_RAW_ERROR_MUST_NOT_SHOW')).not.toBeInTheDocument();expect(readPrivacyIntent()).toEqual(f.intent)
 fireEvent.click(b);await waitFor(()=>expect(f.reload).toHaveBeenCalledOnce());expect(check).toHaveBeenCalledTimes(2)
})
it.each(['child','authority','unmount','intent']as const)('an old %s owner cannot navigate after the health await',async mode=>{
 const f=await setup(),d=deferred<{status:string;normal_business_ready?:boolean}>();vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockReturnValue(d.promise)
 const view=mount();const b=await screen.findByRole('button',{name:label});await waitFor(()=>expect(b).toBeEnabled());fireEvent.click(b)
 if(mode==='child')view.rerender(<PrivacyOperations childId="child-b"initial={preferences}health={health}onHealthChange={vi.fn()}/>);
 if(mode==='authority')changeParentDraftAuthority('locked');if(mode==='unmount')view.unmount();if(mode==='intent')localStorage.removeItem('hsc:privacy-operation-intent:v1')
 d.resolve({status:'recovery_only',normal_business_ready:false});await new Promise(r=>setTimeout(r,0));expect(f.reload).not.toHaveBeenCalled();expect(localStorage.getItem(key)).toBe('PUBLIC_SYNTHETIC_ORIGINAL_WORK')
})
it('a draft navigation veto keeps a visible save-and-retry path and never clears pending intent',async()=>{
 const f=await setup();vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockResolvedValue({status:'recovery_only',normal_business_ready:false})
 const veto=(e:Event)=>e.preventDefault();window.addEventListener('hsc:parent-draft-before-navigation',veto)
 try{mount();const b=await screen.findByRole('button',{name:label});await waitFor(()=>expect(b).toBeEnabled());fireEvent.click(b);await screen.findByText('当前草稿尚未确认保护，暂不离开。请在教材编辑中保存或核对草稿，确认保护后再点此按钮。');expect(f.reload).not.toHaveBeenCalled();expect(readPrivacyIntent()).toEqual(f.intent)
 window.removeEventListener('hsc:parent-draft-before-navigation',veto);fireEvent.click(b);await waitFor(()=>expect(f.reload).toHaveBeenCalledOnce())}finally{window.removeEventListener('hsc:parent-draft-before-navigation',veto)}
})
it('delete cleanup never offers the restore recovery entry or probes recovery health',async()=>{
 const f=await setup('delete'),check=vi.spyOn(localApi,'parentRestoreRecoveryHealth');mount();await screen.findByRole('button',{name:'保留本机记录，结束本次核对'});expect(screen.queryByRole('button',{name:label})).not.toBeInTheDocument();expect(check).not.toHaveBeenCalled();expect(f.reload).not.toHaveBeenCalled()
})
it('a pending health request permits only one navigation even with repeated clicks',async()=>{
 const f=await setup(),d=deferred<{status:string;normal_business_ready?:boolean}>();const check=vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockReturnValue(d.promise)
 mount();const b=await screen.findByRole('button',{name:label});await waitFor(()=>expect(b).toBeEnabled());fireEvent.click(b);fireEvent.click(b);expect(check).toHaveBeenCalledOnce();d.resolve({status:'recovery_only',normal_business_ready:false});await waitFor(()=>expect(f.reload).toHaveBeenCalledOnce())
})

it.each(['wrong_passphrase','damaged_package']as const)('the actual portable restore %s catch reaches this same pending recovery entry without redispatch',async mode=>{
 const f=await setup();localStorage.removeItem('hsc:privacy-operation-intent:v1')
 window.desktop={choosePortableBackup:vi.fn(async()=>({path:'/public-synthetic-fixed-package.hscportable'}))}as never
 vi.spyOn(localApi,'privacyOperationGeneration').mockResolvedValue({generation:'synthetic-generation'})
 vi.mocked(localApi.privacyOperationReceipt).mockImplementation(async()=>{const intent=readPrivacyIntent()!;return{request_id:intent.requestId,kind:intent.kind,child_id:null,scope:null,origin_generation:intent.generation,current_generation:intent.generation,result_generation:null,cleanup_manifest_sha256:intent.manifestSha256,status:'failed',cleanup_allowed:false,reference:null,local_only:true,cloud_transmission_allowed:false}})
 f.portable.mockRejectedValue(new ApiError(mode==='wrong_passphrase'?'迁移口令认证失败。':'迁移包认证失败。',422,'INVALID_BACKUP'))
 vi.spyOn(localApi,'parentRestoreRecoveryHealth').mockResolvedValue({status:'recovery_only',normal_business_ready:false})
 mount();await waitFor(()=>expect(screen.getByRole('button',{name:'选择迁移包'})).toBeEnabled());fireEvent.click(screen.getByRole('button',{name:'选择迁移包'}));await screen.findByText('已选择迁移包')
 fireEvent.change(screen.getByLabelText('迁移备份口令'),{target:{value:'PUBLIC-WRONG-SYNTHETIC-PASSWORD'}});fireEvent.change(screen.getByLabelText('迁移恢复确认'),{target:{value:'确认迁移恢复'}});fireEvent.click(screen.getByRole('button',{name:'迁移恢复'}))
 await waitFor(()=>expect(f.portable).toHaveBeenCalledOnce());const b=await screen.findByRole('button',{name:label});await waitFor(()=>expect(b).toBeEnabled());const original=readPrivacyIntent();expect(original?.kind).toBe('portable_restore');expect(localStorage.getItem(key)).toBe('PUBLIC_SYNTHETIC_ORIGINAL_WORK')
 fireEvent.click(b);await waitFor(()=>expect(f.reload).toHaveBeenCalledOnce());expect(readPrivacyIntent()).toEqual(original);expect(f.portable).toHaveBeenCalledOnce();expect(f.restore).not.toHaveBeenCalled();expect(f.native).not.toHaveBeenCalled()
})
