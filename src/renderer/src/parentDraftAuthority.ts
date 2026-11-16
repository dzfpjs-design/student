/** No credentials or draft bytes. Every unlock/lock/service change revokes old editor I/O. */
export const parentDraftAuthorityEvent='hsc:parent-draft-authority-changed'
let revision=0
export const parentDraftAuthorityVersion=():number=>revision
export function changeParentDraftAuthority(reason:'unlocked'|'locked'|'expired'|'runtime'|'rotation_complete', source?: symbol):void {
  revision++
  window.dispatchEvent(new CustomEvent(parentDraftAuthorityEvent,{detail:{reason,source}}))
}
