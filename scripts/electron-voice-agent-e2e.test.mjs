import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { createNonSpeechBoundaryWav, loadApprovedSemanticFixtures } from './electron-voice-agent-e2e.mjs'

test('creates an in-memory non-speech PCM WAV boundary fixture', () => {
  const wav = createNonSpeechBoundaryWav()
  assert.equal(wav.subarray(0, 4).toString('ascii'), 'RIFF')
  assert.equal(wav.subarray(8, 12).toString('ascii'), 'WAVE')
  assert.equal(wav.readUInt32LE(24), 16_000)
  assert.equal(wav.readUInt16LE(22), 1)
})

test('requires an adult-or-synthetic manifest and verifies every fixture hash', async () => {
  const fixtureDir = await mkdtemp(join(tmpdir(), 'hsc-approved-asr-fixtures-'))
  try {
    const wav = createNonSpeechBoundaryWav()
    const digest = createHash('sha256').update(wav).digest('hex')
    const fixtures = Object.fromEntries(['start', 'pause', 'resume', 'next', 'privacy'].map((name) => [name, { file: `${name}.wav`, sha256: digest }]))
    await Promise.all(Object.keys(fixtures).map((name) => writeFile(join(fixtureDir, `${name}.wav`), wav)))
    await writeFile(join(fixtureDir, 'fixture-manifest.json'), JSON.stringify({ version: 1, license: 'CC0 test fixture', speaker: 'adult_or_synthetic_non_child', fixtures }))
    const loaded = await loadApprovedSemanticFixtures(fixtureDir)
    assert.equal(loaded.metadata.license, 'CC0 test fixture')
    assert.equal(Object.keys(loaded.fixtures).length, 5)
    await writeFile(join(fixtureDir, 'pause.wav'), Buffer.from('not the recorded fixture'))
    await assert.rejects(() => loadApprovedSemanticFixtures(fixtureDir), /SHA-256/)
  } finally {
    await rm(fixtureDir, { recursive: true, force: true })
  }
})
