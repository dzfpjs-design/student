import { expect, it } from 'vitest'
import { childTrialReadiness } from './childTrialReadiness'
const data = () => ({ model: { configured: true }, cloud_permissions: { cloud_model_text_allowed: true, cloud_audio_allowed: true } }) as any
it('accepts only current explicit booleans and grants configuration status, without inventing a network-test receipt', () => {
  expect(childTrialReadiness(data(), { configured: true, enabled: true })).toEqual({ allowed: true, missing: [], unconfirmed: false })
  for (const value of [undefined, null, 'true', 1, {}, []]) {
    const input = data(); input.cloud_permissions.cloud_audio_allowed = value
    expect(childTrialReadiness(input, { configured: true, enabled: true })).toMatchObject({ allowed: false, unconfirmed: true })
    expect(childTrialReadiness(data(), { configured: true, enabled: value })).toMatchObject({ allowed: false, unconfirmed: true })
  }
  expect(childTrialReadiness(data(), { configured: false, enabled: false })).toMatchObject({ allowed: false, unconfirmed: false })
})
