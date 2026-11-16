import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { decodePetListening } from './petListeningAudio'
import type { PetMedia } from './petMedia'

afterEach(() => vi.unstubAllGlobals())
async function fixture() {
  vi.stubGlobal('crypto', webcrypto)
  const bytes = new ArrayBuffer(44)
  const sha = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(n => n.toString(16).padStart(2, '0')).join('')
  const create = vi.fn()
  vi.stubGlobal('OfflineAudioContext', class {
    constructor(...args: unknown[]) { create(...args) }
    decodeAudioData = vi.fn(async () => ({ numberOfChannels: 1, length: 8000, sampleRate: 16000, getChannelData: () => new Float32Array(8000).fill(.25) }))
  })
  const media = { kind: 'listening', source_reviewed: true, audio: { status: 'usable_signal', sha256: sha, frames: 8000,
    sample_rate: 16000, channels: 1, sample_width: 2 } } as PetMedia
  return { bytes, media, create }
}
it('uses browser decoding at the bound original rate and frame count', async () => {
  const { bytes, media, create } = await fixture()
  const result = await decodePetListening(bytes, media)
  expect(create).toHaveBeenCalledWith(1, 8000, 16000)
  expect(result.sampleRate).toBe(16000)
  expect(new Int16Array(result.pcm)).toHaveLength(8000)
  expect(new Int16Array(result.pcm)[0]).toBe(8192)
})
it.each(['digest', 'unreviewed', 'speaking', 'format'])('rejects %s without decoding unrelated audio', async reason => {
  const { bytes, media, create } = await fixture()
  if (reason === 'digest') new Uint8Array(bytes)[0] = 1
  if (reason === 'unreviewed') media.source_reviewed = false
  if (reason === 'speaking') media.kind = 'speaking'
  if (reason === 'format') media.audio.channels = 2
  await expect(decodePetListening(bytes, media)).rejects.toThrow()
  expect(create).not.toHaveBeenCalled()
})
it('rejects decoder resampling rather than fabricating the original number of frames', async () => {
  const { bytes, media } = await fixture()
  vi.stubGlobal('OfflineAudioContext', class {
    decodeAudioData = vi.fn(async () => ({ numberOfChannels: 1, sampleRate: 48000, length: 24000 }))
  })
  await expect(decodePetListening(bytes, media)).rejects.toThrow('PET_LISTENING_FORMAT_CHANGED')
})
