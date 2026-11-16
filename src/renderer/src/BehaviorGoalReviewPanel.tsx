import { useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, Undo2 } from 'lucide-react'
import { requestParentLocal } from './api'
import { behaviorGoalLocalKey, behaviorGoalRecoveryCleared, createBehaviorGoalApi, freshBehaviorGoalDraft, parseBehaviorGoalJournal } from './behaviorGoalApi'
import type { Assistance, BehaviorGoalCommand, BehaviorGoalDraft, BehaviorGoalJournal, BehaviorGoalOpportunity, BehaviorGoalRequest, BehaviorGoalView, Initiator, TriState } from './behaviorGoalApi'
import './BehaviorGoalReviewPanel.css'

export type BehaviorGoalReviewPanelProps = {
  childId: string; experimentId: string; title: string; childAction: string; successSignal: string
  request?: BehaviorGoalRequest; onChanged?: () => Promise<void>; onAuthNeeded?: () => void
}
const criterionLabels: Record<string,string> = {
  marked_uncertain_step:'标出不确定的步骤', stated_specific_blocker:'说清具体卡在哪里',
  requested_help_after_identification:'先指出问题，再请求帮助', single_task_segment_completed:'完成同一任务的一段活动',
  switched_after_segment:'完成这一段之后才切换任务', child_initiated_short_break:'孩子主动发起短暂休息', returned_original_task:'回到原来的任务'
}
const initiatorLabels: Record<Initiator,string>={unknown:'不清楚',child:'孩子主动',parent:'家长发起',agent:'学习伙伴发起'}
const assistanceLabels: Record<Assistance,string>={unknown:'不清楚',none:'没有协助',prompted:'给过提醒',assisted:'参与协助'}
const limitations: Record<string,string>={
  no_qualified_mark_or_specific_blocker_proof:'自动记录不能确认孩子是否标出步骤、说清卡点。',
  help_receipt_alone_does_not_prove_ordered_composite:'求助记录不能单独证明“先指出问题再求助”的顺序。',
  writing_segment_is_not_attention_or_unassisted_work:'书写活动记录不等于专注或独立完成。',
  task_sequence_is_not_unassisted_work:'任务先后顺序不等于没有获得协助。',
  c05_return_is_not_child_initiation:'返回学习的记录不能证明休息由孩子主动发起。',
  no_reliable_visual_fatigue_denominator:'没有足够证据判断当时是否疲劳。'
}
const triText=(v:TriState)=>v===true?'是':v===false?'否':'未知'
function time(value:string):string { return new Date(value).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}) }
function context(item:BehaviorGoalOpportunity):string {
  const subject:Record<string,string>={math:'数学',chinese:'语文',english:'英语',physics:'物理',chemistry:'化学'}
  const task:Record<string,string>={written:'书面任务',reading:'阅读',oral:'口语任务',recall:'回忆检查',exercise:'练习'}
  return `${item.day} ${time(item.window_start)}–${time(item.window_end)} · ${subject[item.context.subject]??'学习任务'} · ${task[item.context.task_type]??'原任务'}${item.context.source_type==='school_homework'?' · 校内作业':''}`
}
function TriSelect({label,value,onChange,disabled}:{label:string;value:TriState;onChange:(v:TriState)=>void;disabled?:boolean}):React.JSX.Element {
  return <label>{label}<select aria-label={label} value={value===null?'unknown':value?'yes':'no'} disabled={disabled}
    onChange={e=>onChange(e.target.value==='unknown'?null:e.target.value==='yes')}><option value="unknown">未知</option><option value="yes">是</option><option value="no">否</option></select></label>
}
function sameRequest(pending:BehaviorGoalCommand,item:BehaviorGoalOpportunity):boolean {
  const original=item.confirmation?.request
  if (!original) return false
  return Object.entries(pending).filter(([k])=>k!=='client_action_id').every(([k,v])=>
    k==='criteria'?Object.entries(pending.criteria).every(([key,value])=>original.criteria[key]===value)
      : original[k as keyof typeof original]===v)
}
function reconcile(j:BehaviorGoalJournal|null,v:BehaviorGoalView):BehaviorGoalJournal|null {
  if (!j) return null
  const item=v.opportunities.find(i=>i.opportunity_id===j.draft.opportunity_id)
  if (!item) return {...j,pending:null,explanation:'原机会已不在当前证据中，旧操作已停用。说明保留在本机，不能用于其他机会。'}
  const stable=item.source_current && j.draft.source_version===item.source_version && j.draft.goal_version===v.goal_version
    && Object.keys(j.draft.criteria).sort().join(',')===item.criteria.slice().sort().join(',')
  if (stable && (j.draft.binding_version===item.binding_version || (j.pending && sameRequest(j.pending,item)))) return j
  return {...j,pending:null,draft:freshBehaviorGoalDraft(item,v.goal_version,j.draft.reason),
    explanation:'来源或当前确认版本已变化，旧操作已停用。保留原说明，判断已重置为未知，请重新核对。'}
}
export function BehaviorGoalReviewPanel(props:BehaviorGoalReviewPanelProps):React.JSX.Element {
  const [epoch,setEpoch]=useState(0)
  useEffect(()=>{
    const clear=(event:Event)=>{const child=(event as CustomEvent<{childId?:string}>).detail?.childId;if(child===undefined||child===props.childId)setEpoch(n=>n+1)}
    const storage=(event:StorageEvent)=>{if(event.key===null||event.key===behaviorGoalLocalKey(props.childId,props.experimentId))setEpoch(n=>n+1)}
    window.addEventListener(behaviorGoalRecoveryCleared,clear);window.addEventListener('storage',storage)
    return()=>{window.removeEventListener(behaviorGoalRecoveryCleared,clear);window.removeEventListener('storage',storage)}
  },[props.childId,props.experimentId])
  return <Review key={`${props.childId}:${props.experimentId}:${epoch}`} {...props}/>
}
function Review({childId,experimentId,title,childAction,successSignal,request=requestParentLocal,onChanged,onAuthNeeded}:BehaviorGoalReviewPanelProps):React.JSX.Element {
  const api=useMemo(()=>createBehaviorGoalApi(request),[request])
  const key=behaviorGoalLocalKey(childId,experimentId)
  const [expanded,setExpanded]=useState(false),[view,setView]=useState<BehaviorGoalView|null>(null)
  const [journal,setJournal]=useState<BehaviorGoalJournal|null>(null),[corrupt,setCorrupt]=useState(false)
  const [message,setMessage]=useState(''),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[modelRead,setModelRead]=useState(false)
  const alive=useRef(true),flight=useRef(false),sequence=useRef(0),saved=useRef<BehaviorGoalJournal|null>(null)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;sequence.current++}},[])
  const persist=(j:BehaviorGoalJournal|null):boolean=>{
    try {if(j)localStorage.setItem(key,JSON.stringify(j));else localStorage.removeItem(key);saved.current=j;setJournal(j);return true}
    catch {setMessage('未能保存本地恢复记录，操作未发送。请先恢复本机存储空间。');return false}
  }
  const load=async():Promise<BehaviorGoalView>=>{
    const token=++sequence.current;setLoading(true);setModelRead(false)
    try {
      const result=await api.read(childId,experimentId)
      if(!alive.current||token!==sequence.current)return result
      setView(result)
      try {
        const raw=localStorage.getItem(key),old=raw?parseBehaviorGoalJournal(raw,childId,experimentId):null
        const next=reconcile(old,result)
        if(JSON.stringify(old)!==JSON.stringify(next)) persist(next)
        else {saved.current=next;setJournal(next)}
        setCorrupt(false)
      }catch {setCorrupt(true);saved.current=null;setJournal(null);setMessage('本机恢复记录损坏，未发送任何操作。请明确放弃损坏记录后重新核对。')}
      return result
    }catch(cause){
      if(alive.current&&token===sequence.current){setView(null);setMessage((cause as {status?:number}).status===404?'这项实验不存在或不属于当前孩子。':'证据未能读取，不能据此确认；请重试读取。');if((cause as {status?:number}).status===401)onAuthNeeded?.()}
      throw cause
    }finally {if(alive.current&&token===sequence.current)setLoading(false)}
  }
  const refresh=()=>{if(!flight.current)void load().catch(()=>{})}
  const choose=(id:string)=>{
    if(saved.current?.pending)return
    const item=view?.opportunities.find(i=>i.opportunity_id===id)
    if(!item||!view){persist(null);return}
    persist({schema:1,child_id:childId,experiment_id:experimentId,draft:freshBehaviorGoalDraft(item,view.goal_version),pending:null,explanation:''});setMessage('')
  }
  const change=(values:Partial<BehaviorGoalDraft>)=>{
    const j=saved.current;if(!j||j.pending)return
    persist({...j,draft:{...j.draft,...values}})
  }
  const send=async(operation:'confirm'|'retract'|'retry')=>{
    if(flight.current||!view||corrupt)return
    flight.current=true;setBusy(true);setMessage('');setModelRead(false)
    let written=false
    try {
      let j=saved.current
      if(operation==='retry') {await load();if(!alive.current)return;j=saved.current;if(!j?.pending)return}
      if(!j)return
      const item=view.opportunities.find(i=>i.opportunity_id===j!.draft.opportunity_id)
      if(!item)return
      const body:BehaviorGoalCommand=operation==='retry'?j.pending!:{
        opportunity_id:item.opportunity_id,expected_version:item.binding_version,
        opportunity_present:operation==='retract'?null:j.draft.opportunity_present,
        criteria:operation==='retract'?Object.fromEntries(item.criteria.map(k=>[k,null])):j.draft.criteria,
        initiated_by:operation==='retract'?'unknown':j.draft.initiated_by,
        assistance_level:operation==='retract'?'unknown':j.draft.assistance_level,
        reason:j.draft.reason,operation,client_action_id:crypto.randomUUID()
      }
      if(!j.pending && !persist({...j,pending:body}))return
      await api.confirm(childId,experimentId,body)
      written=true
      if(!alive.current)return
      if(!persist({...j,pending:null,explanation:''})) {setMessage('服务器已记录，但本机回执清理失败；重新读取后只核对原回执，不会自动提交。');return}
      const refreshes=await Promise.allSettled([load(),api.model(childId),Promise.resolve().then(()=>onChanged?.())])
      if(!alive.current)return
      if(refreshes.every(r=>r.status==='fulfilled')){setModelRead(true);setMessage(body.operation==='retract'?'补充已撤回；后续结果按当前有效证据重算。':'补充已记录；仅代表家长观察，不证明孩子已经形成习惯。')}
      else setMessage('补充已记录，但部分结果尚未刷新。请只刷新读取，不必再次保存。')
    }catch(cause){
      if(!alive.current)return
      if(written)setMessage('补充已记录，但当前结果尚未刷新。请只刷新读取。')
      else if((cause as {status?:number}).status===409){await load().catch(()=>{});if(alive.current)setMessage('原机会或版本已变化，请核对刷新后的证据；未自动重试。')}
      else {setMessage('未收到可核对的回执，原操作已保存在本机。不会自动提交，请按原操作核对回执。');if((cause as {status?:number}).status===401)onAuthNeeded?.()}
    }finally {flight.current=false;if(alive.current)setBusy(false)}
  }
  const selected=view?.opportunities.find(i=>i.opportunity_id===journal?.draft.opportunity_id)
  const draft=journal?.draft,pending=journal?.pending
  const editable=!!selected?.source_current&&!busy&&!loading&&!pending&&!corrupt
  return <div className="behavior-goal-review">
    <button className="secondary-button" aria-expanded={expanded} onClick={()=>{setExpanded(v=>!v);if(!expanded&&!view)refresh()}}>核对具体机会</button>
    {expanded&&<div className="behavior-goal-content">
      <h4>{title} · 家长补充</h4><p>{childAction}。观察标准：{successSignal}</p>
      <p>只补充您了解的具体机会，不需要逐次打卡。未知不算失败；家长补充不等同于自动证据或习惯成效。</p>
      <button className="secondary-button" title="刷新当前证据" disabled={busy||loading} onClick={refresh}><RefreshCw size={16}/>{loading?'正在读取':'刷新当前证据'}</button>
      {message&&<p role="status">{message}</p>}
      {journal?.explanation&&<p role="status">{journal.explanation}</p>}
      {modelRead&&<p className="behavior-goal-readback">成长模型已按当前证据重读；不代表已经形成习惯。</p>}
      {corrupt&&<button onClick={()=>{if(persist(null)){setCorrupt(false);setMessage('损坏记录已放弃，请重新选择具体机会。')}}}>放弃损坏记录</button>}
      {view&&<>
        {view.automatic_limitations.map(code=><p className="behavior-goal-limit" key={code}>{limitations[code]??'这项自动证据尚有未覆盖的条件，需要保留未知。'}</p>)}
        {view.opportunities.length===0?<p>当前没有可核对的具体机会；没有记录不等于没有努力。</p>:<label>选择需要补充的机会<select aria-label="选择需要补充的机会" value={selected?.opportunity_id??''} disabled={busy||loading||!!pending||corrupt} onChange={e=>choose(e.target.value)}>
          <option value="">请选择一次具体机会</option>
          {view.opportunities.map((item,index)=><option key={item.opportunity_id} value={item.opportunity_id}>{index+1}. {context(item)} · {item.source_current?(item.confirmation?'已有补充':'待核对'):'来源已失效'}</option>)}
        </select></label>}
        {selected&&draft&&<div className="behavior-goal-opportunity">
          <h4>原机会 · {selected.day}</h4><p>{context(selected)}（北京时间）</p>
          <p>{selected.phase==='baseline'?'实验前比较期':'实验期间'} · 已覆盖观察 {Math.round(selected.observed_seconds)} 秒；覆盖时长不等于行为完成。</p>
          {!selected.source_current&&<p role="alert">原来源已失效，不能补充认证；以下仅保留原机会供核对。</p>}
          {selected.confirmation?<div className="behavior-goal-current"><strong>当前{selected.confirmation.actor_type==='parent'?'家长':'孩子'}补充 · {selected.confirmation.occurred_at.slice(0,10)}</strong>
            <p>机会存在：{triText(selected.confirmation.opportunity_present)}；{initiatorLabels[selected.confirmation.initiated_by]}；协助：{assistanceLabels[selected.confirmation.assistance_level]}</p>
            {selected.criteria.map(k=><p key={k}>{criterionLabels[k]??'待核对标准'}：{triText(selected.confirmation!.criteria[k])}</p>)}
            {selected.confirmation.request.reason&&<p>{selected.confirmation.request.reason}</p>}
            <button className="secondary-button" disabled={busy||loading||!!pending||corrupt} onClick={()=>void send('retract')}><Undo2 size={16}/>撤回这次补充</button>
          </div>:<p>当前没有有效补充；未知不计为失败。</p>}
          <fieldset disabled={!editable}><legend>仅补充这一次机会</legend>
            <TriSelect label="当时是否有这个机会" value={draft.opportunity_present} onChange={v=>change({opportunity_present:v})}/>
            {selected.criteria.map(k=><div className="behavior-goal-criterion" key={k}>
              <TriSelect label={criterionLabels[k]??'待核对标准'} value={draft.criteria[k]??null} onChange={v=>change({criteria:{...draft.criteria,[k]:v}})}/>
              <small>自动证据：{triText(selected.automatic.criteria[k])}；不推定发起人或独立性。</small>
            </div>)}
            <label>谁发起这次行动<select aria-label="谁发起这次行动" value={draft.initiated_by} onChange={e=>change({initiated_by:e.target.value as Initiator})}>{Object.entries(initiatorLabels).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
            <label>协助程度<select aria-label="协助程度" value={draft.assistance_level} onChange={e=>change({assistance_level:e.target.value as Assistance})}>{Object.entries(assistanceLabels).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
            <label>补充说明（可选）<textarea aria-label="补充说明（可选）" maxLength={500} value={draft.reason} onChange={e=>change({reason:e.target.value})}/></label>
            <button disabled={!editable||selected.criteria.some(k=>!criterionLabels[k])} onClick={()=>void send('confirm')}>保存这次补充</button>
          </fieldset>
        </div>}
      </>}
      {pending&&<div className="behavior-goal-pending"><p>有一项原操作待核对回执；不会自动执行，也不能更换机会后重用。</p><button disabled={busy||loading||!view||corrupt} onClick={()=>void send('retry')}>按原操作核对回执</button></div>}
    </div>}
  </div>
}
