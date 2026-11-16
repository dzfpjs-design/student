import { ParentTextbookDraftJournal,type TextbookEditingPayload } from './parentTextbookDraftJournal'
import { parentDraftAuthorityEvent,parentDraftAuthorityVersion } from './parentDraftAuthority'
import type { ParentDraftRecoveryFactory } from './parentDraftRecoveryPort'

type Api=(path:string,init?:RequestInit)=>Promise<unknown>
type Entry={journal:ParentTextbookDraftJournal;capture:(()=>TextbookEditingPayload|null)|null}
/** App-lifetime private memory only. No DOM, token, localStorage, disk or background retry. */
export class ParentTextbookDraftHost {
  constructor(private recoveryFactory:ParentDraftRecoveryFactory=()=>undefined){}
  private entries=new Map<string,Entry>()
  private listeners=0
  private revoke=():void=>this.suspendAll()
  private beforeUnload=(event:BeforeUnloadEvent):void=>{
    if([...this.entries.values()].some(({journal})=>journal.hasUnconfirmedEditing(journal.state.payload))){event.preventDefault();event.returnValue=''}
  }
  attach():()=>void {
    if(this.listeners++===0){window.addEventListener(parentDraftAuthorityEvent,this.revoke);window.addEventListener('hsc-runtime-restarted',this.revoke);window.addEventListener('beforeunload',this.beforeUnload)}
    return()=>{if(--this.listeners===0){this.suspendAll();window.removeEventListener(parentDraftAuthorityEvent,this.revoke);window.removeEventListener('hsc-runtime-restarted',this.revoke);window.removeEventListener('beforeunload',this.beforeUnload)}}
  }
  entry(api:Api,childId:string,scope:'capture'|'language'):Entry {
    const key=JSON.stringify([childId,scope]);let entry=this.entries.get(key)
    if(!entry){entry={journal:new ParentTextbookDraftJournal(api,childId,scope,parentDraftAuthorityVersion,this.recoveryFactory(childId,scope)),capture:null};this.entries.set(key,entry)}
    return entry
  }
  suspendAll():void {
    for(const entry of this.entries.values()){
      if(entry.capture)entry.journal.keepEditing(entry.capture())
    }
    // Native revoke invalidates ALL scopes: finish every provided snapshot first.
    for(const entry of this.entries.values())entry.journal.suspend(false)
    for(const entry of this.entries.values())entry.journal.revokeRecovery()
  }
}
