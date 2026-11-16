/** Synchronous navigation veto. No destination, draft text or authority is cached. */
const eventName='hsc:parent-draft-before-navigation'

export function requestParentDraftNavigation():boolean {
  return window.dispatchEvent(new Event(eventName,{cancelable:true}))
}

export function guardParentDraftNavigation(canLeave:()=>boolean,onBlocked:()=>void):()=>void {
  const check=(event:Event):void=>{
    if(!canLeave()){event.preventDefault();onBlocked()}
  }
  window.addEventListener(eventName,check)
  return()=>window.removeEventListener(eventName,check)
}
