import { ApiError } from './api'

export type ControlledSelectionKind = 'video' | 'game'
export type ControlledSelectionBinding = { resource_id:string; source_version:string }
export type ControlledSelectionChoice = ControlledSelectionBinding & { title:string }
export type ControlledSelectionOffer = {
  version:1; kind:ControlledSelectionKind; child_id:string; session_id:string;
  status:'unmatched'|'ambiguous'; choices:ControlledSelectionChoice[];
  unavailable_count?:number;
  unselectable_title_count?:number;
  local_only:true; cloud_transmission_allowed:false
}
export type PendingControlledSelection = {
  version:1; offer:ControlledSelectionOffer; rootTurnId:string;
  request?:{query:string; clientActionId:string; binding:ControlledSelectionBinding};
  cancelled?:true;
  local_only:true; cloud_transmission_allowed:false
}

const prefix='hsc:controlled-selection:v1:'
const id=(v:unknown):v is string=>typeof v==='string'&&v.length>0&&v.length<=128
const object=(v:unknown):v is Record<string,unknown>=>Boolean(v)&&typeof v==='object'&&!Array.isArray(v)
function key(sessionId:string,childId:string):string { return prefix+JSON.stringify([childId,sessionId]) }
function canonicalName(value:string):string { return value.normalize('NFKC').toLocaleLowerCase().replace(/[\s\p{P}]/gu,'') }
export type ControlledSelectionSnapshot={key:string;raw:string|null}
export function captureControlledSelection(sessionId:string,childId:string):ControlledSelectionSnapshot {
  const storageKey=key(sessionId,childId)
  return {key:storageKey,raw:localStorage.getItem(storageKey)}
}
export function isControlledSelectionCurrent(snapshot:ControlledSelectionSnapshot):boolean {
  return localStorage.getItem(snapshot.key)===snapshot.raw
}
export function removeControlledSelection(snapshot:ControlledSelectionSnapshot):boolean {
  if(!isControlledSelectionCurrent(snapshot))return false
  localStorage.removeItem(snapshot.key)
  if(localStorage.getItem(snapshot.key)!==null)throw new Error('CONTROLLED_SELECTION_NOT_REMOVED')
  return true
}
export function selectionBinding(v:unknown):v is ControlledSelectionBinding {
  return object(v)&&id(v.resource_id)&&typeof v.source_version==='string'&&/^[a-f0-9]{64}$/u.test(v.source_version)
}
export function matchesControlledSelectionReceipt(value:unknown,binding:ControlledSelectionBinding,kind:ControlledSelectionKind):boolean {
  if(!object(value)||!selectionBinding(value.selection_binding))return false
  return value.selection_binding.resource_id===binding.resource_id&&value.selection_binding.source_version===binding.source_version
    &&String(value[kind==='video'?'curriculum_row_id':'approval_id'])===binding.resource_id
}
export function selectionOffer(v:unknown,sessionId:string,childId:string):v is ControlledSelectionOffer {
  return object(v)&&v.version===1&&['video','game'].includes(String(v.kind))
    &&v.session_id===sessionId&&v.child_id===childId&&v.local_only===true&&v.cloud_transmission_allowed===false
    &&['unmatched','ambiguous'].includes(String(v.status))&&Array.isArray(v.choices)&&v.choices.length>0&&v.choices.length<=100
    &&(v.unavailable_count===undefined||Number.isInteger(v.unavailable_count)&&Number(v.unavailable_count)>=0)
    &&(v.unselectable_title_count===undefined||Number.isInteger(v.unselectable_title_count)&&Number(v.unselectable_title_count)>=0)
    &&v.choices.every(c=>object(c)&&typeof c.title==='string'&&canonicalName(c.title).length>0&&Array.from(c.title).length<=200&&selectionBinding(c))
    &&new Set(v.choices.map(c=>(c as ControlledSelectionChoice).resource_id)).size===v.choices.length
}
export function offerFromSelectionError(error:unknown,kind:ControlledSelectionKind,sessionId:string,childId:string):ControlledSelectionOffer|null {
  if(!(error instanceof ApiError)||error.status!==409
    ||error.code!==(kind==='video'?'CONTROLLED_CONTENT_SELECTION_REQUIRED':'CONTROLLED_GAME_SELECTION_REQUIRED'))return null
  const offer=error.details?.selection
  if(!selectionOffer(offer,sessionId,childId)||offer.kind!==kind)throw new Error('CONTROLLED_SELECTION_OWNER_OR_SOURCE_INVALID')
  return offer
}
export function selectionPrompt(offer:ControlledSelectionOffer):string {
  const names=offer.choices.slice(0,5).map(c=>'“'+c.title+'”').join('、')
  return (offer.status==='unmatched'?'没有找到符合刚才主题的内容。':'有几项符合，还需要你选一下。')
    +(offer.unavailable_count?'有些原文件暂时无法核对，未列入选择。':'')
    +(offer.unselectable_title_count?'有些内容的名称过长，暂时不能选择，请家长缩短名称后再试。':'')
    +`已许可的有${names}。想选哪一个？也可以说先不选。`
}
export function saveControlledSelection(value:PendingControlledSelection,expected?:ControlledSelectionSnapshot):ControlledSelectionSnapshot {
  const {session_id:sid,child_id:child}=value.offer
  if(!selectionOffer(value.offer,sid,child)||!id(value.rootTurnId))throw new Error('CONTROLLED_SELECTION_INVALID')
  const raw=JSON.stringify(value),storageKey=key(sid,child)
  if(expected&&(expected.key!==storageKey||!isControlledSelectionCurrent(expected)))throw new Error('CONTROLLED_SELECTION_REPLACED')
  localStorage.setItem(storageKey,raw)
  if(localStorage.getItem(storageKey)!==raw)throw new Error('CONTROLLED_SELECTION_NOT_SAVED')
  return {key:storageKey,raw}
}
export function readControlledSelection(sessionId:string,childId:string):PendingControlledSelection|null {
  const raw=localStorage.getItem(key(sessionId,childId))
  if(raw===null)return null
  const v:unknown=JSON.parse(raw)
  if(!object(v)||v.version!==1||v.local_only!==true||v.cloud_transmission_allowed!==false
    ||!id(v.rootTurnId)||!selectionOffer(v.offer,sessionId,childId))throw new Error('CONTROLLED_SELECTION_INVALID')
  if(v.cancelled!==undefined&&v.cancelled!==true)throw new Error('CONTROLLED_SELECTION_INVALID')
  if(v.request!==undefined&&(!object(v.request)||!id(v.request.clientActionId)||typeof v.request.query!=='string'
    ||!selectionBinding(v.request.binding)||!v.offer.choices.some(c=>object(v.request)
      &&object(v.request.binding)&&c.resource_id===v.request.binding.resource_id&&c.source_version===v.request.binding.source_version
      &&c.title===v.request.query)))throw new Error('CONTROLLED_SELECTION_REQUEST_INVALID')
  return v as unknown as PendingControlledSelection
}
export function clearControlledSelections(childId?:string,sessionId?:string):void {
  for(let i=localStorage.length-1;i>=0;i--){
    const stored=localStorage.key(i)
    if(!stored?.startsWith(prefix))continue
    try{const scope:unknown=JSON.parse(stored.slice(prefix.length))
      if(Array.isArray(scope)&&(!childId||scope[0]===childId)&&(!sessionId||scope[1]===sessionId))localStorage.removeItem(stored)
    }catch{if(!childId&&!sessionId)localStorage.removeItem(stored)}
  }
}

export type SelectionMeaning={type:'choose';choice:ControlledSelectionChoice}|{type:'cancel'|'clarify'|'ordinal'|'retry'|'other'}
export function resolveControlledSelection(text:string,offer:ControlledSelectionOffer):SelectionMeaning {
  const normalized=canonicalName(text)
  if(/^(先)?(不选了?|不看了?|不玩了?|算了|取消(选择)?|别选了?|停下)$/u.test(normalized))return {type:'cancel'}
  if(/^(重试|再试一次|重试打开|核对上次打开|查看上次打开记录)$/u.test(normalized))return {type:'retry'}
  // No audio-onset offer identity is available here. Even a clear ordinal
  // only requests a local readback, never indexes the current offer to execute.
  if(/第[一二两三四五六七八九十百零\d]+(?:个|项|部|款)?/u.test(normalized))return {type:'ordinal'}
  const found=offer.choices.filter(c=>normalized.includes(canonicalName(c.title)))
  if(!found.length)return {type:'other'}
  if(found.length!==1||/[“”「」『』"‘’]/u.test(text)||/(他说|她说|说过|提到|不要|别|不想|先不|不是|不好|不喜欢|是什么|为什么)/u.test(normalized))return {type:'clarify'}
  const name=canonicalName(found[0].title)
  const rest=normalized.replace(name,'')
  // Only an explicit naming/selection request is executable locally. Unknown
  // wording containing a private title stays local and asks for clarification.
  if(/^(那|那么|我|我们|就|请|帮我|麻烦你|想|要|选|选择|看|看看|播放|打开|玩|先|来|这个|那个|吧|好了|可以吗|好吗|一下)*$/u.test(rest))return {type:'choose',choice:found[0]}
  return {type:'clarify'}
}
