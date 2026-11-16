// System voices are model-bound; an accepted task can still fail on a mismatched voice.
export const COSYVOICE_DEFAULT_MODEL = 'cosyvoice-v3-flash'
export const COSYVOICE_ADULT_VOICES = ['longyingtao_v3', 'longanwen_v3', 'longxiaochun_v3', 'longyingmu_v3', 'longanrou_v3'] as const

export function supportsCompanionVoice(model: string, voice: string, region: string): boolean {
  if (!['cn-beijing', 'ap-southeast-1'].includes(region)) return false
  if (model === 'cosyvoice-v3-plus') return voice === 'longanhuan'
  if (model !== COSYVOICE_DEFAULT_MODEL || !COSYVOICE_ADULT_VOICES.some((candidate) => candidate === voice)) return false
  return region === 'cn-beijing' || !['longxiaochun_v3', 'longanrou_v3'].includes(voice)
}
