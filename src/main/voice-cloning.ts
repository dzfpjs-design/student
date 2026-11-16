import { denyUnpricedMoneyEgress } from './money-egress'
import { createHash } from 'node:crypto'
import { readFile, readdir, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { extname, join } from 'node:path'

export const QWEN_VOICE_CLONE_MODEL = 'qwen3-tts-vc-2026-01-22'
const ENROLLMENT_URL = 'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization'
const SYNTHESIS_URL = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'
const MAX_REFERENCE_BYTES = 10 * 1024 * 1024
const SUPPORTED_MIME = new Map([['.wav', 'audio/wav'], ['.mp3', 'audio/mpeg'], ['.m4a', 'audio/mp4']])

export type ReferenceVoiceSample = {
  id: string
  name: string
  sizeBytes: number
  recommended: boolean
}

type InternalSample = ReferenceVoiceSample & { path: string; mimeType: string }
export type ReferenceVoiceEnrollmentResult = {
  voiceId: string
  model: typeof QWEN_VOICE_CLONE_MODEL
  sampleName: string
  sampleFingerprint: string
  firstFrameMs: number
  totalMs: number
  sampleRate: number
  pcmBase64: string
}

function sampleId(path: string): string {
  return createHash('sha256').update(path).digest('hex').slice(0, 24)
}

async function internalSamples(root = join(homedir(), 'voice_ref')): Promise<InternalSample[]> {
  let entries: string[]
  try { entries = await readdir(root) } catch { return [] }
  const samples: InternalSample[] = []
  for (const name of entries.sort()) {
    const mimeType = SUPPORTED_MIME.get(extname(name).toLowerCase())
    if (!mimeType) continue
    const path = join(root, name)
    const info = await stat(path).catch(() => null)
    if (!info?.isFile() || info.size < 16_000 || info.size > MAX_REFERENCE_BYTES) continue
    samples.push({ id: sampleId(path), name, sizeBytes: info.size, recommended: /reference_15s/i.test(name), path, mimeType })
  }
  return samples.sort((left, right) => Number(right.recommended) - Number(left.recommended) || left.name.localeCompare(right.name, 'zh-CN'))
}

export async function listReferenceVoiceSamples(root?: string): Promise<ReferenceVoiceSample[]> {
  return (await internalSamples(root)).map(({ path: _path, mimeType: _mimeType, ...sample }) => sample)
}

function decodePcmWav(bytes: Buffer): { sampleRate: number; pcm: Buffer } {
  if (bytes.length < 44 || bytes.toString('ascii', 0, 4) !== 'RIFF' || bytes.toString('ascii', 8, 12) !== 'WAVE') throw new Error('音色试听没有返回可播放的 WAV。')
  let offset = 12
  let channels = 0; let sampleRate = 0; let bitsPerSample = 0; let pcm: Buffer | null = null
  while (offset + 8 <= bytes.length) {
    const kind = bytes.toString('ascii', offset, offset + 4)
    const length = bytes.readUInt32LE(offset + 4)
    const start = offset + 8; const end = start + length
    if (end > bytes.length && kind !== 'data') throw new Error(`音色试听 WAV 数据不完整（${kind.trim() || 'unknown'}:${length}/${bytes.length - start}）。`)
    if (kind === 'fmt ' && length >= 16) {
      if (bytes.readUInt16LE(start) !== 1) throw new Error('音色试听不是 PCM 音频。')
      channels = bytes.readUInt16LE(start + 2); sampleRate = bytes.readUInt32LE(start + 4); bitsPerSample = bytes.readUInt16LE(start + 14)
    } else if (kind === 'data') pcm = bytes.subarray(start, Math.min(end, bytes.length))
    offset = Math.min(end + (length % 2), bytes.length)
  }
  if (!pcm || bitsPerSample !== 16 || channels !== 1 || ![16000, 24000, 48000].includes(sampleRate) || pcm.length % 2) throw new Error('音色试听 PCM 参数无效。')
  return { sampleRate, pcm }
}

async function responseJson(response: Response, label: string): Promise<Record<string, any>> {
  const body = await response.json().catch(() => null) as Record<string, any> | null
  if (!response.ok || !body) throw new Error(`${label}失败（HTTP ${response.status}）。`)
  if (body.code || body.message && !body.output) throw new Error(`${label}失败：${String(body.code || 'PROVIDER_ERROR')}。`)
  return body
}

async function findExistingVoice(apiKey: string, preferredName: string, fetchImpl: typeof fetch): Promise<string | null> {
  try {
    const response = await fetchImpl(ENROLLMENT_URL, {
      method: 'POST', headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'qwen-voice-enrollment', input: { action: 'list', page_index: 0, page_size: 100 } }), signal: AbortSignal.timeout(30_000),
    })
    const body = await responseJson(response, '参考音色查询')
    const candidates = [body.output?.voices, body.output?.voice_list, body.output?.items].find(Array.isArray) ?? []
    const match = candidates.find((item: any) => {
      const value = String(item?.voice ?? item?.voice_id ?? '')
      return value.includes(preferredName) && String(item?.target_model ?? QWEN_VOICE_CLONE_MODEL) === QWEN_VOICE_CLONE_MODEL
    })
    const voiceId = String(match?.voice ?? match?.voice_id ?? '')
    return /^[a-zA-Z0-9._:-]{8,160}$/.test(voiceId) ? voiceId : null
  } catch { return null }
}

async function previewVoice(apiKey: string, voiceId: string, fetchImpl: typeof fetch): Promise<{ sampleRate: number; pcm: Buffer; firstFrameMs: number; totalMs: number }> {
  const started = Date.now()
  const response = await fetchImpl(SYNTHESIS_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: QWEN_VOICE_CLONE_MODEL, input: { text: '你好，我会陪你把今天真正要学会的事情，一步一步做好。', voice: voiceId, language_type: 'Chinese' } }),
    signal: AbortSignal.timeout(60_000),
  })
  const body = await responseJson(response, '参考音色试听')
  const audio = body.output?.audio as { data?: string; url?: string } | undefined
  let bytes: Buffer
  if (audio?.data) bytes = Buffer.from(audio.data.replace(/^data:[^;]+;base64,/, ''), 'base64')
  else if (audio?.url) {
    const url = new URL(audio.url)
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname.endsWith('.aliyuncs.com')) throw new Error('参考音色试听返回了不可信的下载地址。')
    url.protocol = 'https:'
    const downloaded = await fetchImpl(url, { signal: AbortSignal.timeout(30_000) })
    if (!downloaded.ok) throw new Error(`参考音色试听下载失败（HTTP ${downloaded.status}）。`)
    bytes = Buffer.from(await downloaded.arrayBuffer())
  } else throw new Error('参考音色试听没有返回音频。')
  const decoded = decodePcmWav(bytes)
  return { ...decoded, firstFrameMs: Date.now() - started, totalMs: Date.now() - started }
}

export async function enrollReferenceVoice(sampleIdValue: string, apiKeyValue: string, rightsConfirmed: boolean, options: { root?: string; fetchImpl?: typeof fetch } = {}): Promise<ReferenceVoiceEnrollmentResult> {
  denyUnpricedMoneyEgress('voice_reference_enrollment_and_preview')
  if (!rightsConfirmed) throw new Error('必须先确认拥有该参考声音的使用授权。')
  const apiKey = apiKeyValue.trim()
  if (apiKey.length < 8) throw new Error('语音服务密钥格式不完整。')
  const sample = (await internalSamples(options.root)).find((item) => item.id === sampleIdValue)
  if (!sample) throw new Error('参考声音样本不存在或不符合大小、格式要求。')
  const source = await readFile(sample.path)
  const fingerprint = createHash('sha256').update(source).digest('hex')
  const fetchImpl = options.fetchImpl ?? fetch
  const preferredName = `hsc${fingerprint.slice(0, 10)}`
  let voiceId = await findExistingVoice(apiKey, preferredName, fetchImpl)
  if (!voiceId) {
    const response = await fetchImpl(ENROLLMENT_URL, {
      method: 'POST',
      headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'qwen-voice-enrollment',
        input: { action: 'create', target_model: QWEN_VOICE_CLONE_MODEL, preferred_name: preferredName, audio: { data: `data:${sample.mimeType};base64,${source.toString('base64')}` }, language: 'zh' },
      }),
      signal: AbortSignal.timeout(60_000),
    })
    const body = await responseJson(response, '参考音色注册')
    voiceId = String(body.output?.voice ?? '')
  }
  if (!/^[a-zA-Z0-9._:-]{8,160}$/.test(voiceId)) throw new Error('参考音色注册没有返回有效音色 ID。')
  const preview = await previewVoice(apiKey, voiceId, fetchImpl)
  return { voiceId, model: QWEN_VOICE_CLONE_MODEL, sampleName: sample.name, sampleFingerprint: fingerprint, firstFrameMs: preview.firstFrameMs, totalMs: preview.totalMs, sampleRate: preview.sampleRate, pcmBase64: preview.pcm.toString('base64') }
}
