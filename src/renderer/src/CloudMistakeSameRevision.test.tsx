import{act,cleanup,fireEvent,render,screen,waitFor}from'@testing-library/react'
import{afterEach,beforeEach,expect,it,vi}from'vitest'
import{MistakeBook}from'./MistakeBook'
import{mistakeLearningApi}from'./mistakeLearningApi'
import{renderMistakeLearningResult}from'./mistakeLearningVoice'
import type{MistakeLearningRendered,MistakeLearningState}from'./mistakeLearningTypes'
const child={child_id:'public-cached-child',nickname:'公开同学',grade:'初三',curriculum_region:'江苏南京',status:'active' as const},sid='public-cached-session',mid='public-cached-case'
const value:MistakeLearningState={version:1,child_id:child.child_id,mistake_id:mid,learning_session_id:'public-learning',revision:4,source_revision:'a'.repeat(64),source:{question:'公开原题',original_answer:'公开原答',subject:'数学',title:'公开已分析错题'},analysis:{summary:'公开已保存分析',wrong_causes:[],knowledge_points:[],correction_strategy:[]},practice_items:[],turns:[],today:{date:'2026-10-29',target_count:2,completed_count:0,remaining_count:2,status:'not_started',stop_reason:null},next_review_at:null,review_due:true,status:'ready',error_code:null,focus:{child_id:child.child_id,session_id:sid,mistake_id:mid,learning_session_id:'public-learning',source_revision:'a'.repeat(64),focus_generation:2,focus_created_at:'2026-10-29T00:00:00Z',revision:4,practice_item_id:null,practice_created_at:null}}
const caseEvents:MistakeLearningRendered[]=[]
const receive=(event:Event)=>{const d=(event as CustomEvent<MistakeLearningRendered>).detail;if(d.kind!=='library')caseEvents.push(d)}
beforeEach(()=>{localStorage.clear();caseEvents.length=0;window.addEventListener('hsc-mistake-learning-rendered',receive);vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:0,y:80,top:80,bottom:180,left:0,right:600,width:600,height:100,toJSON(){return {}}});vi.spyOn(mistakeLearningApi,'state').mockImplementation(async()=>structuredClone(value));vi.spyOn(mistakeLearningApi,'library').mockImplementation(async()=>({version:1,child_id:child.child_id,items:[structuredClone(value)],today:{date:'2026-10-29',due_count:1,remaining_questions:2}}));vi.spyOn(mistakeLearningApi,'release').mockResolvedValue();vi.spyOn(mistakeLearningApi,'mutate').mockImplementation(async(_id,operation)=>({...structuredClone(value),operation,action_status:'completed'}))})
afterEach(()=>{cleanup();window.removeEventListener('hsc-mistake-learning-rendered',receive);vi.restoreAllMocks()})
async function opened(analysis=false){render(<MistakeBook child={child} sessionId={sid} embedded onClose={()=>undefined}/>);const openAnalysis=await screen.findByRole('button',{name:'错因分析'});if(analysis)fireEvent.click(openAnalysis);await waitFor(()=>expect(caseEvents.length).toBeGreaterThan(0));caseEvents.length=0}
it('already-open cached analyze and repeated select each re-read the same version and ACK only after the actual read/layout',async()=>{
 await opened()
 for(const operation of ['analyze','focus'] as const){
  const cached=await mistakeLearningApi.mutate(mid,operation,{child_id:child.child_id,session_id:sid,client_action_id:'public-cached-'+operation})
  const before=vi.mocked(mistakeLearningApi.state).mock.calls.length,aborter=new AbortController();let done=false
  const result=renderMistakeLearningResult({childId:child.child_id,sessionId:sid,state:cached,open:async()=>undefined,current:()=>true,signal:aborter.signal}).then(()=>{done=true},()=>undefined)
  try{await waitFor(()=>expect(done).toBe(true));expect(mistakeLearningApi.state).toHaveBeenCalledTimes(before+1);expect(caseEvents.at(-1)).toMatchObject({mistake_id:mid,revision:4,source_revision:value.source_revision,view_rendered:true});if(operation==='analyze')expect(screen.getByText('公开已保存分析')).toBeVisible()}finally{aborter.abort();await result}
  caseEvents.length=0
 }
})
it('failed fresh read cannot reuse an old same-version rendered view as this request ACK',async()=>{
 await opened(true);vi.mocked(mistakeLearningApi.state).mockRejectedValueOnce(Error('public failed GET'))
 const aborter=new AbortController();let done=false
 const result=renderMistakeLearningResult({childId:child.child_id,sessionId:sid,state:value,open:async()=>undefined,current:()=>true,signal:aborter.signal}).then(()=>{done=true},()=>undefined)
 try{await screen.findByRole('alert');await act(async()=>{await Promise.resolve()});expect(caseEvents).toHaveLength(0);expect(done).toBe(false);expect(screen.getByText('公开已保存分析')).toBeInTheDocument()}finally{aborter.abort();await result}
})
