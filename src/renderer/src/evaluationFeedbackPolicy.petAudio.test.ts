import {expect,it} from 'vitest'
import {evaluationFeedbackPolicy} from './evaluationFeedbackPolicy'
const value=()=>({feedback_delivery_allowed:true,feedback_status:'ready',objective_update_applied:false,local_only:true,cloud_transmission_allowed:false,
 oral_feedback:{version:'oral_teaching_v2',status:'formative_audio_feedback',local_only:true,cloud_transmission_allowed:false,mastery_claim_allowed:false,
 prompt_id:'original',item_version:'v1',feedback_version:'f1',source_version:'s1',
 audio_work_ref:{work_id:'work',generation:0,source_hash:'a'.repeat(64),result_hash:'b'.repeat(64)}}})
it('delivers exact current formative original-audio feedback without mastery',()=>{
 expect(evaluationFeedbackPolicy(value())).toEqual({deliver:true,localOnly:true})
})
it.each(['missing','generation','source','result','mastery','authority'])('rejects %s in formative audio receipt',fault=>{
 const v:any=value()
 if(fault==='missing')delete v.oral_feedback.audio_work_ref
 if(fault==='generation')v.oral_feedback.audio_work_ref.generation=-1
 if(fault==='source')v.oral_feedback.audio_work_ref.source_hash=''
 if(fault==='result')v.oral_feedback.audio_work_ref.result_hash=''
 if(fault==='mastery')v.oral_feedback.mastery_claim_allowed=true
 if(fault==='authority')v.feedback_delivery_allowed=false
 expect(evaluationFeedbackPolicy(v).deliver).toBe(false)
})
