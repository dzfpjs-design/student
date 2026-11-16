import { useEffect, useRef, useState } from 'react'
import { ArrowRight, ChevronRight, RefreshCw, SkipForward, Undo2 } from 'lucide-react'
import { ApiError, localApi } from './api'
import { loadOcrReviewRecovery, saveOcrReviewRecovery, ocrRecoveryClearedEvent, ocrRecoveryChangedEvent, type OcrRetryCommand } from './ocrReviewRecovery'
import './ParentFamilyWorkQueue.css'
import { OriginalSourceReviewPanel } from './OriginalSourceReviewPanel'

export type FamilyWorkKind = 'safety'|'ocr'|'variant'|'specialized'|'rubric'|'report_review'|'delivery_unknown'|'curriculum'|'supply_followup'|'original_question'
export type FamilyWorkTarget = { child_id:string; kind:FamilyWorkKind; panel:string; object_id:string; related_id:string|null }
export type FamilyWorkItem = {
  work_id:string; child_id:string; child_name:string; kind:FamilyWorkKind; object_id:string; version:string; title:string; impact:string;
  priority:number; urgency:'immediate'|'today'|'review'; created_at:string; evidence:Array<{label:string;text:string}>;
  target:{panel:string;object_id:string;related_id:string|null}; bulk_action:'ocr_skip'|null
}
export type FamilyWorkPage = {
  items:FamilyWorkItem[]; children:Array<{child_id:string;nickname:string}>;
  sources:Array<{child_id:string;kind:FamilyWorkKind;status:'ready'|'unavailable';total:number|null;error_code:string|null}>;
  total:number|null; known_total:number; revision:string;next_cursor:string|null;read_at:string;local_only:true;cloud_transmission_allowed:false
  system_verification_total?:number|null
}
export type ParentFamilyWorkQueueProps = {
  children:Array<{child_id:string;nickname:string}>;
  request:<T>(path:string,init?:RequestInit)=>Promise<T>;
  onOpen:(target:FamilyWorkTarget)=>Promise<void>;
  onAuthNeeded:()=>void;
  refreshToken?:number
  audience?:'all'|'parent-decisions'
}
const labels:Record<FamilyWorkKind,string> = {safety:'安全提醒',ocr:'OCR 校对',variant:'错题变式',specialized:'专项题',rubric:'开放评分',report_review:'报告审核',delivery_unknown:'投递未知',curriculum:'课程材料',supply_followup:'原练习续行',original_question:'原题学习目标'}
type OcrReceipt = {review_id:string; child_id:string;artifact_id:string;status:string;proposed_text?:string}

const parentDecisionKinds = new Set<FamilyWorkKind>(['safety','delivery_unknown'])

export function ParentFamilyWorkQueue({children,request,onOpen,onAuthNeeded,refreshToken=0,audience='all'}:ParentFamilyWorkQueueProps):React.JSX.Element {
  const [childId,setChildId]=useState('')
  const [page,setPage]=useState<FamilyWorkPage|null>(null)
  const [loading,setLoading]=useState(false),[readError,setReadError]=useState(''),[message,setMessage]=useState('')
  const [selected,setSelected]=useState(''),[checked,setChecked]=useState<string[]>([])
  const [opening,setOpening]=useState(false),[writing,setWriting]=useState(false)
  const [pending,setPending]=useState<Array<{child:string;command:OcrRetryCommand}>>([])
  const [journalError,setJournalError]=useState(false),[recent,setRecent]=useState<OcrReceipt[]>([])
  const [gap,setGap]=useState<{child_id:string;job_id:string;status:string;evidence:Array<{label:string;text:string}>}|null>(null)
  const [systemVerificationCount,setSystemVerificationCount]=useState(0)
  const [original,setOriginal]=useState<{child:string;commitment:string}|null>(null)
  const epoch=useRef(0),active=useRef(true),writeLock=useRef(false)
  const openEpoch=useRef(0)
  const requestRef=useRef(request),openRef=useRef(onOpen),authRef=useRef(onAuthNeeded)
  requestRef.current=request;openRef.current=onOpen;authRef.current=onAuthNeeded
  const childrenKey=children.map(c=>c.child_id).sort().join('|')
  const name=(id:string):string=>children.find(c=>c.child_id===id)?.nickname??'孩子'
  const scope=useRef(childId);scope.current=childId

  const readJournal=():void=>{
    try {
      setPending(children.map(c=>({child:c.child_id,command:loadOcrReviewRecovery(c.child_id).pending})).filter((v):v is {child:string;command:OcrRetryCommand}=>v.command!==null))
      setJournalError(false)
    } catch {setJournalError(true)}
  }
  const changedJournal=(child:string):void=>{window.dispatchEvent(new CustomEvent(ocrRecoveryClearedEvent,{detail:child}))}
  useEffect(()=>{
    active.current=true;readJournal()
    const storage=(event:StorageEvent):void=>{if(event.key===null||event.key.startsWith('hsc:ocr-review-recovery:'))readJournal()}
    window.addEventListener(ocrRecoveryClearedEvent,readJournal)
    window.addEventListener(ocrRecoveryChangedEvent,readJournal)
    window.addEventListener('storage',storage)
    return ()=>{active.current=false;epoch.current++;window.removeEventListener(ocrRecoveryClearedEvent,readJournal);window.removeEventListener(ocrRecoveryChangedEvent,readJournal);window.removeEventListener('storage',storage)}
  },[childrenKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const load=async(cursor?:string):Promise<void>=>{
    const token=++epoch.current,owner=scope.current
    setLoading(true);setReadError('')
    const params=new URLSearchParams({limit:'20',...(owner?{child_id:owner}:{}),...(cursor?{cursor}:{}),...(audience==='parent-decisions'?{audience:'parent_decisions'}:{})})
    try {
      const value=await requestRef.current<FamilyWorkPage>('/v1/parent/work-queue?'+params.toString())
      const allowed=new Set(children.map(c=>c.child_id))
      if (!value.local_only||value.cloud_transmission_allowed||value.children.some(c=>!allowed.has(c.child_id))
        ||value.items.some(i=>!allowed.has(i.child_id)||(owner&&i.child_id!==owner)||i.target.object_id!==i.object_id)
        ||value.sources.some(s=>!allowed.has(s.child_id)||(owner&&s.child_id!==owner))) throw new Error('待办归属校验失败')
      if(!active.current||token!==epoch.current)return
      if(audience==='parent-decisions'){
        if(value.items.some(item=>!parentDecisionKinds.has(item.kind)))throw new Error('家长决策队列混入系统核对事项')
        setSystemVerificationCount(value.system_verification_total??0)
        setPage(value)
      }else{
        setSystemVerificationCount(0)
        setPage(value)
      }
      setChecked([])
    } catch(cause) {
      if(!active.current||token!==epoch.current)return
      if(cause instanceof ApiError&&cause.status===401)authRef.current()
      setReadError(cause instanceof ApiError&&cause.status===409?'待办来源已变化，请刷新第一页；没有继续使用旧分页。':'读取失败，保留上次结果；尚未读取的来源不能视作零。')
    } finally {if(active.current&&token===epoch.current)setLoading(false)}
  }
  useEffect(()=>{openEpoch.current++;setPage(null);setChecked([]);setSelected('');setMessage('');setOpening(false);setGap(null);setOriginal(null);void load();return()=>{epoch.current++}},[childId,childrenKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const lastRefresh=useRef(refreshToken)
  useEffect(()=>{if(lastRefresh.current!==refreshToken){lastRefresh.current=refreshToken;void load()}},[refreshToken]) // eslint-disable-line react-hooks/exhaustive-deps

  const open=async(item:FamilyWorkItem):Promise<void>=>{
    const token=++openEpoch.current
    setSelected(item.work_id);setOpening(true);setMessage('')
    try {
      setOriginal(null);setGap(null)
      if(item.target.panel==='parent-work-original-check'){
        setOriginal({child:item.child_id,commitment:item.object_id})
      }else if(item.target.panel==='parent-work-supply-gap'){
        const value=await requestRef.current<NonNullable<typeof gap>>(`/v1/parent/children/${encodeURIComponent(item.child_id)}/work-supply-gaps/${encodeURIComponent(item.object_id)}`)
        if(value.child_id!==item.child_id||value.job_id!==item.object_id)throw new Error('供给对象归属不一致')
        if(active.current&&token===openEpoch.current)setGap(value)
      }else await openRef.current({...item.target,child_id:item.child_id,kind:item.kind})
    }
    catch(cause){if(active.current&&token===openEpoch.current)setMessage(cause instanceof Error?cause.message:'对象未能打开，已保留选择，请重试。')}
    finally{if(active.current&&token===openEpoch.current)setOpening(false)}
  }
  const chosen=page?.items.filter(i=>checked.includes(i.work_id))??[]
  const batchAllowed=!!page&&page.total!==null&&!readError&&!loading&&!writing&&!journalError&&!pending.length
  const executeOcr=async(owner:string,command:OcrRetryCommand,items:FamilyWorkItem[]=[]):Promise<void>=>{
    if(writeLock.current||journalError)return
    if(command.kind!=='undo'&&(command.kind!=='batch'||command.decision!=='skip')){
      setMessage('该校对操作需回到原校对面板核对，不会在待办台执行。');return
    }
    const token=epoch.current
    writeLock.current=true;setWriting(true);setMessage('')
    let dispatched=false
    try {
      const record=loadOcrReviewRecovery(owner)
      if(record.pending&&JSON.stringify(record.pending)!==JSON.stringify(command))throw new Error('请先核对原校对请求')
      const artifactIds={...record.artifactIds}
      for(const item of items){if(item.child_id!==owner||item.bulk_action!=='ocr_skip'||!item.target.related_id)throw new Error('校对范围不一致');artifactIds[item.object_id]=item.target.related_id}
      saveOcrReviewRecovery({...record,artifactIds,pending:command});changedJournal(owner)
      dispatched=true
      let receipts:OcrReceipt[]
      if(command.kind==='batch'){
        const result=await localApi.batchOcrReview(owner,command.reviewIds,'skip',command.actionId)
        receipts=result.items
        if(receipts.length!==command.reviewIds.length||new Set(receipts.map(r=>r.review_id)).size!==command.reviewIds.length||receipts.some(r=>r.child_id!==owner||!command.reviewIds.includes(r.review_id)))throw new Error('校对回执归属不一致')
      }else{
        const result=await localApi.undoOcrReview(command.reviewId,owner,command.actionId)
        if(result.child_id!==owner||result.review_id!==command.reviewId)throw new Error('撤销回执归属不一致')
        receipts=[result]
      }
      const latest=loadOcrReviewRecovery(owner)
      if(latest.pending?.actionId===command.actionId){
        const drafts={...latest.drafts};receipts.forEach(r=>delete drafts[r.review_id])
        saveOcrReviewRecovery({...latest,drafts,pending:null});changedJournal(owner)
      }
      if(!active.current||token!==epoch.current)return
      setChecked([]);setRecent(receipts.filter(r=>r.status==='skipped'));setMessage('校对操作已保存；正在重读当前待办，不把历史回执当作当前状态。')
      await load()
    }catch(cause){
      if(!active.current||token!==epoch.current)return
      if(cause instanceof ApiError&&cause.status===401)authRef.current()
      setMessage(dispatched?'操作结果待确认；已保留原请求，不会自动重试。':'恢复记录未能安全保存或存在原待确认请求；未提交新操作。')
      readJournal()
    }finally{writeLock.current=false;if(active.current)setWriting(false)}
  }
  const readRecent=async():Promise<void>=>{
    const token=epoch.current
    try{
      const owner=scope.current
      const results=await Promise.all(children.filter(c=>!owner||c.child_id===owner).map(async c=>{
        const rows=await localApi.ocrReviewHistory(c.child_id)
        if(rows.some(r=>r.child_id!==c.child_id))throw new Error('wrong owner')
        return rows.filter(r=>r.status==='skipped')
      }))
      if(active.current&&token===epoch.current){setRecent(results.flat());setMessage('已读取每个孩子最近十条校对中的跳过记录，可逐条撤销。')}
    }catch{if(active.current&&token===epoch.current)setMessage('最近校对未能完整读取，未将失败视作没有记录。')}
  }

  return <section className="family-work-queue" aria-label="全家待办">
    <header><div><h2>{audience==='parent-decisions'?'需要家长决定':'全家待办'}</h2><p>{page?(page.total===null?`已读取 ${page.known_total} 项，仍有来源未能核对`:`${page.total} 项待处理`):'待办尚未完整读取'}</p></div>
      <label>孩子<select aria-label="待办孩子" value={childId} disabled={writing} onChange={e=>setChildId(e.target.value)}><option value="">全家</option>{children.map(c=><option key={c.child_id} value={c.child_id}>{c.nickname}</option>)}</select></label>
      <button title="刷新待办" aria-label="刷新待办" disabled={loading||writing} onClick={()=>void load()}><RefreshCw size={18}/></button>
    </header>
    {loading&&<p role="status">正在核对各来源。</p>}{readError&&<p role="alert">{readError}</p>}{message&&<p role="status">{message}</p>}
    {audience==='parent-decisions'&&systemVerificationCount>0&&<p className="family-work-system-status">另有 {systemVerificationCount} 项在系统核对队列中，由模型、内置知识库和确定性规则处理，不要求家长充当学科审核员。尚未通过的内容不会进入孩子学习或掌握度。</p>}
    {journalError&&<p role="alert">校对恢复记录无法读取，批量操作已停用。请先在校对面板核对。</p>}
    {pending.filter(p=>!childId||p.child===childId).map(p=><div className="family-work-pending" key={p.child}><p>{name(p.child)}：有校对操作待核对，尚未自动重试。</p>
      {(p.command.kind==='batch'&&p.command.decision==='skip'||p.command.kind==='undo')?<button disabled={writing||journalError} onClick={()=>void executeOcr(p.child,p.command)}><RefreshCw size={16}/>重试原校对请求</button>:<p>请回到原 OCR 校对面板处理该请求。</p>}</div>)}
    {page&&<details><summary>来源核对与数量</summary><ul>{page.sources.map(s=><li key={s.child_id+s.kind}>{name(s.child_id)} · {labels[s.kind]}：{s.status==='ready'?`${s.total} 项`:'未能读取，请刷新重试'}</li>)}</ul></details>}
    {page?.total===0&&!loading&&!readError&&<p>当前没有待办。后续审核、撤回或新证据会改变此结果。</p>}
    {original&&(!childId||childId===original.child)&&<OriginalSourceReviewPanel key={original.child+':'+original.commitment}
      childId={original.child} commitmentId={original.commitment} request={request} onAuthNeeded={onAuthNeeded}
      onClose={()=>setOriginal(null)} onSaved={()=>void load()}/>}
    {gap&&<section className="family-work-gap" aria-label="供给阻塞详情" data-work-object={gap.job_id}><h3>{name(gap.child_id)} · 原供给任务</h3><p>{gap.status==='blocked'?'任务仍被阻断':'任务状态已变化，请刷新待办核对。'}</p><dl>{gap.evidence.map((e,i)=><div key={i}><dt>{e.label}</dt><dd>{e.text}</dd></div>)}</dl><button onClick={()=>setGap(null)}>关闭详情</button></section>}
    <div className="family-work-list">{page?.items.map((item,index)=><article key={item.work_id} aria-current={selected===item.work_id?'true':undefined} className={item.urgency==='immediate'?'is-urgent':''}>
      <header><div><span>{item.child_name} · {labels[item.kind]} · {item.urgency==='immediate'?'立即确认':item.urgency==='today'?'优先处理':'待审核'}</span><h3>{item.title}</h3></div>
        {item.bulk_action==='ocr_skip'&&<input type="checkbox" aria-label={`选择第${index+1}项${item.child_name}的${item.title}`} checked={checked.includes(item.work_id)} disabled={!batchAllowed||(chosen.length>0&&chosen[0].child_id!==item.child_id)} onChange={e=>setChecked(c=>e.target.checked?[...c,item.work_id]:c.filter(id=>id!==item.work_id))}/>}</header>
      <p>{item.impact}</p><dl>{item.evidence.map((e,i)=><div key={i}><dt>{e.label}</dt><dd>{e.text||'暂无可展示摘要，请查看原对象。'}</dd></div>)}</dl>
      <button disabled={opening||writing} onClick={()=>void open(item)}>查看并处理<ArrowRight size={16}/></button>
    </article>)}</div>
    <footer>{chosen.length>0&&<button disabled={!batchAllowed} onClick={()=>void executeOcr(chosen[0].child_id,{kind:'batch',reviewIds:chosen.map(i=>i.object_id),decision:'skip',actionId:crypto.randomUUID()},chosen)}><SkipForward size={16}/>跳过所选文字校对</button>}
      {page?.next_cursor&&<button disabled={loading||writing||!!readError} onClick={()=>void load(page.next_cursor!)}>下一页<ChevronRight size={16}/></button>}
      <button disabled={writing} onClick={()=>void readRecent()}>最近跳过的校对</button>
    </footer>
    {recent.filter(r=>!childId||r.child_id===childId).map(r=><div key={r.review_id} className="family-work-recent"><span>{name(r.child_id)} · {r.proposed_text?.slice(0,80)||'已跳过的文字校对'}</span><button disabled={writing||pending.length>0||journalError} onClick={()=>void executeOcr(r.child_id,{kind:'undo',reviewId:r.review_id,actionId:crypto.randomUUID()})}><Undo2 size={16}/>撤销跳过</button></div>)}
  </section>
}
