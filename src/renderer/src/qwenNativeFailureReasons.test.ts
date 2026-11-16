// @vitest-environment node
import {expect,it}from'vitest'
import{classifyQwenRealtimeError}from'./QwenRealtimeVoiceInput'
import reasons from '../../test-fixtures/native-fixed-failure-reasons.json'
for(const[message,code]of Object.entries(reasons))it('classifies reviewed exact site '+code+':'+message.slice(0,12),()=>{expect(classifyQwenRealtimeError(message)).toBe(code)})
it('keeps a genuinely unknown failure unknown instead of promoting arbitrary text to a trusted native site',()=>{expect(classifyQwenRealtimeError('PRIVATE_CHILD_CONTENT sk-synthetic-1f05292cfadcac8b92dd')).toBe('QWEN_REALTIME_UNKNOWN');expect(classifyQwenRealtimeError('旧原话只是没有拿到某种身份')).toBe('QWEN_REALTIME_UNKNOWN');expect(classifyQwenRealtimeError('首次语音原话的供应商身份尚未确认，没有重复操作。 PRIVATE_CHILD_CONTENT')).toBe('QWEN_REALTIME_UNKNOWN')})
