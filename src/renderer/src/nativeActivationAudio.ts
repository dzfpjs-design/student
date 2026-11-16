import {AdaptiveVoiceActivityDetector} from './voiceActivityDetector'
import {matchWakeWord} from './wakeWordRuntime'
import type {QwenRealtimeInputCapture} from './qwenRealtimeVoice'
export type NativeActivationOwner={sessionId:string;childId:string;root:MediaStreamTrack;word:string;epoch:number}
export type NativeActivationSegment={token:number;owner:NativeActivationOwner;capture:QwenRealtimeInputCapture;rate:number;frames:Float32Array[];samples:number;ended:boolean;quietMs:number;overflow:boolean}
type Options={current:()=>NativeActivationOwner|null;standby:()=>boolean;capture:(owner:NativeActivationOwner,capturedAt:number)=>QwenRealtimeInputCapture;now?:()=>number}
const same=(a:NativeActivationOwner,b:NativeActivationOwner|null)=>!!b&&a.sessionId===b.sessionId&&a.childId===b.childId&&a.root===b.root&&a.word===b.word&&a.epoch===b.epoch
const clean=(text:string)=>text.replace(/[\s，。！？、,.!?;；:："“”'‘’（）()【】\[\]-]/gu,'').toLowerCase()
/** Vocative prefix only. A name occurring inside an ordinary sentence never activates. */
export function registeredWakePrefix(text:string,word:string):{matched:boolean;command:string}{
 let value=text.trim().replace(/^[\s，。！？、,.!?;；:：]+/u,'')
 let matched=false,greetingUsed=false
 for(let repeat=0;repeat<6;repeat++){
  const chars=[...value],count=clean(word).length;let prefix='',end=0
  while(end<chars.length&&clean(prefix).length<count)prefix+=chars[end++]
  const match=matchWakeWord(prefix,word)
  if(!match.matched||clean(match.command)){
   if(!matched&&!greetingUsed&&repeat===0){greetingUsed=true;const greeted=value.replace(/^(?:喂|你好|嗨|嘿)[\s，。！？、,.!?;；:：]*/u,'');if(greeted!==value){value=greeted;repeat--;continue}}
   break
  }
  matched=true;value=chars.slice(end).join('').replace(/^[\s，。！？、,.!?;；:：]+/u,'')
 }
 return {matched,command:matched?value:''}
}
// Exact full remaining clause only; no general-intent classification or substring stop.
const privacyStops=['别听了','不要听了','不要再听了','停止全部监听','停止所有监听','关闭麦克风','关掉麦克风','把麦克风关掉','别再监听了','停止监听','停止录音','关闭录音','暂停摄像头和麦克风','暂停麦克风和摄像头','关闭摄像头和麦克风','关闭麦克风和摄像头','停止摄像头和麦克风','停止麦克风和摄像头','摄像头和麦克风都关掉','麦克风和摄像头都关掉','隐私模式','隐私暂停','stop listening','stop recording','privacy mode','关闭摄像头','暂停摄像头','关闭观察','别看了','不要看了','停止观察'].map(clean)
const privacyPattern=new RegExp('^(?:(?:请你|请|麻烦你|麻烦|帮我|现在|先|马上|立即|我们|咱们)){0,3}(?:'+[...privacyStops,'进入隐私模式'].join('|')+')(?:(?:吧|了|一下|谢谢你|谢谢)){0,3}$','u')
export function isExactStandbyPrivacyStop(text:string):boolean{return privacyPattern.test(clean(text))}

/** Ephemeral contiguous PCM, frozen at a real accepted onset. No transcript is
 * admitted and no old canonical/local-recovery source is upgraded. */
export class NativeActivationAudioBuffer{
 private detector=new AdaptiveVoiceActivityDetector();private segment:NativeActivationSegment|null=null
 private pre:Float32Array[]=[];private preSamples=0;private owner:NativeActivationOwner|null=null
 private rate=0;private lastFrameSamples=0;private epoch=0;private lastAt=0;private disposed=false
 constructor(private readonly options:Options){}
 private zero(frames:Float32Array[]):void{for(const f of frames)f.fill(0)}
 invalidate():void{this.epoch++;this.zero(this.segment?.frames??[]);this.segment=null;this.zero(this.pre);this.pre=[];this.preSamples=0;this.owner=null;this.rate=0;this.lastAt=0;this.detector.reset()}
 processFrame(frame:Float32Array,rate:number):void{
  const owner=this.options.current(),now=(this.options.now??(()=>performance.now()))()
  if(this.disposed||!owner||!this.options.standby()||!frame.length||frame.length>8192||rate<8000||rate>96000||!Number.isFinite(rate)||!frame.every(Number.isFinite)){this.invalidate();return}
  if(!this.owner||!same(this.owner,owner)||this.rate!==rate||this.lastAt&&now-this.lastAt>500)this.invalidate()
  this.owner=owner;this.rate=rate;this.lastAt=now;this.lastFrameSamples=frame.length
  let sum=0;for(const value of frame)sum+=value*value
  const activity=this.detector.observe(Math.sqrt(sum/frame.length),frame.length/rate*1000)
  if((!this.segment||this.segment.ended)&&activity.speechDetected&&activity.voiced){
   const capture=this.options.capture(owner,Math.round(Date.now()-(this.preSamples+frame.length)/rate*1000))
   if(capture.source!=='accepted_onset'||capture.sessionId!==owner.sessionId||capture.reference.child_id!==owner.childId||capture.reference.local_recovery_only===true){this.invalidate();return}
   this.zero(this.segment?.frames??[])
   this.segment={token:++this.epoch,owner:{...owner},capture:structuredClone(capture),rate,frames:this.pre,samples:this.preSamples,ended:false,quietMs:0,overflow:false}
   Object.freeze(this.segment.capture.reference);Object.freeze(this.segment.capture)
   this.pre=[];this.preSamples=0
  }
  const segment=this.segment
  if(segment&&!segment.ended){
   segment.samples+=frame.length
   if(segment.samples>rate*12){this.zero(segment.frames);segment.frames=[];segment.overflow=true}
   if(!segment.overflow)segment.frames.push(frame.slice())
   segment.quietMs=activity.voiced?0:segment.quietMs+frame.length/rate*1000
   if(segment.quietMs>=800){segment.ended=true;this.detector.reset()}
   return
  }
  this.pre.push(frame.slice());this.preSamples+=frame.length
  while(this.preSamples>rate*.5){const first=this.pre.shift()!;this.preSamples-=first.length;first.fill(0)}
 }
 token():number|undefined{const s=this.segment;return s&&(s.overflow||s.samples<=Math.floor(s.rate*2.4)+this.lastFrameSamples)?s.token:undefined}
 owns(segment:NativeActivationSegment):boolean{return !this.disposed&&same(segment.owner,this.options.current())&&(this.options.now??(()=>performance.now()))()-this.lastAt<=500}
 take(token:unknown):NativeActivationSegment|null{
  const segment=this.segment
  if(!segment||token!==segment.token||!this.owns(segment))return null
  this.segment=null;this.zero(this.pre);this.pre=[];this.preSamples=0;this.detector.reset();return segment
 }
 dispose():void{this.disposed=true;this.invalidate()}
}
