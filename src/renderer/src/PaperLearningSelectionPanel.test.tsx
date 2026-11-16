import {useState} from 'react'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {clearCurriculumSupplyLocalState} from './curriculumSupplyApi'
import type {CurriculumParentRequest} from './curriculumSupplyTypes'
import {ApiError} from './api'
import {CurriculumSupplyReviewPanel} from './CurriculumSupplyReviewPanel'
import {PaperLearningSelectionPanel} from './PaperLearningSelectionPanel'
const key='hsc-paper-learning-selection-pending:child-a'
const selection={session_id:'session-a',child_id:'child-a',version:0,status:'empty',items:[]}
const options={child_id:'child-a',origin_generation:'generation-a',sessions:[{session_id:'session-a',started_at:'2026-10-17T08:00:00Z',state:'study',selection}]}
const props={childId:'child-a',candidateId:'candidate-a',candidateVersion:4,isCurrentAuthority:()=>true}
beforeEach(()=>localStorage.clear());afterEach(()=>cleanup())
async function open(request:CurriculumParentRequest){render(<PaperLearningSelectionPanel {...props} request={request}/>);fireEvent.click(screen.getByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')}
it('publication mount never chooses or starts; explicit action persists request before POST',async()=>{
 const request=vi.fn(async(_path:string,init?:RequestInit)=>{
  if(!init?.method)return options
  const body=JSON.parse(String(init.body));expect(JSON.parse(localStorage.getItem(key)!)).toEqual(body)
  expect(body).toMatchObject({session_id:'session-a',candidate_id:'candidate-a',candidate_version:4,expected_generation:'generation-a',expected_version:0})
  return {...selection,version:1,status:'selected',items:[{candidate_id:'candidate-a',candidate_version:4,title:'合成数学原题'}]}
 })
 render(<PaperLearningSelectionPanel {...props} request={request}/>);expect(request).not.toHaveBeenCalled();cleanup()
 await open(request);fireEvent.click(screen.getByText('只选这道题供本次学习'))
 await screen.findByText('合成数学原题');expect(localStorage.getItem(key)).toBeNull();expect(request).toHaveBeenCalledTimes(2)
})
it('lost commit response survives remount and retries exact action/version without a new choice',async()=>{
 let fail=true;const bodies:unknown[]=[]
 const request=vi.fn(async(_p:string,init?:RequestInit)=>{if(!init?.method)return options;bodies.push(JSON.parse(String(init.body)));if(fail){fail=false;throw Error('lost response')}return {...selection,version:1,status:'selected',items:[]}})
 await open(request);fireEvent.click(screen.getByText('只选这道题供本次学习'))
 await screen.findByText('核对并重试上次选择');await waitFor(()=>expect(screen.getByText('核对并重试上次选择')).toBeEnabled())
 const original=localStorage.getItem(key);expect(original).not.toBeNull();cleanup()
 render(<PaperLearningSelectionPanel {...props} request={request}/>);fireEvent.click(await screen.findByText('核对并重试上次选择'))
 await waitFor(()=>expect(localStorage.getItem(key)).toBeNull());expect(bodies[1]).toEqual(bodies[0])
})
it('authority revoked during response keeps pending and never displays stale success',async()=>{
 let allowed=true,resolve!:(v:unknown)=>void
 const request=vi.fn(async(_p:string,init?:RequestInit)=>!init?.method?options:new Promise(r=>{resolve=r}))
 render(<PaperLearningSelectionPanel {...props} isCurrentAuthority={()=>allowed} request={request}/>);fireEvent.click(screen.getByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')
 fireEvent.click(screen.getByText('只选这道题供本次学习'));allowed=false;resolve({...selection,version:1,status:'selected',items:[]})
 await new Promise(r=>setTimeout(r,0));expect(localStorage.getItem(key)).not.toBeNull();expect(screen.queryByText(/本次材料已保存/)).toBeNull()
})
it('child switch clears old feedback and does not send old pending for the new child',async()=>{
 const request=vi.fn(async()=>options);const view=render(<PaperLearningSelectionPanel {...props} request={request}/>);
 fireEvent.click(screen.getByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')
 view.rerender(<PaperLearningSelectionPanel {...props} childId="child-b" request={request}/>);
 expect(screen.queryByText('本次尚未选材料。')).toBeNull();expect(screen.queryByText('只选这道题供本次学习')).toBeNull();expect(request).toHaveBeenCalledTimes(1)
})
it('damaged durable request is retained and cannot be silently replaced',async()=>{
 localStorage.setItem(key,'{broken');const request=vi.fn(async()=>options);await open(request)
 expect(localStorage.getItem(key)).toBe('{broken');expect(screen.getByText('只选这道题供本次学习')).toBeDisabled();expect(request).toHaveBeenCalledTimes(1)
})

it('normal child cleanup clears only that child durable pending; all cleanup clears remaining',()=>{
 localStorage.setItem(key,'a');localStorage.setItem('hsc-paper-learning-selection-pending:child-b','b')
 clearCurriculumSupplyLocalState('child-a');expect(localStorage.getItem(key)).toBeNull()
 expect(localStorage.getItem('hsc-paper-learning-selection-pending:child-b')).toBe('b')
 clearCurriculumSupplyLocalState();expect(localStorage.getItem('hsc-paper-learning-selection-pending:child-b')).toBeNull()
})

it.each([
 {status:401,code:'PARENT_SESSION_REQUIRED'},
 {status:401,code:undefined},
 {status:undefined,code:'PARENT_SESSION_REQUIRED'},
])('recognizes parent expiry before session wording, preserves journal through PIN cancel and exact retry after remount: %j',async failure=>{
 let expired=true;const unlock=vi.fn(),bodies:unknown[]=[]
 const request=vi.fn<CurriculumParentRequest>(async(_path,init)=>{
  if(!init?.method)return options
  bodies.push(JSON.parse(String(init.body)))
  if(expired)throw new ApiError('授权失效',failure.status,failure.code)
  return {...selection,version:1,status:'selected',items:[]}
 })
 function ExistingPinCallbackHost(){
  const [showPin,setShowPin]=useState(false),[epoch,setEpoch]=useState(0)
  return <><PaperLearningSelectionPanel key={epoch} {...props} request={request} onRequireParentAuth={()=>{unlock();setShowPin(true)}}/>
   {showPin&&<div role="dialog" aria-label="既有家长PIN回调测试边界"><button onClick={()=>setShowPin(false)}>取消解锁</button>
    <button onClick={()=>{expired=false;setShowPin(false);setEpoch(value=>value+1)}}>已有PIN解锁成功，重新挂载</button></div>}</>
 }
 render(<ExistingPinCallbackHost/>)
 fireEvent.click(screen.getByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')
 fireEvent.click(screen.getByText('只选这道题供本次学习'))
 const original=await waitFor(()=>{const value=localStorage.getItem(key);expect(value).not.toBeNull();return value!})
 const authButton=await screen.findByRole('button',{name:'重新解锁家长操作'})
 expect(screen.getByText('家长授权已失效，请重新解锁。原选择请求保留，解锁后可核对并重试上次选择。')).toBeInTheDocument()
 expect(screen.queryByText('本次学习会话已变化，请核对当前孩子和学习会话。')).toBeNull()
 expect(screen.getByText('核对并重试上次选择')).toBeDisabled();expect(unlock).not.toHaveBeenCalled()
 fireEvent.click(authButton);expect(unlock).toHaveBeenCalledTimes(1)
 // Exercise explicit cancellation at the existing callback boundary.
 fireEvent.click(screen.getByText('取消解锁'))
 expect(screen.queryByRole('dialog')).toBeNull()
 expect(localStorage.getItem(key)).toBe(original);expect(bodies).toHaveLength(1)
 fireEvent.click(screen.getByText('核对并重试上次选择'));expect(bodies).toHaveLength(1)
 fireEvent.click(screen.getByText('重新解锁家长操作'))
 fireEvent.click(screen.getByText('已有PIN解锁成功，重新挂载'))
 expect(localStorage.getItem(key)).toBe(original)
 fireEvent.click(await screen.findByText('核对并重试上次选择'))
 await waitFor(()=>expect(localStorage.getItem(key)).toBeNull());expect(bodies[1]).toEqual(bodies[0])
})

it('parent read expiry exposes the same PIN callback and a successful post-unlock refresh restores the existing mounted panel',async()=>{
 let expired=true;const unlock=vi.fn(),request=vi.fn<CurriculumParentRequest>(async()=>{
  if(expired)throw new ApiError('授权失效',401,'PARENT_SESSION_REQUIRED')
  return options
 })
 render(<PaperLearningSelectionPanel {...props} request={request} onRequireParentAuth={unlock}/>)
 fireEvent.click(screen.getByText('核对当前学习与材料'));fireEvent.click(await screen.findByText('重新解锁家长操作'))
 expect(unlock).toHaveBeenCalledTimes(1);expect(localStorage.getItem(key)).toBeNull()
 expired=false;fireEvent.click(screen.getByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')
 expect(screen.queryByText('重新解锁家长操作')).toBeNull();expect(screen.getByText('只选这道题供本次学习')).toBeEnabled()
})

it('the actual curriculum review shell forwards its existing parent PIN callback to the material selector',async()=>{
 const child={child_id:'child-a',nickname:'孩子甲',grade:'初三'},local={local_only:true,cloud_transmission_allowed:false}
 const scope={grade:'初三',subject:'数学',edition:'合成教材',semester:'上学期',standard_scope:'第四学段',textbook_source_id:'book'}
 const prompt='用求根公式解x²-5x+6=0，写出两个根。',quote='理解一元二次方程求根公式并求出方程的根。'
 const original={schema:'curriculum_objective_source_v1',subject:'数学',grade:'初三',knowledge_node_id:'formula',assessment_mode:'objective',answer_type:'text',prompt,expected_answer:{local_rule:'local_exact_v1',exact_answers:['2,3']}}
 const item={...local,candidate_id:'candidate-a',child_id:'child-a',version:4,state:'published',eligible:true,blockers:[],next_action:'plan_selection',supersedes:null,diagnostic_item_id:'item-a',
  payload:{knowledge_node_id:'formula',excerpt_id:'excerpt',requirement_id:'req',standard_page:12,standard_quote:quote,explanation:'合成原题核对',difficulty:1,seed_item_id:null,stage:'parallel',validator:'curriculum_objective_exact_source_v1',prompt,expected_answer:null,objective_source:original},
  review_packet:{fingerprint:'source1',scope,textbook:{source_id:'book',title:'合成原题',version_label:'合成教材',source_ref:'isolated',review_status:'approved',excerpt_id:'excerpt',content_text:JSON.stringify(original)},standard:{source_id:'std',official_url:'isolated:std',page_number:12,requirement_text:quote,quote},review_scope:'baseline_only'},stage_review_context:null,events:[]}
 const unlock=vi.fn();let expired=false
 const request=vi.fn<CurriculumParentRequest>(async(path,init)=>{
  if(path.endsWith('/paper-learning-selection')){if(expired)throw new ApiError('授权失效',401,'PARENT_SESSION_REQUIRED');return options}
  if(path.endsWith('/oral-authoring'))return {...local,child_id:'child-a',options:[]}
  if(path.endsWith('/options'))return {...local,child_id:'child-a',grade:'初三',semesters:['上学期'],sources:[{source_id:'book',title:'合成教材',subject:'数学',edition:'合成教材',available:true,blockers:[],excerpt_count:1}]}
  if(path.endsWith('/audit'))return {...local,child_id:'child-a',current_curriculum:[{version:1,scope}],blocked_job_counts:{},eligible_curriculum_items:1,qualified_stages:[],packages:[{...item,prompt}],thinking_or_transfer_coverage_claim_allowed:false}
  return item
 })
 render(<CurriculumSupplyReviewPanel child={child} request={request} onRequireParentAuth={unlock}/>)
 fireEvent.click(await screen.findByText('核对当前学习与材料'));await screen.findByText('本次尚未选材料。')
 expired=true;fireEvent.click(screen.getByText('只选这道题供本次学习'))
 fireEvent.click(await screen.findByRole('button',{name:'重新解锁家长操作'}));expect(unlock).toHaveBeenCalledTimes(1)
 expect(localStorage.getItem(key)).not.toBeNull()
})
