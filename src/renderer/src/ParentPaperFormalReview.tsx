import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {ParentPaperFormalQueue} from './ParentPaperFormalQueue'
import type { CurriculumChild, CurriculumPackage } from './curriculumSupplyTypes'

type Options={status:'ready'|'blocked';child:CurriculumChild;blockers:string[];
  draft_identity?:{revision:number;storage_generation:string;last_action_id:string};
  selection?:{version:number;scope:{edition:string;semester:string}};
  node?:{id:string;title:string;subject:string;binding:string};
  seeds:{diagnostic_item_id:string;prompt:string;binding:string}[];
  standards:{key:string;title:string;standard_page:number;standard_quote:string;binding:string}[]}
type Sent={receipt_id:string;route:'reuse'|'parallel'|'first_source';diagnostic_item_id:string|null}
type Props={childId:string;api:(path:string,init?:RequestInit)=>Promise<unknown>;onSave:()=>Promise<unknown>;blocked:boolean;draftBinding:string}
type Pending={body:Record<string,unknown>;generation:string}
const meaningNames={whole_answer_variants:'整题可接受的完整写法（各行是不同写法）',ordered_numeric_parts:'按题目顺序逐项作答（每行是一部分数字答案）'}

function restoredStatus(route:Sent['route'],candidate:CurriculumPackage|null):string{
  if(route==='reuse')return '此题沿用已审核的合格原题版本；没有另建审核稿或平行题，继续查看不会重新发布。'
  const labels={draft:'待独立校验',blocked:'来源或材料待核对',validated:'已校验，待人工审核',reviewed:'已人工审核，待明确发布',published:'已有发布记录',withdrawn:'已撤回',rejected:'已退回'}
  if(!candidate||!Object.hasOwn(labels,candidate.state))return '送审记录已读取，当前审核状态暂不能确认。请核对记录；不会自动批准或发布。'
  const version=Number.isSafeInteger(candidate.version)&&candidate.version>0?`（版本${candidate.version}）`:''
  const prefix=candidate.state==='draft'?'已进入正式审核队列。':'已读取本题的正式审核记录。'
  return `${prefix}读取时为：${labels[candidate.state]}${version}。当前状态和资格以下方核对结果为准；继续查看不会自动批准或发布。`
}

const waitLimitMs=10_000
/** Stop waiting locally; an aborted POST may already have committed. */
function boundedWait<T>(controller:AbortController,run:(signal:AbortSignal)=>Promise<T>):Promise<T>{
  return new Promise((resolve,reject)=>{
    let settled=false
    const finish=(success:boolean,value:unknown):void=>{
      if(settled)return
      settled=true;clearTimeout(timer);controller.signal.removeEventListener('abort',aborted)
      if(success)resolve(value as T);else reject(value)
    }
    const aborted=():void=>finish(false,new DOMException('已停止等待，原请求结果仍需核对。','AbortError'))
    const timer=setTimeout(()=>{finish(false,Error('PAPER_LOCAL_WAIT_LIMIT'));controller.abort()},waitLimitMs)
    controller.signal.addEventListener('abort',aborted,{once:true})
    if(controller.signal.aborted){aborted();return}
    try{Promise.resolve(run(controller.signal)).then(value=>finish(true,value),error=>finish(false,error))}
    catch(error){finish(false,error)}
  })
}

/** A bridge into the existing review state machine, never an alternate publisher. */
export function ParentPaperFormalReview(props:Props):React.JSX.Element {return <ScopedPaperFormalReview key={props.childId} {...props}/> }
function ScopedPaperFormalReview({childId,api,onSave,blocked,draftBinding}:Props):React.JSX.Element{
  const [storedOptions,setOptions]=useState<Options|null>(null),[optionsBinding,setOptionsBinding]=useState(''),[person,setPerson]=useState<CurriculumChild|null>(null),[mode,setMode]=useState('')
  const [origin,setOrigin]=useState('')
  const [numeric,setNumeric]=useState('integer'),[size,setSize]=useState('2'),[standard,setStandard]=useState('')
  const [confirmed,setConfirmed]=useState(false),[license,setLicense]=useState(''),[busy,setBusy]=useState(false)
  const [message,setMessage]=useState(''),[candidate,setCandidate]=useState<CurriculumPackage|null>(null)
  const [pending,setPending]=useState<Pending|null>(null),[receipts,setReceipts]=useState<{audit_id:string;occurred_at:string;title:string;state:string}[]>([])
  const callbacks=useRef({api,onSave});callbacks.current={api,onSave}
  const bindingNow=useRef(draftBinding);bindingNow.current=draftBinding
  const options=optionsBinding===draftBinding?storedOptions:null
  const generation=useRef(0),blockedNow=useRef(blocked);blockedNow.current=blocked
  const base=`/v1/parent/knowledge-graph/textbook-capture/${encodeURIComponent(childId)}/objective-review`
  const active=useRef<{turn:number;controller:AbortController}|null>(null)
  const previous=useRef({draftBinding,blocked})
  function beginWait(text:string):number{
    active.current?.controller.abort()
    const turn=++generation.current
    active.current={turn,controller:new AbortController()};setBusy(true);setMessage(text)
    return turn
  }
  function endWait(turn:number):void{
    if(turn!==generation.current)return
    active.current?.controller.abort();active.current=null;setBusy(false)
  }
  function stopWaiting():void{
    generation.current++;active.current?.controller.abort();active.current=null;setBusy(false)
    setMessage('已停止等待。保存或送审结果仍需核对，原请求与文字保留；没有另建请求或当成失败。')
  }
  function request(turn:number,path:string,init?:RequestInit):Promise<unknown>{
    const owner=active.current
    if(!owner||owner.turn!==turn||turn!==generation.current)return Promise.reject(Error('PAPER_WAIT_SUPERSEDED'))
    return boundedWait(owner.controller,signal=>callbacks.current.api(path,{...init,signal}))
  }
  useLayoutEffect(()=>{
    if(previous.current.draftBinding!==draftBinding||previous.current.blocked!==blocked){
      generation.current++;active.current?.controller.abort();active.current=null;setBusy(false)
    }
    previous.current={draftBinding,blocked}
  },[draftBinding,blocked])
  useEffect(()=>{
    const turn=generation.current,controller=new AbortController()
    void boundedWait(controller,signal=>callbacks.current.api(base+'/submissions',{signal})).then(v=>{if(turn===generation.current&&!blockedNow.current)setReceipts(Array.isArray((v as {items?:unknown[]})?.items)?(v as {items:typeof receipts}).items.filter(r=>typeof r.audit_id==='string'&&typeof r.occurred_at==='string'&&typeof r.title==='string'&&typeof r.state==='string'):[])}).catch(()=>undefined)
    return()=>{generation.current++;controller.abort();active.current?.controller.abort();active.current=null}
  },[base])
  async function refreshReceipts():Promise<void>{
    if(blocked||busy)return
    const turn=beginWait('正在读取已保存的送审记录…'),binding=bindingNow.current
    try{
      const result=await request(turn,base+'/submissions') as {items:typeof receipts}
      if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      if(!Array.isArray(result.items))throw Error('RECEIPT_SCOPE_CHANGED')
      setReceipts(result.items.filter(r=>typeof r.audit_id==='string'&&typeof r.occurred_at==='string'&&typeof r.title==='string'&&typeof r.state==='string'))
      setMessage('已核对本机保存的送审记录。请按题号继续；当前新改动没有送出，未确认的旧请求仍保留。')
    }catch{if(turn===generation.current)setMessage('尚未读到已保存的送审记录。请恢复连接后再核对；原请求与当前文字都未替换。')}
    finally{endWait(turn)}
  }
  async function prepare():Promise<void>{
    const turn=beginWait('正在核对本次操作，原请求与文字仍保留…'),binding=bindingNow.current
    try{
      const owner=active.current;if(!owner)return
      await boundedWait(owner.controller,()=>callbacks.current.onSave());if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      const next=await request(turn,base+'/options') as Options;if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      if(next?.child?.child_id!==childId||!Array.isArray(next.standards)||next.status==='ready'&&!Array.isArray(next.seeds)||!['ready','blocked'].includes(next.status))throw Error('SOURCE_SCOPE_CHANGED')
      setOptions(next);setOptionsBinding(binding);setPerson(next.child);setStandard('');setOrigin('');setConfirmed(false)
      if(next.status==='blocked')setMessage('草稿仍已保存。请先选择当前知识点，并在教材与课标中补齐当前范围；不会补造题源资格。')
      else if(!next.standards.length)setMessage('当前没有可核对的已审核课标。请先补齐课标，原稿不会丢失。')
    }catch{if(turn===generation.current)setMessage('尚未确认草稿与送审条件，请先核对保存状态；不会送出未确认文字。')}
    finally{endWait(turn)}
  }
  async function generate(sent:Sent,storageGeneration:string,turn:number,binding:string):Promise<void>{
    if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
    const result=await request(turn,base+'/'+encodeURIComponent(sent.receipt_id)+'/generate',{
      method:'POST',body:JSON.stringify({expected_generation:storageGeneration})}) as {receipt:Sent;candidate:CurriculumPackage|null;child:CurriculumChild}
    if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
    if(result?.child?.child_id!==childId||result.candidate&&result.candidate.child_id!==childId)throw Error('SOURCE_SCOPE_CHANGED')
    setPerson(result.child);setCandidate(result.candidate);setPending(null)
    setMessage(restoredStatus(result.receipt.route,result.candidate))
  }
  async function submit(retry=false):Promise<void>{
    if(blocked||busy)return
    const turn=beginWait('正在核对本次操作，原请求与文字仍保留…'),binding=bindingNow.current
    try{
      let frozen=pending
      if(!retry){
        if(optionsBinding!==binding||!options?.draft_identity||!options.selection||!options.node||!confirmed||!license||!mode)return
        const chosen=options.standards.find(v=>v.key===standard);if(!chosen)return
        const seed=options.seeds.find(v=>v.diagnostic_item_id===origin);if(options.seeds.length&&!seed)return
        const semantics=mode==='whole_answer_variants'?{kind:mode}:{kind:mode,numeric_kind:numeric,tuple_size:Number(size)}
        frozen={generation:options.draft_identity.storage_generation,body:{draft_identity:options.draft_identity,
          seed_choice:seed?{diagnostic_item_id:seed.diagnostic_item_id,binding:seed.binding}:null,
          expected_selection_version:options.selection.version,expected_node_binding:options.node.binding,standard_key:chosen.key,standard_binding:chosen.binding,
          answer_semantics:semantics,originals_confirmed:true,license_status:license,difficulty:1,action_id:'paper-review-'+crypto.randomUUID()}}
        setPending(frozen)
      }
      if(!frozen)return
      const sent=await request(turn,base,{method:'POST',body:JSON.stringify(frozen.body)}) as Sent
      if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      await generate(sent,frozen.generation,turn,binding)
    }catch{if(turn===generation.current)setMessage('本次送审结果尚未确认。请核对上次送审；不会另建请求或自动批准。重新打开本页也可继续已保存的送审。')}
    finally{endWait(turn)}
  }
  async function resume(receiptId:string):Promise<void>{
    if(blocked||busy)return
    const turn=beginWait('正在核对本次操作，原请求与文字仍保留…'),binding=bindingNow.current
    try{
      const current=await request(turn,`/v1/parent/knowledge-graph/textbook-capture/${encodeURIComponent(childId)}/drafts/capture`) as {draft:{storage_generation:string}}
      if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      const sent=await request(turn,base+'/'+encodeURIComponent(receiptId)) as Sent
      if(turn!==generation.current||blockedNow.current||binding!==bindingNow.current)return
      await generate(sent,current.draft.storage_generation,turn,binding)
    }catch{if(turn===generation.current)setMessage('尚未核对这份送审的保存状态或权限。原记录保留，请恢复连接后重试核对。')}
    finally{endWait(turn)}
  }
  return <section aria-label="单题正式审核衔接">
    <h3>把已保存的单题送入正式审核</h3>
    <p>送审核会冻结本题的两份原页和答案含义，不批准题目，也不改变当前教材。填写下一题不会改写本题。</p>
    <button className="secondary-button" type="button" disabled={blocked||busy||!!pending} onClick={()=>void prepare()}>核对已保存稿的送审条件</button>
    {storedOptions&&optionsBinding!==draftBinding&&!pending&&<p role="status">草稿已改动。请重新保存并核对送审条件，上一版不会代替当前文字送审核。</p>}
    {options?.status==='ready'&&<fieldset disabled={blocked||busy||!!pending}>
      <p>知识点：{options.node?.title} · 当前教材：{options.selection?.scope.edition} · {options.selection?.scope.semester}</p>
      {!!options.seeds.length&&<label>对应的已审核原题<select aria-label="对应的已审核原题" value={origin} onChange={e=>setOrigin(e.target.value)}>
        <option value="">请选择本题要对照的原题，系统不会自动选同知识点的其他题</option>{options.seeds.map(seed=><option key={seed.diagnostic_item_id} value={seed.diagnostic_item_id}>{seed.prompt}</option>)}</select></label>}
      {!options.seeds.length&&<p>当前知识点没有兼容的已审核原题，将进入首次题源审核。</p>}
      <label>答案如何评分<select aria-label="答案如何评分" value={mode} onChange={e=>setMode(e.target.value)}>
        <option value="">请选择，系统不会从换行推断</option>{Object.entries(meaningNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
      {mode==='ordered_numeric_parts'&&<><label>数字类型<select aria-label="数字类型" value={numeric} onChange={e=>setNumeric(e.target.value)}><option value="integer">整数</option><option value="rational">分数或小数</option></select></label>
        <label>依次作答的部分数<select aria-label="依次作答的部分数" value={size} onChange={e=>setSize(e.target.value)}>{[2,3,4,5,6,7,8].map(n=><option key={n}>{n}</option>)}</select></label></>}
      <p>文字多评分点暂不能按数字分题送审，请使用现有开放题量规审核；不会把各部分误当可任选其一。</p>
      <label>对应课标<select aria-label="对应课标" value={standard} onChange={e=>setStandard(e.target.value)}><option value="">请选择已审核课标</option>{options.standards.map(s=><option key={s.key} value={s.key}>{s.title} · 第{s.standard_page}页</option>)}</select></label>
      {standard&&<blockquote>{options.standards.find(s=>s.key===standard)?.standard_quote}</blockquote>}
      <label>原页使用范围<select aria-label="原页使用范围" value={license} onChange={e=>setLicense(e.target.value)}><option value="">请选择真实来源权限</option><option value="family_owned_index">家庭合法持有，仅本机索引使用</option><option value="public">可公开使用的原始资料</option></select></label>
      <label><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>我已对照两份原页核对完整题干和参考答案；这不是对孩子掌握的确认</label>
      <button className="primary-button" type="button" disabled={!mode||!standard||!license||!confirmed||!!options.seeds.length&&!origin} onClick={()=>void submit()}>送审核（不批准、不发布）</button>
    </fieldset>}
    {pending&&<button className="secondary-button" type="button" disabled={blocked||busy} onClick={()=>void submit(true)}>核对上次送审</button>}
    <button className="secondary-button" type="button" disabled={blocked||busy} onClick={()=>void refreshReceipts()}>刷新已保存送审</button>
    {!!receipts.length&&<details><summary>继续先前已保存的送审</summary>{receipts.map(r=><button key={r.audit_id} type="button" disabled={blocked||busy} onClick={()=>void resume(r.audit_id)}>继续送审：{r.title} · {({awaiting_formal_validation:'待建立审核稿',draft:'待核验',blocked:'需核对来源',validated:'待人工审核',reviewed:'待明确发布',published:'已有发布记录，恢复时核对来源',withdrawn:'已撤回',reused:'沿用已审核版本'} as Record<string,string>)[r.state]??'需核对'} · {r.occurred_at}</button>)}</details>}
    {message&&<p role="status" aria-live="polite">{message}</p>}
    {busy&&<button className="secondary-button" type="button" onClick={stopWaiting}>停止等待，保留原请求</button>}
    {person&&candidate&&<ParentPaperFormalQueue child={person} request={api} focusRequest={{objectId:candidate.candidate_id,nonce:candidate.candidate_id}}/>}
  </section>
}
