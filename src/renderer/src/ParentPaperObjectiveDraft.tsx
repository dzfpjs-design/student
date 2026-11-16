import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ParentPaperFormalReview } from './ParentPaperFormalReview'
import type { PaperObjectiveDraft } from './parentTextbookDraftJournal'
import { objectiveTextLength } from './parentTextbookDraftJournal'
import type { ParentTextbookSourceReader } from './TextbookLanguageCandidateReview'

type Page = { capture_ref:string;source_version:string;status:string;raw_image_retained:boolean;page_context:Record<string,unknown> }
type Props = {
  childId:string;api:(path:string,init?:RequestInit)=>Promise<unknown>;artifacts:readonly Page[]
  value:PaperObjectiveDraft|null;onChange:(value:PaperObjectiveDraft)=>void;onSave:()=>Promise<unknown>
  readSource?:ParentTextbookSourceReader;blocked:boolean
}
type Node = { id:string;title:string;subject:string }
const roles = [['paper','试卷原页'],['answer_key','答案原页']] as const

/** Editing only. Original-source approval and publication remain separate operations. */
export function ParentPaperObjectiveDraft({childId,api,artifacts,value,onChange,onSave,readSource,blocked}:Props):React.JSX.Element {
  const [open,setOpen]=useState(value!==null)
  const [selection,setSelection]=useState({paper:value?.paper.capture_ref??'',answer_key:value?.answer_key.capture_ref??''})
  const [nodes,setNodes]=useState<Node[]>([]),[nodeError,setNodeError]=useState(false)
  const [images,setImages]=useState<Partial<Record<'paper'|'answer_key',string>>>({})
  const [imageScope,setImageScope]=useState('')
  const [zoom,setZoom]=useState<{role:'paper'|'answer_key';url:string;scope:string}|null>(null),[scale,setScale]=useState(1)
  const dialog=useRef<HTMLDivElement>(null),opener=useRef<HTMLButtonElement|null>(null),titleId=useId()
  const [imageError,setImageError]=useState(false),[saving,setSaving]=useState(false),[message,setMessage]=useState('')
  const callbacks=useRef({api,readSource});callbacks.current={api,readSource}
  useEffect(()=>{
    if(value){setOpen(true);setSelection({paper:value.paper.capture_ref,answer_key:value.answer_key.capture_ref})}
  },[value?.paper.capture_ref,value?.answer_key.capture_ref])
  useEffect(()=>{
    if(!open)return
    let current=true
    void callbacks.current.api(`/v1/children/${encodeURIComponent(childId)}/knowledge-graph`).then(response=>{
      const result=response as {nodes?:unknown[]}
      if(!Array.isArray(result.nodes))throw Error('候选知识点暂不可用')
      const available=result.nodes.flatMap(item=>{
        const n=item as Record<string,unknown>|null
        return n&&n.node_type==='skill'&&typeof n.knowledge_node_id==='string'&&typeof n.title==='string'&&typeof n.subject==='string'
          ? [{id:n.knowledge_node_id,title:n.title,subject:n.subject}]:[]
      })
      if(current){setNodes(available);setNodeError(false)}
    }).catch(()=>{if(current){setNodes([]);setNodeError(true)}})
    return()=>{current=false}
  },[childId,open])
  const pages=artifacts.filter(page=>page.raw_image_retained&&['needs_review','ready','imported'].includes(page.status))
  const pairCurrent=!!value&&roles.every(([role])=>selection[role]===value[role].capture_ref
    &&pages.some(page=>page.capture_ref===value[role].capture_ref&&page.source_version===value[role].source_version))
  const currentImageScope=JSON.stringify([childId,value?.paper,value?.answer_key])
  const zoomVisible=!!zoom&&!blocked&&pairCurrent&&zoom.scope===currentImageScope&&imageScope===currentImageScope
  useLayoutEffect(()=>{
    if(!zoomVisible)return
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return()=>{if(opener.current?.isConnected)opener.current.focus()}
  },[zoomVisible,zoom?.url])
  const rawQuestion=value?.raw_editing?.full_question??value?.full_question??''
  const rawAnswers=value?.raw_editing?.official_answers_text??value?.official_answers.join('\n')??''
  const answerTooLong=rawAnswers.split('\n').length>24||rawAnswers.split('\n').some(answer=>objectiveTextLength(answer)>200)
  useEffect(()=>{
    const controller=new AbortController(),urls:string[]=[]
    setImages({});setImageScope(currentImageScope);setImageError(false);setZoom(null)
    if(value&&pairCurrent&&!blocked&&callbacks.current.readSource){
      for(const [role]of roles){
        const ref=value[role].capture_ref
        void callbacks.current.readSource(childId,ref,controller.signal).then(source=>{
          if(controller.signal.aborted)return
          if(!['image/png','image/jpeg'].includes(source.mediaType)||source.bytes.byteLength>18_000_000||source.bytes.byteLength===0)throw Error('原页无法核对')
          const url=URL.createObjectURL(new Blob([source.bytes],{type:source.mediaType}));urls.push(url)
          setImages(prior=>({...prior,[role]:url}))
        }).catch(()=>{if(!controller.signal.aborted)setImageError(true)})
      }
    }
    return()=>{controller.abort();for(const url of urls)URL.revokeObjectURL(url)}
  },[childId,value?.paper.capture_ref,value?.paper.source_version,value?.answer_key.capture_ref,value?.answer_key.source_version,pairCurrent,blocked])
  function select(role:'paper'|'answer_key',ref:string):void {
    const next={...selection,[role]:ref};setSelection(next);setMessage('')
    bindPair(next)
  }
  function bindPair(next:typeof selection):void {
    setMessage('')
    const paper=pages.find(p=>p.capture_ref===next.paper),answer=pages.find(p=>p.capture_ref===next.answer_key)
    if(!paper||!answer||paper.capture_ref===answer.capture_ref)return
    onChange({...value,question_number:value?.question_number??'',full_question:value?.full_question??'',
      official_answers:value?.official_answers??[],candidate_node_id:value?.candidate_node_id??null,
      paper:{capture_ref:paper.capture_ref,source_version:paper.source_version},
      answer_key:{capture_ref:answer.capture_ref,source_version:answer.source_version}})
  }
  function edit(patch:Partial<PaperObjectiveDraft>,rawPatch?:Partial<NonNullable<PaperObjectiveDraft['raw_editing']>>):void {
    if(!value)return
    const raw={full_question:rawQuestion,official_answers_text:rawAnswers,...rawPatch},answers=raw.official_answers_text.split('\n')
    setMessage('');onChange({...value,...patch,raw_editing:raw,full_question:objectiveTextLength(raw.full_question)<=1200?raw.full_question:'',
      official_answers:answers.length<=24&&answers.every(answer=>objectiveTextLength(answer)<=200)?answers:[]})
  }
  async function save():Promise<void>{
    setSaving(true);setMessage('')
    try{await onSave();setMessage('单题审核草稿已保存；尚未批准或生成复测。')}
    catch{setMessage('本次未确认保存。请按草稿状态核对或重试，文字仍保留在本窗口。')}
    finally{setSaving(false)}
  }
  return <section aria-label="单题审核草稿">
    <button className="secondary-button" type="button" onClick={()=>setOpen(true)}>整理单题审核草稿</button>
    {open&&<>
      <h3>把原题与官方答案放在一起核对</h3>
      <p>这里只保存待审核文字与原页，不批准题源、不判定孩子作答，也不生成复测。</p>
      <fieldset disabled={blocked||saving}>
        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(240px,1fr))',gap:12}}>
          {roles.map(([role,label])=><div key={role}>
            <label>{label}<select aria-label={label} value={selection[role]} onChange={event=>select(role,event.target.value)}>
              <option value="">请选择已保存的原页</option>
              {pages.map(page=><option key={page.capture_ref} value={page.capture_ref}>
                {String(page.page_context.title??'未命名原页')} · 第{String(page.page_context.page_number??'未知')}页
              </option>)}
            </select></label>
            {images[role]&&!blocked&&pairCurrent&&imageScope===currentImageScope&&<figure className="parent-ocr-source"><img src={images[role]} alt={label} style={{maxWidth:'100%'}}/>
              <figcaption><button className="secondary-button" type="button" onClick={event=>{opener.current=event.currentTarget;setScale(1);setZoom({role,url:images[role]!,scope:currentImageScope})}}>在本页放大核对{label}</button></figcaption></figure>}
          </div>)}
        </div>
        {(!value||selection.paper===selection.answer_key)&&<p>请先选择两张不同的试卷和答案原页，再填写题目。</p>}
        {value&&!pairCurrent&&<p role="alert">原页版本已变化，已保存文字仍保留。请重新选择当前原页核对。</p>}
        {value&&!pairCurrent&&<button className="secondary-button" type="button" onClick={()=>bindPair(selection)}>使用当前原页版本继续核对</button>}
        {imageError&&<p role="alert">原页暂时无法读取，草稿文字仍保留，不能据此认定题目已经核实。</p>}
        <fieldset disabled={!value||!pairCurrent}>
          <label>题号<input aria-label="题号" maxLength={40} value={value?.question_number??''} onChange={e=>edit({question_number:e.target.value})}/></label>
          <label>完整题干（待审核）<textarea aria-label="完整题干（待审核）" value={rawQuestion} onChange={e=>edit({},{full_question:e.target.value})}/></label>
          <label>官方答案（待审核，每行一项）<textarea aria-label="官方答案（待审核，每行一项）" value={rawAnswers} onChange={e=>edit({},{official_answers_text:e.target.value})}/></label>
          <p>原始输入可先保存。正式候选要求题干不超过 1200 字，答案每行最多 200 字、最多 24 行；不合格时不生成规范候选，不批准题源。</p>
          {(answerTooLong||objectiveTextLength(rawQuestion)>1200)&&<p role="alert">原文不会截断，仍可保存和重开；当前规范候选待修正，不能据此发布题源。</p>}
          <label>知识点候选（可暂不选择）<select aria-label="知识点候选（可暂不选择）" value={value?.candidate_node_id??''} onChange={e=>edit({candidate_node_id:e.target.value||null})}>
            <option value="">尚未映射，稍后审核</option>
            {value?.candidate_node_id&&!nodes.some(node=>node.id===value.candidate_node_id)&&<option value={value.candidate_node_id}>先前候选暂不可核对，请重新选择</option>}
            {nodes.map(node=><option key={node.id} value={node.id}>{node.subject} · {node.title}</option>)}
          </select></label>
          {nodeError&&<p>知识点候选暂不可读，可先保存未映射草稿；不会补造知识点。</p>}
          <button className="primary-button" type="button" disabled={saving} onClick={()=>void save()}>{saving?'正在保存单题草稿…':'保存单题审核草稿'}</button>
        </fieldset>
      </fieldset>
      {zoomVisible&&zoom&&<div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId}
        style={{position:'fixed',inset:0,zIndex:10000,padding:16,background:'#fff',border:'2px solid #365d4e',boxShadow:'0 0 0 100vmax #0009',display:'flex',flexDirection:'column'}}
        onKeyDown={event=>{
          if(event.key==='Escape'){event.preventDefault();event.stopPropagation();setZoom(null)}
          if(event.key==='Tab'){
            const items=Array.from(dialog.current?.querySelectorAll<HTMLElement>('button,[tabindex="0"]')??[]).filter(item=>!(item instanceof HTMLButtonElement&&item.disabled))
            const first=items[0],last=items.at(-1)
            if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
            else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
          }
        }}>
        <h3 id={titleId}>{zoom.role==='paper'?'试卷原页':'答案原页'}放大核对</h3>
        <div><button className="secondary-button" type="button" onClick={()=>setZoom(null)}>关闭原页放大</button>
          <button className="secondary-button" type="button" disabled={scale>=3} onClick={()=>setScale(v=>Math.min(3,v+1))}>放大</button>
          <button className="secondary-button" type="button" disabled={scale<=1} onClick={()=>setScale(v=>Math.max(1,v-1))}>缩小</button>
          <button className="secondary-button" type="button" onClick={()=>setScale(1)}>适合窗口</button></div>
        <p>原图仅在本应用查看。可用 Tab、滚动键或拖动滚动条核对，Esc 关闭。</p>
        <div tabIndex={0} aria-label="原页放大滚动区域" style={{overflow:'auto',flex:1,minHeight:0}}>
          <img src={zoom.url} alt={zoom.role==='paper'?'放大的试卷原页':'放大的答案原页'} style={{display:'block',maxWidth:scale===1?'100%':'none',width:scale===1?'auto':`${scale*100}%`,height:'auto'}}/>
        </div>
      </div>}
      {message&&<p role="status">{message}</p>}
      {value&&pairCurrent&&<ParentPaperFormalReview childId={childId} api={api} onSave={onSave} blocked={blocked||saving} draftBinding={JSON.stringify(value)}/> }
    </>}
  </section>
}
