// @vitest-environment node
import{expect,it}from'vitest'
import{sanitizeDiagnosticValue}from'./diagnostics'
import reasons from'../test-fixtures/native-fixed-failure-reasons.json'
it('retains exact reason codes without retaining messages or identifiers',()=>{for(const code of new Set(Object.values(reasons)))expect(sanitizeDiagnosticValue({reason_code:code,message:'PRIVATE_SENTINEL',transcript:'PRIVATE_SENTINEL',session_id:'PRIVATE_SENTINEL'})).toEqual({reason_code:code})})
it('does not accept an invented native code as a trusted diagnostic reason',()=>{expect(sanitizeDiagnosticValue({reason_code:'QWEN_NATIVE_INVENTED_PRIVATE',message:'PRIVATE_SENTINEL'})).toEqual({})})
