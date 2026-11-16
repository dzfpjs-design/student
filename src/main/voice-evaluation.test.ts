import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { VoiceEvaluationCatalog } from './voice-evaluation'

describe('VoiceEvaluationCatalog', () => {
  let root = ''
  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), 'hsc-voice-eval-'))
    await mkdir(join(root, 'aliyun_cosyvoice_samples')); await mkdir(join(root, 'sherpa_onnx_cpu_samples'))
    await writeFile(join(root, 'aliyun_cosyvoice_samples', 'longyingtao_v3.mp3'), Buffer.from([1, 2, 3]))
    await writeFile(join(root, 'sherpa_onnx_cpu_samples', 'zh-en-0.wav'), Buffer.from([4, 5, 6]))
    await writeFile(join(root, 'candidates.json'), JSON.stringify({
      primary_candidates: [{ voice: 'longyingtao_v3', name: '龙应桃', trait: '温柔淡定女', sample: 'aliyun_cosyvoice_samples/longyingtao_v3.mp3' }],
      secondary_role_candidates: [{ voice: 'longhuhu_v3', name: '龙呼呼', trait: '女童', sample: '../outside.mp3' }],
      offline_fallback: { samples_directory: 'sherpa_onnx_cpu_samples' }
    }))
  })
  afterEach(async () => { await rm(root, { recursive: true, force: true }) })

  it('exposes primary, restricted secondary, and local fallback cohorts without packaging them', async () => {
    const catalog = new VoiceEvaluationCatalog(root)
    const candidates = await catalog.list()
    expect(candidates.find((candidate) => candidate.id === 'longyingtao_v3')?.role).toBe('primary')
    expect(candidates.find((candidate) => candidate.id === 'longhuhu_v3')?.allowedUse).toContain('仅故事角色')
    expect(candidates.filter((candidate) => candidate.role === 'offline_fallback')).toHaveLength(4)
    await expect(catalog.audio('longyingtao_v3')).resolves.toEqual({ mimeType: 'audio/mpeg', base64: Buffer.from([1, 2, 3]).toString('base64') })
    await expect(catalog.audio('longhuhu_v3')).rejects.toThrow('超出只读包')
  })

  it('is absent unless an explicit evaluation root is supplied', async () => {
    expect(await new VoiceEvaluationCatalog(undefined).list()).toEqual([])
  })
})
