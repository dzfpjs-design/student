import {expect,it} from 'vitest'
import {sanitizeDiagnosticValue} from './diagnostics'
import {DIAGNOSTIC_ROUTE_TEMPLATES} from './diagnostic-route-templates'
it('keeps native client operation metadata while excluding dispatch secrets, original speech and private outcomes',()=>{
 const privateValue='SYNTHETIC_PRIVATE_CHILD_RECORD_DO_NOT_MAIL'
 const safe=sanitizeDiagnosticValue({native_operation:'client',native_phase:'complete',native_capability:'device.connect',status:'completed',durationMs:24,
  completion_lease:privateValue,client_dispatch:{turn:{transcript:privateValue}},
  result:{completion_lease:privateValue,host_turn:{transcript:privateValue},host_execution:{result:{question:privateValue}}}})
 expect(safe).toMatchObject({native_operation:'client',native_phase:'complete',native_capability:'device.connect',status:'completed',durationMs:24})
 expect(JSON.stringify(safe)).not.toContain(privateValue)
 expect(DIAGNOSTIC_ROUTE_TEMPLATES).toContain('/v1/sessions/{session_id}/voice/realtime/native-client')
})

it('drops invented capability and phase labels instead of mailing attacker-supplied text',()=>{expect(sanitizeDiagnosticValue({native_capability:'PRIVATE_CHILD_TEXT',native_phase:'PRIVATE_TOKEN'})).toEqual({})})
