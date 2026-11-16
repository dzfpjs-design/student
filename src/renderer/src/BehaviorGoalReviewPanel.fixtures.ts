import type { BehaviorGoalCommand, BehaviorGoalView } from './behaviorGoalApi'
export const criteria = ['marked_uncertain_step','stated_specific_blocker','requested_help_after_identification']
export function command(): BehaviorGoalCommand {
  return {opportunity_id:'opportunity',expected_version:'a'.repeat(64),opportunity_present:null,
    criteria:Object.fromEntries(criteria.map(k=>[k,null])),initiated_by:'unknown',assistance_level:'unknown',
    client_action_id:'parent-command',operation:'confirm',reason:''}
}
export function goalView(child='a'): BehaviorGoalView {
  return {child_id:child,experiment_id:'goal',status:'ready',goal_version:'b'.repeat(64), criteria,
    local_only:true,cloud_transmission_allowed:false, automatic_limitations:['no_qualified_mark_or_specific_blocker_proof'],
    opportunities:[{opportunity_id:'opportunity',binding_version:'a'.repeat(64),source_version:'c'.repeat(64),
      source_current:true,phase:'current',day:'2026-08-28',window_start:'2026-08-28T10:00:00+00:00',window_end:'2026-08-28T10:05:00+00:00',
      context:{subject:'math',task_type:'written',source_type:'school_homework'},observed_seconds:300,criteria,
      automatic:{criteria:Object.fromEntries(criteria.map(k=>[k,null])),initiated_by:'unknown',assistance_level:'unknown',
        basis:[],confidence:null,independent_response:false}, confirmation:null}]}
}
