import { readFile, stat } from 'node:fs/promises'
import { resolve, sep } from 'node:path'

export type VoiceEvaluationCandidate = {
  id: string
  name: string
  trait: string
  role: 'primary' | 'secondary' | 'offline_fallback'
  allowedUse: string
}

type CandidateFile = { voice: string; name: string; trait: string; sample: string; hypothesis?: string }
type CandidateManifest = { primary_candidates?: CandidateFile[]; secondary_role_candidates?: CandidateFile[]; offline_fallback?: { engine?: string; samples_directory?: string } }

export class VoiceEvaluationCatalog {
  private readonly root: string
  private readonly manifestPath: string
  constructor(root: string | undefined) {
    this.root = root ? resolve(root) : ''
    this.manifestPath = this.root ? resolve(this.root, 'candidates.json') : ''
  }

  async list(): Promise<VoiceEvaluationCandidate[]> {
    const manifest = await this.manifest()
    if (!manifest) return []
    return [
      ...(manifest.primary_candidates ?? []).map((candidate) => ({ id: candidate.voice, name: candidate.name, trait: candidate.trait, role: 'primary' as const, allowedUse: '可作为主陪伴声的盲听候选；仍须完成许可和儿童验收。' })),
      ...(manifest.secondary_role_candidates ?? []).map((candidate) => ({ id: candidate.voice, name: candidate.name, trait: candidate.trait, role: 'secondary' as const, allowedUse: candidate.voice === 'longhuhu_v3' ? '仅故事角色；不得作为成人陪伴者或主声。' : '仅游戏/庆祝等限定场景；不得作为全天主声。' })),
      ...(manifest.offline_fallback?.samples_directory ? ['zh-en-0', 'zh-en-1', 'zh-en-2', 'zh-en-3'].map((id) => ({ id, name: `VITS-Melo ${id}`, trait: '中英本地 CPU 试听', role: 'offline_fallback' as const, allowedUse: '仅断网或云端失败的本地 CPU 兜底试听；模型权重许可与实时率须另验。' })) : [])
    ]
  }

  async audio(candidateId: string): Promise<{ mimeType: 'audio/mpeg' | 'audio/wav'; base64: string }> {
    const manifest = await this.manifest()
    if (!manifest) throw new Error('评测声音包未启用或不可读取。')
    const candidate = [...(manifest.primary_candidates ?? []), ...(manifest.secondary_role_candidates ?? [])].find((item) => item.voice === candidateId)
    const relative = candidate?.sample ?? (candidateId.match(/^zh-en-[0-3]$/) ? `${manifest.offline_fallback?.samples_directory}/${candidateId}.wav` : null)
    if (!relative) throw new Error('评测声音候选不存在。')
    const path = this.safePath(relative)
    const details = await stat(path)
    if (!details.isFile() || details.size < 1 || details.size > 12_000_000) throw new Error('评测声音文件大小无效。')
    const mimeType = path.endsWith('.mp3') ? 'audio/mpeg' : path.endsWith('.wav') ? 'audio/wav' : null
    if (!mimeType) throw new Error('评测声音格式不受支持。')
    return { mimeType, base64: (await readFile(path)).toString('base64') }
  }

  private async manifest(): Promise<CandidateManifest | null> {
    if (!this.root) return null
    try { return JSON.parse(await readFile(this.manifestPath, 'utf8')) as CandidateManifest } catch { return null }
  }

  private safePath(relative: string): string {
    if (!relative || relative.includes('\0')) throw new Error('评测声音路径无效。')
    const path = resolve(this.root, relative)
    if (!path.startsWith(`${this.root}${sep}`)) throw new Error('评测声音路径超出只读包。')
    return path
  }
}
