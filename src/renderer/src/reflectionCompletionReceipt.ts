import type {SessionWorkspaceState} from './types'
export type ReflectionSavedReceipt = {
  session_id: string; child_id: string; session_status: 'completed'; daily_report_ready: boolean; client_action_id: string
}
/** A minimal business save receipt. It contains no text, lease, or authority to
 * repeat the reflection. Its pending journal is for read-only diagnosis. */
export function reflectionSavedReceipt(saved: SessionWorkspaceState, sessionId: string, childId: string): ReflectionSavedReceipt {
  if (saved.session.session_id !== sessionId || saved.session.child_id !== childId || saved.session.status !== 'completed'
    || saved.session.state !== 'completed' || typeof saved.daily_report_ready !== 'boolean') throw Error('REFLECTION_SAVE_UNCONFIRMED')
  return {session_id:sessionId,child_id:childId,session_status:'completed',daily_report_ready:saved.daily_report_ready,client_action_id:'reflection-close:'+sessionId}
}
const key = (turnId:string):string => 'hsc-native-reflection-close-v1:'+turnId
export function keepReflectionCompletion(turnId:string,receipt:ReflectionSavedReceipt):void {
  const text=JSON.stringify({version:1,agent_turn_id:turnId,status:'unconfirmed',receipt})
  localStorage.setItem(key(turnId),text)
  if(localStorage.getItem(key(turnId))!==text)throw Error('REFLECTION_COMPLETION_JOURNAL_UNCONFIRMED')
}
export function clearReflectionCompletion(turnId:string):void {try{localStorage.removeItem(key(turnId))}catch{/* An old journal never grants execution. */}}
