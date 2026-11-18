import {useCallback,useEffect,useLayoutEffect,useRef,useState} from 'react'
import {ApiError} from './api'
import {mistakeLearningApi,readMistakeLearningLocal,saveMistakeLearningLocal,clearMistakeLearningPending,pendingMistakeLearningRequests,archiveMistakeLearningDraft,readMistakeLearningIntake,saveMistakeLearningIntake,type MistakeLearningIntakeLocal,type MistakeLearningDraft,type MistakeLearningPending} from './mistakeLearningApi'
import {MISTAKE_LEARNING_SURFACE_EVENT,MISTAKE_LEARNING_RENDERED_EVENT,type MistakeLearningState,type MistakeLearningMutation,type MistakeLearningOperation,type MistakeLearningPanel,type MistakeLearningSurface} from './mistakeLearningTypes'
import {MistakeQuestionCrop,OriginalReviewControls,OriginalReviewOutcome,reviewLabel,type OriginalReviewRequest} from './MistakeOriginalReview'
import {MISTAKE_COACHING_VOICE_EVENT,type MistakeCoachingVoice} from './mistakeLearningVoice'

function approvedReviewFocus(state:MistakeLearningState):boolean {
 const graph=state.graph_context,focus=state.graph_review_focus
 if(!focus||graph?.status!=='matched'||graph.primary?.review_status!=='approved')return false
 return focus.kind==='primary' ? focus.title===graph.primary.title : graph.prerequisites.some(point=>point.title===focus.title&&point.review_status==='approved'&&point.relation_review_status==='approved')
}

function GraphStudyNotes({state}:{state:MistakeLearningState}):React.JSX.Element|null{
 const graph=state.graph_context
 if(!graph)return null
 if(graph.status==='unavailable')return <p className="cloud-learning-notice">暂时读不到本机知识图谱。你仍可以继续看原题和分析。</p>
 if(graph.status==='no_match')return <p className="cloud-learning-notice">这题还没有匹配到知识图谱，可以先按原题分析。</p>
 if(graph.status==='ambiguous')return <p className="cloud-learning-notice">这题可能涉及{graph.alternatives.slice(0,2).map(point=>`「${point.title}」`).join('或')||'几个相近知识点'}，暂时单独分析。</p>
 if(!graph.primary)return null
 const approvedPrerequisites=graph.primary.review_status==='approved' ? graph.prerequisites.filter(point=>point.review_status==='approved'&&point.relation_review_status==='approved') : []
 const checks=(state.analysis?.prerequisite_checks??[]).filter(check=>approvedPrerequisites.some(point=>point.title===check.title))
 return <section className="cloud-cause" aria-label="知识图谱学习线索">
  <h4>{graph.primary.review_status==='approved'?'图谱关联':'待核对线索'} · {graph.primary.title}</h4>
  {graph.primary.review_status!=='approved'&&<p>候选知识点，结合原题核对。</p>}
  {checks.length>0?<ul>{checks.map(check=><li key={check.title}>
    <strong>先核对{check.title}</strong><p>{check.question}</p>
    {check.evidence_quote&&<small>原作答线索「{check.evidence_quote}」</small>}
  </li>)}</ul>:approvedPrerequisites.length>0&&<p>可以先检查{approvedPrerequisites.slice(0,2).map(point=>`「${point.title}」`).join('、')}。</p>}
 </section>
}

type DetailView = MistakeLearningPanel | 'source'
const bookViews: Array<[DetailView, string]> = [['redo','复习原题'],['source','图文核对'],['analysis','错因分析'],['practice','变式练习'],['coach','逐题讲解']]
type Props={childId:string;mistakeId:string;sessionId?:string;active:boolean;refreshToken:number;onChanged?:(state:MistakeLearningState)=>void;onLearningFocus?:(surface:MistakeLearningSurface|null,ownerId:string)=>void;onCoachSpeech?:(text:string,scope:AbortSignal)=>void;autoAnalyzeActionId?:string;bookView?:boolean;viewRequest?:{panel:MistakeLearningPanel;token:number};onOriginalReviewRequest?:OriginalReviewRequest}
function restored(childId:string,mistakeId:string){try{return {draft:readMistakeLearningLocal<MistakeLearningDraft>('draft',childId,mistakeId),pending:readMistakeLearningLocal<MistakeLearningPending>('pending',childId,mistakeId),error:false}}catch{return {draft:null,pending:null,error:true}}}
function userMessage(error:unknown):string{
 const code=error instanceof ApiError?error.code??'':''
 if(code==='MISTAKE_LEARNING_CLOUD_LABEL_RIGHT_REQUIRED')return '这道旧练习的云端使用授权已变化，题目和作答仍在本机。请重新生成练习。'
 if(/REVISION|SOURCE|SCOPE/.test(code))return '题目或进度刚有更新，之前写下的话仍保留。先刷新，再继续这一步。'
 if(/PRIVACY|CONSENT|PERMISSION/.test(code))return '云端学习暂未开启，已经保存的内容仍在。请让家长检查服务设置。'
 return '这一步暂时没有完成，已经保存的内容和写下的话都还在。'
}
export function CloudMistakeLearning({childId,mistakeId,sessionId,active,refreshToken,onChanged,onLearningFocus,onCoachSpeech,autoAnalyzeActionId,bookView=false,viewRequest,onOriginalReviewRequest}:Props):React.JSX.Element{
 const [restore]=useState(()=>restored(childId,mistakeId))
 const [state,setState]=useState<MistakeLearningState|null>(null)
 const [detailView,setDetailView]=useState<DetailView>(viewRequest?.panel??'redo')
 const detailViewRef=useRef(detailView);detailViewRef.current=detailView
 useEffect(()=>{if(viewRequest)setDetailView(viewRequest.panel)},[viewRequest])
 const [readReceipt,setReadReceipt]=useState(0)
 const stateRef=useRef(state);stateRef.current=state
 const [draft,setDraft]=useState<MistakeLearningDraft|null>(restore.draft)
 const draftRef=useRef(draft);draftRef.current=draft
 const [pending,setPending]=useState<MistakeLearningPending|null>(restore.pending)
 const pendingRef=useRef(pending);pendingRef.current=pending
 const [busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState<string|null>(restore.error?'上次的暂存内容还没有读到，请先保留本页并重试读取。':null),[storageError,setStorageError]=useState<string|null>(null)
 const owner=useRef(crypto.randomUUID()),mounted=useRef(false),activeRef=useRef(active),readGeneration=useRef(0),operationGeneration=useRef(0),working=useRef(false),question=useRef<HTMLParagraphElement|null>(null),originalQuestion=useRef<HTMLParagraphElement|null>(null),variantQuestion=useRef<HTMLParagraphElement|null>(null),root=useRef<HTMLElement|null>(null),analysisPanel=useRef<HTMLElement|null>(null),threadEnd=useRef<HTMLDivElement|null>(null)
 activeRef.current=active
 const handlers=useRef({onChanged,onLearningFocus,onCoachSpeech});handlers.current={onChanged,onLearningFocus,onCoachSpeech}
 const coachSpeech=useRef<AbortController|null>(null)
 const stopCoachSpeech=useCallback(()=>{coachSpeech.current?.abort();coachSpeech.current=null},[])
 useLayoutEffect(()=>stopCoachSpeech,[stopCoachSpeech,childId,mistakeId,sessionId,active,bookView,detailView,state?.source_revision])
 const lastSurface=useRef(''),focusRequest=useRef<{source:string;body:MistakeLearningMutation}|null>(null)
 const paused=useRef(false)
 const current=():boolean=>mounted.current&&activeRef.current&&!paused.current
 const release=():void=>{const focus=stateRef.current?.focus;if(sessionId&&focus?.session_id===sessionId){stateRef.current={...stateRef.current!,focus:null};void mistakeLearningApi.release(childId,sessionId,focus.focus_generation).catch(()=>{/* DOM is already hidden; no stale surface can authorize a new input. */})}}
 const publish=useCallback((surface:MistakeLearningSurface|null)=>{
  const fingerprint=JSON.stringify(surface?{...surface,progress_revision:0,focus:{...surface.focus,revision:0}}:null)
  if(lastSurface.current===fingerprint)return
  lastSurface.current=fingerprint
  handlers.current.onLearningFocus?.(surface,owner.current)
  window.dispatchEvent(new CustomEvent(MISTAKE_LEARNING_SURFACE_EVENT,{detail:{owner_id:owner.current,child_id:childId,session_id:sessionId,mistake_id:mistakeId,surface}}))
 },[childId,mistakeId,sessionId])
 useLayoutEffect(()=>{mounted.current=true;return()=>{mounted.current=false;readGeneration.current++;operationGeneration.current++;publish(null);release()}},[publish])
 useEffect(()=>{
  const pause=()=>{stopCoachSpeech();paused.current=true;readGeneration.current++;operationGeneration.current++;publish(null);release();setBusy(false);setError('云端学习已暂停，已经保存的内容仍在。')}
  const clear=(event:Event)=>{if((event as CustomEvent<{childId:string}>).detail?.childId!==childId)return;stopCoachSpeech();readGeneration.current++;operationGeneration.current++;publish(null);release();draftRef.current=null;pendingRef.current=null;stateRef.current=null;setDraft(null);setPending(null);setState(null);setLoading(false);setError('当前孩子的错题学习暂存已清除。')}
  const visibility=()=>{if(document.hidden)stopCoachSpeech()}
  window.addEventListener('hsc-privacy-pause',pause);window.addEventListener('hsc-cloud-mistake-cleared',clear)
  document.addEventListener('visibilitychange',visibility)
  return()=>{window.removeEventListener('hsc-privacy-pause',pause);window.removeEventListener('hsc-cloud-mistake-cleared',clear);document.removeEventListener('visibilitychange',visibility)}
 },[childId,publish,stopCoachSpeech])
 const apply=(value:MistakeLearningState):void=>{
  const prior=stateRef.current
  if(prior?.source_revision===value.source_revision&&prior.revision>value.revision)return
  if(bookView){
   const oldReview=prior?.original_review?.last_result, lastReview=value.original_review?.last_result
   const newIncorrect=value.review_result?.judgement==='incorrect'||(prior&&lastReview?.judgement==='incorrect'&&lastReview.created_at!==oldReview?.created_at)
   const readyItem=value.practice_items.find(row=>row.status==='ready')
   const oldReadyItem=prior?.practice_items.find(row=>row.status==='ready')
   const lastTurn=value.turns.at(-1)
   const newCoachVariant=value.coach_variant?.status==='ready'
    && value.coach_variant.practice_item_id!==prior?.coach_variant?.practice_item_id && (prior||!viewRequest)
   if(newIncorrect||newCoachVariant)setDetailView('coach')
   else if(prior&&readyItem&&readyItem.practice_item_id!==oldReadyItem?.practice_item_id)setDetailView('practice')
   else if(prior&&value.analysis&&!prior.analysis&&!autoAnalyzeActionId)setDetailView('analysis')
   else if(prior&&lastTurn?.kind==='coach'&&lastTurn.role==='assistant'&&lastTurn.turn_id!==prior.turns.at(-1)?.turn_id)setDetailView('coach')
  }
  stateRef.current=value;setState(value);handlers.current.onChanged?.(value)
 }
 const load=useCallback(async()=>{
  if(!current())return
  const generation=++readGeneration.current
  setLoading(true)
  try{
   let value=await mistakeLearningApi.state(mistakeId,childId,sessionId)
   if(!current()||generation!==readGeneration.current)return
   if(sessionId&&(!value.focus||value.focus.session_id!==sessionId)){
    if(focusRequest.current?.source!==value.source_revision)focusRequest.current={source:value.source_revision,body:{child_id:childId,session_id:sessionId,client_action_id:crypto.randomUUID(),expected_revision:value.revision}}
    value=await mistakeLearningApi.mutate(mistakeId,'focus',focusRequest.current.body)
    if(!current()||generation!==readGeneration.current)return
    value=await mistakeLearningApi.state(mistakeId,childId,sessionId)
    if(!value.focus||value.focus.session_id!==sessionId){focusRequest.current=null;throw Error('FOCUS_UNCONFIRMED')}
   }
   const previous=stateRef.current
   if(previous&&previous.source_revision!==value.source_revision){operationGeneration.current++;publish(null)}
   apply(value);setReadReceipt(generation);if(!restore.error)setError(null)
   if(!draftRef.current){const initial:MistakeLearningDraft={version:1,child_id:childId,mistake_id:mistakeId,source_revision:value.source_revision,coach:'',answer:null};draftRef.current=initial;setDraft(initial)}
  }catch(cause){if(current()&&generation===readGeneration.current)setError(userMessage(cause))}
  finally{if(current()&&generation===readGeneration.current)setLoading(false)}
 },[childId,mistakeId,sessionId,publish,bookView,autoAnalyzeActionId,viewRequest])
 useEffect(()=>{if(active)void load();else{readGeneration.current++;focusRequest.current=null;publish(null);release()}},[active,refreshToken,load,publish])
 useEffect(()=>{
  if(!active||state?.status!=='processing')return
  let attempts=0,inFlight=false
  const timer=setInterval(()=>{if(!inFlight&&current()&&++attempts<=20){inFlight=true;void load().finally(()=>{inFlight=false})}},1500)
  return()=>clearInterval(timer)
 },[active,state?.status,load])
 const item=state?.practice_items.find(row=>row.status==='ready')??null
 const readyVariantId=state?.coach_variant?.status==='ready'?state.coach_variant.practice_item_id:null
 useLayoutEffect(()=>{
  if(!active||!state?.focus||state.focus.session_id!==sessionId){publish(null);return}
  let cancelled=false,frame:number|null=null
  const observe=()=>{
   if(cancelled||!current())return
   const value=stateRef.current,focus=value?.focus;if(!value||!focus){publish(null);return}
   if(!root.current?.isConnected||root.current.closest('[hidden],[aria-hidden="true"]')){publish(null);return}
   const variant=value.coach_variant?.status==='ready'?value.coach_variant:null
   const visibleVariant=variant&&(!bookView||detailViewRef.current==='coach')?variant:null
   const dailyItem=value.practice_items.find(row=>row.status==='ready')
   const dailyPracticeId=variant?.practice_item_id===focus.practice_item_id?dailyItem?.practice_item_id:undefined
   const visible=visibleVariant??((!bookView||detailViewRef.current==='practice')?dailyItem??null:null)
   const node=visibleVariant?variantQuestion.current:visible?question.current:originalQuestion.current
   const box=node?.getBoundingClientRect()
   const visibleMatchesFocus=!visible||focus.practice_item_id===visible.practice_item_id||dailyPracticeId===visible.practice_item_id
   const questionVisible=visibleMatchesFocus&&!!node?.isConnected&&!node.closest('[hidden],[aria-hidden="true"]')&&!!box&&box.width>0&&box.height>0
   publish({version:1,child_id:childId,session_id:sessionId,mistake_id:mistakeId,learning_session_id:focus.learning_session_id,source_revision:value.source_revision,focus_generation:focus.focus_generation,progress_revision:value.revision,focus,practice_item_id:visible?.practice_item_id??null,daily_practice_item_id:dailyPracticeId,question_visible:questionVisible})
  }
  const schedule=()=>{if(frame!==null)return;frame=requestAnimationFrame(()=>{frame=null;observe()})}
  if(item&&(!bookView||detailView==='practice'))question.current?.scrollIntoView?.({block:'start',behavior:'auto'})
  schedule();window.addEventListener('scroll',schedule,true);window.addEventListener('resize',schedule)
  return()=>{cancelled=true;if(frame!==null)cancelAnimationFrame(frame);window.removeEventListener('scroll',schedule,true);window.removeEventListener('resize',schedule);publish(null)}
 },[active,state?.focus?.focus_generation,state?.focus?.practice_item_id,state?.source_revision,item?.practice_item_id,readyVariantId,childId,mistakeId,sessionId,publish,bookView,detailView])
 const saveDraft=(next:MistakeLearningDraft):void=>{
  draftRef.current=next;setDraft(next)
  try{saveMistakeLearningLocal('draft',next);setStorageError(null)}catch{setStorageError('本页暂存还没有确认，请先不要关闭。写下的话仍留在这里。')}
 }
 const updateText=(kind:'coach'|'answer'|'coach_variant_answer',text:string):void=>{
  if(!state)return
  const previous=draftRef.current??{version:1 as const,child_id:childId,mistake_id:mistakeId,source_revision:state.source_revision,coach:'',answer:null}
  if(previous.source_revision!==state.source_revision){try{archiveMistakeLearningDraft(previous)}catch{setStorageError('之前写下的话还没有安全暂存，请先保留本页。');return}}
  const base=previous.source_revision===state.source_revision?previous:{...previous,source_revision:state.source_revision,coach:'',answer:null,coach_variant_answer:null}
  if(kind==='coach'){saveDraft({...base,coach:text});return}
  const target=kind==='answer'?item:state.coach_variant
  if(target?.status==='ready')saveDraft({...base,[kind]:{practice_item_id:target.practice_item_id,text}})
 }
 const execute=async(record:MistakeLearningPending,speakNewReply=false):Promise<void>=>{
  if(!current()||working.current||!stateRef.current)return
  if(record.source_revision!==stateRef.current.source_revision){setError('题目已经更新，原来写下的话保留着；请核对当前题目后再发送。');return}
  try{saveMistakeLearningLocal('pending',record)}catch{setStorageError('这次请求还没有暂存成功，没有发起新操作。请保留本页后重试。');return}
  let speechScope:AbortController|null=null
  const before=stateRef.current
  if(speakNewReply&&sessionId&&handlers.current.onCoachSpeech&&(!bookView||detailViewRef.current==='coach')){
   stopCoachSpeech()
   speechScope=new AbortController();coachSpeech.current=speechScope
  }
  working.current=true;setBusy(true);pendingRef.current=record;setPending(record);setError(null)
  const generation=++operationGeneration.current
  try{
   const result=await mistakeLearningApi.mutate(mistakeId,record.operation,record.body)
   if(!current()||generation!==operationGeneration.current)return
   if(result.source_revision!==record.source_revision){publish(null);setError('题目内容刚有变化，之前写下的话还在。请刷新后继续。');return}
   apply(result)
   if(result.action_status==='unknown'||result.action_status==='processing'){
    const retained={...record,status:'unknown' as const,...(result.pending_client_action_id&&result.pending_client_action_id!==record.body.client_action_id?{waiting_for_action_id:result.pending_client_action_id}:{})};saveMistakeLearningLocal('pending',retained);pendingRef.current=retained;setPending(retained)
    setError('这次请求的结果还在核对，原请求已经保留；不用重复发送。');return
   }
   if(result.action_status==='failed'||result.status==='failed'){
    const retained={...record,status:'failed' as const};saveMistakeLearningLocal('pending',retained);pendingRef.current=retained;setPending(retained)
    setError(record.body.voice_input_id?'这次没能完成，刚才的话已保存。可以再说一遍。':'这次没能完成，原内容已经保留。可以重试刚才这一步。');return
   }
   clearMistakeLearningPending(childId,mistakeId,record.body.client_action_id);const retained=pendingMistakeLearningRequests(childId,mistakeId).find(item=>item.status==='unknown'||item.status==='pending')??null;pendingRef.current=retained;setPending(retained)
   const d=draftRef.current
   if(d&&record.operation==='answer'&&d.answer&&d.answer.practice_item_id===record.body.practice_item_id&&d.answer.text===record.body.utterance)saveDraft({...d,answer:null})
   if(d&&record.operation==='answer'&&result.coach_variant?.status==='answered'&&d.coach_variant_answer&&d.coach_variant_answer.practice_item_id===record.body.practice_item_id&&d.coach_variant_answer.text===record.body.utterance)saveDraft({...d,coach_variant_answer:null})
   if(d&&record.operation==='coach'&&d.coach===record.body.utterance)saveDraft({...draftRef.current!,coach:''})
   const reply=result.turns.at(-1)
   if(speechScope&&!speechScope.signal.aborted&&stateRef.current===result&&result.operation===record.operation&&result.action_status==='completed'
      &&result.child_id===childId&&result.mistake_id===mistakeId&&result.focus?.session_id===sessionId
      &&result.focus?.focus_generation===before.focus?.focus_generation&&reply?.role==='assistant'
      &&reply.turn_id!==before.turns.at(-1)?.turn_id&&reply.text.trim()){
    if(record.operation==='coach'&&reply.kind==='coach'){
     const variant=result.coach_phase==='independent_practice'?result.coach_variant:null
     const spoken=variant?.status==='ready'?`${reply.text}\n${variant.question}`:reply.text
     handlers.current.onCoachSpeech?.(spoken,speechScope.signal)
    }else if(record.operation==='answer'&&record.body.voice_input_id&&reply.kind==='feedback'
       &&reply.practice_item_id===record.body.practice_item_id&&result.coach_variant?.practice_item_id===reply.practice_item_id){
     const feedback=result.coach_variant.feedback
     const spoken=feedback?[feedback.feedback,feedback.judgement!=='correct'?feedback.next_hint:null].filter(Boolean).join(' '):''
     if(spoken)handlers.current.onCoachSpeech?.(spoken,speechScope.signal)
    }
   }
  }catch(cause){
   if(current()&&generation===operationGeneration.current){const retained={...record,status:'unknown' as const};try{saveMistakeLearningLocal('pending',retained)}catch{/* Keep the original durable request. */}pendingRef.current=retained;setPending(retained);setError(userMessage(cause))}
  }finally{working.current=false;if(current()){setBusy(false)}}
 }
 const run=(operation:Exclude<MistakeLearningOperation,'focus'>,values:Partial<MistakeLearningMutation>={}):void=>{
  const value=stateRef.current;if(!value||!current()||working.current||restore.error)return
  let requests:MistakeLearningPending[];try{requests=pendingMistakeLearningRequests(childId,mistakeId)}catch{setStorageError('原来的暂存请求尚未读到，请保留本页后重试。');return}
  const previous=requests.find(item=>item.source_revision===value.source_revision&&item.status!=='failed'&&item.operation===operation&&['practice_item_id','utterance','style','count','original_review'].every(key=>(item.body as Record<string,unknown>)[key]===(values as Record<string,unknown>)[key]))
  if(previous){pendingRef.current=previous;setPending(previous);setError('这次原请求仍在核对，请先查看它的结果，不用重复发送。');return}
  if(pendingRef.current?.status==='failed')clearMistakeLearningPending(childId,mistakeId,pendingRef.current.body.client_action_id)
  const record:MistakeLearningPending={version:1,child_id:childId,mistake_id:mistakeId,source_revision:value.source_revision,operation,status:'pending',body:{child_id:childId,...(sessionId?{session_id:sessionId}:{}),client_action_id:crypto.randomUUID(),expected_revision:value.revision,...values}}
  void execute(record,operation==='coach')
 }
 const runCoach=(event:React.MouseEvent<HTMLButtonElement>,values:Partial<MistakeLearningMutation>):void=>{
  // A pointer click hands the next Space key to the workspace voice control.
  // A keyboard-activated button keeps its normal focus and keyboard behavior.
  if(event.detail>0)event.currentTarget.blur()
  run('coach',values)
 }
 const lastCoachingVoice=useRef('')
 const acceptCoachingVoice=useRef<(event:CustomEvent<MistakeCoachingVoice>)=>void>(()=>undefined)
 acceptCoachingVoice.current=event=>{
  const value=stateRef.current,focus=value?.focus
  if(!event.detail?.capture)return
  const {capture,voiceInputId}=event.detail
  if(event.defaultPrevented||!bookView||detailViewRef.current!=='coach'||!current()||document.hidden||!value||!focus||!sessionId
    ||capture.session_id!==sessionId||capture.child_id!==childId||capture.mistake_id!==mistakeId
    ||capture.learning_session_id!==value.learning_session_id||capture.source_revision!==value.source_revision
    ||capture.focus_generation!==focus.focus_generation)return
  const variant=value.coach_variant?.status==='ready'?value.coach_variant:null
  if(variant?capture.practice_item_id!==variant.practice_item_id:capture.practice_item_id!==null||!value.coach_turn_id)return
  event.preventDefault()
  if(lastCoachingVoice.current===voiceInputId)return
  lastCoachingVoice.current=voiceInputId
  const pending=pendingRef.current
  if(working.current||restore.error||value.status==='processing'||(pending&&pending.status!=='failed')){
   setError('刚才的话已保存。上一轮还在核对，请等结果出来再说。')
   return
  }
  const targets:Partial<MistakeLearningMutation>=variant?{practice_item_id:variant.practice_item_id}
   :{original_review:true,style:'socratic',coach_turn_id:value.coach_turn_id!}
  if(pending?.status==='failed')clearMistakeLearningPending(childId,mistakeId,pending.body.client_action_id)
  void execute({version:1,child_id:childId,mistake_id:mistakeId,source_revision:value.source_revision,
   operation:variant?'answer':'coach',status:'pending',body:{child_id:childId,session_id:sessionId,
    client_action_id:`mistake-voice:${voiceInputId}`,voice_input_id:voiceInputId,source_revision:value.source_revision,
    expected_revision:value.revision,...targets}},true)
 }
 useLayoutEffect(()=>{
  if(!active||!sessionId||!bookView||detailView!=='coach')return
  const receive=(event:Event):void=>acceptCoachingVoice.current(event as CustomEvent<MistakeCoachingVoice>)
  window.addEventListener(MISTAKE_COACHING_VOICE_EVENT,receive)
  return()=>window.removeEventListener(MISTAKE_COACHING_VOICE_EVENT,receive)
 },[active,sessionId,bookView,detailView])
 const checkPending=async(record:MistakeLearningPending):Promise<void>=>{
  if(!current()||working.current)return
  working.current=true;setBusy(true);const generation=++operationGeneration.current
  try{
   const result=await mistakeLearningApi.action(mistakeId,childId,record.waiting_for_action_id??record.body.client_action_id)
   if(!current()||generation!==operationGeneration.current)return
   if(result.source_revision!==stateRef.current?.source_revision){setError('旧题的原请求仍保留，结果不会用到当前题目。');return}
   apply(result)
   if(result.action_status==='completed'||result.action_status==='failed'){
    if(record.waiting_for_action_id||result.action_status==='completed'){clearMistakeLearningPending(childId,mistakeId,record.body.client_action_id);const rest=pendingMistakeLearningRequests(childId,mistakeId).find(row=>row.status!=='failed')??null;pendingRef.current=rest;setPending(rest);setError(record.waiting_for_action_id?'上一项结果已读到，写下的话仍保留，可以继续新的请求。':null)}
    else{const failed={...record,status:'failed' as const};saveMistakeLearningLocal('pending',failed);pendingRef.current=failed;setPending(failed);setError('这一步没有完成，可以重试刚才的请求。')}
   }else setError('原请求还在处理中，内容已经保存，不会重复调用。')
  }catch(cause){if(current()&&generation===operationGeneration.current){if(cause instanceof ApiError&&cause.status===404){const unsent={...record,not_found:true};saveMistakeLearningLocal('pending',unsent);pendingRef.current=unsent;setPending(unsent);setError('原请求暂时未送达，可以继续发送这份已保存的内容。')}else setError(userMessage(cause))}}
  finally{working.current=false;if(current())setBusy(false)}
 }
 const autoStarted=useRef(false)
 useEffect(()=>{
  if(!autoAnalyzeActionId||autoStarted.current||!state||state.analysis||!current()||loading||sessionId&&!state.focus||pendingRef.current)return
  autoStarted.current=true
  void execute({version:1,child_id:childId,mistake_id:mistakeId,source_revision:state.source_revision,operation:'analyze',status:'pending',body:{child_id:childId,...(sessionId?{session_id:sessionId}:{}),client_action_id:autoAnalyzeActionId,expected_revision:state.revision}})
 },[autoAnalyzeActionId,state?.source_revision,state?.focus?.focus_generation,loading])
 useLayoutEffect(()=>{
  if(!active||!state||loading||readReceipt!==readGeneration.current)return
  const mine=state,frame=requestAnimationFrame(()=>{
   if(!current()||stateRef.current!==mine||readReceipt!==readGeneration.current)return
   const box=root.current?.getBoundingClientRect()
   if(!box||box.width<=0||box.height<=0||box.bottom<=0||box.top>=window.innerHeight)return
   window.dispatchEvent(new CustomEvent(MISTAKE_LEARNING_RENDERED_EVENT,{detail:{owner_id:owner.current,child_id:childId,session_id:sessionId,mistake_id:mistakeId,learning_session_id:mine.learning_session_id,source_revision:mine.source_revision,revision:mine.revision,visible:true,view_rendered:true}}))
  });return()=>cancelAnimationFrame(frame)
 },[active,state?.revision,state?.source_revision,state?.focus?.focus_generation,readReceipt,loading])
 const previousDisplay=useRef<{item:string|null;analysis:string|null}>({item:null,analysis:null})
 useLayoutEffect(()=>{
  if(!active||!state)return
  const previous=previousDisplay.current
  if(item&&previous.item!==item.practice_item_id&&(!bookView||detailView==='practice'))question.current?.scrollIntoView?.({block:'start',behavior:'auto'})
  else if(!item&&state.analysis&&previous.analysis!==state.analysis.summary&&(!bookView||detailView==='analysis'))analysisPanel.current?.scrollIntoView?.({block:'start',behavior:'auto'})
  const thread=threadEnd.current?.parentElement;if(thread)thread.scrollTop=thread.scrollHeight
  previousDisplay.current={item:item?.practice_item_id??null,analysis:state.analysis?.summary??null}
 },[active,state?.analysis?.summary,state?.turns.length,item?.practice_item_id,bookView,detailView])
 const blocked=busy||!active||paused.current||loading||restore.error||Boolean(sessionId&&!state?.focus)
 if(!state)return <section className="cloud-mistake-learning" aria-label="错题学习">{loading?<p role="status">正在读取这道题的学习进度…</p>:<p role="alert">{error??'这道题暂时没有读到。'}</p>}<button className="secondary-button" onClick={()=>void load()} disabled={loading}>重试读取</button></section>
 const sourceChanged=!!draft&&draft.source_revision!==state.source_revision
 const feedbacks=state.practice_items.filter(row=>row.feedback)
 const answerText=!sourceChanged&&item&&draft?.answer?.practice_item_id===item.practice_item_id?(draft.answer?.text??''):''
 const variant=state.coach_variant
 const variantAnswerText=!sourceChanged&&variant&&draft?.coach_variant_answer?.practice_item_id===variant.practice_item_id?draft.coach_variant_answer.text:''
 const coachScope: Partial<MistakeLearningMutation> = bookView ? { original_review:true } : { practice_item_id:item?.practice_item_id }
 return <section ref={root} data-mistake-learning-owner={owner.current} data-mistake-learning-id={mistakeId} data-mistake-learning-session={sessionId??''} data-mistake-learning-panel={bookView?detailView:undefined} className="cloud-mistake-learning" aria-label="错题学习">
  <header className="cloud-learning-title"><div><span>{state.source.subject}{bookView&&reviewLabel(state)?` · ${reviewLabel(state)}`:''}</span><h2>{state.source.title||'一起弄懂这道题'}</h2></div><span className="cloud-learning-progress">今日已练 {state.today.completed_count} / {state.today.target_count}</span></header>
  {bookView?<>
   <nav className="cloud-book-detail-tabs" aria-label="错题详情">{bookViews.map(([key,label])=><button type="button" key={key} aria-pressed={detailView===key} onClick={()=>setDetailView(key)}>{label}</button>)}</nav>
   {detailView==='redo'&&<section className="cloud-original-review" data-mistake-review-id={active?mistakeId:undefined} data-mistake-review-title={active?state.source.title:undefined} data-mistake-review-revision={active?state.source_revision:undefined}><h3>先独立做一遍</h3><p ref={originalQuestion} data-mistake-learning-question="original">{state.source.question}</p>
     <OriginalReviewControls key={state.source_revision} record={state} active={active&&!paused.current} unavailable={blocked} onRequest={onOriginalReviewRequest} onResult={apply}/>
     <div className="cloud-learning-controls"><button type="button" className="secondary-button" disabled={blocked} onClick={()=>{setDetailView('analysis');if(!state.analysis)run('analyze')}}>{state.analysis?'重看分析':'分析这题'}</button></div>
    </section>}
   {detailView==='source'&&<div className="cloud-original-grid"><MistakeQuestionCrop record={state} active={active&&!paused.current}/><section><h3>题干文字</h3><p ref={originalQuestion} data-mistake-learning-question="original">{state.source.question}</p>{state.source.original_answer&&<details><summary>查看当时作答</summary><p>{state.source.original_answer}</p></details>}</section></div>}
   {detailView!=='redo'&&detailView!=='source'&&(detailView!=='practice'||!item)&&<div className="cloud-book-question-context"><span>原题</span><p ref={originalQuestion} data-mistake-learning-question="original">{state.source.question}</p></div>}
  </>:<div className="cloud-original-grid"><section><h3>原题</h3><p ref={originalQuestion} data-mistake-learning-question="original">{state.source.question}</p></section><section><h3>你当时的回答</h3><p>{state.source.original_answer||'还没有记录，可以直接告诉我。'}</p></section></div>}
  {state.error_code==='MODEL_MISTAKE_REASONING_CONFIG_REQUIRED'&&<p role="alert" className="cloud-learning-notice">学习模型还没准备好，请家长检查云端模型设置。题目和原话已保留。</p>}
  {state.error_code==='MISTAKE_LEARNING_CLOUD_LABEL_RIGHT_REQUIRED'&&<p role="alert" className="cloud-learning-notice">这道旧练习的云端使用授权已变化，题目和作答仍在本机。请重新生成练习。</p>}
  {error&&<div role="alert" className="cloud-learning-notice">{error}</div>}{storageError&&<p role="alert" className="cloud-learning-notice">{storageError}</p>}
  {sourceChanged&&<details className="cloud-learning-notice"><summary>题目已有更新，之前写下的话已保留，不会自动发给新题。</summary><p>{draft?.answer?.text}</p>{draft?.coach_variant_answer&&<p>{draft.coach_variant_answer.text}</p>}<p>{draft?.coach}</p></details>}
  {pending&&<div className="cloud-pending-actions"><p>{pending.status==='failed'?'刚才这一步没有完成。':'原请求已保留，正在等结果确认。'}</p>{pending.status==='failed'?(!pending.body.voice_input_id&&<button className="secondary-button" disabled={busy||pending.source_revision!==state.source_revision} onClick={()=>run(pending.operation,{practice_item_id:pending.body.practice_item_id,utterance:pending.body.utterance,style:pending.body.style,count:pending.body.count,original_review:pending.body.original_review})}>重试刚才这一步</button>):<button className="secondary-button" disabled={busy} onClick={()=>void (pending.not_found?execute(pending):checkPending(pending))}>{pending.not_found?'继续发送原请求':'核对原请求结果'}</button>}<button className="secondary-button" disabled={busy} onClick={()=>void load()}>刷新已保存进度</button></div>}
  {(!bookView||detailView==='analysis')&&<section ref={analysisPanel} className="cloud-analysis"><h3>这题的关键</h3><GraphStudyNotes state={state}/>{state.analysis?<><p>{state.analysis.summary}</p>{state.analysis.wrong_causes.map((cause,index)=><div className="cloud-cause" key={index}><strong>{cause.label}</strong><p>{cause.explanation}</p></div>)}<div className="cloud-knowledge">{state.analysis.knowledge_points.slice(0,3).map((point,index)=><section key={index}><h4>{point.label}</h4><p>{point.description}</p></section>)}</div>{state.analysis.correction_strategy.length>0&&<ol>{state.analysis.correction_strategy.map((step,index)=><li key={index}>{step}</li>)}</ol>}</>:<p>{state.analysis_review_required?(state.tutor_route_configured===false?'原分析已保留。请先在家长中心配置讲题模型，再重新分析。':'原分析已保留，当前讲题模型需要重新分析这道题。'):'先看看卡住的那一步，再用新题把它弄明白。'}</p>}<div className="cloud-learning-controls"><button className="primary-button" disabled={blocked} onClick={()=>void run('analyze')}>{state.analysis?'更新错因分析':'分析这题'}</button>{!bookView&&<><button className="secondary-button" disabled={blocked} onClick={()=>run('practice',{count:2})}>练两题</button><button className="secondary-button" disabled={blocked} onClick={()=>run('practice',{count:3})}>练三题</button></>}</div></section>}
  {state.today.status==='needs_explanation'&&<p className="cloud-learning-notice" role="status">先不急着加题，把卡住的一步说清楚，再继续。</p>}
  {state.graph_practice_stale&&(!bookView||detailView==='practice')&&<p className="cloud-learning-notice">知识点依据已变化或尚未核对，旧练习和反馈已保留。请重新生成本轮练习。</p>}
  {bookView&&detailView==='practice'&&!item&&<section className="cloud-practice"><h3>换一种问法</h3><p>请小知出两三道同类题，再独立做一遍。</p><div className="cloud-learning-controls"><button className="primary-button" disabled={blocked} onClick={()=>run('practice',{count:2})}>练两题</button><button className="secondary-button" disabled={blocked} onClick={()=>run('practice',{count:3})}>练三题</button></div></section>}
  {item&&(!bookView||detailView==='practice')&&<section className="cloud-practice"><div className="cloud-section-heading"><h3>{item.graph_target?.kind==='prerequisite'?'先试一道前置检查题':'试一道类似的题'}</h3><span>第 {item.position} 题</span></div>{item.graph_target&&<p>这题练习「{item.graph_target.title}」</p>}<p ref={question} data-practice-item-id={item.practice_item_id} className="cloud-practice-question" data-question-viewport="cloud-mistake">{item.question}</p><label>你的想法<textarea maxLength={4000} disabled={!active||paused.current||restore.error} value={answerText} onChange={event=>updateText('answer',event.target.value)} placeholder="直接说出你的想法，也可以写在这里。"/></label><button className="primary-button" disabled={blocked||!answerText.trim()} onClick={()=>run('answer',{practice_item_id:item.practice_item_id,utterance:answerText})}>保存回答，看看这一步</button></section>}
  {feedbacks.length>0&&(!bookView||detailView==='practice')&&<section className="cloud-feedback"><h3>刚才的练习</h3>{feedbacks.slice(-3).map(row=><article key={row.practice_item_id}><strong>第 {row.position} 题</strong><p>{row.feedback!.feedback}</p><p>{row.feedback!.explanation}</p>{row.feedback!.next_hint&&<p>{row.feedback!.next_hint}</p>}</article>)}</section>}
  {(!bookView||detailView==='coach')&&<section className="cloud-coach"><h3>还想弄明白哪里？</h3>
    {bookView&&state.original_review?.last_result?.judgement==='incorrect'&&<OriginalReviewOutcome result={state.original_review.last_result} wrongCount={state.original_review.wrong_count} showHint={!state.turns.some(turn=>turn.role==='assistant'&&turn.text===state.original_review?.last_result?.next_hint)}/>}
    <div className="cloud-learning-controls">
      <button className="secondary-button" disabled={blocked} onClick={event=>runCoach(event,{...coachScope,style:'socratic',utterance:'请给我一点提示，先不要直接告诉答案。'})}>提示一点</button>
      <button className="secondary-button" disabled={blocked} onClick={event=>runCoach(event,{...coachScope,style:'socratic',utterance:'请带我一步一步弄懂这道题。'})}>讲解这题</button>
      {bookView&&<button className="secondary-button" onClick={()=>setDetailView('redo')}>回到原题重做</button>}
    </div>
    <div className="cloud-learning-thread" aria-label="这道题的对话">{state.turns.map(turn=><article className={turn.role==='user'?'is-user':'is-assistant'} key={turn.turn_id}><span>{turn.role==='user'?'你':'学习伙伴'}</span><p>{turn.text}</p></article>)}<div ref={threadEnd}/></div>
    <label>继续问一问<textarea maxLength={4000} disabled={!active||paused.current||restore.error} value={sourceChanged?'':draft?.coach??''} onChange={event=>updateText('coach',event.target.value)} placeholder="哪里还没想通？可以直接对我说。"/></label>
    <button className="primary-button" disabled={blocked||sourceChanged||!(draft?.coach??'').trim()} onClick={event=>runCoach(event,{...coachScope,style:'socratic',utterance:draft!.coach})}>发送这句话</button>
    {variant&&<section className="cloud-practice" aria-label="独立变式题">
      <h3>独立试一道变式题</h3>
      <p ref={variantQuestion} data-practice-item-id={variant.practice_item_id} className="cloud-practice-question">{variant.question}</p>
      {variant.status==='ready'&&<>
        <label>你的变式题答案<textarea maxLength={4000} disabled={!active||paused.current||restore.error} value={variantAnswerText}
          onChange={event=>updateText('coach_variant_answer',event.target.value)} placeholder="可以说出你的做法，也可以写在这里。"/></label>
        <button className="primary-button" disabled={blocked||!variantAnswerText.trim()}
          onClick={()=>run('answer',{practice_item_id:variant.practice_item_id,utterance:variantAnswerText})}>提交变式题答案</button>
      </>}
      {variant.feedback&&<div className="cloud-feedback"><p>{variant.feedback.feedback}</p><p>{variant.feedback.explanation}</p></div>}
    </section>}
  </section>}
  <footer className="cloud-review-summary">{state.today.status==='done'?<p>今天这份练习已经保存。</p>:<p>今天还安排 {state.today.remaining_count} 道，按自己的节奏来。</p>}{approvedReviewFocus(state)&&state.graph_review_focus&&<p>{state.graph_review_focus.state==='checked'?'已独立检查':'复习重点'}「{state.graph_review_focus.title}」</p>}{state.next_review_at&&<p>下次复习：{new Date(state.next_review_at).toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'})}</p>}{busy&&<p role="status">正在保存并准备下一步…</p>}</footer>
 </section>
}

type IntakeProps={childId:string;sessionId?:string;active:boolean;onSaved:(mistakeId:string,analysisActionId:string)=>void}
export function CloudMistakeIntake({childId,sessionId,active,onSaved}:IntakeProps):React.JSX.Element{
 const fresh=():MistakeLearningIntakeLocal=>({version:1,child_id:childId,question:'',submitted_answer:'',client_action_id:crypto.randomUUID(),analysis_action_id:crypto.randomUUID(),status:'draft',...(sessionId?{session_id:sessionId}:{})})
 const [initial]=useState(()=>{try{return {value:readMistakeLearningIntake(childId)??fresh(),error:false}}catch{return {value:fresh(),error:true}}})
 const [value,setValue]=useState(initial.value),[busy,setBusy]=useState(false),[error,setError]=useState<string|null>(initial.error?'上次写下的题目暂时没有读到，请先保留本页；没有覆盖原内容。':null)
 const mounted=useRef(false),activeRef=useRef(active),sending=useRef(false),epoch=useRef(0),paused=useRef(false);activeRef.current=active
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const current=():boolean=>mounted.current&&activeRef.current&&!paused.current
 useEffect(()=>{const pause=()=>{paused.current=true;epoch.current++;setBusy(false)};const clear=(event:Event)=>{if((event as CustomEvent<{childId:string}>).detail?.childId!==childId)return;epoch.current++;setValue(fresh());setBusy(false);setError(null)};window.addEventListener('hsc-privacy-pause',pause);window.addEventListener('hsc-cloud-mistake-cleared',clear);return()=>{window.removeEventListener('hsc-privacy-pause',pause);window.removeEventListener('hsc-cloud-mistake-cleared',clear)}},[childId])
 const save=(next:MistakeLearningIntakeLocal):boolean=>{try{saveMistakeLearningIntake(next);setValue(next);setError(null);return true}catch{setValue(next);setError('这份题目还没有安全暂存，请先保留本页再重试。');return false}}
 const send=async():Promise<void>=>{
  if(!current()||sending.current||initial.error||!value.question.trim())return
  const record={...value,status:'unknown' as const};if(!save(record))return
  sending.current=true;setBusy(true);const mine=epoch.current
  try{
   const result=await mistakeLearningApi.intake(childId,{question:record.question,submitted_answer:record.submitted_answer,client_action_id:record.client_action_id,...(record.session_id?{session_id:record.session_id}:{})})
   if(!current()||mine!==epoch.current)return
   const saved={...record,status:'saved' as const,mistake_id:result.mistake_id};if(!save(saved))return
   onSaved(result.mistake_id,record.analysis_action_id)
  }catch(cause){if(current()&&mine===epoch.current)setError(userMessage(cause)+' 题目原文已暂存，重试只核对这一次保存。')}
  finally{sending.current=false;if(current())setBusy(false)}
 }
 const editable=!initial.error&&value.status==='draft'&&!busy&&active&&!paused.current
 return <section className="cloud-mistake-intake" aria-label="添加一道题"><h2>从这道题开始</h2><p>把卡住的题目告诉我，原来的答案记得多少就写多少。不用先选错因。</p>{error&&<p role="alert" className="cloud-learning-notice">{error}</p>}
  <label>题目<textarea maxLength={12000} value={value.question} disabled={!editable} onChange={event=>save({...value,question:event.target.value})} placeholder="可以直接对学习伙伴说，也可以把题目写在这里。"/></label>
  <label>你当时的回答（可不填）<textarea maxLength={8000} value={value.submitted_answer} disabled={!editable} onChange={event=>save({...value,submitted_answer:event.target.value})}/></label>
  <div className="cloud-learning-controls">{value.status==='saved'&&value.mistake_id?<><button className="primary-button" disabled={!active||paused.current||busy} onClick={()=>onSaved(value.mistake_id!,value.analysis_action_id)}>继续学习这道题</button><button className="secondary-button" disabled={!active||paused.current||busy} onClick={()=>save(fresh())}>再记一道题</button></>:<button className="primary-button" disabled={!active||paused.current||busy||initial.error||!value.question.trim()} onClick={()=>void send()}>{busy?'正在保存题目…':value.status==='unknown'?'核对原保存请求':'保存题目，开始分析'}</button>}</div>
 </section>
}
