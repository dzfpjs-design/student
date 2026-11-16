import { expect, it } from 'vitest'
import { RealtimeAudioActivityHealth } from './realtimeAudioActivityHealth'
const sample = (energy: number, duration: number, sourceId = 'owned') => ({ energy, duration, sourceId })
it('uses interval RMS, permits ongoing activity and times out only after thirty seconds without another activity pulse', () => {
  const health = new RealtimeAudioActivityHealth()
  expect(health.observe(sample(0, 0), 0)).toBe(false)
  for (let i = 1; i <= 40; i++) expect(health.observe(sample(i * 0.1, i), i * 1000)).toBe(false)
  for (let i = 41; i < 70; i++) expect(health.observe(sample(4, i), i * 1000)).toBe(false)
  expect(health.observe(sample(4, 70), 70_000)).toBe(true)
})
it.each(['source', 'energy_reset', 'duration_reset', 'clock_backwards', 'event_loop_gap', 'missing', 'invalid'] as const)('does not carry activity across an uncertain measurement boundary: %s', boundary => {
  const health = new RealtimeAudioActivityHealth()
  health.observe(sample(0, 0), 0);health.observe(sample(0.1, 1), 1000)
  const next = boundary === 'source' ? sample(0.1, 2, 'replacement') : boundary === 'energy_reset' ? sample(0, 2)
    : boundary === 'duration_reset' ? sample(0.1, 0) : boundary === 'missing' ? null : boundary === 'invalid' ? sample(NaN, 2) : sample(0.1, 2)
  const at = boundary === 'clock_backwards' ? 500 : boundary === 'event_loop_gap' ? 10_000 : 2000
  expect(health.observe(next, at)).toBe(false)
  const stableEnergy = next && Number.isFinite(next.energy) ? next.energy : 0.1
  health.observe(sample(stableEnergy, 3, next?.sourceId), at + 1000)
  for (let i = 1; i <= 40; i++) expect(health.observe(sample(stableEnergy, 3 + i, next?.sourceId), at + 1000 + i * 1000)).toBe(false)
})
it('does not turn quiet positive energy or a sub-frame sample into confirmed activity', () => {
  const health = new RealtimeAudioActivityHealth();health.observe(sample(0, 0), 0)
  for (let i = 1; i <= 50; i++) expect(health.observe(sample(i * 0.0001, i), i * 1000)).toBe(false)
  health.reset();health.observe(sample(0, 0), 0)
  expect(health.observe(sample(0.1, 0.01), 10)).toBe(false)
  for (let i = 1; i <= 40; i++) expect(health.observe(sample(0.1, i), i * 1000)).toBe(false)
})
