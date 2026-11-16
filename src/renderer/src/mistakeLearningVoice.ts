import type {MistakeLearningLibrary,MistakeLearningState,MistakeLearningSurface,MistakeLearningRendered} from './mistakeLearningTypes'

export type MistakeLearningCapture={version:1;child_id:string;session_id:string;mistake_id:string;
 learning_session_id:string;source_revision:string;focus_generation:number;practice_item_id:string|null;question_visible:true}
export const MISTAKE_COACHING_VOICE_EVENT='hsc-mistake-coaching-voice'
export type MistakeCoachingVoice={capture:MistakeLearningCapture;voiceInputId:string}
type SurfaceEvent={owner_id:string;child_id:string;session_id?:string;mistake_id:string;surface:MistakeLearningSurface|null}
const surfaces=new Map<string,SurfaceEvent>()
const key=(sid:string,cid:string)=>JSON.stringify([sid,cid])
const visible=(node:Element|null):node is Element=>{
 if(!node||!node.isConnected||node.closest('[hidden],[aria-hidden="true"]'))return false
 const box=node.getBoundingClientRect();return box.width>0&&box.height>0
}
if(typeof window!=='undefined')window.addEventListener('hsc-mistake-learning-surface',event=>{
 const detail=(event as CustomEvent<SurfaceEvent>).detail
 if(!detail||typeof detail.owner_id!=='string'||typeof detail.child_id!=='string'||!detail.session_id)return
 const k=key(detail.session_id,detail.child_id)
 if(!detail.surface){if(surfaces.get(k)?.owner_id===detail.owner_id)surfaces.delete(k);return}
 if(detail.surface.child_id!==detail.child_id||detail.surface.session_id!==detail.session_id||detail.surface.mistake_id!==detail.mistake_id)return
 surfaces.set(k,structuredClone(detail))
})

/** Called at first PCM/onset, never again at ASR completion to choose a newer item. */
export function mistakeLearningOnset(sessionId:string,childId:string):MistakeLearningCapture|null{
 const entry=surfaces.get(key(sessionId,childId)),s=entry?.surface
 if(!entry||!s||s.question_visible!==true||!Number.isSafeInteger(s.focus_generation)||Number(s.focus_generation)<1
   ||!s.learning_session_id||!/^[a-f0-9]{64}$/.test(s.source_revision))return null
 const root=[...document.querySelectorAll('[data-mistake-learning-owner]')].find(node=>
  node.getAttribute('data-mistake-learning-owner')===entry.owner_id&&node.getAttribute('data-mistake-learning-id')===s.mistake_id
  &&node.getAttribute('data-mistake-learning-session')===sessionId)
 if(!visible(root??null))return null
 if(s.practice_item_id){
  const question=[...root!.querySelectorAll('[data-practice-item-id]')].find(node=>node.getAttribute('data-practice-item-id')===s.practice_item_id)
  if(!visible(question??null)||s.focus.practice_item_id!==s.practice_item_id&&s.daily_practice_item_id!==s.practice_item_id)return null
 }else if(!visible(root!.querySelector('[data-mistake-learning-question="original"]'))){
  return null
 }
 return structuredClone({version:1,child_id:childId,session_id:sessionId,mistake_id:s.mistake_id,
  learning_session_id:s.learning_session_id,source_revision:s.source_revision,
  focus_generation:s.focus_generation as number,practice_item_id:s.practice_item_id,question_visible:true})
}

/** Offers the already saved input only to the same currently displayed lesson. */
export function claimMistakeCoachingVoice(capture:MistakeLearningCapture|undefined,voiceInputId:string,transcript:string):boolean{
 if(!capture||!transcript.trim()||!/^[A-Za-z0-9_-]{1,160}$/.test(voiceInputId))return false
 const live=mistakeLearningOnset(capture.session_id,capture.child_id)
 if(!live||live.mistake_id!==capture.mistake_id||live.learning_session_id!==capture.learning_session_id
  ||live.source_revision!==capture.source_revision||live.focus_generation!==capture.focus_generation
  ||live.practice_item_id!==capture.practice_item_id)return false
 return !window.dispatchEvent(new CustomEvent<MistakeCoachingVoice>(MISTAKE_COACHING_VOICE_EVENT,
  {detail:{capture,voiceInputId},cancelable:true}))
}

/** Subscribe before refresh: a DOM event alone is not a completion receipt. */
export function renderMistakeLearningResult(options:{childId:string;sessionId:string;state?:MistakeLearningState;
 library?:MistakeLearningLibrary;open:()=>Promise<unknown>;current:()=>boolean;signal:AbortSignal}):Promise<void>{
 const {state}=options
 const operation=state?.operation
 const lastTurn=state?.turns.at(-1)
 const answeredCoachVariant=operation==='answer'&&state?.coach_variant&&lastTurn?.kind==='feedback'
  &&lastTurn.role==='assistant'&&lastTurn.practice_item_id===state.coach_variant.practice_item_id
 const panel=operation==='analyze'?'analysis':operation==='coach'||answeredCoachVariant?'coach':operation==='practice'||operation==='answer'?'practice':undefined
 return new Promise((resolve,reject)=>{
  let settled=false
  const finish=(error?:Error)=>{if(settled)return;settled=true;clearTimeout(timer);window.removeEventListener('hsc-mistake-learning-rendered',receive);options.signal.removeEventListener('abort',abort);error?reject(error):resolve()}
  const abort=()=>finish(Error('MISTAKE_LEARNING_VIEW_CHANGED'))
  const receive=(event:Event)=>{
   const d=(event as CustomEvent<MistakeLearningRendered>).detail,s=options.state
   if(!options.current()){abort();return}
   if(!d||d.view_rendered!==true||d.visible!==true||d.child_id!==options.childId||d.session_id!==options.sessionId)return
   if(s&&(d.kind==='library'||d.mistake_id!==s.mistake_id||d.learning_session_id!==s.learning_session_id
    ||d.source_revision!==s.source_revision||d.revision!==s.revision))return
   if(!s&&(d.kind!=='library'||d.view!=='today'))return
   const root=s?[...document.querySelectorAll('[data-mistake-learning-owner]')].find(n=>n.getAttribute('data-mistake-learning-owner')===d.owner_id&&n.getAttribute('data-mistake-learning-id')===s.mistake_id):document.querySelector('.cloud-book-grid, .cloud-book-empty')
   if(!visible(root??null)||panel&&root?.getAttribute('data-mistake-learning-panel')!==panel)return
   finish()
  }
  const timer=setTimeout(()=>finish(Error('MISTAKE_LEARNING_VIEW_UNCONFIRMED')),20000)
  window.addEventListener('hsc-mistake-learning-rendered',receive);options.signal.addEventListener('abort',abort,{once:true})
  if(options.signal.aborted||!options.current()){abort();return}
  void options.open().then(()=>{
   if(!options.current()){abort();return}
   window.dispatchEvent(new CustomEvent('hsc-mistake-learning-refresh',{detail:{child_id:options.childId,session_id:options.sessionId,
    ...(options.state?{mistake_id:options.state.mistake_id,panel}:{view:'today'})}}))
  }).catch(()=>finish(Error('MISTAKE_LEARNING_VIEW_UNAVAILABLE')))
 })
}
