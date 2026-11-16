// @vitest-environment node
import{expect,it}from'vitest'
import{NativeResponseDiagnostics}from'./qwenNativeResponseDiagnostic'
const created={type:'response.created'}
it('counts received vs scheduled PCM and first-PCM latency separately without storing content',()=>{
 let now=100;const d=new NativeResponseDiagnostics(()=>now)
 d.observe('PRIVATE_ID',created,true)
 now=110;d.observe('PRIVATE_ID',{type:'response.audio.delta',delta:'PRIVATE_PCM'},true);d.scheduledPcm('PRIVATE_ID')
 now=120;d.observe('PRIVATE_ID',{type:'response.audio.delta',delta:'PRIVATE_PCM'},true)
 d.observe('PRIVATE_ID',{type:'response.text.done',text:'PRIVATE_FAMILY_CONTENT'},true)
 now=150;const result=d.observe('PRIVATE_ID',{type:'response.done',response:{status:'completed'}},true)!
 expect(result).toMatchObject({response_elapsed_ms:50,first_pcm_ms:10,response_text_chars:22,response_text_kind:'substantive',audio_chunk_count:2,pcm_chunk_count:1,pcm_scheduled:true,physical_audibility_verified:false})
 expect(JSON.stringify(result)).not.toMatch(/PRIVATE_/)
 expect(d.observe('PRIVATE_ID',{type:'response.done',response:{status:'completed'}},true)).toBeUndefined()
})
it('classifies empty, whitespace, punctuation, and unknown raw reasons as closed values',()=>{
 for(const [text,kind]of [['','empty'],['  ','whitespace'],['。…','punctuation'],['想法','substantive']]){
  const d=new NativeResponseDiagnostics(()=>1);d.observe('r',created,true);d.observe('r',{type:'response.audio_transcript.done',transcript:text},true)
  const result=d.observe('r',{type:'response.done',response:{status:'incomplete',status_details:{reason:'PRIVATE_REASON',error:{message:'PRIVATE_ERROR'}}}},true)!
  expect(result).toMatchObject({response_text_kind:kind,response_reason:'other',response_error_present:true,pcm_scheduled:false})
  expect(result.first_pcm_ms).toBeUndefined();expect(JSON.stringify(result)).not.toContain('PRIVATE_')
 }
})
it('does not create unknown identities, keeps finite capacity, and clears on connection close',()=>{
 const d=new NativeResponseDiagnostics(()=>1)
 expect(d.observe('unknown',{type:'response.done',response:{status:'failed'}},false)).toBeUndefined()
 for(let i=0;i<17;i++)d.observe('r'+i,created,true)
 expect(d.observe('r0',{type:'response.done',response:{status:'failed'}},false)).toBeUndefined()
 expect(d.observe('r16',{type:'response.done',response:{status:'failed'}},false)).toBeTruthy()
 d.clear();expect(d.observe('r15',{type:'response.done',response:{status:'failed'}},false)).toBeUndefined()
})
it('marks input interruption before PCM without changing late terminal ownership',()=>{
 const d=new NativeResponseDiagnostics(()=>2);d.observe('old',created,true);d.inputStarted('old')
 const result=d.observe('old',{type:'response.done',response:{status:'cancelled',status_details:{reason:'turn_detected'}}},false)!
 expect(result).toMatchObject({new_input_before_pcm:true,terminal_current:false,response_reason:'turn_detected',pcm_scheduled:false})
 const after=new NativeResponseDiagnostics(()=>2);after.observe('new',created,true);after.scheduledPcm('new');after.inputStarted('new')
 expect(after.observe('new',{type:'response.done',response:{status:'completed'}},true)!.new_input_before_pcm).toBe(false)
})
