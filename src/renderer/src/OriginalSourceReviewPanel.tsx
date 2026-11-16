import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Search, Undo2, X } from 'lucide-react'
import { ApiError } from './api'
import type { ParentFamilyWorkQueueProps } from './ParentFamilyWorkQueue'
import './OriginalSourceReviewPanel.css'

type Reference = { item_id:string; item_version:string; node_id:string; target:string; question:string }
export type OriginalSourceView = {
  child_id:string; commitment_id:string; version:string; status:string; original_question:string; subject:string
  references:Reference[]; truncated:boolean; mapping:null|{decision:string;note:string;original_quote?:string;reference_quote?:string}
  local_only:true; cloud_transmission_allowed:false; mastery_update_allowed:false
}
type Decision = { action_id:string; version:string; decision:'approve'|'revoke'; item_id:string|null; item_version:string|null;
  original_quote:string; reference_quote:string; note:string }
type Draft = { child:string; commitment:string; version:string; item:string; itemVersion:string; quote:string; note:string; pending:Decision|null }
type Result = { receipt:null|{child_id:string;commitment_id:string;action_id:string;decision:string;revision:number};current:OriginalSourceView }
type Props = {childId:string;commitmentId:string;request:ParentFamilyWorkQueueProps['request'];onAuthNeeded:()=>void;onClose:()=>void;onSaved:()=>void}
export const originalReviewKey = (child:string,commitment:string):string => `hsc-original-source-review:${encodeURIComponent(child)}:${encodeURIComponent(commitment)}`
const empty = (child:string,commitment:string):Draft => ({child,commitment,version:'',item:'',itemVersion:'',quote:'',note:'',pending:null})
function validDraft(value:Draft,child:string,commitment:string):boolean {
  if(!value||value.child!==child||value.commitment!==commitment||!['version','item','itemVersion','quote','note'].every(k=>typeof value[k as keyof Draft]==='string'))return false
  const p=value.pending
  return p===null||!!p&&typeof p.action_id==='string'&&!!p.action_id&&typeof p.version==='string'
    &&['approve','revoke'].includes(p.decision)&&['original_quote','reference_quote','note'].every(k=>typeof p[k as keyof Decision]==='string')
    &&(p.item_id===null||typeof p.item_id==='string')&&(p.item_version===null||typeof p.item_version==='string')
}
function validView(value:OriginalSourceView,child:string,commitment:string):boolean {
  return !!value&&value.child_id===child&&value.commitment_id===commitment&&value.local_only===true
    &&value.cloud_transmission_allowed===false&&value.mastery_update_allowed===false
    &&typeof value.version==='string'&&!!value.version&&typeof value.original_question==='string'
    &&Array.isArray(value.references)&&value.references.every(r=>!!r&&['item_id','item_version','node_id','target','question'].every(k=>typeof r[k as keyof Reference]==='string'))
    &&(value.mapping===null||!!value.mapping&&typeof value.mapping.note==='string'&&['approved','revoked'].includes(value.mapping.decision))
}

export function OriginalSourceReviewPanel({childId,commitmentId,request,onAuthNeeded,onClose,onSaved}:Props):React.JSX.Element {
  const key=originalReviewKey(childId,commitmentId),path=`/v1/parent/children/${encodeURIComponent(childId)}/original-checks/${encodeURIComponent(commitmentId)}`
  const [view,setView]=useState<OriginalSourceView|null>(null),[draft,setDraft]=useState(()=>empty(childId,commitmentId))
  const [busy,setBusy]=useState(false),[readError,setReadError]=useState(''),[storageError,setStorageError]=useState(''),[message,setMessage]=useState(''),[search,setSearch]=useState('')
  const [unapplied,setUnapplied]=useState<string|null>(null)
  const current=useRef(draft),raw=useRef<string|null>(null),epoch=useRef(0),lock=useRef(false),cleared=useRef(false)
  const callbacks=useRef({request,onAuthNeeded,onSaved});callbacks.current={request,onAuthNeeded,onSaved}
  const persist=(next:Draft):boolean=>{
    current.current=next;setDraft(next)
    try {
      if(cleared.current||localStorage.getItem(key)!==raw.current)throw Error('changed')
      const encoded=JSON.stringify(next)
      localStorage.setItem(key,encoded)
      if(localStorage.getItem(key)!==encoded)throw Error('readback')
      raw.current=encoded;setStorageError('');return true
    }catch{setStorageError('本机草稿未能安全保存，未提交新操作。当前文字仍保留，请核对存储后重新打开。');return false}
  }
  const fail=(cause:unknown):void=>{if(cause instanceof ApiError&&cause.status===401)callbacks.current.onAuthNeeded()}
  const load=async(query=''):Promise<void>=>{
    if(lock.current||cleared.current)return
    const token=++epoch.current
    lock.current=true;setBusy(true);setReadError('');setUnapplied(null)
    try {
      const value=await callbacks.current.request<OriginalSourceView>(path+(query?'?search='+encodeURIComponent(query):''))
      if(token!==epoch.current||cleared.current)return
      if(!validView(value,childId,commitmentId))throw Error('source owner')
      setView(value)
      if(!current.current.version&&!current.current.pending)persist({...current.current,version:value.version})
    }catch(cause){if(token===epoch.current){fail(cause);setReadError('原题来源未能核对，不能提交新审核。请刷新重试。')}}
    finally{if(token===epoch.current){lock.current=false;setBusy(false)}}
  }
  useEffect(()=>{
    cleared.current=false;lock.current=false;setView(null);setReadError('');setStorageError('');setMessage('');setSearch('');setUnapplied(null)
    try {
      raw.current=localStorage.getItem(key)
      const saved=raw.current===null?empty(childId,commitmentId):JSON.parse(raw.current) as Draft
      if(!validDraft(saved,childId,commitmentId))throw Error('invalid journal')
      current.current=saved;setDraft(saved);void load()
    }catch{setStorageError('本机恢复记录无法核对，未覆盖原记录或提交操作。')}
    const clear=(event:Event):void=>{
      if((event as CustomEvent).detail!==childId)return
      cleared.current=true;epoch.current++;lock.current=false;setBusy(false);setView(null);setUnapplied(null)
      current.current=empty(childId,commitmentId);setDraft(current.current)
      setStorageError('相关记录已清理，请关闭详情后重新核对当前来源。')
    }
    const storage=(event:StorageEvent):void=>{
      if(event.key!==key&&event.key!==null)return
      if(event.key===null||event.newValue===null){clear(new CustomEvent('clear',{detail:childId}));return}
      epoch.current++;lock.current=false;setBusy(false)
      setStorageError('另一窗口已修改恢复记录，请重新打开核对，当前草稿未覆盖。')
    }
    window.addEventListener('hsc-original-source-review-cleared',clear);window.addEventListener('storage',storage)
    return()=>{epoch.current++;window.removeEventListener('hsc-original-source-review-cleared',clear);window.removeEventListener('storage',storage)}
  },[key]) // eslint-disable-line react-hooks/exhaustive-deps

  const submit=async(command:Decision,receiptOnly=false):Promise<void>=>{
    if(lock.current||cleared.current||storageError)return
    if(current.current.pending&&JSON.stringify(current.current.pending)!==JSON.stringify(command))return
    if(!current.current.pending&&!persist({...current.current,pending:command}))return
    try {if(localStorage.getItem(key)!==raw.current)throw Error('changed')}
    catch{setStorageError('本机请求记录未能核对，请重新打开核对。');return}
    const token=epoch.current
    lock.current=true;setBusy(true);setMessage('');setUnapplied(null)
    try {
      const result=receiptOnly?await callbacks.current.request<Result>(path+'/receipts/'+encodeURIComponent(command.action_id)):
        await callbacks.current.request<Result>(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(command)})
      if(token!==epoch.current||cleared.current)return
      if(!validView(result.current,childId,commitmentId))throw Error('current owner')
      if(!result.receipt){
        if(!receiptOnly)throw Error('missing receipt')
        setView(result.current);setMessage('未找到原提交回执，仍保留原请求；未自动重发。')
        if(result.current.version!==command.version)setUnapplied(command.action_id)
        return
      }
      const r=result.receipt
      if(r.child_id!==childId||r.commitment_id!==commitmentId||r.action_id!==command.action_id
        ||r.decision!==(command.decision==='approve'?'approved':'revoked')||!Number.isInteger(r.revision)||r.revision<1)throw Error('receipt identity')
      const next={...current.current,pending:null,version:result.current.version,item:'',itemVersion:''}
      if(!persist(next))return
      setView(result.current);setReadError('');setMessage('原操作已确认保存，以下为当前关联状态；尚不代表孩子掌握。')
      callbacks.current.onSaved()
    }catch(cause){if(token===epoch.current){fail(cause);setMessage('操作结果待确认，已保留原请求。请先核对原提交结果，不会自动重试。')}}
    finally{if(token===epoch.current){lock.current=false;setBusy(false)}}
  }
  const reference=view?.references.find(r=>r.item_id===draft.item&&r.item_version===draft.itemVersion)
  const stale=!!view&&draft.version!==view.version
  const writable=!!view&&!busy&&!readError&&!storageError&&!draft.pending&&!stale
  const approve=!!reference&&!!draft.quote.trim()&&!!view?.original_question.includes(draft.quote)&&draft.note.trim().length>=8
  const decide=(decision:'approve'|'revoke'):void=>{
    if(!writable||!view||draft.note.trim().length<8||decision==='approve'&&!approve||decision==='revoke'&&view.mapping?.decision!=='approved')return
    void submit({action_id:crypto.randomUUID(),version:view.version,decision,item_id:decision==='approve'?reference!.item_id:null,
      item_version:decision==='approve'?reference!.item_version:null,original_quote:decision==='approve'?draft.quote:'',
      reference_quote:decision==='approve'?reference!.target:'',note:draft.note})
  }
  return <section className="original-source-review" aria-label="原题学习目标审核">
    <header><h3>原题学习目标</h3><div>
      <button aria-label="刷新原题来源" title="刷新原题来源" disabled={busy||cleared.current} onClick={()=>void load(search)}><RefreshCw size={18}/></button>
      <button aria-label="关闭原题审核" title="关闭原题审核" onClick={onClose}><X size={18}/></button></div></header>
    {busy&&<p role="status">正在核对。</p>}{readError&&<p role="alert">{readError}</p>}{storageError&&<p role="alert">{storageError}</p>}{message&&<p role="status">{message}</p>}
    {storageError&&!cleared.current&&view&&<button disabled={busy} onClick={()=>persist(current.current)}><RefreshCw size={16}/>重试保存本机草稿</button>}
    {view&&<><h4>孩子的原题</h4><p className="original-source-text">{view.original_question}</p>
      <p>{view.mapping?.decision==='approved'?(view.status==='mapping_required'?'原目标关联需要重新核对':'家长已确认目标关联'):'目标关联尚未确认'}{view.mapping?.decision==='revoked'?'，原关联已撤销':''}。{view.status==='ready'?'已有不同的合格题目，等待孩子语音发起检查。':view.status==='new_material_required'?'仍缺少不同的合格题目。':''}</p>
      {view.mapping&&<><p>已保存的目标：{view.mapping.reference_quote||'未提供当前目标依据'}</p>
        <p>已保存的原题依据：{view.mapping.original_quote||'未提供当前原题依据'}</p><p>已保存的审核依据：{view.mapping.note}</p></>}
      {stale&&!draft.pending&&<p role="alert">来源版本已变化，原草稿保留。
        <button disabled={busy||!!storageError} onClick={()=>persist({...draft,version:view.version,item:'',itemVersion:''})}>核对当前来源，重新选题</button></p>}
      <form onSubmit={e=>{e.preventDefault();void load(search)}} className="original-source-search">
        <label>搜索学习目标<input value={search} maxLength={120} disabled={busy} onChange={e=>setSearch(e.target.value)}/></label>
        <button type="submit" aria-label="搜索目标" title="搜索目标" disabled={busy}><Search size={18}/></button>
      </form>
      <label>参考题与目标<select aria-label="参考题与目标" value={reference?.item_id??''} disabled={!writable}
        onChange={e=>{const r=view.references.find(v=>v.item_id===e.target.value);persist({...draft,item:r?.item_id??'',itemVersion:r?.item_version??''})}}>
        <option value="">请选择</option>{view.references.map(r=><option key={r.item_id} value={r.item_id}>{r.target} · {r.question.slice(0,65)}</option>)}</select></label>
      {!view.references.length&&<p>没有匹配的合格参考题，不能强行关联。</p>}{view.truncated&&<p>结果较多，请缩小搜索范围。</p>}
      {reference&&<><h4>参考题</h4><p className="original-source-text">{reference.question}</p><p>学习目标：{reference.target}</p></>}
    </>}
    <label>原题中的依据<textarea value={draft.quote} maxLength={1500} disabled={!writable} onChange={e=>persist({...draft,quote:e.target.value})}/></label>
    <label>审核依据与备注<textarea value={draft.note} maxLength={1500} disabled={!writable} onChange={e=>persist({...draft,note:e.target.value})}/></label>
    {draft.pending?<div className="original-source-actions"><p>有一项原提交等待核对。</p>
      <button disabled={busy||!!storageError} onClick={()=>void submit(draft.pending!,true)}><RefreshCw size={16}/>核对原提交结果</button>
      <button disabled={busy||!!storageError} onClick={()=>void submit(draft.pending!)}><RefreshCw size={16}/>重试原提交</button>
      {view&&unapplied===draft.pending.action_id&&view.version!==draft.pending.version&&<button disabled={busy||!!storageError}
        onClick={()=>{if(persist({...draft,pending:null,version:view.version,item:'',itemVersion:''})){setUnapplied(null);setMessage('已保留备注，按当前来源重新选题；旧请求没有当作已保存的审核。')}}}>放弃未落库旧请求，重新审核</button>}</div>:
      <div className="original-source-actions"><button disabled={!writable||!approve} onClick={()=>decide('approve')}><Check size={16}/>确认目标关联</button>
      {view?.mapping?.decision==='approved'&&<button disabled={!writable||draft.note.trim().length<8} onClick={()=>decide('revoke')}><Undo2 size={16}/>撤销目标关联</button>}</div>}
  </section>
}
