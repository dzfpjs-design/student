/** Bounded, content-free observations. Supplier IDs exist only as internal map keys. */
export type NativeResponseCancelSource = 'speech_onset' | 'confirmed_input' | 'response_binding' | 'other_local'
export type NativeResponseSummary={response_status:'completed'|'cancelled'|'failed'|'incomplete';response_reason:'none'|'max_output_tokens'|'content_filter'|'client_cancelled'|'turn_detected'|'other';response_elapsed_ms:number;response_text_chars:number;response_text_kind:'empty'|'whitespace'|'punctuation'|'substantive';audio_chunk_count:number;pcm_chunk_count:number;first_pcm_ms?:number;pcm_scheduled:boolean;new_input_before_pcm:boolean;terminal_current:boolean;has_tool_call:boolean;response_error_present:boolean;physical_audibility_verified:false;cancel_source?:NativeResponseCancelSource}
type Observation={started:number;textChars:number;textKind:NativeResponseSummary['response_text_kind'];audioChunks:number;pcmChunks:number;firstPcm?:number;inputBeforePcm:boolean;hasTool:boolean;cancelSource?:NativeResponseCancelSource}
const kind=(text:string):Observation['textKind']=>text.length===0?'empty':text.trim().length===0?'whitespace':/^[\p{P}\p{S}\s]+$/u.test(text)?'punctuation':'substantive'
const elapsed=(start:number,now:number):number=>Math.max(0,Math.min(86400000,Math.round(now-start)))
const object=(v:unknown):Record<string,unknown>|undefined=>v!==null&&typeof v==='object'&&!Array.isArray(v)?v as Record<string,unknown>:undefined
export class NativeResponseDiagnostics{
 private readonly responses=new Map<string,Observation>()
 constructor(private readonly now:()=>number=()=>performance.now()){}
 cancelRequested(id:string,source:NativeResponseCancelSource):void{
  const row=this.responses.get(id)
  if(row)row.cancelSource=source
 }
 inputStarted(id:string):void{const row=this.responses.get(id);if(row&&row.firstPcm===undefined)row.inputBeforePcm=true}
 scheduledPcm(id:string):void{const row=this.responses.get(id);if(!row)return;row.pcmChunks=Math.min(1000000,row.pcmChunks+1);if(row.firstPcm===undefined)row.firstPcm=elapsed(row.started,this.now())}
 clear():void{this.responses.clear()}
 observe(id:string,event:{type?:string;delta?:unknown;text?:unknown;transcript?:unknown;response?:unknown},current:boolean):NativeResponseSummary|undefined{
  if(event.type==='response.created'){
   if(this.responses.has(id))return
   if(this.responses.size>=16)this.responses.delete(this.responses.keys().next().value!)
   this.responses.set(id,{started:this.now(),textChars:0,textKind:'empty',audioChunks:0,pcmChunks:0,inputBeforePcm:false,hasTool:false});return
  }
  const row=this.responses.get(id);if(!row)return
  if(event.type==='response.audio.delta'&&typeof event.delta==='string'){row.audioChunks=Math.min(1000000,row.audioChunks+1);return}
  if(event.type==='response.function_call_arguments.done'){row.hasTool=true;return}
  if((event.type==='response.audio_transcript.delta'||event.type==='response.text.delta')&&typeof event.delta==='string'){
   row.textChars=Math.min(1000000,row.textChars+event.delta.length)
   const next=kind(event.delta),rank={empty:0,whitespace:1,punctuation:2,substantive:3};if(rank[next]>rank[row.textKind])row.textKind=next;return
  }
  if(event.type==='response.audio_transcript.done'||event.type==='response.text.done'){
   const text=typeof event.transcript==='string'?event.transcript:typeof event.text==='string'?event.text:undefined
   if(text!==undefined){row.textChars=Math.min(1000000,text.length);row.textKind=kind(text)}return
  }
  if(event.type!=='response.done')return
  const response=object(event.response),status=response?.status
  if(status!=='completed'&&status!=='cancelled'&&status!=='failed'&&status!=='incomplete')return
  this.responses.delete(id)
  const details=object(response?.status_details),rawReason=details?.reason
  const reason:NativeResponseSummary['response_reason']=rawReason===undefined||rawReason===null?'none':['max_output_tokens','content_filter','client_cancelled','turn_detected'].includes(rawReason as string)?rawReason as NativeResponseSummary['response_reason']:'other'
  return{response_status:status,response_reason:reason,response_elapsed_ms:elapsed(row.started,this.now()),response_text_chars:row.textChars,response_text_kind:row.textKind,audio_chunk_count:row.audioChunks,pcm_chunk_count:row.pcmChunks,...(row.firstPcm===undefined?{}:{first_pcm_ms:row.firstPcm}),pcm_scheduled:row.pcmChunks>0,new_input_before_pcm:row.inputBeforePcm,terminal_current:current,has_tool_call:row.hasTool,response_error_present:object(details?.error)!==undefined,physical_audibility_verified:false,...(row.cancelSource?{cancel_source:row.cancelSource}:{})}
 }
}
