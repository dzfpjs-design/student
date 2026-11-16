/// <reference types="node" />
// @vitest-environment node
import {readFileSync} from 'node:fs'
import {fileURLToPath} from 'node:url'
import {transformSync} from 'esbuild'
import {afterEach,expect,it,vi} from 'vitest'
import * as voice from './companionVoice'
import {ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,ordinaryResponseGrant,registerOrdinarySession,revokeOrdinaryResponses} from './ordinaryResponseGrant'

let dispose:(()=>void)|undefined
afterEach(()=>{revokeOrdinaryResponses();dispose?.();vi.restoreAllMocks();vi.unstubAllGlobals()})

// Exercise the real caller expression, including its own recursive fallback,
// without initializing unrelated renderer/device lifecycles.
function callerExpression(path:string):string {
 const source=readFileSync(path,'utf8'),start='  const speakReply = ',end='\n  useDailyReflectionSummary'
 const from=source.indexOf(start),to=source.indexOf(end,from)
 if(from<0||to<0||source.indexOf(start,from+1)>=0)throw Error('ACTUAL_CALLER_NOT_FOUND')
 return transformSync(source.slice(from,to),{loader:'tsx',target:'es2022'}).code

}
async function fallback(result:'unavailable'|'cancelled'|'rendered'|'unknown'|Error) {
 const source=process.env.ORDINARY_SOURCE_PATH??fileURLToPath(new URL('./SessionWorkspace.tsx',import.meta.url))
 dispose=registerOrdinarySession({sessionId:'s',childId:'child',state:'study'})
 vi.spyOn(voice,'getCompanionVoiceState').mockReturnValue('idle')
 vi.stubGlobal('window',{desktop:{openPublicReplySpeech:vi.fn()},setTimeout,clearTimeout})
 const local=vi.fn(),provider=vi.fn(async()=>{if(result instanceof Error)throw result;return result})
 const ports={speechSuppressedRef:{current:false},mountedRef:{current:true},qwenRealtimeActive:true,
 commandGenerationRef:{current:1},ordinarySessionRef:{current:{state:'study'}},workspace:{session:{session_id:'s',child_id:'child'}},
 setVoiceDelivery:vi.fn(),playPublicReply:provider,ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,realtimeSpeechCaptureRef:{current:null},
 lastSpeechRequestedAtRef:{current:0},setVoiceActivationSignal:vi.fn(),privacyStopped:false,tutorialOwnsSpeech:false,
 playLocalSpeech:local,speakLocally:local,ordinaryTutorPlayback:()=>{throw Error('UNEXPECTED_TUTOR')},
 onboardingRef:{current:null},setOnboarding:vi.fn()}
 const code=callerExpression(source)+';return speakReply'
 // Preserve self-recursion name in the actual expression's lexical scope.
 const fn=new Function(...Object.keys(ports),code)(...Object.values(ports))
 fn('公开合成的实际Runtime回复',undefined,{localOnly:true,sourceAgentTurnId:'saved-agent',ordinaryResponseAllowed:true})
 for(let i=0;i<8;i++)await Promise.resolve()
 return{provider,local,end:()=>{const call=local.mock.lastCall!;vi.spyOn(voice,'getCompanionPlaybackLease').mockReturnValue({id:'native-local',generation:1,sessionId:'s',kind:'local_sapi',text:call[0]})
 vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking');call[3]();vi.mocked(voice.getCompanionPlaybackLease).mockReturnValue(null);vi.mocked(voice.getCompanionVoiceState).mockReturnValue('idle');call[4].onPlaybackEnd()}}
}
it('definitive unavailable preserves actual AgentTurn through the local provider skip',async()=>{
 const actual=await fallback('unavailable');expect(actual.provider).toHaveBeenCalledOnce();expect(actual.local).toHaveBeenCalledOnce()
 actual.end();expect(ordinaryResponseGrant('s','child')?.binding).toEqual({version:'ordinary_prompt_response_v1',session_state:'study',source_agent_turn_id:'saved-agent'})
})
it.each(['cancelled','rendered','unknown',new Error('SAFE_UNCONFIRMED')] as const)('non-unavailable %s never creates a local fallback response grant',async(result)=>{
 const actual=await fallback(result);expect(actual.provider).toHaveBeenCalledOnce();expect(actual.local).not.toHaveBeenCalled();expect(ordinaryResponseGrant('s','child')).toBeNull()
})
it('actual workspace final-board factory preserves the controller-provided Agent source alongside Tutor source',()=>{
 const path=process.env.ORDINARY_SOURCE_PATH??fileURLToPath(new URL('./SessionWorkspace.tsx',import.meta.url)),source=readFileSync(path,'utf8')
 const from=source.indexOf('    response: (state,text,current'),to=source.indexOf('\n  }),',from)
 expect(from).toBeGreaterThan(-1);expect(to).toBeGreaterThan(from)
 const expression=source.slice(from+'    response: '.length,to).trim().replace(/,$/,'')
 const code=transformSync('const response='+expression,{loader:'tsx',target:'es2022'}).code
 dispose=registerOrdinarySession({sessionId:'s',childId:'child',state:'study'})
 const ports={ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,workspace:{session:{session_id:'s',child_id:'child'}},ordinarySessionRef:{current:{state:'study'}},
 mountedRef:{current:true},speechSuppressedRef:{current:false},privacyStopPending:()=>false}
 const response=new Function(...Object.keys(ports),code+';return response')(...Object.values(ports))
 const permit=response({turn_id:'saved-tutor'},'真实末段',()=>true,'saved-agent')
 expect(permit.grant.binding).toEqual({version:'ordinary_prompt_response_v1',session_state:'study',source_agent_turn_id:'saved-agent',source_tutor_turn_id:'saved-tutor'})
})
