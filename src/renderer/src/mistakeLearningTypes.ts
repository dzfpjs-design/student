export type MistakeLearningOperation='analyze'|'practice'|'coach'|'answer'|'focus'|'review'
export type MistakeLearningSource={subject:string;title:string;question:string;original_answer:string;created_at?:string;has_question_crop?:boolean}
export type OriginalMistakeModality = 'paper' | 'voice'
export type OriginalMistakeResult = {judgement:'correct'|'incorrect'|'uncertain';feedback:string;next_hint:string;modality:OriginalMistakeModality;created_at:string}
export type MistakeLearningFeedback={kind:'model_formative';judgement:'correct'|'partly_correct'|'incorrect'|'uncertain';feedback:string;explanation:string;reference_answer:string;next_hint:string}
export type MistakeLearningFocus={mistake_id:string;child_id:string;session_id:string;source_revision:string;focus_generation:number;focus_created_at:string;learning_session_id:string;revision:number;practice_item_id:string|null;practice_created_at:string|null}
export type MistakeGraphPoint = {key:string;title:string;review_status:string;confidence:number}
export type MistakeGraphPrerequisite = {key:string;title:string;review_status:string;relation_review_status:string}
export type MistakeGraphContext = {
  status:'matched'|'ambiguous'|'no_match'|'unavailable'
  graph_version:string
  subject?:string
  primary:MistakeGraphPoint|null
  prerequisites:MistakeGraphPrerequisite[]
  alternatives:MistakeGraphPoint[]
}
export type MistakePrerequisiteCheck = {title:string;question:string;evidence_quote?:string}
export type MistakeGraphReviewFocus = {title:string;kind:'prerequisite'|'primary';state:'due'|'scheduled'|'checked'}
export type MistakeLearningPracticeItem={practice_item_id:string;position:number;question:string;stage:string;difficulty:number;status:'ready'|'answered'|'stale';attempts:number;help_count:number;feedback:MistakeLearningFeedback|null;graph_target?:{title:string;kind:'prerequisite'|'primary'}|null}
export type MistakeLearningState={
 version:1;mistake_id:string;child_id:string;learning_session_id:string|null;revision:number;source_revision:string
 source:MistakeLearningSource
 graph_context?:MistakeGraphContext|null
 graph_review_focus?:MistakeGraphReviewFocus|null
 graph_practice_stale?:boolean
 analysis:null|{summary:string;wrong_causes:Array<{label:string;explanation:string}>;knowledge_points:Array<{label:string;description:string}>;correction_strategy:string[];prerequisite_checks?:MistakePrerequisiteCheck[]}
 practice_items:MistakeLearningPracticeItem[]
 coach_variant?:MistakeLearningPracticeItem|null
 coach_phase?:'question'|'explain_step'|'independent_practice'
 coach_turn_id?:string|null
 turns:Array<{turn_id:string;role:'user'|'assistant';kind:'coach'|'answer'|'feedback';text:string;practice_item_id:string|null;created_at:string}>
 today:{date:string;target_count:number;completed_count:number;remaining_count:number;status:'not_started'|'in_progress'|'done'|'needs_explanation';stop_reason:null|'two_failures'}
 next_review_at:string|null;review_due:boolean;status:'ready'|'processing'|'failed'|'unknown';error_code:string|null
 original_review?:{status:'not_started'|'not_mastered'|'retest_pending';wrong_count:number;last_result:OriginalMistakeResult|null}
 review_result?:OriginalMistakeResult|null
 focus:MistakeLearningFocus|null
 active_action?:{client_action_id:string;operation:MistakeLearningOperation;status:'processing'|'unknown';error_code:string|null;updated_at:string}|null
 pending_client_action_id?:string
 operation?:MistakeLearningOperation;action_status?:'completed'|'failed'|'unknown'|'processing'
}
export type MistakeLearningLibrary={version:1;child_id:string;items:MistakeLearningState[];graph_pending_count?:number;today:{date:string;due_count:number;remaining_questions:number}}
export type MistakeLearningMutation={child_id:string;session_id?:string;client_action_id:string;expected_revision?:number;practice_item_id?:string;utterance?:string;style?:'direct'|'socratic';count?:1|2|3;modality?:OriginalMistakeModality;source_revision?:string;artifact_id?:string;voice_input_id?:string;original_review?:true;coach_turn_id?:string}
export type MistakeLearningSurface={version:1;child_id:string;session_id?:string;mistake_id:string;learning_session_id:string;source_revision:string;focus_generation:number;progress_revision:number;focus:MistakeLearningFocus;practice_item_id:string|null;daily_practice_item_id?:string;question_visible:boolean}
export const MISTAKE_LEARNING_REFRESH_EVENT='hsc-mistake-learning-refresh'
export const MISTAKE_LEARNING_SURFACE_EVENT='hsc-mistake-learning-surface'
export type MistakeLearningPanel='redo'|'analysis'|'practice'|'coach'
export type MistakeLearningRefresh={child_id:string;session_id?:string;mistake_id?:string;view?:'today';review?:boolean;capture?:boolean;panel?:MistakeLearningPanel}
export const MISTAKE_LEARNING_RENDERED_EVENT='hsc-mistake-learning-rendered'
export type MistakeLearningRendered={kind?:'case'|'library';owner_id:string;child_id:string;session_id?:string;visible:true;view_rendered:true;view?:'today'|'all';mistake_id?:string;learning_session_id?:string|null;source_revision?:string;revision?:number}
export function requestMistakeLearningRefresh(detail:MistakeLearningRefresh):void{window.dispatchEvent(new CustomEvent(MISTAKE_LEARNING_REFRESH_EVENT,{detail}))}

export type MistakeLearningIntake={question:string;submitted_answer?:string;subject?:string;session_id?:string;client_action_id:string}
export type MistakeLearningIntakeReceipt={version:1;child_id:string;mistake_id:string;saved:true}
