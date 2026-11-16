export type MistakeLearningDraftExportEntry = {key:string;value:string}
const MAX_ENTRIES=4096
const MAX_BYTES=8*1024*1024
const unreadable=()=>new Error('错题草稿未能完整读取，未生成导出文件。请保留当前客户端后重试。')
class DraftExportCapacityError extends Error{}
const tooLarge=()=>new DraftExportCapacityError('错题草稿超出导出容量（8 MiB 或 4096 条），未生成可能缺失内容的导出文件。')

/** Preserve exact local labor strings; never parse or silently discard a record. */
export function collectMistakeLearningDrafts(childId:string):MistakeLearningDraftExportEntry[]{
 if(typeof childId!=='string'||!childId||childId.length>160)throw unreadable()
 let child:string
 try{child=encodeURIComponent(childId)}catch{throw unreadable()}
 const draft=`hsc-cloud-mistake-draft-v1:${child}:`,pending=`hsc-cloud-mistake-pending-v1:${child}:`,intake=`hsc-cloud-mistake-intake-v1:${child}`
 let storage:Storage
 try{storage=localStorage}catch{throw unreadable()}
 const keys=():string[]=>{
  const selected:string[]=[]
  try{
   const length=storage.length
   if(!Number.isSafeInteger(length)||length<0)throw unreadable()
   for(let i=0;i<length;i++){
    const key=storage.key(i)
    if(typeof key!=='string')throw unreadable()
    if(key.startsWith(draft)||key.startsWith(pending)||key===intake){selected.push(key);if(selected.length>MAX_ENTRIES)throw tooLarge()}
   }
  }catch(cause){if(cause instanceof DraftExportCapacityError)throw cause;throw unreadable()}
  selected.sort((a,b)=>a.localeCompare(b))
  if(new Set(selected).size!==selected.length)throw unreadable()
  return selected
 }
 const snapshot=keys(),entries:MistakeLearningDraftExportEntry[]=[],encoder=new TextEncoder()
 let bytes=2 // Array brackets; each entry is measured as its exact JSON transport.
 for(const key of snapshot){
  let value:string|null
  try{value=storage.getItem(key)}catch{throw unreadable()}
  if(typeof value!=='string')throw unreadable()
  const entry={key,value};bytes+=encoder.encode(JSON.stringify(entry)).byteLength+(entries.length?1:0)
  if(bytes>MAX_BYTES)throw tooLarge()
  entries.push(entry)
 }
 if(JSON.stringify(keys())!==JSON.stringify(snapshot))throw unreadable()
 return entries
}
