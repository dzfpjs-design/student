import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {mistakeLearningOnset,renderMistakeLearningResult} from './mistakeLearningVoice'
import type {MistakeLearningRendered,MistakeLearningState,MistakeLearningSurface} from './mistakeLearningTypes'

let scopeNumber=0
const scopes:Array<{child:string;session:string;mistake:string;owner:string}>=[]
function scope(){const n=++scopeNumber;const value={child:`public-child-${n}`,session:`public-session-${n}`,mistake:`public-case-${n}`,owner:`public-owner-${n}`};scopes.push(value);return value}
function state(s:ReturnType<typeof scope>):MistakeLearningState{return {version:1,child_id:s.child,mistake_id:s.mistake,learning_session_id:'public-learning',revision:7,source_revision:'a'.repeat(64),source:{question:'公开题目',original_answer:'公开原答',subject:'数学',title:'公开学习'},analysis:null,practice_items:[],turns:[],today:{date:'2026-10-29',target_count:2,completed_count:0,remaining_count:2,status:'not_started',stop_reason:null},next_review_at:null,review_due:true,status:'ready',error_code:null,focus:null}}
function surface(s:ReturnType<typeof scope>,practice:string|null='practice-one'):MistakeLearningSurface{return {version:1,child_id:s.child,session_id:s.session,mistake_id:s.mistake,learning_session_id:'public-learning',source_revision:'a'.repeat(64),focus_generation:2,progress_revision:7,question_visible:true,practice_item_id:practice,focus:{mistake_id:s.mistake,child_id:s.child,session_id:s.session,source_revision:'a'.repeat(64),focus_generation:2,focus_created_at:'2026-10-29T00:00:00Z',learning_session_id:'public-learning',revision:7,practice_item_id:practice,practice_created_at:practice?'2026-10-29T00:00:00Z':null}}}
function publish(s:ReturnType<typeof scope>,value:MistakeLearningSurface|null){window.dispatchEvent(new CustomEvent('hsc-mistake-learning-surface',{detail:{owner_id:s.owner,child_id:s.child,session_id:s.session,mistake_id:s.mistake,surface:value}}))}
function dom(s:ReturnType<typeof scope>,practice='practice-one'){
 const root=document.createElement('section');root.dataset.mistakeLearningOwner=s.owner;root.dataset.mistakeLearningId=s.mistake;root.dataset.mistakeLearningSession=s.session
 const question=document.createElement('p');question.dataset.practiceItemId=practice;question.textContent='公开当前题';root.append(question);document.body.append(root);return {root,question}
}
function rendered(s:ReturnType<typeof scope>,revision=7):MistakeLearningRendered{return {owner_id:s.owner,child_id:s.child,session_id:s.session,mistake_id:s.mistake,learning_session_id:'public-learning',source_revision:'a'.repeat(64),revision,view_rendered:true,visible:true}}
function announce(detail:MistakeLearningRendered){window.dispatchEvent(new CustomEvent('hsc-mistake-learning-rendered',{detail}))}
beforeEach(()=>{document.body.replaceChildren();vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:0,y:0,top:0,bottom:80,left:0,right:600,width:600,height:80,toJSON(){return {}}})})
afterEach(()=>{for(const s of scopes)publish(s,null);scopes.length=0;document.body.replaceChildren();vi.restoreAllMocks()})

it('requires the actual connected owner/case/session/question DOM before an onset can bind',()=>{
 const s=scope();publish(s,surface(s));expect(mistakeLearningOnset(s.session,s.child)).toBeNull()
 const {root,question}=dom(s)
 expect(mistakeLearningOnset(s.session,s.child)).toMatchObject({mistake_id:s.mistake,practice_item_id:'practice-one',focus_generation:2})
 root.hidden=true;expect(mistakeLearningOnset(s.session,s.child)).toBeNull();root.hidden=false
 question.hidden=true;expect(mistakeLearningOnset(s.session,s.child)).toBeNull();question.hidden=false
 question.dataset.practiceItemId='different-question';expect(mistakeLearningOnset(s.session,s.child)).toBeNull();question.dataset.practiceItemId='practice-one'
 root.dataset.mistakeLearningId='different-case';expect(mistakeLearningOnset(s.session,s.child)).toBeNull();root.dataset.mistakeLearningId=s.mistake
 root.dataset.mistakeLearningSession='different-session';expect(mistakeLearningOnset(s.session,s.child)).toBeNull();root.dataset.mistakeLearningSession=s.session
 root.dataset.mistakeLearningOwner='replacement-owner';expect(mistakeLearningOnset(s.session,s.child)).toBeNull()
 expect(mistakeLearningOnset(s.session,'other-child')).toBeNull()
})

it('freezes onset and published source independently from mutations or the next practice item',()=>{
 const s=scope(),value=surface(s),{question}=dom(s);publish(s,value)
 const first=mistakeLearningOnset(s.session,s.child)!;expect(first.practice_item_id).toBe('practice-one')
 value.practice_item_id='practice-two';value.focus.practice_item_id='practice-two';value.source_revision='b'.repeat(64)
 expect(mistakeLearningOnset(s.session,s.child)?.practice_item_id).toBe('practice-one')
 value.focus.source_revision=value.source_revision;question.dataset.practiceItemId='practice-two';publish(s,value)
 const next=mistakeLearningOnset(s.session,s.child)!;expect(next.practice_item_id).toBe('practice-two');expect(first.practice_item_id).toBe('practice-one');expect(first.source_revision).toBe('a'.repeat(64))
 first.source_revision='c'.repeat(64);expect(mistakeLearningOnset(s.session,s.child)?.source_revision).toBe('b'.repeat(64))
})

it('allows only a displayed alternate daily question or displayed original while the variant has focus',()=>{
 const s=scope(),{question}=dom(s,'daily-one')
 const value=surface(s,'coach-variant')
 publish(s,{...value,practice_item_id:'daily-one'})
 expect(mistakeLearningOnset(s.session,s.child)).toBeNull()
 publish(s,{...value,practice_item_id:'daily-one',daily_practice_item_id:'daily-one'})
 expect(mistakeLearningOnset(s.session,s.child)?.practice_item_id).toBe('daily-one')
 publish(s,{...value,practice_item_id:null})
 expect(mistakeLearningOnset(s.session,s.child)).toBeNull()
 question.dataset.mistakeLearningQuestion='original'
 expect(mistakeLearningOnset(s.session,s.child)?.practice_item_id).toBeNull()
 expect(mistakeLearningOnset(s.session,s.child)?.question_visible).toBe(true)
 question.setAttribute('aria-hidden','true')
 expect(mistakeLearningOnset(s.session,s.child)).toBeNull()
})

it('ignores an old owner clear and refuses late old-owner source when only the replacement DOM remains',()=>{
 const old=scope(),fresh={...old,owner:'replacement-public-owner'};dom(fresh);publish(fresh,surface(fresh));publish(old,null)
 expect(mistakeLearningOnset(fresh.session,fresh.child)?.practice_item_id).toBe('practice-one')
 publish(old,surface(old));expect(mistakeLearningOnset(old.session,old.child)).toBeNull()
})

it('installs the rendered listener before opening and accepts a synchronous matching event only with real DOM',async()=>{
 const s=scope();dom(s)
 const open=vi.fn(async()=>{announce(rendered(s))})
 await expect(renderMistakeLearningResult({childId:s.child,sessionId:s.session,state:state(s),open,current:()=>true,signal:new AbortController().signal})).resolves.toBeUndefined()
 expect(open).toHaveBeenCalledTimes(1)
})

it('keeps a native answer to the independent variant on the Coach panel until its result is rendered',async()=>{
 const s=scope(),{root}=dom(s)
 const v=state(s)
 v.operation='answer'
 v.coach_variant={practice_item_id:'coach-variant',position:1,question:'公开变式题',stage:'near_transfer',difficulty:1,status:'answered',attempts:1,help_count:0,feedback:null}
 v.turns=[{turn_id:'graded',role:'assistant',kind:'feedback',text:'公开点评',practice_item_id:v.coach_variant.practice_item_id,created_at:'2026-10-29T09:01:00Z'}]
 root.dataset.mistakeLearningPanel='practice'
 const done=renderMistakeLearningResult({childId:s.child,sessionId:s.session,state:v,open:async()=>undefined,current:()=>true,signal:new AbortController().signal})
 let resolved=false
 void done.then(()=>{resolved=true})
 announce(rendered(s));await Promise.resolve();expect(resolved).toBe(false)
 root.dataset.mistakeLearningPanel='coach'
 announce(rendered(s));await done;expect(resolved).toBe(true)
})

it('does not ACK an event alone, hidden DOM, mismatching revision or another owner; matching visible DOM can finish',async()=>{
 const s=scope();let resolved=false
 const done=renderMistakeLearningResult({childId:s.child,sessionId:s.session,state:state(s),open:async()=>undefined,current:()=>true,signal:new AbortController().signal}).then(()=>{resolved=true})
 announce(rendered(s));await Promise.resolve();expect(resolved).toBe(false)
 const {root}=dom(s);root.hidden=true;announce(rendered(s));await Promise.resolve();expect(resolved).toBe(false)
 root.hidden=false;announce(rendered(s,6));await Promise.resolve();expect(resolved).toBe(false)
 announce({...rendered(s),owner_id:'old-owner'});await Promise.resolve();expect(resolved).toBe(false)
 announce(rendered(s));await done;expect(resolved).toBe(true)
})

it('owner retirement or abort rejects the pending render rather than acknowledging a late event',async()=>{
 const s=scope();dom(s);let current=true;const aborter=new AbortController()
 const done=renderMistakeLearningResult({childId:s.child,sessionId:s.session,state:state(s),open:async()=>undefined,current:()=>current,signal:aborter.signal})
 const rejected=expect(done).rejects.toThrow('MISTAKE_LEARNING_VIEW_CHANGED');current=false;announce(rendered(s));await rejected
 const again=renderMistakeLearningResult({childId:s.child,sessionId:s.session,state:state(s),open:async()=>undefined,current:()=>true,signal:aborter.signal})
 const aborted=expect(again).rejects.toThrow('MISTAKE_LEARNING_VIEW_CHANGED');aborter.abort();await aborted
})

it('today-review ACK requires a rendered library, including the actually shown empty state',async()=>{
 const s=scope();let resolved=false
 const done=renderMistakeLearningResult({childId:s.child,sessionId:s.session,open:async()=>undefined,current:()=>true,signal:new AbortController().signal}).then(()=>{resolved=true})
 const detail:MistakeLearningRendered={kind:'library',owner_id:s.owner,child_id:s.child,session_id:s.session,view:'today',visible:true,view_rendered:true}
 announce(detail);await Promise.resolve();expect(resolved).toBe(false)
 const empty=document.createElement('section');empty.className='cloud-book-empty';document.body.append(empty)
 announce({...detail,view:'all'});await Promise.resolve();expect(resolved).toBe(false)
 announce(detail);await done;expect(resolved).toBe(true)
})
