export type BehaviorGoalRequest = (path: string, init?: RequestInit) => Promise<unknown>
export type TriState = boolean | null
export type Initiator = 'child' | 'parent' | 'agent' | 'unknown'
export type Assistance = 'none' | 'prompted' | 'assisted' | 'unknown'
export type BehaviorGoalCommand = {
  opportunity_id: string; expected_version: string; opportunity_present: TriState
  criteria: Record<string, TriState>; initiated_by: Initiator; assistance_level: Assistance
  client_action_id: string; operation: 'confirm' | 'retract'; reason: string
}
export type BehaviorGoalOpportunity = {
  opportunity_id: string; binding_version: string; source_version: string; source_current: boolean
  phase: string; day: string; window_start: string; window_end: string
  context: { subject: string; task_type: string; source_type: string }
  observed_seconds: number; criteria: string[]
  automatic: { criteria: Record<string, TriState>; initiated_by: Initiator; assistance_level: Assistance
    basis: string[]; confidence: number | 'qualified_direct_records_not_psychological_inference' | null; independent_response: boolean }
  confirmation: null | {
    event_id: string; occurred_at: string; opportunity_present: TriState; criteria: Record<string, TriState>
    initiated_by: Initiator; assistance_level: Assistance; actor_type: string
    request: Omit<BehaviorGoalCommand, 'client_action_id'>
  }
}
export type BehaviorGoalView = {
  child_id: string; experiment_id: string; status: string; goal_version: string
  criteria: string[]; opportunities: BehaviorGoalOpportunity[]; automatic_limitations: string[]
  local_only: true; cloud_transmission_allowed: false
}
export type BehaviorGoalDraft = {
  opportunity_id: string; binding_version: string; source_version: string; goal_version: string
  opportunity_present: TriState; criteria: Record<string, TriState>
  initiated_by: Initiator; assistance_level: Assistance; reason: string
}
export type BehaviorGoalJournal = {
  schema: 1; child_id: string; experiment_id: string; draft: BehaviorGoalDraft
  pending: BehaviorGoalCommand | null; explanation: string
}
const initiators = ['child','parent','agent','unknown']
const assistance = ['none','prompted','assisted','unknown']
const hash = (v: unknown): v is string => typeof v==='string' && /^[a-f0-9]{64}$/.test(v)
const string = (v: unknown): v is string => typeof v==='string' && v.length>0
const object = (v: unknown): v is Record<string, unknown> => v!==null && typeof v==='object' && !Array.isArray(v)
const tri = (v: unknown): v is TriState => v===null || typeof v==='boolean'
const triMap = (v: unknown): v is Record<string, TriState> => object(v) && Object.values(v).every(tri)
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(string)
function invalid(): never { throw new Error('本地行为证据格式不一致，请刷新核对。') }
function choices(v: Record<string, unknown>): boolean {
  return tri(v.opportunity_present) && triMap(v.criteria) && initiators.includes(String(v.initiated_by)) && assistance.includes(String(v.assistance_level))
}
export function validateBehaviorGoalCommand(v: unknown): asserts v is BehaviorGoalCommand {
  if (!object(v) || Object.keys(v).sort().join(',')!==['opportunity_id','expected_version','opportunity_present','criteria','initiated_by','assistance_level','client_action_id','operation','reason'].sort().join(',')
    || !string(v.opportunity_id) || v.opportunity_id.length>200 || !hash(v.expected_version) || !choices(v)
    || !string(v.client_action_id) || v.client_action_id.length>200 || !['confirm','retract'].includes(String(v.operation))
    || typeof v.reason!=='string' || v.reason.length>500) invalid()
}
function view(raw: unknown, child: string, experiment: string): BehaviorGoalView {
  if (!object(raw) || raw.child_id!==child || raw.experiment_id!==experiment || raw.local_only!==true || raw.cloud_transmission_allowed!==false
    || !hash(raw.goal_version) || !strings(raw.criteria) || !strings(raw.automatic_limitations) || !Array.isArray(raw.opportunities)
    || !['ready','unsupported'].includes(String(raw.status))) invalid()
  for (const row of raw.opportunities) {
    if (!object(row) || !string(row.opportunity_id) || !hash(row.binding_version) || !hash(row.source_version)
      || typeof row.source_current!=='boolean' || !string(row.day) || !string(row.phase) || !string(row.window_start) || !string(row.window_end)
      || !Number.isFinite(Date.parse(row.window_end)) || !Number.isFinite(Date.parse(row.window_start))
      || !strings(row.criteria) || new Set(row.criteria).size!==row.criteria.length
      || !object(row.context) || !['subject','task_type','source_type'].every(k=>string((row.context as Record<string,unknown>)[k]))
      || typeof row.observed_seconds!=='number' || !Number.isFinite(row.observed_seconds) || row.observed_seconds<0
      || !object(row.automatic) || !triMap(row.automatic.criteria) || !strings(row.automatic.basis)
      || !initiators.includes(String(row.automatic.initiated_by)) || !assistance.includes(String(row.automatic.assistance_level))
      || typeof row.automatic.independent_response!=='boolean'
      || (row.automatic.confidence!==null && row.automatic.confidence!=='qualified_direct_records_not_psychological_inference'
        && (typeof row.automatic.confidence!=='number' || !Number.isFinite(row.automatic.confidence) || row.automatic.confidence<0 || row.automatic.confidence>1))) invalid()
    const automatic=row.automatic
    if (row.criteria.some(k=>!Object.hasOwn(automatic.criteria as object,k))) invalid()
    if (row.confirmation!==null) {
      const c=row.confirmation
      if (!row.source_current || !object(c) || !string(c.event_id) || !string(c.occurred_at) || !['parent','child'].includes(String(c.actor_type)) || !choices(c) || !object(c.request)) invalid()
      // Read requests include trusted server provenance; writes below never copy those fields.
      const request = {...Object.fromEntries(Object.keys(emptyCommand()).map(k=>[k,(c.request as Record<string,unknown>)[k]])),client_action_id:'read-only-validation'}
      validateBehaviorGoalCommand(request)
      if (request.opportunity_id!==row.opportunity_id || row.criteria.some(k=>!Object.hasOwn(c.criteria as object,k))) invalid()
    }
  }
  if (new Set(raw.opportunities.map(row=>row.opportunity_id)).size!==raw.opportunities.length) invalid()
  return raw as unknown as BehaviorGoalView
}
function emptyCommand(): BehaviorGoalCommand {
  return {opportunity_id:'',expected_version:'',opportunity_present:null,criteria:{},initiated_by:'unknown',assistance_level:'unknown',client_action_id:'',operation:'confirm',reason:''}
}
export function createBehaviorGoalApi(request: BehaviorGoalRequest) {
  const base=(child:string,experiment:string)=>`/v1/parent/children/${encodeURIComponent(child)}/behavior/habit-experiments/${encodeURIComponent(experiment)}`
  return {
    read: async (child:string,experiment:string):Promise<BehaviorGoalView> => view(await request(base(child,experiment)+'/opportunities',{cache:'no-store'}),child,experiment),
    confirm: async (child:string,experiment:string,body:BehaviorGoalCommand) => {
      validateBehaviorGoalCommand(body)
      const result = await request(base(child,experiment)+'/confirmation',{method:'POST',body:JSON.stringify(body),cache:'no-store'})
      if (!object(result) || result.status!=='recorded' || !string(result.event_id) || typeof result.replayed!=='boolean'
        || typeof result.historical_receipt!=='boolean' || result.local_only!==true || result.cloud_transmission_allowed!==false) invalid()
      return {historical:result.historical_receipt}
    },
    model: async (child:string) => {
      const result=await request(`/v1/parent/children/${encodeURIComponent(child)}/longitudinal-model`,{cache:'no-store'})
      if (!object(result) || result.child_id!==child || result.is_current!==true || !string(result.evidence_fingerprint)
        || !object(result.payload) || !object(result.payload.behavior)) invalid()
      return {fingerprint:result.evidence_fingerprint}
    }
  }
}
const prefix='hsc:behavior-goal-review:'
export const behaviorGoalRecoveryCleared='hsc-behavior-goal-recovery-cleared'
export function behaviorGoalLocalKey(child:string,experiment:string):string { return `${prefix}${encodeURIComponent(child)}:${encodeURIComponent(experiment)}` }
export function clearBehaviorGoalLocalState(child?:string):void {
  const scope=child===undefined ? prefix : `${prefix}${encodeURIComponent(child)}:`
  for (const key of Object.keys(localStorage)) if (key.startsWith(scope)) localStorage.removeItem(key)
  window.dispatchEvent(new CustomEvent(behaviorGoalRecoveryCleared,{detail:{childId:child}}))
}
export function parseBehaviorGoalJournal(raw:string,child:string,experiment:string):BehaviorGoalJournal {
  const v:unknown=JSON.parse(raw)
  if (!object(v) || v.schema!==1 || v.child_id!==child || v.experiment_id!==experiment || !object(v.draft)
    || !string(v.draft.opportunity_id) || !hash(v.draft.binding_version) || !hash(v.draft.source_version) || !hash(v.draft.goal_version)
    || !choices(v.draft) || typeof v.draft.reason!=='string' || v.draft.reason.length>500 || typeof v.explanation!=='string') invalid()
  if (v.pending!==null) {
    validateBehaviorGoalCommand(v.pending)
    if (v.pending.opportunity_id!==v.draft.opportunity_id || v.pending.expected_version!==v.draft.binding_version
      || Object.keys(v.pending.criteria).sort().join(',')!==Object.keys(v.draft.criteria as object).sort().join(',')) invalid()
  }
  return v as unknown as BehaviorGoalJournal
}
export function freshBehaviorGoalDraft(item:BehaviorGoalOpportunity,goal:string,reason=''):BehaviorGoalDraft {
  return {opportunity_id:item.opportunity_id,binding_version:item.binding_version,source_version:item.source_version,goal_version:goal,
    opportunity_present:null,criteria:Object.fromEntries(item.criteria.map(k=>[k,null])),initiated_by:'unknown',assistance_level:'unknown',reason}
}
