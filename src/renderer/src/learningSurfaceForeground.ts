const learningEvents=['hsc-study-learning-voice-action','hsc-study-tutor-recovery','hsc-mistake-capture-voice-action','hsc-mistake-voice-action','hsc-confirm-question-capture','hsc-retry-question-capture','hsc-cancel-question-capture','hsc-request-question-capture','hsc-diagnostic-replay-read','hsc-reflection-voice-action','hsc-native-writing-control','hsc-document-review-revision'] as const
/** A hidden saved study component keeps its draft, but cannot claim a new
 * window voice command. Device/privacy controls deliberately remain separate. */
export function pauseHiddenLearningReceivers():()=>void{
 const hold=(event:Event):void=>{event.preventDefault();event.stopImmediatePropagation();const detail=(event as CustomEvent<{resolve?:(handled:boolean)=>void}>).detail;if(typeof detail?.resolve==='function')detail.resolve(false)}
 for(const type of learningEvents)window.addEventListener(type,hold,{capture:true})
 return()=>{for(const type of learningEvents)window.removeEventListener(type,hold,{capture:true})}
}
export function nativeCapabilityNeedsLearningSurface(capability:string,answer:boolean):boolean{
 if(answer)return false
 return /^(?:learning|planning|task|diagnostic|exam|oral|writing|native_writing|pet_vocabulary|pet|reflection)\./.test(capability)
}
