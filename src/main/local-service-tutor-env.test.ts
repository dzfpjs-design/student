import { describe, expect, it } from 'vitest'
import { buildLocalServiceChildEnvironment, type LocalServiceChildEnvironmentValues } from './local-service'
const base: LocalServiceChildEnvironmentValues={sessionToken:'synthetic-session',dataEncryptionKey:'synthetic-data-key',dataDir:'/test',databasePath:'/test/db',localModelDir:'/test/models',modelConfig:null,voiceConfig:null}
describe('dedicated tutor runtime configuration boundary',()=>{
 it('strips inherited tutor settings and does not forward an unmigrated v1 key',()=>{
  const env=buildLocalServiceChildEnvironment({HSC_TUTOR_EXPLANATION_API_KEY:'synthetic-e74218d83f772d8f0f85',HSC_TUTOR_EXPLANATION_BASE_URL:'https://attacker.example.test/v1'}, {...base,tutorExplanationConfig:{apiKey:'synthetic-9a62bdc43deccaddc5cd'}})
  expect(env.HSC_TUTOR_EXPLANATION_API_KEY).toBeUndefined();expect(env.HSC_TUTOR_EXPLANATION_BASE_URL).toBeUndefined();expect(env.HSC_TUTOR_EXPLANATION_MODEL).toBeUndefined()
 })
 it('passes only a complete locally validated route, retaining a v1 key migrated in the same config',()=>{
  const env=buildLocalServiceChildEnvironment({HSC_TUTOR_EXPLANATION_BASE_URL:'https://attacker.example.test/v1'}, {...base,tutorExplanationConfig:{apiKey:'synthetic-valid-key',baseUrl:'https://tutor.example.test:443/v1/',model:'deepseek-v4.1-flash'}})
  expect(env.HSC_TUTOR_EXPLANATION_API_KEY).toBe('synthetic-valid-key');expect(env.HSC_TUTOR_EXPLANATION_BASE_URL).toBe('https://tutor.example.test/v1');expect(env.HSC_TUTOR_EXPLANATION_MODEL).toBe('deepseek-v4.1-flash')
 })
 it('does not forward malformed key material',()=>{
  const env=buildLocalServiceChildEnvironment({}, {...base,tutorExplanationConfig:{apiKey:'bad key\n',baseUrl:'https://tutor.example.test/v1',model:'deepseek-v4.1-flash'}})
  expect(env.HSC_TUTOR_EXPLANATION_API_KEY).toBeUndefined();expect(env.HSC_TUTOR_EXPLANATION_BASE_URL).toBeUndefined()
 })
 it('does not forward a key to an invalid or local endpoint',()=>{
  const env=buildLocalServiceChildEnvironment({}, {...base,tutorExplanationConfig:{apiKey:'synthetic-valid-key',baseUrl:'http://127.0.0.1/v1',model:'deepseek-v4.1-flash'}})
  expect(env.HSC_TUTOR_EXPLANATION_API_KEY).toBeUndefined();expect(env.HSC_TUTOR_EXPLANATION_BASE_URL).toBeUndefined()
 })
})
