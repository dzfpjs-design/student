import type { BootstrapState } from './types'

export type ChildTrialReadiness = { allowed: boolean; missing: string[]; unconfirmed: boolean }

/** Configuration truth only. This never claims a cloud connection was tested. */
export function childTrialReadiness(data: Pick<BootstrapState, 'model' | 'cloud_permissions'>,
  realtime: unknown): ChildTrialReadiness {
  const missing: string[] = []
  let unconfirmed = false
  if (data.model?.configured !== true) {
    missing.push(data.model?.configured === false ? '讲题模型尚未配置' : '讲题模型状态尚未核对')
    if (data.model?.configured !== false) unconfirmed = true
  }
  const voice = realtime && typeof realtime === 'object' && !Array.isArray(realtime)
    ? realtime as { configured?: unknown; enabled?: unknown } : null
  if (voice?.configured !== true) {
    missing.push(voice?.configured === false ? '实时语音尚未配置' : '实时语音状态尚未核对')
    if (voice?.configured !== false) unconfirmed = true
  } else if (voice.enabled !== true) {
    missing.push(voice.enabled === false ? '实时语音尚未启用' : '实时语音启用状态尚未核对')
    if (voice.enabled !== false) unconfirmed = true
  }
  for (const [key, label] of [['cloud_model_text_allowed', '云端文本'], ['cloud_audio_allowed', '云端音频']] as const) {
    const allowed = data.cloud_permissions?.[key]
    if (allowed !== true) {
      missing.push(allowed === false ? `${label}尚未获家长授权` : `${label}授权状态尚未核对`)
      if (allowed !== false) unconfirmed = true
    }
  }
  return { allowed: missing.length === 0, missing, unconfirmed }
}
