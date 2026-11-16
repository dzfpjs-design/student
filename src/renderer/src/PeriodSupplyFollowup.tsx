import { useEffect, useRef, useState } from 'react'
import { ArrowRight, RefreshCw } from 'lucide-react'
import { ApiError, requestParentLocal } from './api'
import type { SupplyFollowup } from './ParentSupplyFollowupPanel'
import './PeriodSupplyFollowup.css'

type Historical = {
  root_obligation_id:string; original_date:string; title:string; original_reason:string;
  historical_status:'unfulfilled'|'explicitly_deferred'|'practice_completed'|'cancelled'; deferral_reason:string
  mastery_claim_allowed:false
}
type Result = {
  child_id:string; snapshot_id:string|null; period_report_id:string; as_of:string|null;
  status:'ready'|'unavailable'; unfulfilled_count:number|null;
  items:Array<{historical:Historical; current:SupplyFollowup|null}>;
  local_only:true; cloud_transmission_allowed:false; mastery_claim_allowed:false
}
export type PeriodSupplyFollowupProps = {
  childId:string; snapshotId?:string; periodReportId?:string;
  request?:typeof requestParentLocal; onOpenSupply?:(rootId:string)=>Promise<void>
}
const past = {unfulfilled:'当时尚无完成记录',explicitly_deferred:'当时明确延期',practice_completed:'当时已有练习完成记录',cancelled:'当时已取消'}
const present:Record<SupplyFollowup['status'],string> = {needs_plan:'待核对今天计划',ready:'材料已备好，尚未开始',awaiting_resources:'待补原音',scheduled:'尚未开始',active:'正在练习',paused:'进度已保留',practice_completed:'已有练习完成记录',cancelled:'已取消',source_changed:'来源需重新核对',missing_source:'待补材料',needs_replan:'安排需核对',waiting_constraints:'时间需核对'}

export function PeriodSupplyFollowup({childId,snapshotId,periodReportId,request=requestParentLocal,onOpenSupply}:PeriodSupplyFollowupProps):React.JSX.Element {
  const [open,setOpen]=useState(false),[loaded,setLoaded]=useState<{scope:string;value:Result}|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const generation=useRef(0),owner=useRef(''),read=useRef(request)
  const scope=JSON.stringify([childId,snapshotId,periodReportId]);owner.current=scope;read.current=request
  const data=loaded?.scope===scope?loaded.value:null
  const load=async()=>{
    const epoch=++generation.current,identity=scope
    setBusy(true);setError('')
    try {
      const suffix=snapshotId?`period-supply-followups/${encodeURIComponent(snapshotId)}`:`period-reports/${encodeURIComponent(periodReportId??'')}/supply-followups`
      const value=await read.current<Result>(`/v1/parent/children/${encodeURIComponent(childId)}/${suffix}`,{cache:'no-store'})
      if(value.child_id!==childId||(snapshotId&&value.snapshot_id!==snapshotId)||(periodReportId&&value.period_report_id!==periodReportId)
        ||value.local_only!==true||value.cloud_transmission_allowed!==false||value.mastery_claim_allowed!==false
        ||!['ready','unavailable'].includes(value.status)||!Array.isArray(value.items)
        ||new Set(value.items.map(r=>r.historical.root_obligation_id)).size!==value.items.length
        ||value.items.some(r=>!Object.hasOwn(past,r.historical.historical_status)||r.historical.mastery_claim_allowed!==false
          ||(r.current&&(r.current.child_id!==childId||r.current.root_obligation_id!==r.historical.root_obligation_id
            ||r.current.mastery_claim_allowed!==false||!Object.hasOwn(present,r.current.status)))))throw Error('scope')
      if(epoch===generation.current&&owner.current===identity)setLoaded({scope:identity,value})
    }catch(cause){
      if(epoch!==generation.current||owner.current!==identity)return
      if(cause instanceof ApiError&&[401,403,404].includes(cause.status??0))setLoaded(null)
      setError(cause instanceof ApiError&&cause.status===404?'这份报告已删除或不属于当前孩子。':cause instanceof ApiError&&[401,403].includes(cause.status??0)?'家长授权已失效，请重新解锁后核对。':'当前原事项未能读全，不能把旧状态当成今天可执行。请刷新重试。')
    }finally{if(epoch===generation.current&&owner.current===identity)setBusy(false)}
  }
  useEffect(()=>{setLoaded(null);setError('');setBusy(false);if(open)void load();return()=>{generation.current++}},[scope,open]) // eslint-disable-line react-hooks/exhaustive-deps
  return <details className="period-supply-followup" open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary>本期原练习与今天续行</summary>
    {open&&<section aria-label="本期原练习与今天续行" aria-busy={busy}>
      <header><h4>历史总结与当前安排</h4><button type="button" className="icon-button" disabled={busy} title="刷新当前续行状态" aria-label="刷新当前续行状态" onClick={()=>void load()}><RefreshCw size={16}/></button></header>
      {busy&&<p role="status">正在核对这份报告的原事项…</p>}
      {error&&<p role="alert">{error}</p>}
      {data?.status==='unavailable'&&<p>这份历史报告尚未记录可核对的原事项总结。可刷新本周或本月报告形成新稿；旧报告保留，不表示当时没有待办。</p>}
      {data?.status==='ready'&&<>
        <p>当时记录截至 {data.as_of?new Date(data.as_of).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'时间待核对'}。今天状态单独核对，不改写原报告。</p>
        <p>{error?'当前总数尚未核对。':data.unfulfilled_count===null?'部分原记录无法核对，当前总数未知。':`这些原事项中，当前有${data.unfulfilled_count}项尚无完成或取消记录。`}</p>
        {!data.items.length&&<p>本期范围内没有记录到这类原练习安排，不推断孩子的能力或学习态度。</p>}
        {data.items.map(({historical:h,current})=><article key={h.root_obligation_id}>
          <h5>{h.title}</h5><dl><dt>最初安排</dt><dd>{h.original_date} · {h.original_reason}</dd>
            <dt>当时记录</dt><dd>{past[h.historical_status]}。{h.deferral_reason}</dd>
            <dt>今天状态</dt><dd>{error?'尚待重新核对':current?present[current.status]:'原记录已删除或当前不可核对'}</dd>
            <dt>今天下一步</dt><dd>{error?'先刷新确认，不按上次状态执行。':current?.next_action??'保留这份历史总结；原记录缺失时不另选同技能题替代。'}</dd></dl>
          {current?.status==='practice_completed'&&<p>这是练习完成记录，不表示已掌握。</p>}
          {onOpenSupply&&current&&!error&&<button type="button" onClick={()=>{if(owner.current!==scope)return;const identity=scope;void onOpenSupply(h.root_obligation_id).catch(()=>{if(owner.current===identity)setError('未能准确打开原事项，记录已保留，请重试。')})}}><ArrowRight size={16}/>查看原事项</button>}
        </article>)}
      </>}
    </section>}
  </details>
}
