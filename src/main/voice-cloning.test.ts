import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { enrollReferenceVoice, listReferenceVoiceSamples, QWEN_VOICE_CLONE_MODEL } from './voice-cloning'

function wav(): Buffer {
  const pcm = Buffer.from([1, 0, 2, 0])
  const output = Buffer.alloc(44 + pcm.length)
  output.write('RIFF', 0); output.writeUInt32LE(36 + pcm.length, 4); output.write('WAVEfmt ', 8); output.writeUInt32LE(16, 16); output.writeUInt16LE(1, 20); output.writeUInt16LE(1, 22); output.writeUInt32LE(24000, 24); output.writeUInt32LE(48000, 28); output.writeUInt16LE(2, 32); output.writeUInt16LE(16, 34); output.write('data', 36); output.writeUInt32LE(pcm.length, 40); pcm.copy(output, 44)
  return output
}

describe('reference voice enrollment', () => {
  let root = ''
  afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }) })

  it('discovers bounded audio files without exposing local paths and prefers the prepared sample', async () => {
    root = await mkdtemp(join(tmpdir(), 'voice-ref-'))
    await writeFile(join(root, 'source.mp3'), Buffer.alloc(20_000, 1))
    await writeFile(join(root, 'companion_female_reference_15s.wav'), Buffer.alloc(30_000, 2))
    const samples = await listReferenceVoiceSamples(root)
    expect(samples[0]).toMatchObject({ name: 'companion_female_reference_15s.wav', recommended: true })
    expect(JSON.stringify(samples)).not.toContain(root)
  })

  it('base64-enrolls the selected sample, verifies a real preview, and never sends a public sample URL', async () => {
    root = await mkdtemp(join(tmpdir(), 'voice-ref-'))
    await writeFile(join(root, 'companion_female_reference_15s.wav'), Buffer.alloc(20_000, 3))
    const [sample] = await listReferenceVoiceSamples(root)
    const calls: Array<{ url: string; body?: any }> = []
    const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input); const body = init?.body ? JSON.parse(String(init.body)) : undefined; calls.push({ url, body })
      if (calls.length === 1) return new Response(JSON.stringify({ output: { voices: [] } }), { status: 200 })
      if (calls.length === 2) return new Response(JSON.stringify({ output: { voice: 'qwen3-tts-vc-hsc-reference-1234' } }), { status: 200 })
      return new Response(JSON.stringify({ output: { audio: { data: wav().toString('base64') } } }), { status: 200 })
    }) as unknown as typeof fetch
    const result = await enrollReferenceVoice(sample.id, 'secret-value', true, { root, fetchImpl })
    expect(result).toMatchObject({ voiceId: 'qwen3-tts-vc-hsc-reference-1234', model: QWEN_VOICE_CLONE_MODEL, sampleRate: 24000 })
    expect(calls[1].body.input.audio.data).toMatch(/^data:audio\/wav;base64,/)
    expect(calls[1].body.input).not.toHaveProperty('url')
    expect(calls[2].body.input.voice).toBe(result.voiceId)
  })

  it('requires an explicit rights assertion before any network request', async () => {
    root = await mkdtemp(join(tmpdir(), 'voice-ref-'))
    await expect(enrollReferenceVoice('missing', 'secret-value', false, { root, fetchImpl: vi.fn() as unknown as typeof fetch })).rejects.toThrow('使用授权')
  })
})
