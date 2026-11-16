// @vitest-environment node
import{mkdtemp,readFile,rm}from'node:fs/promises'
import{tmpdir}from'node:os';import{join}from'node:path';import{expect,it}from'vitest'
import{DiagnosticRecorder,sanitizeDiagnosticValue}from'./diagnostics'
export const wrapperProducerCases=[{"event":"structured_event","source":"local_service","details":{}},{"event":"document.capture.frame_deferred","source":"renderer","details":{"reason":"transient_occlusion"}},{"event":"voice.capture.protected_tail_failed","source":"renderer","details":{"reason_code":"quiet_not_verified"}},{"event":"voice.capture.unknown_overlap_held","source":"renderer","details":{"reason_code":"source_or_close_unknown"}},{"event":"voice.capture.echo_cancellation_policy","source":"renderer","details":{"echo_mode":"all","echo_negotiation":"accepted","acoustic_quality_verified":false}},{"event":"voice.capture.original_prefix_admitted","source":"renderer","details":{}},{"event":"voice.capture.original_prefix_held","source":"renderer","details":{}},{"event":"voice.capture.original_prefix_discarded","source":"renderer","details":{}},{"event":"privacy_database_delete_failed","source":"local_service","details":{}},{"event":"privacy_analysis_candidate_residual","source":"local_service","details":{}},{"event":"privacy_delete_dependency_blocked","source":"local_service","details":{}},{"event":"learning_adaptation_pending","source":"local_service","details":{}},{"event":"document_learning_worker_error","source":"local_service","details":{}},{"event":"speaking_candidate_worker_error","source":"local_service","details":{}},{"event":"pet_audio_worker_error","source":"local_service","details":{}}] as const
for(const example of wrapperProducerCases)it(`retains fixed wrapper producer ${example.event} and only closed metadata`,async()=>{
 const root=await mkdtemp(join(tmpdir(),'public-wrapper-event-'))
 try{const logger=new DiagnosticRecorder(root)
 await logger.record({source:example.source as 'renderer'|'local_service',event:example.event,level:'warn',message:'PRIVATE_SENTINEL',details:{...example.details,transcript:'PRIVATE_SENTINEL',question:'PRIVATE_SENTINEL',input_id:'PRIVATE_SENTINEL',session_id:'PRIVATE_SENTINEL',samples:12345}})
 const raw=await readFile(logger.logPath,'utf8'),row=JSON.parse(raw)
 expect(row.event).toBe(example.event);expect(row.source).toBe(example.source);expect(row.level).toBe('warn');expect(row.details).toEqual(example.details);expect(raw).not.toContain('PRIVATE_SENTINEL')
 }finally{await rm(root,{recursive:true,force:true})}
})
it('retains finite echo policy and public failure reasons without generic policy passthrough',()=>{
 expect(sanitizeDiagnosticValue({echo_mode:'browser-selected',echo_negotiation:'timed_out',acoustic_quality_verified:false})).toEqual({echo_mode:'browser-selected',echo_negotiation:'timed_out',acoustic_quality_verified:false})
 expect(sanitizeDiagnosticValue({reason_code:'quiet_not_verified'})).toEqual({reason_code:'quiet_not_verified'})
 expect(sanitizeDiagnosticValue({reason_code:'source_or_close_unknown'})).toEqual({reason_code:'source_or_close_unknown'})
 expect(sanitizeDiagnosticValue({mode:'all',negotiation:'accepted',echo_mode:'PRIVATE_SENTINEL',echo_negotiation:'PRIVATE_SENTINEL',acoustic_quality_verified:true,reason:'transient_occlusion_PRIVATE_SENTINEL',reason_code:'quiet_not_verified_PRIVATE_SENTINEL',samples:12345})).toEqual({})
})
it('keeps arbitrary IPC/child strings and near-match producer names unregistered',async()=>{
 const root=await mkdtemp(join(tmpdir(),'public-wrapper-unknown-'))
 try{const logger=new DiagnosticRecorder(root)
 for(const event of ['PRIVATE_SENTINEL','voice.capture.echo_cancellation_policy.extra','privacy_database_delete_failed.extra'])await logger.record({source:'renderer',event,details:{transcript:'PRIVATE_SENTINEL',input_id:'PRIVATE_SENTINEL'}})
 const raw=await readFile(logger.logPath,'utf8');expect(raw.trim().split('\n').map(x=>JSON.parse(x).event)).toEqual(['unregistered_event','unregistered_event','unregistered_event']);expect(raw).not.toContain('PRIVATE_SENTINEL')
 }finally{await rm(root,{recursive:true,force:true})}
})
