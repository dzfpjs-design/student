import {afterEach,expect,it} from 'vitest'
import {clearVoiceCaptureContext,trackDiagnosticCapture,voiceCaptureReference,setVoiceQuestionCapture} from './voiceCaptureContext'
import type {DiagnosticPromptResult} from './types'
const answered={status:'answered',reason:null,prompt:{prompt_id:'written',item_version:'v1',child_id:'child',origin_session_id:'session',
  diagnostic_item_id:'item',item:{track:'pet',assessment_mode:'pet_reference_rubric',answer_type:'writing'}},
  attempt:{child_id:'child',session_id:'session',diagnostic_item_id:'item',attempt_id:'saved'}} as DiagnosticPromptResult
afterEach(()=>clearVoiceCaptureContext())
it('retains answered-writing identity for explicit controls, not as an unanswered draft',async()=>{
  const value=await trackDiagnosticCapture('session',async()=>answered,true)
  expect(value.status).toBe('answered')
  expect(voiceCaptureReference('session','child').question_reference).toMatchObject({binding:{prompt_id:'written',item_version:'v1'}})
  expect(voiceCaptureReference('session','other').question_reference).toBeNull()
})
it('old draft read cannot steal another current prompt or non-diagnostic task',async()=>{
  for(const other of [{kind:'diagnostic' as const,binding:{prompt_id:'new',item_version:'v2'}},
    {kind:'exam' as const,binding:{run_id:'exam',position:0,item_id:'item',source_version:'v1'}}]){
    setVoiceQuestionCapture('session','child',other)
    await trackDiagnosticCapture('session',async()=>answered)
    expect(voiceCaptureReference('session','child').question_reference).toEqual(other)
  }
})
it('unowned, missing or withdrawn answers do not produce writing control metadata',async()=>{
  for(const value of [{...answered,attempt:null},{...answered,reason:'withdrawn'},
    {...answered,attempt:{...answered.attempt!,child_id:'other'}}]){
    await trackDiagnosticCapture('session',async()=>value,true)
    expect(voiceCaptureReference('session','child').question_reference).toBeNull()
  }
})
