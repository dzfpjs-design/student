// @vitest-environment node
import {mkdtemp,readFile,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {expect,it} from 'vitest'
import {DiagnosticRecorder,sanitizeDiagnosticValue} from './diagnostics'

it('retains the actual existing finite echo-policy event rather than erasing its identity',async()=>{
 const root=await mkdtemp(join(tmpdir(),'public-echo-policy-audit-'))
 try{
  const logger=new DiagnosticRecorder(root)
  await logger.record({source:'renderer',event:'voice.realtime.echo_cancellation_policy',level:'warn',
   details:{echo_mode:'browser-selected',echo_negotiation:'rejected',acoustic_quality_verified:false,
    question:'PRIVATE_PUBLIC_TEST_SENTINEL',transcript:'PRIVATE_PUBLIC_TEST_SENTINEL'}})
  const row=JSON.parse((await readFile(logger.logPath,'utf8')).trim())
  expect(row.event).toBe('voice.realtime.echo_cancellation_policy')
  // The producer currently uses these finite fields. The recommended fix uses
  // explicit echo_mode/echo_negotiation so ordinary voice `mode` stays separate.
  expect(row.details).toEqual({echo_mode:'browser-selected',echo_negotiation:'rejected',acoustic_quality_verified:false})
 }finally{await rm(root,{recursive:true,force:true})}
})

it('continues dropping private content even when keys imitate proposed echo fields',()=>{
 expect(sanitizeDiagnosticValue({echo_mode:'PRIVATE_PUBLIC_TEST_SENTINEL',echo_negotiation:'PRIVATE_PUBLIC_TEST_SENTINEL',acoustic_quality_verified:true,
  question:'PRIVATE_PUBLIC_TEST_SENTINEL',transcript:'PRIVATE_PUBLIC_TEST_SENTINEL',session_id:'PRIVATE_PUBLIC_TEST_SENTINEL'})).toEqual({})
})
