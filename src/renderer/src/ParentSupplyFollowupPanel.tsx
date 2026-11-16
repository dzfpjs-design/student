import { useEffect, useRef, useState } from 'react'
import { ArrowRight, RefreshCw } from 'lucide-react'
import { ApiError } from './api'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'
import './ParentSupplyFollowupPanel.css'

type Status = 'needs_plan'|'ready'|'awaiting_resources'|'scheduled'|'active'|'paused'|'practice_completed'|'cancelled'|'source_changed'|'missing_source'|'needs_replan'|'waiting_constraints'
export type SupplyFollowup = {
  child_id:string; root_obligation_id:string; original_session_id:string; original_date:string;
  title:string; original_reason:string; decision_id:string; current_session_id:string|null;
  plan_item_id:string|null; status:Status; deferral_reason:string; next_action:string; version:string;
  local_only:true; cloud_transmission_allowed:false; mastery_claim_allowed:false
  material_object_id:string
  original_question?:string|null; subject?:string|null
}
export type ParentSupplyFollowupProps = ParentWorkFocusProps & {
  childId:string; request:<T>(path:string,init?:RequestInit)=>Promise<T>; onAuthNeeded:()=>void
  onOpenMaterial?:(objectId:string)=>Promise<void>
}
const labels:Record<Status,string> = {needs_plan:'待确认今天计划',ready:'题目已备好',awaiting_resources:'待补原音',
  scheduled:'尚未开始',active:'正在练习',paused:'进度已保留',practice_completed:'已有完成记录',cancelled:'已取消',
  source_changed:'来源需重新核对',missing_source:'待补材料',needs_replan:'安排需核对',waiting_constraints:'时间需核对'}

export function ParentSupplyFollowupPanel({childId,request,onAuthNeeded,focusRequest,onFocusResolved,onOpenMaterial}:ParentSupplyFollowupProps):React.JSX.Element|null {
  const [value,setValue]=useState<SupplyFollowup|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const epoch=useRef(0),scope=useRef(''),root=useRef<HTMLElement>(null)
  const readRef=useRef(request),authRef=useRef(onAuthNeeded),resolvedRef=useRef(onFocusResolved)
  readRef.current=request;authRef.current=onAuthNeeded;resolvedRef.current=onFocusResolved
  const identity=childId+':'+(focusRequest?.objectId??'');scope.current=identity
  const load=async():Promise<void>=>{
    if(!focusRequest)return
    const token=++epoch.current,owner=identity,objectId=focusRequest.objectId,nonce=focusRequest.nonce
    setBusy(true);setError('')
    try {
      const next=await readRef.current<SupplyFollowup>(`/v1/parent/children/${encodeURIComponent(childId)}/supply-followups/${encodeURIComponent(objectId)}`,{cache:'no-store'})
      if(next.child_id!==childId||next.root_obligation_id!==objectId||next.local_only!==true||next.cloud_transmission_allowed!==false
        ||next.mastery_claim_allowed!==false||!Object.hasOwn(labels,next.status)||typeof next.next_action!=='string'||typeof next.version!=='string'
        ||typeof next.material_object_id!=='string'||!next.material_object_id)throw Error('scope')
      if(token!==epoch.current||scope.current!==owner)return
      setValue(next)
    }catch(cause){
      if(token!==epoch.current||scope.current!==owner)return
      const missing=cause instanceof ApiError&&cause.status===404
      if(missing)setValue(null)
      if(cause instanceof ApiError&&cause.status===401){setValue(null);authRef.current()}
      const message=missing?'这条原事项已删除或不属于当前孩子。':'未能核对当前状态，不能把上次读取的安排视为现在可执行。'
      setError(message);resolvedRef.current?.(nonce,message)
    }finally{if(token===epoch.current&&scope.current===owner)setBusy(false)}
  }
  useEffect(()=>{setValue(null);setError('');setBusy(false);void load();return()=>{epoch.current++}},[identity,focusRequest?.nonce]) // eslint-disable-line react-hooks/exhaustive-deps
  useParentWorkFocus(focusRequest,!!value&&!busy&&!error,root,onFocusResolved)
  if(!focusRequest)return null
  return <section ref={root} className="parent-supply-followup" aria-label="原练习续行" aria-busy={busy}>
    <header><h3>原练习续行</h3><button type="button" className="icon-button" aria-label="刷新原练习状态" title="刷新原练习状态" disabled={busy} onClick={()=>void load()}><RefreshCw size={16}/></button></header>
    {busy&&<p role="status">正在核对原记录与今天安排…</p>}
    {error&&<p role="alert">{error}</p>}
    {value&&value.child_id===childId&&value.root_obligation_id===focusRequest.objectId&&<div data-work-object={value.root_obligation_id}>
      <h4>{value.title}</h4><p className="supply-current-status" data-status={value.status}>{error?'上次读取：':''}{labels[value.status]}</p>
      {value.original_question&&<><h4>{value.subject?`${value.subject} · 原题记录`:'原题记录'}</h4><p>{value.original_question}</p></>}
      <dl><dt>最初安排</dt><dd>{value.original_date}</dd><dt>原记录原因</dt><dd>{value.original_reason}</dd>
        {value.deferral_reason!==value.original_reason&&<><dt>最近保留原因</dt><dd>{value.deferral_reason}</dd></>}</dl>
      <h4>今天下一步</h4><p>{error?'当前状态尚未核对，先刷新确认。':value.next_action}</p>
      {onOpenMaterial&&!error&&<button type="button" disabled={busy} onClick={()=>{
        const owner=scope.current
        void onOpenMaterial(value.material_object_id).catch(()=>{if(scope.current===owner)setError('材料未能准确打开，原事项仍保留，请重试。')})
      }}><ArrowRight size={16}/>核对材料</button>}
    </div>}
  </section>
}
