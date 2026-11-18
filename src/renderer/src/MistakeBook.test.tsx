import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {MistakeBook} from './MistakeBook'
import {mistakeLearningApi} from './mistakeLearningApi'
import {claimMistakeCoachingVoice,mistakeLearningOnset} from './mistakeLearningVoice'
import {mistakeWorkspaceVoiceCommand} from './mistakeWorkspaceVoice'
import {localApi} from './api'
import type {MistakeGraphContext,MistakeLearningState} from './mistakeLearningTypes'
import type {ChildProfile,MistakeItem} from './types'
const child:ChildProfile={child_id:'public-child',nickname:'公开同学',grade:'四年级',curriculum_region:'江苏南京',status:'active'}
const old={mistake_id:'public-case',child_id:child.child_id,status:'awaiting_confirmation',subject:'数学',title:'公开加法错题',prompt_summary:'1+1等于多少？',submitted_answer:'3',knowledge_node:{title:'加法'},mastery_gate:null} as MistakeItem
const original:MistakeLearningState={version:1,mistake_id:'public-case',child_id:child.child_id,learning_session_id:'public-learning',revision:0,source_revision:'a'.repeat(64),source:{subject:'数学',title:'公开加法错题',question:'1+1等于多少？',original_answer:'3'},analysis:null,practice_items:[],turns:[],today:{date:'2026-10-29',target_count:2,completed_count:0,remaining_count:2,status:'not_started',stop_reason:null},next_review_at:null,review_due:true,status:'ready',error_code:null,focus:null}
let state:MistakeLearningState
beforeEach(()=>{
 localStorage.clear();state=structuredClone(original)
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:0,y:180,top:180,bottom:240,left:0,right:600,width:600,height:60,toJSON(){return {}}})
 vi.spyOn(localApi,'mistakes').mockResolvedValue({items:[old],counts:{awaiting_confirmation:1,today_strengthening:0,due_review:0,mastered:0}})
 vi.spyOn(localApi,'nextMistakeActivity').mockResolvedValue({kind:'confirmation',stage:null,item:null})
 vi.spyOn(mistakeLearningApi,'library').mockImplementation(async()=>({version:1,child_id:child.child_id,items:[state],today:{date:'2026-10-29',due_count:1,remaining_questions:state.today.remaining_count}}))
 vi.spyOn(mistakeLearningApi,'state').mockImplementation(async()=>structuredClone(state))
 vi.spyOn(mistakeLearningApi,'mutate').mockImplementation(async(_mid,op,body)=>{
  if(op==='focus')state={...state,focus:{mistake_id:state.mistake_id,child_id:child.child_id,session_id:'public-session',source_revision:state.source_revision,focus_generation:1,focus_created_at:new Date().toISOString(),learning_session_id:state.learning_session_id!,revision:state.revision,practice_item_id:null,practice_created_at:null}}
  if(op==='analyze')state={...state,revision:state.revision+1,analysis:{summary:'这里把两个一合起来数错了一步。',wrong_causes:[{label:'数量对应',explanation:'一个一再添一个一，应数到二。'}],knowledge_points:[{label:'加法的含义',description:'把两部分数量合起来。'}],correction_strategy:['先用两样小物品摆一摆。']}}
  return {...structuredClone(state),operation:op,action_status:'completed'}
 })
})
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('keeps old feedback visible and offers a new practice after the graph changes',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 state={...state,graph_practice_stale:true,practice_items:[{
  practice_item_id:'old-item',position:1,question:'旧版本的练习题',stage:'near_transfer',difficulty:1,
  status:'stale',attempts:1,help_count:0,feedback:{kind:'model_formative',judgement:'correct',feedback:'之前的回答已保存。',explanation:'旧反馈内容。',reference_answer:'2',next_hint:''}
 }]}
 render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} active refreshToken={0}/>)
 expect(await screen.findByText('知识点依据已变化或尚未核对，旧练习和反馈已保留。请重新生成本轮练习。')).toBeVisible()
 expect(screen.getByText('之前的回答已保存。')).toBeVisible()
 expect(screen.queryByRole('button',{name:'保存回答，看看这一步'})).not.toBeInTheDocument()
 expect(screen.getByRole('button',{name:'练两题'})).toBeEnabled()
})
it('explains why old analysis is hidden when the tutor route is not configured',async()=>{
 state={...state,analysis:null,analysis_review_required:true,tutor_route_configured:false}
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 fireEvent.click(await screen.findByRole('button',{name:'错因分析'}))
 expect(await screen.findByText('原分析已保留。请先在家长中心配置讲题模型，再重新分析。')).toBeInTheDocument()
})

it('ordinary mistakes open directly into analysis and today practice without asking the child for grading evidence or diagnosis forms',async()=>{
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 expect(await screen.findByRole('button',{name:'分析这题'})).toBeInTheDocument()
 expect(screen.getByRole('button',{name:/^今日复习\s*\d+$/})).toBeInTheDocument()
 expect(screen.queryByText('正在核对评分依据')).not.toBeInTheDocument();expect(screen.queryByText('掌握门槛')).not.toBeInTheDocument()
 expect(screen.queryByLabelText('错因候选')).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'分析这题'}))
 expect(await screen.findByText('这里把两个一合起来数错了一步。')).toBeInTheDocument()
 expect(screen.getByText('加法的含义')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'更新错因分析'}))
 await waitFor(()=>expect(vi.mocked(mistakeLearningApi.mutate).mock.calls.filter(call=>call[1]==='analyze')).toHaveLength(2))
 expect(screen.getByText(/今日已练\s*0\s*\/\s*2/)).toBeInTheDocument()
})

function topicCases() {
 const analysis = {summary:'两个量要分别数清。',wrong_causes:[],knowledge_points:[{label:'加法的含义',description:'合起两部分。'}],correction_strategy:[]}
 const first = {...original,analysis}
 const second = {...first,mistake_id:'public-case-second',source_revision:'b'.repeat(64),source:{...first.source,title:'第二道加法题',question:'3+1等于多少？'},
  analysis:{...analysis,knowledge_points:[{label:'计数顺序',description:'顺次加一。'},...analysis.knowledge_points]}}
 const other = {...second,mistake_id:'public-other-subject',source:{...second.source,subject:'科学',title:'另一学科题'}}
 const items = [first,second,other]
 vi.mocked(mistakeLearningApi.library).mockImplementation(async()=>({version:1,child_id:child.child_id,items,today:{date:'2026-10-29',due_count:3,remaining_questions:2}}))
 vi.mocked(mistakeLearningApi.state).mockImplementation(async id => {
  const item = items.find(candidate=>candidate.mistake_id===id)!
  return {...item,focus:{mistake_id:id,child_id:child.child_id,session_id:'public-session',source_revision:item.source_revision,focus_generation:1,
   focus_created_at:'2026-10-29T09:02:00Z',learning_session_id:item.learning_session_id!,revision:item.revision,practice_item_id:null,practice_created_at:null}}
 })
 vi.spyOn(mistakeLearningApi,'release').mockResolvedValue(undefined)
 return {first,second,other}
}

it('loads the saved topic first, then shows a generated lesson tied to the exact filtered wrong questions', async () => {
 const {first,second,other} = topicCases()
 const sources = [first,second].map(({mistake_id,source_revision})=>({mistake_id,source_revision}))
 const scope = {version:1,child_id:child.child_id,subject:'数学',knowledge_point:'加法的含义',sources:[...sources].reverse()}
 const lesson = {summary:'两道题都要合起两个独立的量。',patterns:[{label:'把已经数过的量再次计入',explanation:'先确定每个加数代表哪一部分。',mistake_ids:[first.mistake_id,second.mistake_id]}],learning_steps:['用两组小物件分别摆出两个加数。'],socratic_question:'哪一组对应第一个加数？'}
 const ready = {...scope,status:'ready',explanation:lesson,created_at:'2026-11-09T10:30:00Z'}
 const fetchTopic = vi.fn().mockResolvedValueOnce(Response.json({...scope,status:'missing',explanation:null,created_at:null}))
  .mockResolvedValueOnce(Response.json(ready)).mockResolvedValueOnce(Response.json(ready))
 vi.stubGlobal('fetch', fetchTopic)

 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 fireEvent.change(screen.getByRole('combobox',{name:'学科'}),{target:{value:'数学'}})
 fireEvent.click(screen.getByRole('button',{name:'打开云端专题'}))
 const generate = await screen.findByRole('button',{name:'生成云端专题'})
 const [url, readRequest] = fetchTopic.mock.calls[0]
 expect(url).toMatch(/\/v1\/children\/public-child\/mistake-learning\/topics\/explain$/)
 expect(JSON.parse(readRequest.body)).toEqual({session_id:'public-session',subject:'数学',knowledge_point:'加法的含义',sources,reuse_only:true})
 fireEvent.click(generate)
 expect(await screen.findByText(lesson.summary)).toBeVisible()
 expect(JSON.parse(fetchTopic.mock.calls[1][1].body)).toEqual({...JSON.parse(readRequest.body),reuse_only:false})
 expect(screen.getByText(lesson.socratic_question)).toBeVisible()
 const topic = screen.getByRole('region',{name:'加法的含义专题讲解'})
 expect(within(topic).getByRole('button',{name:first.source.title})).toBeVisible()
 expect(within(topic).queryByText(other.source.title)).not.toBeInTheDocument()

 fireEvent.click(screen.getByRole('button',{name:'收起专题'}))
 fireEvent.click(screen.getByRole('button',{name:'打开云端专题'}))
 expect(await screen.findByText(lesson.summary)).toBeVisible()
 expect(JSON.parse(fetchTopic.mock.calls[2][1].body).reuse_only).toBe(true)
 fireEvent.click(within(screen.getByRole('region',{name:'加法的含义专题讲解'})).getByRole('button',{name:second.source.title}))
 expect(await screen.findByRole('button',{name:'图文核对'})).toBeVisible()
 expect(screen.getByRole('button',{name:/^错题库/})).toHaveAttribute('aria-current','page')
})

it('aborts a topic request when the selected questions change and keeps an old lesson off the screen', async () => {
 topicCases()
 let respond: (value: Response)=>void = ()=>undefined
 const fetchTopic = vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(resolve => { respond = resolve }))
 vi.stubGlobal('fetch',fetchTopic)

 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 fireEvent.click(screen.getByRole('button',{name:'打开云端专题'}))
 await waitFor(()=>expect(fetchTopic).toHaveBeenCalledOnce())
 const signal = fetchTopic.mock.calls[0][1].signal
 fireEvent.change(screen.getByRole('combobox',{name:'学科'}),{target:{value:'语文'}})
 await waitFor(()=>expect(signal?.aborted).toBe(true))
 await act(async()=>{ respond(Response.json({status:'ready',explanation:{summary:'过期的专题讲解'}})) })
 expect(screen.queryByText('过期的专题讲解')).not.toBeInTheDocument()
 expect(screen.getByText('还没有可归类的错题')).toBeVisible()
})

it('groups graph-linked cases by the canonical point even without analysis and keeps similar legacy labels and other graph keys separate', async () => {
 const {first,second} = topicCases()
 const canonical = '合并两个数量'
 const graph: MistakeGraphContext = {status:'matched',graph_version:'graph@v2',subject:'数学',
  primary:{key:'canonical-addition',title:canonical,review_status:'approved',confidence:.95},prerequisites:[],alternatives:[]}
 const graphFirst = {...first,graph_context:graph}
 const graphSecond = {...second,graph_context:graph,source:{...second.source,subject:''},analysis:null}
 const legacy = {...first,mistake_id:'legacy-graph-label',source:{...first.source,title:'旧标签题'},
  analysis:{...first.analysis,knowledge_points:[{label:canonical,description:'只存在于分析标签。'}]}}
 const otherKey = {...first,mistake_id:'other-canonical-key',source:{...first.source,title:'同名知识点的另一道题'},
  graph_context:{...graph,primary:{...graph.primary!,key:'different-canonical-addition'}}}
 const graphItems = [graphFirst,graphSecond,legacy,otherKey]
 vi.mocked(mistakeLearningApi.library).mockResolvedValue({version:1,child_id:child.child_id,items:graphItems,today:{date:'2026-10-29',due_count:4,remaining_questions:2}})
 vi.mocked(mistakeLearningApi.state).mockImplementation(async id=>{
  const item = graphItems.find(candidate=>candidate.mistake_id===id)!
  return {...item,focus:{mistake_id:id,child_id:child.child_id,session_id:'public-session',source_revision:item.source_revision,focus_generation:1,
   focus_created_at:'2026-10-29T09:02:00Z',learning_session_id:item.learning_session_id!,revision:item.revision,practice_item_id:null,practice_created_at:null}}
 })
 const sources = [graphFirst,graphSecond].map(({mistake_id,source_revision})=>({mistake_id,source_revision,graph_version:graph.graph_version}))
 const scope = {subject:'数学',knowledge_point:canonical,knowledge_node_key:'canonical-addition',sources}
 const lesson = {summary:'用同一种计数单位合并两组数量。',patterns:[{label:'重复计数',explanation:'每件物品只计一次。',mistake_ids:sources.map(source=>source.mistake_id)}],
  learning_steps:['分开摆放两组物品。','把两组物品合在一起再数。'],socratic_question:'什么情况下需要先把两组分开数？'}
 const fetchTopic = vi.fn().mockResolvedValueOnce(Response.json({...scope,version:1,child_id:child.child_id,status:'ready',
  explanation:lesson,created_at:'2026-11-09T10:30:00Z'}))
  .mockResolvedValueOnce(Response.json({detail:{code:'MISTAKE_TOPIC_GRAPH_STALE'}},{status:409}))
 vi.stubGlobal('fetch',fetchTopic)
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 const topics = within(screen.getByRole('region',{name:'高频题型排行'})).getAllByRole('article')
 expect(topics).toHaveLength(3)
 expect(screen.getAllByRole('heading',{name:canonical})).toHaveLength(3)
 expect(within(topics[0]).getByText(/2 道错题 · 知识图谱/)).toBeVisible()
 expect(screen.getByText(/1 道错题 · 按分析标签/)).toBeVisible()
 const open = screen.getByRole('button',{name:'打开云端专题'})
 fireEvent.click(open)
 expect(await screen.findByText(lesson.summary)).toBeVisible()
 expect(JSON.parse(fetchTopic.mock.calls[0][1].body)).toEqual({session_id:'public-session',...scope,reuse_only:true})
 fireEvent.click(screen.getByRole('button',{name:'收起专题'}))
 fireEvent.click(screen.getByRole('button',{name:'打开云端专题'}))
 expect(await screen.findByText(/知识图谱已更新/)).toBeVisible()
 expect(screen.queryByText(lesson.summary)).not.toBeInTheDocument()
 const review = screen.getByRole('region',{name:canonical+'专题讲解'})
 expect(within(review).getByRole('button',{name:first.source.title})).toBeVisible()
 expect(within(review).getByRole('button',{name:second.source.title})).toBeVisible()
 expect(within(review).queryByRole('button',{name:legacy.source.title})).not.toBeInTheDocument()
})

it('progressively matches local topics only on their tab and stops when the remaining count stops falling', async () => {
 const {first,second} = topicCases()
 const library = {version:1 as const,child_id:child.child_id,items:[first,second],today:{date:'2026-10-29',due_count:2,remaining_questions:2}}
 const read = vi.mocked(mistakeLearningApi.library)
 read.mockResolvedValueOnce({...library,graph_pending_count:2})
  .mockResolvedValueOnce({...library,graph_pending_count:1})
  .mockResolvedValue({...library,graph_pending_count:1})
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 await act(async()=>{await new Promise(resolve=>setTimeout(resolve,300))})
 expect(read).toHaveBeenCalledTimes(1)
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 expect(screen.getByText('正在本机匹配知识点，剩余 2 道。')).toBeVisible()
 expect(screen.getByRole('heading',{name:'加法的含义'})).toBeVisible()
 expect(await screen.findByText('还有 1 道待匹配，可以刷新继续。')).toBeVisible()
 expect(read).toHaveBeenCalledTimes(3)
 await act(async()=>{await new Promise(resolve=>setTimeout(resolve,300))})
 expect(read).toHaveBeenCalledTimes(3)
})

it('aborts a progressive topic library read when the learner leaves that tab', async () => {
 const {first,second} = topicCases()
 const library = {version:1 as const,child_id:child.child_id,items:[first,second],today:{date:'2026-10-29',due_count:2,remaining_questions:2}}
 const read = vi.mocked(mistakeLearningApi.library)
 let complete = (): void => undefined
 read.mockResolvedValueOnce({...library,graph_pending_count:2})
  .mockImplementationOnce(() => new Promise(resolve => {complete=()=>resolve({...library,graph_pending_count:1})}))
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 await waitFor(()=>expect(read).toHaveBeenCalledTimes(2))
 const signal = read.mock.calls[1][1]
 expect(signal?.aborted).toBe(false)
 fireEvent.click(screen.getByRole('button',{name:/^错题库/}))
 expect(signal?.aborted).toBe(true)
 await act(async()=>{complete()})
 expect(screen.queryByText(/正在本机匹配知识点/)).not.toBeInTheDocument()
})

it('opens the voice-requested topic tab without a cloud call, then reads a validated lesson only on an explicit voice request', async () => {
 const {first,second} = topicCases()
 const sources = [first,second].map(({mistake_id,source_revision})=>({mistake_id,source_revision}))
 const lesson = {summary:'两道题都要分别数清。',patterns:[{label:'重复计数',explanation:'每个物件只计一次。',mistake_ids:[first.mistake_id,second.mistake_id]}],learning_steps:['分别数两个集合。'],socratic_question:'你会先数哪一组？'}
 const fetchTopic = vi.fn().mockResolvedValueOnce(Response.json({version:1,child_id:child.child_id,subject:'数学',knowledge_point:'加法的含义',sources,status:'ready',explanation:lesson,created_at:'2026-11-09T10:30:00Z'}))
 vi.stubGlobal('fetch',fetchTopic)
 const say = vi.fn<(text: string, scope: AbortSignal) => void>()
 const onHandled = vi.fn()
 const sessionId = 'public-session'
 const props = {child,sessionId,embedded:true,onClose:()=>undefined,onTopicSpeech:say,onTopicVoiceHandled:onHandled}
 const open = {id:'open-topics',childId:child.child_id,sessionId,explain:false}
 const {rerender} = render(<MistakeBook {...props} topicVoiceRequest={{...open,sessionId:'other-session'}}/>)
 await screen.findByRole('button',{name:'图文核对'})
 expect(say).not.toHaveBeenCalled()
 expect(mistakeWorkspaceVoiceCommand('小知，帮我打开高频题型。','小知')).toBe('topics')
 rerender(<MistakeBook {...props} topicVoiceRequest={open}/>)
 await waitFor(()=>expect(say).toHaveBeenCalledWith('高频题型已打开。',expect.any(AbortSignal)))
 expect(fetchTopic).not.toHaveBeenCalled()
 expect(mistakeWorkspaceVoiceCommand('小知，讲讲这组错题。','小知')).toBe('topic_explain')
 rerender(<MistakeBook {...props} topicVoiceRequest={{...open,id:'explain-topics',explain:true}}/>)
 expect(await screen.findByText(lesson.summary)).toBeVisible()
 expect(fetchTopic).toHaveBeenCalledOnce()
 expect(JSON.parse(fetchTopic.mock.calls[0][1].body)).toEqual({session_id:sessionId,subject:'数学',knowledge_point:'加法的含义',sources,reuse_only:false})
 expect(say).toHaveBeenCalledTimes(2)
 expect(say.mock.calls[1][0]).toContain(lesson.socratic_question)
 expect(say.mock.calls[1][1].aborted).toBe(false)
 expect(onHandled.mock.calls).toEqual([['open-topics'],['explain-topics']])
 rerender(<MistakeBook {...props} active={false}/>)
 expect(say.mock.calls[1][1].aborted).toBe(true)
})

it('keeps prior answers out of the original review, captures paper only when requested, and resumes the saved wrong count',async()=>{
 state={...state,analysis:{summary:'参考答案是 2。',wrong_causes:[],knowledge_points:[],correction_strategy:[]}}
 const onReview=vi.fn(async({method}:Parameters<import('./MistakeOriginalReview').OriginalReviewRequest>[0])=>{
  const result={judgement:'incorrect' as const,modality:method,feedback:'这次还没有合在一起数。',next_hint:'一个一再添一个一，应该怎么数？',created_at:'2026-10-29T09:02:00Z'}
  state={...state,revision:state.revision+1,original_review:{status:'not_mastered',wrong_count:2,last_result:result},turns:[{turn_id:'public-review-hint',role:'assistant',kind:'coach',text:result.next_hint,practice_item_id:null,created_at:result.created_at}]}
  return {...state,review_result:result}
 })
 const tree=render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined} onOriginalReviewRequest={onReview}/>)
 const paper=await screen.findByRole('button',{name:'在草稿纸上重做'})
 await waitFor(()=>expect(paper).toBeEnabled())
 expect(screen.queryByText('参考答案是 2。')).not.toBeInTheDocument()
 expect(screen.queryByText('你当时的回答')).not.toBeInTheDocument()
 expect(tree.container.querySelector('[data-mistake-review-id]')).toHaveAttribute('data-mistake-review-id',state.mistake_id)
 fireEvent.click(paper)
 expect(onReview).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'拍草稿并判断'}))
 expect(await screen.findByText(/未掌握 · 累计答错 2 次/)).toBeVisible()
 expect(screen.getByText('一个一再添一个一，应该怎么数？')).toBeVisible()
 expect(tree.container.querySelector('[data-mistake-review-id]')).toBeNull()
 expect(onReview).toHaveBeenCalledWith(expect.objectContaining({method:'paper',record:expect.objectContaining({mistake_id:state.mistake_id})}))
 tree.unmount()
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined} onOriginalReviewRequest={onReview}/>)
 expect(await screen.findByText(/未掌握 · 累计答错 2 次/)).toBeVisible()
})

it('cancels the active spoken review when leaving the original question',async()=>{
 let answerSignal:AbortSignal|undefined
 const onReview:import('./MistakeOriginalReview').OriginalReviewRequest=({signal})=>{
  answerSignal=signal
  return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Cancelled','AbortError')),{once:true}))
 }
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined} onOriginalReviewRequest={onReview}/>)
 const speak=await screen.findByRole('button',{name:'用语音讲解'})
 await waitFor(()=>expect(speak).toBeEnabled())
 fireEvent.click(speak)
 expect(answerSignal?.aborted).toBe(false)
 fireEvent.click(screen.getByRole('button',{name:'图文核对'}))
 await waitFor(()=>expect(answerSignal?.aborted).toBe(true))
 expect(screen.queryByText('Cancelled')).not.toBeInTheDocument()
})

it('shows the next retest immediately even when a correct answer removes the question from the due list',async()=>{
 const onReview:import('./MistakeOriginalReview').OriginalReviewRequest=async({method})=>{
  const result={judgement:'correct' as const,modality:method,feedback:'这次独立答对了。',next_hint:'',created_at:'2026-10-29T09:02:00Z'}
  state={...state,revision:state.revision+1,review_due:false,next_review_at:'2026-10-31T09:02:00Z',original_review:{status:'retest_pending',wrong_count:1,last_result:result}}
  return {...state,review_result:result}
 }
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined} onOriginalReviewRequest={onReview}/>)
 await screen.findByRole('button',{name:'用语音讲解'})
 fireEvent.click(screen.getByRole('button',{name:/^今日复习\s*1$/}))
 const speak=await screen.findByRole('button',{name:'用语音讲解'})
 await waitFor(()=>expect(speak).toBeEnabled())
 fireEvent.click(speak)
 expect(await screen.findByText(/独立答对 · 待复测/)).toBeVisible()
 expect(screen.getByText(/下次复习/)).toBeVisible()
 expect(screen.getByRole('button',{name:/^今日复习\s*0$/})).toBeVisible()
})

it('a source revision change keeps the old words but never submits them as the new question answer',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 const {saveMistakeLearningLocal}=await import('./mistakeLearningApi')
 const oldSource='0'.repeat(64)
 saveMistakeLearningLocal('draft',{version:1,child_id:child.child_id,mistake_id:state.mistake_id,source_revision:oldSource,coach:'旧题的问题',answer:{practice_item_id:'old-item',text:'旧题原答'}})
 state={...state,practice_items:[{practice_item_id:'new-item',position:1,question:'现在的另一题',stage:'near_transfer',difficulty:1,status:'ready',attempts:0,help_count:0,feedback:null}]}
 render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} active refreshToken={0}/>)
 expect(await screen.findByText('现在的另一题')).toBeInTheDocument()
 expect(screen.getByLabelText('你的想法')).toHaveValue('')
 expect(screen.getByText('旧题原答')).toBeInTheDocument()
 expect(screen.getByRole('button',{name:'保存回答，看看这一步'})).toBeDisabled()
 fireEvent.change(screen.getByLabelText('你的想法'),{target:{value:'新题答复'}})
 fireEvent.click(screen.getByRole('button',{name:'保存回答，看看这一步'}))
 await waitFor(()=>expect(mistakeLearningApi.mutate).toHaveBeenCalledWith(state.mistake_id,'answer',expect.objectContaining({practice_item_id:'new-item',utterance:'新题答复'})))
 expect(Object.keys(localStorage).some(k=>k.endsWith(oldSource))).toBe(true)
})

it('unknown requests use read-only receipts and a distinct explicit coaching request can continue',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 const mutate=vi.mocked(mistakeLearningApi.mutate)
 mutate.mockImplementation(async(_mid,operation)=>({...state,operation,action_status:operation==='analyze'?'unknown':'completed'}))
 const read=vi.spyOn(mistakeLearningApi,'action').mockImplementation(async()=>({...state,operation:'analyze',action_status:'unknown'}))
 render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} active refreshToken={0}/>)
 fireEvent.click(await screen.findByRole('button',{name:'分析这题'}))
 expect(await screen.findByRole('button',{name:'核对原请求结果'})).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'核对原请求结果'}))
 await waitFor(()=>expect(read).toHaveBeenCalledTimes(1));expect(mutate).toHaveBeenCalledTimes(1)
 await waitFor(()=>expect(screen.getByRole('button',{name:'讲解这题'})).toBeEnabled())
 fireEvent.click(screen.getByRole('button',{name:'讲解这题'}))
 await waitFor(()=>expect(mutate).toHaveBeenCalledTimes(2))
 expect(mutate.mock.calls[1].slice(0,3)).toEqual([state.mistake_id,'coach',expect.objectContaining({style:'socratic',utterance:'请带我一步一步弄懂这道题。'})])
})

it('reads newly saved click and typed coaching replies once and cancels when the question panel closes',async()=>{
 const question='两个一合在一起，你从一接着数会数到几？'
 const invitation='现在请独立做下面这一道变式题。'
 const variant={practice_item_id:'spoken-variant',position:1,question:'两本书再添三本，一共有几本？',stage:'near_transfer',difficulty:1,status:'ready' as const,attempts:0,help_count:0,feedback:null}
 const speak=vi.fn<(text:string,scope:AbortSignal)=>void>()
 const mutate=vi.mocked(mistakeLearningApi.mutate)
 const defaultMutate=mutate.getMockImplementation()!
 mutate.mockImplementation(async(mid,operation,body)=>{
  if(operation!=='coach')return defaultMutate(mid,operation,body)
  const opening=state.turns.length===0
  const reply={turn_id:opening?'new-coach-turn':'ready-coach-turn',role:'assistant' as const,kind:'coach' as const,
   text:opening?question:invitation,practice_item_id:null,created_at:'2026-11-14T18:00:00Z'}
  state={...state,revision:state.revision+1,coach_phase:opening?'question':'independent_practice',
   coach_variant:opening?null:variant,turns:[...state.turns,reply]}
  return {...structuredClone(state),operation,action_status:'completed'}
 })
 const props={child,sessionId:'public-session',embedded:true,onClose:()=>undefined,onCoachSpeech:speak}
 const view=render(<MistakeBook {...props}/>)
 fireEvent.click(await screen.findByRole('button',{name:'逐题讲解'}))
 const start=screen.getByRole('button',{name:'讲解这题'})
 start.focus()
 fireEvent.click(start,{detail:1})
 expect(start).not.toHaveFocus()
 await waitFor(()=>expect(speak).toHaveBeenCalledExactlyOnceWith(question,expect.any(AbortSignal)))
 const openingScope=speak.mock.calls[0][1]
 expect(openingScope.aborted).toBe(false)
 fireEvent.change(screen.getByLabelText('继续问一问'),{target:{value:'我会从一数到二。'}})
 const send=screen.getByRole('button',{name:'发送这句话'})
 send.focus()
 fireEvent.click(send,{detail:0})
 expect(send).toHaveFocus()
 await waitFor(()=>expect(speak).toHaveBeenLastCalledWith(`${invitation}\n${variant.question}`,expect.any(AbortSignal)))
 expect(openingScope.aborted).toBe(true)
 const variantScope=speak.mock.calls[1][1]
 fireEvent.click(screen.getByRole('button',{name:'图文核对'}))
 expect(variantScope.aborted).toBe(true)
 view.unmount()
 render(<MistakeBook {...props}/>)
 await screen.findByRole('region',{name:'独立变式题'})
 expect(speak).toHaveBeenCalledTimes(2)
})

it('keeps a saved coach reply silent if the learner left the question before it returned',async()=>{
 const question='从一再往后数一个数是什么？'
 const speak=vi.fn()
 const mutate=vi.mocked(mistakeLearningApi.mutate)
 const defaultMutate=mutate.getMockImplementation()!
 let finish:((value:MistakeLearningState)=>void)|undefined
 mutate.mockImplementation(async(mid,operation,body)=>operation==='coach'
  ?new Promise(resolve=>{finish=resolve}):defaultMutate(mid,operation,body))
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined} onCoachSpeech={speak}/>)
 fireEvent.click(await screen.findByRole('button',{name:'逐题讲解'}))
 fireEvent.click(screen.getByRole('button',{name:'讲解这题'}))
 await waitFor(()=>expect(finish).toBeTypeOf('function'))
 fireEvent.click(screen.getByRole('button',{name:'图文核对'}))
 finish?.({...state,revision:state.revision+1,operation:'coach',action_status:'completed',coach_phase:'question',
  turns:[{turn_id:'late-coach',role:'assistant',kind:'coach',text:question,practice_item_id:null,created_at:'2026-11-14T18:00:00Z'}]})
 expect(await screen.findByText(question)).toBeVisible()
 expect(speak).not.toHaveBeenCalled()
})

it('uses the captured voice ID for an active coach and that coach’s visible independent variant',async()=>{
 const sessionId='public-session'
 const invitation='现在请独立做下面这一道变式题。'
 const variant={practice_item_id:'voice-variant',position:1,question:'先有两本书，再添三本。现在有几本？',stage:'near_transfer',
  difficulty:1,status:'ready' as const,attempts:0,help_count:0,feedback:null}
 const speak=vi.fn()
 const mutate=vi.mocked(mistakeLearningApi.mutate)
 const defaultMutate=mutate.getMockImplementation()!
 let releaseVoice:(()=>void)|undefined
 mutate.mockImplementation(async(mid,operation,body)=>{
  if(operation==='coach'){
   const opening=!body.voice_input_id
   if(!opening)await new Promise<void>(resolve=>{releaseVoice=resolve})
   const reply={turn_id:opening?'coach-turn':'closed-coach-turn',role:'assistant' as const,kind:'coach' as const,
    text:opening?'从一再往后数一个数是什么？':invitation,practice_item_id:null,created_at:'2026-11-14T18:00:00Z'}
   state={...state,revision:state.revision+1,coach_turn_id:opening?reply.turn_id:null,
    coach_phase:opening?'question':'independent_practice',coach_variant:opening?null:variant,turns:[...state.turns,reply],
    focus:{...state.focus!,practice_item_id:opening?null:variant.practice_item_id,practice_created_at:opening?null:reply.created_at}}
  }else if(operation==='answer'){
   const feedback={kind:'model_formative' as const,judgement:'correct' as const,feedback:'这道变式你做对了。',
    explanation:'两本加三本是五本。',reference_answer:'5',next_hint:''}
   state={...state,revision:state.revision+1,coach_variant:{...variant,status:'answered',attempts:1,feedback},
    turns:[...state.turns,{turn_id:'variant-feedback',role:'assistant',kind:'feedback',text:feedback.feedback,
     practice_item_id:variant.practice_item_id,created_at:'2026-11-14T18:03:00Z'}]}
  }else return defaultMutate(mid,operation,body)
  return {...structuredClone(state),operation,action_status:'completed'}
 })
 render(<MistakeBook child={child} sessionId={sessionId} embedded onClose={()=>undefined} onCoachSpeech={speak}/>)
 fireEvent.click(await screen.findByRole('button',{name:'逐题讲解'}))
 await waitFor(()=>expect(mistakeLearningOnset(sessionId,child.child_id)?.practice_item_id).toBeNull())
 const originalCapture=mistakeLearningOnset(sessionId,child.child_id)!
 expect(claimMistakeCoachingVoice(originalCapture,'unarmed-voice','我看不出来。')).toBe(false)
 fireEvent.click(screen.getByRole('button',{name:'讲解这题'}))
 await screen.findByText('从一再往后数一个数是什么？')
 act(()=>expect(claimMistakeCoachingVoice(originalCapture,'saved-child-voice','我看不出来。')).toBe(true))
 expect(mutate).toHaveBeenLastCalledWith(state.mistake_id,'coach',{child_id:child.child_id,session_id:sessionId,
  client_action_id:'mistake-voice:saved-child-voice',voice_input_id:'saved-child-voice',source_revision:state.source_revision,
  expected_revision:1,original_review:true,style:'socratic',coach_turn_id:'coach-turn'})
 act(()=>expect(claimMistakeCoachingVoice(originalCapture,'another-saved-voice','再讲一遍。')).toBe(true))
 expect(await screen.findByText(/上一轮还在核对/)).toBeVisible()
 expect(mutate.mock.calls.filter(([,operation])=>operation==='coach')).toHaveLength(2)
 await act(async()=>releaseVoice?.())
 await waitFor(()=>expect(mistakeLearningOnset(sessionId,child.child_id)?.practice_item_id).toBe(variant.practice_item_id))
 expect(claimMistakeCoachingVoice(originalCapture,'stale-original-voice','五本。')).toBe(false)
 const variantCapture=mistakeLearningOnset(sessionId,child.child_id)!
 act(()=>expect(claimMistakeCoachingVoice(variantCapture,'saved-variant-voice','五本。')).toBe(true))
 expect(await within(screen.getByRole('region',{name:'独立变式题'})).findByText('这道变式你做对了。')).toBeVisible()
 expect(mutate.mock.calls.at(-1)?.slice(0,3)).toEqual([state.mistake_id,'answer',{child_id:child.child_id,session_id:sessionId,
  client_action_id:'mistake-voice:saved-variant-voice',voice_input_id:'saved-variant-voice',source_revision:state.source_revision,
  expected_revision:2,practice_item_id:variant.practice_item_id}])
 expect(speak).toHaveBeenLastCalledWith('这道变式你做对了。',expect.any(AbortSignal))
})

it('opens original-question coaching, preserves both drafts on reopening, and submits the independent variant to its own item',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 const {readMistakeLearningLocal,saveMistakeLearningLocal}=await import('./mistakeLearningApi')
 const item={practice_item_id:'daily-item',position:1,question:'2+2等于多少？',stage:'near_transfer',difficulty:1,status:'ready' as const,attempts:0,help_count:0,feedback:null}
 const variant={...item,practice_item_id:'coached-variant',question:'先有2本书，又拿来3本。现在有几本书？'}
 const existing={version:1 as const,child_id:child.child_id,mistake_id:state.mistake_id,source_revision:state.source_revision,coach:'',answer:{practice_item_id:item.practice_item_id,text:'日常练习草稿'}}
 saveMistakeLearningLocal('draft',existing)
 state={...state,practice_items:[item]}
 const mutate=vi.mocked(mistakeLearningApi.mutate).mockImplementation(async(_mid,operation)=>{
  if(operation==='coach')state={...state,revision:state.revision+1,coach_variant:variant}
  if(operation==='answer')state={...state,revision:state.revision+1,coach_variant:{...variant,status:'answered',attempts:1,
   feedback:{kind:'model_formative',judgement:'correct',feedback:'你独立做对了。',explanation:'2加3等于5。',reference_answer:'5',next_hint:''}}}
  return {...structuredClone(state),operation,action_status:'completed'}
 })
 const props={childId:child.child_id,mistakeId:state.mistake_id,active:true,refreshToken:0,bookView:true}
 const first=render(<CloudMistakeLearning {...props}/>)
 fireEvent.click(await screen.findByRole('button',{name:'逐题讲解'}))
 fireEvent.click(screen.getByRole('button',{name:'讲解这题'}))
 await waitFor(()=>expect(mutate).toHaveBeenCalledWith(state.mistake_id,'coach',expect.objectContaining({style:'socratic',original_review:true})))
 const question=await screen.findByRole('region',{name:'独立变式题'})
 expect(within(question).getByText(variant.question)).toBeVisible()
 expect(within(question).queryByText('2加3等于5。')).not.toBeInTheDocument()
 fireEvent.change(within(question).getByLabelText('你的变式题答案'),{target:{value:'两本加三本是五本。'}})
 first.unmount()
 render(<CloudMistakeLearning {...props}/>)
 const draft=await screen.findByLabelText('你的变式题答案')
 expect(draft).toHaveValue('两本加三本是五本。')
 fireEvent.click(screen.getByRole('button',{name:'提交变式题答案'}))
 await waitFor(()=>expect(mutate).toHaveBeenCalledWith(state.mistake_id,'answer',expect.objectContaining({practice_item_id:variant.practice_item_id,utterance:'两本加三本是五本。'})))
 expect(await screen.findByText('你独立做对了。')).toBeVisible()
 expect(readMistakeLearningLocal('draft',child.child_id,state.mistake_id)).toMatchObject({answer:existing.answer,coach_variant_answer:null})
})

it('binds voice onset to the independent variant only while its exact Coach question is visible',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 const {mistakeLearningOnset}=await import('./mistakeLearningVoice')
 const sessionId='public-session'
 const variant={practice_item_id:'voice-coach-variant',position:1,question:'独立变式题干',stage:'near_transfer',difficulty:1,status:'ready' as const,attempts:0,help_count:0,feedback:null}
 state={...state,coach_variant:variant,focus:{mistake_id:state.mistake_id,child_id:child.child_id,session_id:sessionId,
  source_revision:state.source_revision,focus_generation:1,focus_created_at:'2026-10-29T09:00:00Z',learning_session_id:state.learning_session_id!,revision:state.revision,
  practice_item_id:variant.practice_item_id,practice_created_at:'2026-10-29T09:01:00Z'}}
 vi.spyOn(mistakeLearningApi,'release').mockResolvedValue(undefined)
 render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} sessionId={sessionId} active refreshToken={0} bookView/>)
 expect(await screen.findByText(variant.question)).toBeVisible()
 await waitFor(()=>expect(mistakeLearningOnset(sessionId,child.child_id)).toMatchObject({practice_item_id:variant.practice_item_id}))
 fireEvent.click(screen.getByRole('button',{name:'图文核对'}))
 expect(screen.queryByText(variant.question)).not.toBeInTheDocument()
 await waitFor(()=>expect(mistakeLearningOnset(sessionId,child.child_id)).toBeNull())
})

it('hidden book releases the real focus and never publishes a visible question surface',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 const release=vi.spyOn(mistakeLearningApi,'release').mockResolvedValue()
 const listener=vi.fn();window.addEventListener('hsc-mistake-learning-surface',listener)
 const view=render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} sessionId="public-session" active refreshToken={0}/>)
 await screen.findByRole('button',{name:'分析这题'})
 await waitFor(()=>expect(state.focus).not.toBeNull())
 listener.mockClear();view.rerender(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} sessionId="public-session" active={false} refreshToken={0}/>)
 await waitFor(()=>expect(release).toHaveBeenCalledWith(child.child_id,'public-session',1))
 expect(listener.mock.calls.every(([event])=>(event as CustomEvent).detail.surface===null)).toBe(true)
 window.removeEventListener('hsc-mistake-learning-surface',listener)
})

it('new-question save retries the identical original scoped body, with no graph or mandatory original answer',async()=>{
 const {CloudMistakeIntake}=await import('./CloudMistakeLearning')
 const saved=vi.fn(),intake=vi.spyOn(mistakeLearningApi,'intake').mockRejectedValueOnce(Error('public transport failure')).mockResolvedValue({version:1,child_id:child.child_id,mistake_id:'new-public-case',saved:true})
 const view=render(<CloudMistakeIntake childId={child.child_id} sessionId="original-session" active onSaved={saved}/>)
 fireEvent.change(screen.getByLabelText('题目'),{target:{value:'公开题：2+3是多少？'}})
 fireEvent.click(screen.getByRole('button',{name:'保存题目，开始分析'}))
 await screen.findByRole('button',{name:'核对原保存请求'})
 await waitFor(()=>expect(screen.getByRole('button',{name:'核对原保存请求'})).toBeEnabled())
 view.rerender(<CloudMistakeIntake childId={child.child_id} sessionId="later-session" active onSaved={saved}/>)
 fireEvent.click(screen.getByRole('button',{name:'核对原保存请求'}))
 await waitFor(()=>expect(saved).toHaveBeenCalledWith('new-public-case',expect.any(String)))
 expect(intake.mock.calls[1]).toEqual(intake.mock.calls[0])
 expect(intake.mock.calls[0][1]).toMatchObject({submitted_answer:'',session_id:'original-session'})
 expect('knowledge_node_id' in intake.mock.calls[0][1]).toBe(false)
})

it('wrong child replies are rejected rather than painted into the current book',async()=>{
 const {checkedMistakeLearningState}=await import('./mistakeLearningApi')
 expect(()=>checkedMistakeLearningState({...state,child_id:'other-child'},child.child_id,state.mistake_id)).toThrow(/当前孩子或题目不一致/)
})

it('clearing local labor retires the in-flight answer before its late reply can restore removed drafts or pending requests',async()=>{
 const {CloudMistakeLearning}=await import('./CloudMistakeLearning')
 let resolve!:(value:MistakeLearningState)=>void
 vi.mocked(mistakeLearningApi.mutate).mockImplementation(()=>new Promise(r=>{resolve=r}))
 render(<CloudMistakeLearning childId={child.child_id} mistakeId={state.mistake_id} active refreshToken={0}/>)
 fireEvent.click(await screen.findByRole('button',{name:'分析这题'}))
 await waitFor(()=>expect(Object.keys(localStorage).some(k=>k.includes('pending'))).toBe(true))
 localStorage.clear();act(()=>{window.dispatchEvent(new CustomEvent('hsc-cloud-mistake-cleared',{detail:{childId:child.child_id}}))})
 await act(async()=>{resolve({...state,operation:'analyze',action_status:'completed',analysis:{summary:'不应重新显示的晚结果',wrong_causes:[],knowledge_points:[],correction_strategy:[]}})})
 expect(screen.queryByText('不应重新显示的晚结果')).not.toBeInTheDocument();expect(localStorage.length).toBe(0)
})

it('privacy pause blocks the same pending intake reply from re-saving a cleared draft or triggering analysis',async()=>{
 const {CloudMistakeIntake}=await import('./CloudMistakeLearning')
 let resolve!:(value:{version:1;child_id:string;mistake_id:string;saved:true})=>void
 vi.spyOn(mistakeLearningApi,'intake').mockImplementation(()=>new Promise(r=>{resolve=r}))
 const saved=vi.fn();render(<CloudMistakeIntake childId={child.child_id} active onSaved={saved}/>)
 fireEvent.change(screen.getByLabelText('题目'),{target:{value:'公开测试题'}});fireEvent.click(screen.getByRole('button',{name:'保存题目，开始分析'}))
 await waitFor(()=>expect(mistakeLearningApi.intake).toHaveBeenCalled())
 act(()=>{window.dispatchEvent(new Event('hsc-privacy-pause'));localStorage.clear();window.dispatchEvent(new CustomEvent('hsc-cloud-mistake-cleared',{detail:{childId:child.child_id}}))})
 await act(async()=>{resolve({version:1,child_id:child.child_id,mistake_id:'late-case',saved:true})})
 expect(saved).not.toHaveBeenCalled();expect(localStorage.length).toBe(0)
})

it('does not present unreviewed graph matches as a confirmed topic', async () => {
 const {first,second} = topicCases()
 const graph: MistakeGraphContext = {status:'matched',graph_version:'graph@v2',subject:'数学',
  primary:{key:'unreviewed-point',title:'待审核知识点',review_status:'unreviewed',confidence:.95},prerequisites:[],alternatives:[]}
 const items = [first,second].map(item=>({...item,graph_context:graph}))
 vi.mocked(mistakeLearningApi.library).mockResolvedValue({version:1,child_id:child.child_id,items,today:{date:'2026-10-29',due_count:2,remaining_questions:2}})
 vi.mocked(mistakeLearningApi.state).mockImplementation(async id=>{
  const item = items.find(candidate=>candidate.mistake_id===id)!
  return {...item,focus:{mistake_id:id,child_id:child.child_id,session_id:'public-session',source_revision:item.source_revision,focus_generation:1,focus_created_at:'2026-10-29T09:02:00Z',learning_session_id:item.learning_session_id!,revision:item.revision,practice_item_id:null,practice_created_at:null}}
 })
 const fetchTopic = vi.fn()
 vi.stubGlobal('fetch',fetchTopic)
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 expect(screen.getByText(/部分知识点还需核对/)).toBeVisible()
 expect(screen.queryByRole('heading',{name:'待审核知识点'})).not.toBeInTheDocument()
 expect(screen.queryByRole('button',{name:'打开云端专题'})).not.toBeInTheDocument()
 expect(fetchTopic).not.toHaveBeenCalled()
})

it('shows an unreviewed match as a hypothesis and suppresses stale prerequisite claims', async () => {
 const {first,second} = topicCases()
 const graph: MistakeGraphContext = {status:'matched',graph_version:'graph@v2',subject:'数学',
  primary:{key:'unreviewed-point',title:'磁极线索',review_status:'unreviewed',confidence:.95},
  prerequisites:[{key:'unreviewed-pre',title:'待核对的前置',review_status:'unreviewed',relation_review_status:'unreviewed'}],alternatives:[]}
 const item = {...first,graph_context:graph,graph_review_focus:{title:'磁极线索',kind:'primary' as const,state:'due' as const},analysis:{...first.analysis,prerequisite_checks:[{title:'待核对的前置',question:'旧的错误检查？'}]}}
 const items = [item,second]
 vi.mocked(mistakeLearningApi.library).mockResolvedValue({version:1,child_id:child.child_id,items,today:{date:'2026-10-29',due_count:2,remaining_questions:2}})
 vi.mocked(mistakeLearningApi.state).mockImplementation(async id=>{
  const value = items.find(candidate=>candidate.mistake_id===id)!
  return {...value,focus:{mistake_id:id,child_id:child.child_id,session_id:'public-session',source_revision:value.source_revision,focus_generation:1,focus_created_at:'2026-10-29T09:02:00Z',learning_session_id:value.learning_session_id!,revision:value.revision,practice_item_id:null,practice_created_at:null}}
 })
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'错因分析'}))
 expect(await screen.findByText('待核对线索 · 磁极线索')).toBeVisible()
 expect(screen.getByText('候选知识点，结合原题核对。')).toBeVisible()
 expect(screen.queryByText('复习重点「磁极线索」')).not.toBeInTheDocument()
 expect(screen.queryByText('旧的错误检查？')).not.toBeInTheDocument()
 expect(screen.queryByText(/可以先检查.*待核对的前置/)).not.toBeInTheDocument()
})

it('offers an independent-analysis topic when graph matches are unreviewed', async () => {
 const {first,second} = topicCases()
 const graph: MistakeGraphContext = {status:'matched',graph_version:'graph@v2',subject:'数学',
  primary:{key:'unreviewed-point',title:'待审核图谱标签',review_status:'unreviewed',confidence:.95},prerequisites:[],alternatives:[]}
 const label='相同的独立分析标签'
 const items=[first,second].map(item=>({...item,graph_context:graph,analysis_independent:true,
  analysis:{...item.analysis,knowledge_points:[{label,description:'依据题目独立分析。'}]}}))
 vi.mocked(mistakeLearningApi.library).mockResolvedValue({version:1,child_id:child.child_id,items,today:{date:'2026-10-29',due_count:2,remaining_questions:2}})
 vi.mocked(mistakeLearningApi.state).mockImplementation(async id=>{
  const item=items.find(candidate=>candidate.mistake_id===id)!
  return {...item,focus:{mistake_id:id,child_id:child.child_id,sessionId:'public-session',session_id:'public-session',source_revision:item.source_revision,focus_generation:1,focus_created_at:'2026-10-29T09:02:00Z',learning_session_id:item.learning_session_id!,revision:item.revision,practice_item_id:null,practice_created_at:null}}
 })
 const fetchTopic=vi.fn().mockImplementation(async (_url,req)=>Response.json({version:1,child_id:child.child_id,...JSON.parse(req.body),status:'missing',explanation:null,created_at:null}))
 vi.stubGlobal('fetch',fetchTopic)
 render(<MistakeBook child={child} sessionId="public-session" embedded onClose={()=>undefined}/>)
 await screen.findByRole('button',{name:'图文核对'})
 fireEvent.click(screen.getByRole('button',{name:'高频题型'}))
 expect(screen.getByRole('heading',{name:label})).toBeVisible()
 expect(screen.queryByRole('heading',{name:'待审核图谱标签'})).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'打开云端专题'}))
 await waitFor(()=>expect(fetchTopic).toHaveBeenCalledOnce())
 const sent=JSON.parse(fetchTopic.mock.calls[0][1].body)
 expect(sent.knowledge_point).toBe(label)
 expect(sent.knowledge_node_key).toBeUndefined()
 expect(JSON.stringify(sent)).not.toContain('unreviewed-point')
})
