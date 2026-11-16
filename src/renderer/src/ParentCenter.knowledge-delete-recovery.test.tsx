import {StrictMode} from 'react'
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {PrivacyOperations} from './ParentCenter'
import {ApiError,localApi} from './api'
import {preparePrivacyIntent,readPrivacyIntent,operationIdentity} from './privacyOperationRecovery'
import {changeParentDraftAuthority} from './parentDraftAuthority'
import type {SystemHealth,PrivacyPreferences} from './types'
const health={database_integrity:'ok',database_size_bytes:0,disk_free_bytes:100,pending_report_jobs:0,pending_agent_turns:0,model_failures_last_7_days:0,active_sensor_leases:0,last_backup:null,maintenance:{daily_backup:null,retention_cleanup:null},pending_deletion_jobs:0,recent_maintenance_failures:[],raw_media_files_stored:0,devices:[],device_incidents:[],recent_model_failures:[],recent_report_failures:[],recent_agent_failures:[],recent_deletion_failures:[]} as SystemHealth
const prefs={cloud_model_text_allowed:false,cloud_audio_allowed:false,raw_media_storage_allowed:false,model_improvement_allowed:false,model_call_retention_days:90,tutor_turn_retention_days:180,report_retention_days:730,updated_at:''} as PrivacyPreferences
const generation='same-generation-original'
afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks()})
async function fixture(scope='knowledge',child='a'){
 const intent=await preparePrivacyIntent('delete',child,scope,generation);let committed=false
 vi.spyOn(localApi,'listBackups').mockResolvedValue([]);vi.spyOn(localApi,'captureArtifacts').mockResolvedValue([]);vi.spyOn(localApi,'systemHealth').mockResolvedValue(health)
 const read=vi.spyOn(localApi,'privacyOperationReceipt').mockImplementation(async()=>{if(!committed)throw new ApiError('not found',404);return{request_id:intent.requestId,kind:'delete',child_id:child,scope,origin_generation:generation,result_generation:generation,current_generation:generation,cleanup_manifest_sha256:intent.manifestSha256,status:'committed',cleanup_allowed:true,reference:{type:'deletion_job',id:'synthetic-job'},local_only:true,cloud_transmission_allowed:false}})
 const recover=vi.spyOn(localApi,'recoverNativePrivacyOperation').mockRejectedValue(new ApiError('not found',404))
 const gen=vi.spyOn(localApi,'privacyOperationGeneration').mockResolvedValue({generation})
 const send=vi.spyOn(localApi,'deleteChildData').mockImplementation(async()=>{committed=true;return{deleted_counts:{},recovery_rotation:{status:'completed',invalidated_backup_count:0,removed_backup_bundle_files:0,removed_upgrade_snapshot_files:0,replacement_backup_id:'synthetic'}}})
 return{intent,read,gen,send,recover,view:()=>render(<PrivacyOperations childId={child}initial={prefs}health={health}onHealthChange={()=>{}}/>)}
}
it('explicitly continues only the original knowledge deletion after authenticated missing receipt without a new identity',async()=>{
 const f=await fixture();f.view();const button=await screen.findByRole('button',{name:'继续原学习资料删除'});expect(button).toBeDisabled();expect(f.send).not.toHaveBeenCalled();fireEvent.change(screen.getByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(button);await waitFor(()=>expect(f.send).toHaveBeenCalledWith('a','knowledge','确认删除',operationIdentity(f.intent)));await waitFor(()=>expect(readPrivacyIntent()).toBeNull());await waitFor(()=>expect(screen.queryByRole('button',{name:'重试本机清理'})).not.toBeInTheDocument());expect(f.send).toHaveBeenCalledTimes(1)
})
it('never sends a deletion on mount or ordinary retry; a second confirmation is mandatory',async()=>{
 const f=await fixture();f.view();const button=await screen.findByRole('button',{name:'继续原学习资料删除'});fireEvent.click(screen.getByRole('button',{name:'重试本机清理'}));await waitFor(()=>expect(f.recover).toHaveBeenCalledTimes(1));expect(f.send).not.toHaveBeenCalled();expect(button).toBeDisabled()
})
it.each(['learning','media','all_history'])('does not offer a continuation for %s scope',async scope=>{
 const f=await fixture(scope);f.view();await screen.findByText(/原隐私操作的提交状态/);expect(screen.queryByRole('button',{name:'继续原学习资料删除'})).not.toBeInTheDocument();expect(f.send).not.toHaveBeenCalled()
})
it('does not expose another child original deletion',async()=>{
 const f=await fixture();render(<PrivacyOperations childId="b"initial={prefs}health={health}onHealthChange={()=>{}}/>);await screen.findByText(/原隐私操作的提交状态/);expect(screen.queryByRole('button',{name:'继续原学习资料删除'})).not.toBeInTheDocument();expect(f.send).not.toHaveBeenCalled()
})
it('does not offer the continuation after generation has changed',async()=>{
 const f=await fixture();f.gen.mockResolvedValue({generation:'new-generation-after-restore'});f.view();await screen.findByText(/原隐私操作的提交状态/);expect(screen.queryByRole('button',{name:'继续原学习资料删除'})).not.toBeInTheDocument();expect(f.send).not.toHaveBeenCalled();expect(readPrivacyIntent()).toEqual(f.intent)
})
it.each(['lock','runtime','child','generation','intent'])('rejects %s change while rechecking the original request before POST',async change=>{
 const f=await fixture(),view=f.view();const button=await screen.findByRole('button',{name:'继续原学习资料删除'});let finish!:(v:{generation:string})=>void;f.gen.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}));fireEvent.change(screen.getByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(button);await waitFor(()=>expect(f.gen).toHaveBeenCalledTimes(2))
 if(change==='lock')act(()=>changeParentDraftAuthority('locked'))
 if(change==='runtime')act(()=>window.dispatchEvent(new Event('hsc-runtime-restarted')))
 if(change==='child')view.rerender(<PrivacyOperations childId="b"initial={prefs}health={health}onHealthChange={()=>{}}/>)
 if(change==='intent')localStorage.setItem('hsc:privacy-operation-intent:v1',JSON.stringify({...f.intent,requestId:'different-original-request'}))
 await act(async()=>finish({generation:change==='generation'?'new-generation-after-restore':generation}));expect(f.send).not.toHaveBeenCalled();expect(readPrivacyIntent()).not.toBeNull()
})
it('does not POST if a committed receipt appears during the explicit recheck',async()=>{
 const f=await fixture();f.view();const button=await screen.findByRole('button',{name:'继续原学习资料删除'});f.read.mockResolvedValueOnce({request_id:f.intent.requestId,status:'committed'} as never);fireEvent.change(screen.getByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(button);await screen.findByText(/没有创建新的删除请求/);expect(f.send).not.toHaveBeenCalled();expect(readPrivacyIntent()).toEqual(f.intent)
})
it('retains the exact original action after an unknown replay failure and requires another explicit confirmation',async()=>{
 const f=await fixture();f.send.mockRejectedValueOnce(Error('lost response'));f.view();fireEvent.change(await screen.findByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(screen.getByRole('button',{name:'继续原学习资料删除'}));await screen.findByText(/原学习资料删除尚未核对完成/);expect(readPrivacyIntent()).toEqual(f.intent);expect(screen.getByLabelText('继续原学习资料删除确认')).toHaveValue('');expect(screen.getByRole('button',{name:'继续原学习资料删除'})).toBeDisabled();expect(f.send).toHaveBeenCalledTimes(1)
})
it('does not apply late replay completion to the new child or clear the unresolved intent',async()=>{
 const f=await fixture(),view=f.view();let finish!:(v:Awaited<ReturnType<typeof localApi.deleteChildData>>)=>void;f.send.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}));fireEvent.change(await screen.findByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(screen.getByRole('button',{name:'继续原学习资料删除'}));await waitFor(()=>expect(f.send).toHaveBeenCalledTimes(1));const reads=f.read.mock.calls.length;view.rerender(<PrivacyOperations childId="b"initial={prefs}health={health}onHealthChange={()=>{}}/>);await act(async()=>finish({deleted_counts:{},recovery_rotation:{status:'completed',invalidated_backup_count:0,removed_backup_bundle_files:0,removed_upgrade_snapshot_files:0,replacement_backup_id:'x'}}));expect(f.read).toHaveBeenCalledTimes(reads);expect(readPrivacyIntent()).toEqual(f.intent);expect(screen.queryByRole('button',{name:'继续原学习资料删除'})).not.toBeInTheDocument()
})

it('does not revive original knowledge confirmation after child A -> B -> A during generation await',async()=>{
 const f=await fixture(),view=f.view();const button=await screen.findByRole('button',{name:'继续原学习资料删除'})
 let finish!:(v:{generation:string})=>void;f.gen.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 fireEvent.change(screen.getByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}});fireEvent.click(button)
 await waitFor(()=>expect(f.gen).toHaveBeenCalledTimes(2))
 view.rerender(<PrivacyOperations childId="b" initial={prefs} health={health} onHealthChange={()=>{}}/> )
 view.rerender(<PrivacyOperations childId="a" initial={prefs} health={health} onHealthChange={()=>{}}/> )
 await act(async()=>finish({generation}))
 expect(f.send).not.toHaveBeenCalled()
 expect(readPrivacyIntent()).toEqual(f.intent)
})

it('offers original knowledge continuation after actual StrictMode initial effect replay',async()=>{
 const f=await fixture()
 render(<StrictMode><PrivacyOperations childId="a" initial={prefs} health={health} onHealthChange={()=>{}}/></StrictMode>)
 await screen.findByRole('button',{name:'继续原学习资料删除'})
 expect(f.send).not.toHaveBeenCalled()
})

it('allows a fresh confirmation after retiring the A -> B -> A pending confirmation',async()=>{
 const f=await fixture(),view=f.view();await screen.findByRole('button',{name:'继续原学习资料删除'})
 let finish!:(v:{generation:string})=>void;f.gen.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 fireEvent.change(screen.getByLabelText('继续原学习资料删除确认'),{target:{value:'确认删除'}})
 fireEvent.click(screen.getByRole('button',{name:'继续原学习资料删除'}));await waitFor(()=>expect(f.gen).toHaveBeenCalledTimes(2))
 view.rerender(<PrivacyOperations childId="b" initial={prefs} health={health} onHealthChange={()=>{}}/>)
 view.rerender(<PrivacyOperations childId="a" initial={prefs} health={health} onHealthChange={()=>{}}/>)
 await act(async()=>finish({generation}));expect(f.send).not.toHaveBeenCalled()
 const input=await screen.findByLabelText('继续原学习资料删除确认')
 expect(input).toBeEnabled()
 fireEvent.change(input,{target:{value:'确认删除'}})
 expect(screen.getByRole('button',{name:'继续原学习资料删除'})).toBeEnabled()
 fireEvent.click(screen.getByRole('button',{name:'继续原学习资料删除'}))
 await waitFor(()=>expect(f.send).toHaveBeenCalledTimes(1))
 expect(f.send).toHaveBeenCalledWith('a','knowledge','确认删除',operationIdentity(f.intent))
})
