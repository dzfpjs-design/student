// @vitest-environment node
import {expect,it,vi} from 'vitest'
import {signNativeAbsence} from './native-launch-attestation'
vi.mock('electron',()=>({app:{isPackaged:false,getPath:()=>'/synthetic-profile'}}))
import {LocalServiceManager,buildLocalServiceChildEnvironment} from './local-service'
const binding={childId:'child-a',sessionId:'session-a',kind:'video' as const,requestId:'original-turn',intentId:'11111111-1111-4111-8111-111111111111',activityId:'original-activity',status:'stopped'}
const version={nonce:'22222222-2222-4222-8222-222222222222',revision:3}
it('MAC binds exact original scope, activity, nonce and native revision',()=>{
  const valid=signNativeAbsence('a'.repeat(64),binding,'cancelled_before_dispatch',version)
  expect(valid).toMatch(/^[a-f0-9]{64}$/)
  for(const key of ['childId','sessionId','requestId','intentId','activityId'] as const){
    expect(signNativeAbsence('a'.repeat(64),{...binding,[key]:'different'},'cancelled_before_dispatch',version)).not.toBe(valid)
  }
  expect(signNativeAbsence('a'.repeat(64),binding,'owner_exit_before_registration',version)).not.toBe(valid)
  expect(signNativeAbsence('b'.repeat(64),binding,'cancelled_before_dispatch',version)).not.toBe(valid)
  expect(signNativeAbsence('a'.repeat(64),binding,'cancelled_before_dispatch',{...version,revision:4})).not.toBe(valid)
  expect(()=>signNativeAbsence(undefined,binding,'cancelled_before_dispatch',version)).toThrow('UNAVAILABLE')
  expect(()=>signNativeAbsence('a'.repeat(64),binding,'observed_closed',version)).toThrow('INVALID')
})
it('backend environment strips inherited native authority; ordinary runtime config does not expose it',()=>{
  const values={sessionToken:'renderer-token',dataEncryptionKey:'data-key',dataDir:'/tmp/synthetic',databasePath:'/tmp/synthetic/test.db',localModelDir:'/tmp/models',modelConfig:null,voiceConfig:null}
  expect(buildLocalServiceChildEnvironment({HSC_NATIVE_LAUNCH_ATTESTATION_KEY:'inherited'},values)).not.toHaveProperty('HSC_NATIVE_LAUNCH_ATTESTATION_KEY')
  expect(buildLocalServiceChildEnvironment({}, {...values,nativeLaunchAttestationKey:'a'.repeat(64)}).HSC_NATIVE_LAUNCH_ATTESTATION_KEY).toBe('a'.repeat(64))
  const manager=new LocalServiceManager()
  expect(manager.getRuntime()).not.toHaveProperty('nativeLaunchAttestationKey')
  expect(()=>manager.signNativeLaunchAbsence(binding,'cancelled_before_dispatch',version)).toThrow('UNAVAILABLE')
})
