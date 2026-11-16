import {useEffect,useRef,useState} from 'react'
import type {CurriculumParentRequest} from './curriculumSupplyTypes'

type Selection={session_id:string;child_id:string;version:number;status:'empty'|'selected'|'blocked';reason?:string;items:{candidate_id:string;candidate_version:number;title:string}[]}
type Options={child_id:string;origin_generation:string;sessions:{session_id:string;started_at:string;state:string;selection:Selection}[]}
type Command={session_id:string;action_id:string;expected_version:number;expected_generation:string;operation:'replace'|'append'|'clear';candidate_id?:string;candidate_version?:number}
type Props={childId:string;candidateId:string;candidateVersion:number;request:CurriculumParentRequest;isCurrentAuthority?:()=>boolean;onRequireParentAuth?:()=>void;disabled?:boolean}
const journalKey=(child:string)=>`hsc-paper-learning-selection-pending:${child}`
const parentAuthRequired=(value:unknown):boolean=>typeof value==='object'&&value!==null
  &&(('status'in value&&value.status===401)||('code'in value&&value.code==='PARENT_SESSION_REQUIRED'))
const messageFor=(value:unknown):string=>{
  if(parentAuthRequired(value))return '家长授权已失效，请重新解锁。原选择请求保留，解锁后可核对并重试上次选择。'
  const code=typeof value==='object'&&value!==null&&'code'in value?String(value.code):''
  if(code==='PAPER_SELECTION_DUPLICATE')return '这道题已经在本次材料里，没有重复加入。请核对当前选择。'
  if(code.includes('VERSION_CONFLICT')||code.includes('SUPERSEDED'))return '本次材料已被另一操作更新。原请求仍保留；请先核对当前选择，再明确替换。'
  if(code.includes('RESTORE')||code.includes('SOURCE'))return '材料来源、发布版本或恢复状态已变化。原记录保留，请核对后重新选择本次材料。'
  if(code.includes('SESSION'))return '本次学习会话已变化，请核对当前孩子和学习会话。'
  return '本次选择结果尚未确认。原请求已保存在本机；请核对或重试原请求，不会自动另选材料。'
}
export function PaperLearningSelectionPanel(props:Props):React.JSX.Element{
  return <ScopedPaperSelection key={props.childId} {...props}/>
}
function ScopedPaperSelection({childId,candidateId,candidateVersion,request,isCurrentAuthority,onRequireParentAuth,disabled}:Props):React.JSX.Element{
  const [options,setOptions]=useState<Options|null>(null),[sid,setSid]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false)
  const [pending,setPending]=useState<Command|null>(null),[journalBroken,setJournalBroken]=useState(false),[authRequired,setAuthRequired]=useState(false)
  const generation=useRef(0),mounted=useRef(true),current=useRef(isCurrentAuthority);current.current=isCurrentAuthority
  const base=`/api/parent/children/${encodeURIComponent(childId)}/paper-learning-selection`
  const active=useRef<AbortController|null>(null)
  const valid=()=>mounted.current&&(current.current?.()??true)
  useEffect(()=>{
    mounted.current=true
    try{
      const raw=localStorage.getItem(journalKey(childId))
      if(raw){const v=JSON.parse(raw) as Command
        if(!v||!['replace','append','clear'].includes(v.operation)||typeof v.session_id!=='string'||typeof v.action_id!=='string'
          ||!Number.isInteger(v.expected_version)||v.expected_version<0||typeof v.expected_generation!=='string'
          ||v.operation!=='clear'&&(typeof v.candidate_id!=='string'||!Number.isInteger(v.candidate_version)))throw Error('invalid journal')
        setPending(v);setNotice('上次选择还待核对，请先核对当前选择或重试原请求。')}
    }catch{setJournalBroken(true);setNotice('本机上次选择记录无法核对，未覆盖。请先恢复记录或核对当前选择。')}
    return()=>{mounted.current=false;generation.current++;active.current?.abort()}
  },[childId])
  async function call(path:string,init?:RequestInit):Promise<unknown>{
    const controller=new AbortController();active.current?.abort();active.current=controller
    let timer:ReturnType<typeof setTimeout>|undefined
    try{return await Promise.race([request(path,{...init,signal:controller.signal}),new Promise((_,reject)=>{
      timer=setTimeout(()=>{controller.abort();reject(Error('PAPER_SELECTION_WAIT_UNKNOWN'))},10_000)
      controller.signal.addEventListener('abort',()=>reject(Error('PAPER_SELECTION_WAIT_UNKNOWN')),{once:true})
    })])}finally{if(timer)clearTimeout(timer);if(active.current===controller)active.current=null}
  }
  async function load():Promise<void>{
    if(busy||!valid())return
    const g=++generation.current;setBusy(true);setNotice('正在核对当前孩子已保存的本次材料…')
    try{const v=await call(base) as Options;if(!valid()||g!==generation.current)return
      if(v.child_id!==childId||typeof v.origin_generation!=='string'||!Array.isArray(v.sessions)
        ||v.sessions.some(s=>s.selection.child_id!==childId||s.selection.session_id!==s.session_id))throw Error('scope changed')
      setAuthRequired(false);setOptions(v);setSid(old=>v.sessions.some(s=>s.session_id===old)?old:v.sessions.length===1?v.sessions[0].session_id:'')
      setNotice(v.sessions.length?'已核对本次选择。选材料不会替孩子开始学习；孩子说“做这张卷”才会呈现题目。':'当前没有进行中的学习会话。孩子开始本次学习后再选择材料。')
    }catch(e){if(valid()&&g===generation.current){if(parentAuthRequired(e))setAuthRequired(true);setNotice(messageFor(e))}}finally{if(valid()&&g===generation.current)setBusy(false)}
  }
  async function send(command:Command):Promise<void>{
    if(busy||authRequired||!valid())return
    const g=++generation.current;setBusy(true);setNotice('正在保存本次学习材料，原请求保留…')
    try{
      const value=await call(base,{method:'POST',body:JSON.stringify(command)}) as Selection
      if(!valid()||g!==generation.current)return
      if(value.child_id!==childId||value.session_id!==command.session_id||!Number.isInteger(value.version))throw Error('scope changed')
      setAuthRequired(false);localStorage.removeItem(journalKey(childId));setPending(null)
      setOptions(o=>o?{...o,sessions:o.sessions.map(s=>s.session_id===value.session_id?{...s,selection:value}:s)}:o)
      setNotice(value.status==='blocked'?'原选择已保存，但材料当前需重新核对；不会换成其他题。':command.operation==='clear'?'已取消本次材料选择，已有回答仍保留。':'本次材料已保存。孩子说“做这张卷”或“下一题”可继续；不会自动开始，也没有新增云端用途许可。')
    }catch(e){if(valid()&&g===generation.current){if(parentAuthRequired(e))setAuthRequired(true);setNotice(messageFor(e))}}finally{if(valid()&&g===generation.current)setBusy(false)}
  }
  function choose(operation:Command['operation']):void{
    if(!options||!selection||busy||authRequired||pending||journalBroken||!valid())return
    const command:Command={session_id:sid,action_id:'paper-select-'+crypto.randomUUID(),expected_version:selection.version,
      expected_generation:options.origin_generation,operation,...(operation!=='clear'?{candidate_id:candidateId,candidate_version:candidateVersion}:{})}
    try{localStorage.setItem(journalKey(childId),JSON.stringify(command));setPending(command);void send(command)}
    catch{setJournalBroken(true);setNotice('无法保存本机重试记录，尚未发送选择。请恢复本机存储后重试。')}
  }
  function acknowledge():void{
    if(!options||busy||authRequired||!valid())return
    try{localStorage.removeItem(journalKey(childId));setPending(null);setJournalBroken(false);setNotice('已核对当前选择，旧请求停止重试；如需改变材料，请明确选择。')}
    catch{setNotice('尚未清除本机旧请求，请恢复本机存储后再核对。')}
  }
  const selection=options?.sessions.find(s=>s.session_id===sid)?.selection
  return <section aria-label="本次学习材料"><h4>选择这次要做的题</h4>
    <p>为当前孩子这次学习选这道题或几道题。发布只加入题库；选择后仍等孩子开口开始。</p>
    <button type="button" disabled={disabled||busy} onClick={()=>void load()}>核对当前学习与材料</button>
    {options&&options.sessions.length>1&&<label>选择本次学习<select value={sid} disabled={disabled||busy||!!pending} onChange={e=>{setSid(e.target.value);setNotice('已切换本次学习，请核对下面保存的材料。')}}><option value="">请选择</option>{options.sessions.map(s=><option key={s.session_id} value={s.session_id}>{new Date(s.started_at).toLocaleString('zh-CN')} · {s.state==='study'?'学习中':'进行中'}</option>)}</select></label>}
    {selection&&<><p>{selection.status==='blocked'?'当前选择需重新核对。已有回答保留。':selection.items.length?'本次已选材料：':'本次尚未选材料。'}</p>
      {!!selection.items.length&&<ol>{selection.items.map(v=><li key={v.candidate_id}>{v.title}</li>)}</ol>}
      <div className="csr-actions"><button type="button" disabled={disabled||busy||authRequired||!!pending||journalBroken} onClick={()=>choose('replace')}>只选这道题供本次学习</button>
      <button type="button" disabled={disabled||busy||authRequired||!!pending||journalBroken||selection.status==='blocked'||selection.items.some(v=>v.candidate_id===candidateId)} onClick={()=>choose('append')}>加入本次已选材料</button>
      <button type="button" disabled={disabled||busy||authRequired||!!pending||journalBroken||!selection.items.length} onClick={()=>choose('clear')}>取消本次材料选择</button></div></>}
    {pending&&<button type="button" disabled={disabled||busy||authRequired} onClick={()=>void send(pending)}>核对并重试上次选择</button>}
    {(pending||journalBroken)&&options&&<button type="button" disabled={disabled||busy||authRequired} onClick={acknowledge}>已核对当前选择，停止重试旧请求</button>}
    {busy&&<button type="button" onClick={()=>{generation.current++;active.current?.abort();setBusy(false);setNotice('已停止等待。原选择请求仍保留，请核对当前选择或重试原请求。')}}>停止等待，保留原请求</button>}
    {authRequired&&onRequireParentAuth&&<button type="button" disabled={disabled||busy} onClick={onRequireParentAuth}>重新解锁家长操作</button>}
    {authRequired&&!onRequireParentAuth&&<p role="status">请返回家长入口重新解锁，原选择请求仍保留。</p>}
    {notice&&<p role="status" aria-live="polite">{notice}</p>}
  </section>
}
