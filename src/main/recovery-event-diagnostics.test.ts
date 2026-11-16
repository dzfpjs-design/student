// @vitest-environment node
import {mkdtemp,readFile,rm}from'node:fs/promises'
import{tmpdir}from'node:os'
import{join}from'node:path'
import{expect,it}from'vitest'
import{DiagnosticRecorder,sanitizeDiagnosticValue}from'./diagnostics'
const names=['runtime_awake_unconfirmed','protected_startup_failed','voice.realtime.pending_input_recovery','voice.realtime.cancellation_journal_unconfirmed','voice.agent.identity_index_restored','voice.agent.identity_index_unconfirmed','voice.realtime.identity_index_restored','voice.realtime.identity_index_unconfirmed','voice.realtime.cancellation_recovered','voice.realtime.cancellation_recovery_unconfirmed','voice.realtime.cancellation_recovery_invalid','voice.recovery.receipt_check_incomplete','voice.realtime.resume_receipt_retained','voice.original_prompt.admission','voice.capture.protected_tail_window_failed','app_quit_backend_abnormal_acknowledged','restart_after_abnormal_shutdown']
const details=(name:string):Record<string,unknown>=>name.endsWith('identity_index_restored')?{restored:2,has_more:false,learning_tasks_replayed:0,contains_transcript:false}:name==='voice.realtime.cancellation_recovered'?{confirmed:3,scope:'canonical_voice_only',learning_tasks_replayed:0}:name==='voice.capture.protected_tail_window_failed'?{window:'recovery',recovery_windows:2}:name==='voice.original_prompt.admission'?{allowed:true,capture_ready:false}:name==='voice.recovery.receipt_check_incomplete'?{reason:'deadline'}:name==='runtime_awake_unconfirmed'?{error_code:'RUNTIME_AWAKE_UNCONFIRMED'}:name==='restart_after_abnormal_shutdown'?{error_code:'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'}:name==='app_quit_backend_abnormal_acknowledged'?{error_code:'LOCAL_SERVICE_SHUTDOWN_ABNORMAL',exitCode:1}:{}
for(const name of names)it(`retains reviewed actual producer ${name} with closed public metadata`,async()=>{
 const root=await mkdtemp(join(tmpdir(),'public-startup-event-'))
 try{
  const logger=new DiagnosticRecorder(root)
  await logger.record({source:'renderer',event:name,level:name==='voice.realtime.cancellation_recovery_invalid'?'error':'warn',message:'PRIVATE_SENTINEL',details:{...details(name),transcript:'PRIVATE_SENTINEL',question:'PRIVATE_SENTINEL',input_id:'PRIVATE_SENTINEL',voice_input_id:'PRIVATE_SENTINEL'}})
  const raw=await readFile(logger.logPath,'utf8'),row=JSON.parse(raw.trim())
  expect(row.event).toBe(name);expect(row.details).toEqual(details(name))
  expect(row.level).toBe(name==='voice.realtime.cancellation_recovery_invalid'?'error':'warn')
  expect(raw).not.toContain('PRIVATE_SENTINEL')
 }finally{await rm(root,{recursive:true,force:true})}
})
it('preserves zero/false-only privacy assertions and bounded canonical counts',()=>{
 expect(sanitizeDiagnosticValue({restored:0,confirmed:1000000,has_more:true,learning_tasks_replayed:0,contains_transcript:false,recovery_windows:0,window:'initial',scope:'canonical_voice_only',reason:'read_failed',allowed:false,capture_ready:true})).toEqual({restored:0,confirmed:1000000,has_more:true,learning_tasks_replayed:0,contains_transcript:false,recovery_windows:0,window:'initial',scope:'canonical_voice_only',reason:'read_failed',allowed:false,capture_ready:true})
})
it('rejects unreviewed event names, false privacy claims and arbitrary recovery fields',async()=>{
 const invalid={restored:1.5,confirmed:-1,has_more:'PRIVATE_SENTINEL',learning_tasks_replayed:1,contains_transcript:true,recovery_windows:3,window:'PRIVATE_SENTINEL',scope:'PRIVATE_SENTINEL',reason:'PRIVATE_SENTINEL',allowed:'PRIVATE_SENTINEL',capture_ready:'PRIVATE_SENTINEL',input_id:'PRIVATE_SENTINEL'}
 expect(sanitizeDiagnosticValue(invalid)).toEqual({})
 for(const value of [-1,1.5,1000001,NaN,Infinity,'PRIVATE_SENTINEL',true])expect(sanitizeDiagnosticValue({restored:value,confirmed:value})).toEqual({})
 for(const value of [1,false,'0'])expect(sanitizeDiagnosticValue({learning_tasks_replayed:value})).toEqual({})
 const root=await mkdtemp(join(tmpdir(),'public-unknown-event-'))
 try{const logger=new DiagnosticRecorder(root);await logger.record({source:'renderer',event:'PRIVATE_SENTINEL',details:invalid});const raw=await readFile(logger.logPath,'utf8');expect(JSON.parse(raw).event).toBe('unregistered_event');expect(raw).not.toContain('PRIVATE_SENTINEL')}finally{await rm(root,{recursive:true,force:true})}
})
it('retains all reviewed records and public correlation metadata in one recorder file',async()=>{
 const root=await mkdtemp(join(tmpdir(),'public-main-zip-events-'))
 try{
  const logger=new DiagnosticRecorder(root)
  for(const event of names)await logger.record({source:'renderer',event,level:'warn',correlationId:'11111111-1111-4111-8111-111111111111',details:{...details(event),transcript:'PRIVATE_SENTINEL',session_id:'PRIVATE_SENTINEL',error:{message:'PRIVATE_SENTINEL'}}})
  const raw=await readFile(logger.logPath,'utf8');expect(raw).not.toContain('PRIVATE_SENTINEL')
  expect(raw.trim().split('\n').map(x=>JSON.parse(x).event)).toEqual(names)
 }finally{await rm(root,{recursive:true,force:true})}
})
