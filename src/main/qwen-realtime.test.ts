import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  available: true,
  encryptString: vi.fn((value: string) => Buffer.from(`protected:${value}`)),
  decryptString: vi.fn((value: Buffer) => value.toString().replace(/^protected:/, '')),
}))

vi.mock('electron', () => ({
  app: { getPath: () => '' },
  safeStorage: {
    isEncryptionAvailable: () => electron.available,
    encryptString: electron.encryptString,
    decryptString: electron.decryptString,
  },
}))

import {
  defaultQwenRealtimeConfig,
  exchangeQwenRealtimeSdp,
  QwenRealtimeSettingsStore,
  summarizeQwenRealtime,
  testQwenRealtimeConfig,
  validateQwenRealtimeConfig,
} from './qwen-realtime'

describe('Qwen realtime settings and signaling', () => {
  let directory = ''
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'hsc-qwen-realtime-')); electron.available = true })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('persists the credential encrypted and returns only a redacted summary', async () => {
    const store = new QwenRealtimeSettingsStore(directory)
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    await store.save(config)
    expect(await readFile(join(directory, 'qwen-realtime-settings.json'), 'utf8')).not.toContain(config.apiKey)
    expect(await new QwenRealtimeSettingsStore(directory).load()).toEqual(config)
    expect(summarizeQwenRealtime(config)).toEqual({
      configured: true, endpoint: 'https://ws-test.cn-beijing.maas.aliyuncs.com', model: 'qwen3.8-omni-flash-realtime', voice: 'Tina', enabled: true,
    })
  })

  it('accepts only the selected realtime model and Bailian endpoint', () => {
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    expect(validateQwenRealtimeConfig(config)).toEqual(config)
    expect(() => validateQwenRealtimeConfig({ ...config, model: 'qwen3.5-omni-flash-realtime' as never })).toThrow('Qwen3.8')
    expect(() => validateQwenRealtimeConfig({ ...config, endpoint: 'https://voice.example.test' })).toThrow('百炼')
  })

  it('proxies SDP without returning or logging the API key', async () => {
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    const offer = `v=0\r\n${'a'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
    const answer = `v=0\r\n${'b'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
    const request = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(init?.headers).toEqual({ Authorization: 'Bearer synthetic-secret-value', 'Content-Type': 'application/sdp' })
      expect(init?.body).toBe(offer)
      return new Response(answer, { status: 200, headers: { 'Content-Type': 'application/sdp' } })
    })
    const result = await exchangeQwenRealtimeSdp(config, offer, request as typeof fetch)
    expect(result).toEqual({ answerSdp: answer, model: config.model, voice: config.voice })
    expect(JSON.stringify(result)).not.toContain(config.apiKey)
  })

  it('rejects malformed offers and provider error bodies', async () => {
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    await expect(exchangeQwenRealtimeSdp(config, 'not-sdp', vi.fn() as never)).rejects.toThrow('Offer SDP')
    const offer = `v=0\r\n${'a'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
    await expect(exchangeQwenRealtimeSdp(config, offer, vi.fn(async () => new Response('{"message":"no"}', { status: 401 })) as typeof fetch)).rejects.toThrow('401')
  })

  it('verifies that the selected workspace exposes the exact realtime model', async () => {
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    const request = vi.fn(async () => new Response(JSON.stringify({ data: [{ id: 'qwen3.8-omni-flash-realtime' }] }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    await expect(testQwenRealtimeConfig(config, request as typeof fetch)).resolves.toMatchObject({ ok: true, model: config.model })
    expect(request).toHaveBeenCalledOnce()
  })

  it('accepts the native Bailian workspace model-list response', async () => {
    const config = defaultQwenRealtimeConfig('synthetic-secret-value', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
    const request = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      output: {
        total: 2,
        models: [
          { model: 'qwen3.8-omni-flash' },
          { model: 'qwen3.8-omni-flash-realtime' },
        ],
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await expect(testQwenRealtimeConfig(config, request as typeof fetch)).resolves.toMatchObject({
      ok: true,
      model: config.model,
    })
  })
})
