import {mkdtemp,readFile,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {expect,it} from 'vitest'
import {DiagnosticRecorder} from './diagnostics'
it('keeps finite cloud-learning metadata and configuration failures while discarding learning content',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hsc-public-mistake-log-'))
 try{
  const logger=new DiagnosticRecorder(root)
  for(const operation of ['analyze','practice','coach','answer','verify'])await logger.record({source:'local_service',event:'mistake.learning.model_result',details:{request_type:'mistake_learning_'+operation,mistake_status:'completed',model_provider:'qwen',model_name:'qwen3.7-plus',latency_ms:20,input_tokens:40,output_tokens:50,question:'PRIVATE_QUESTION',answer:'PRIVATE_ANSWER',utterance:'PRIVATE_WORDS',child_id:'PRIVATE_ID'}})
  await logger.record({source:'local_service',event:'mistake.learning.action_result',level:'warn',details:{request_type:'mistake_learning_analyze',mistake_status:'failed',error_code:'MODEL_MISTAKE_REASONING_CONFIG_REQUIRED',elapsed_ms:1}})
  await logger.record({source:'local_service',event:'local_api_request',details:{method:'POST',route_template:'/v1/mistakes/{mistake_id}/learning/{operation}',statusCode:200,url:'PRIVATE_URL'}})
  const raw=await readFile(logger.logPath,'utf8'),rows=raw.trim().split('\n').map(line=>JSON.parse(line))
  expect(rows.slice(0,5).map(r=>r.details.request_type)).toEqual(['analyze','practice','coach','answer','verify'].map(x=>'mistake_learning_'+x))
  expect(rows[0].details).toMatchObject({model_provider:'qwen',model_name:'qwen3.7-plus',input_tokens:40,output_tokens:50})
  expect(rows[5].details.error_code).toBe('MODEL_MISTAKE_REASONING_CONFIG_REQUIRED')
  expect(rows[6].details.route_template).toBe('/v1/mistakes/{mistake_id}/learning/{operation}')
  expect(raw).not.toContain('PRIVATE_')
 }finally{await rm(root,{recursive:true,force:true})}
})
