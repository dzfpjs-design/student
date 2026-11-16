import type { CompanionAvatarMode } from './CompanionAvatar'

/** Stable renderer contract shared by the current local segmented pipeline and
 * the optional Pipecat worker. Audio remains memory-only in either runtime. */
export type RealtimeVoiceRuntimeKind = 'segmented_local' | 'pipecat' | 'qwen_omni_realtime'
export type RealtimeVoicePhase = 'idle' | 'connecting' | 'wake_standby' | 'listening' | 'transcribing' | 'tool_running' | 'speaking' | 'backpressure' | 'recovering' | 'error'

export type RealtimeVoiceStatus = {
  runtime: RealtimeVoiceRuntimeKind
  phase: RealtimeVoicePhase
  /** Conservative local VAD segment activity (including endpoint silence), not
   * verified speaker identity. Suppressed during companion playback; never use
   * alone as permission to interrupt TTS. Missing means unavailable, not idle. */
  speechActive?: boolean
  /** Unverified candidate audio is being buffered. Holds transport handoff
   * only; never a human-speech or playback-interruption permission. */
  capturePending?: boolean
  /** A live PCM frame has arrived from the current recorder. Not inferred from UI phase. */
  captureReady?: boolean
  pendingUtterances: number
  recoverable: boolean
  detail?: string
}

export const idleRealtimeVoiceStatus: RealtimeVoiceStatus = {
  runtime: 'segmented_local',
  phase: 'idle',
  speechActive: false,
  pendingUtterances: 0,
  recoverable: true,
}

export function avatarModeForRealtimeVoice(status: RealtimeVoiceStatus, agentBusy: boolean): CompanionAvatarMode {
  if (status.phase === 'error') return 'error'
  if (status.phase === 'listening') return 'listening'
  if (status.phase === 'speaking') return 'speaking'
  if (status.phase === 'connecting' || status.phase === 'transcribing' || status.phase === 'tool_running'
    || status.phase === 'backpressure' || status.phase === 'recovering' || agentBusy) return 'thinking'
  return 'waiting'
}

export function wakeListenerMessage(status: RealtimeVoiceStatus,
  settings: { wakeWord: string; enabled: boolean; inactivitySeconds: number }, paused: boolean): string {
  if (paused) return '语音已暂停，当前不监听唤醒词'
  if (status.phase === 'error') return '麦克风暂不可用，唤醒监听未启动'
  if (status.phase === 'connecting') return '正在连接麦克风，监听尚未就绪'
  if (status.phase === 'listening') return settings.enabled
    ? `连续对话中，静默${settings.inactivitySeconds}秒后待机` : '正在听你说，唤醒词待机未启用'
  if (status.phase === 'wake_standby' && settings.enabled) return `待机中，说“${settings.wakeWord}”唤醒`
  if (status.phase === 'transcribing' || status.phase === 'backpressure') return '正在处理已收到的语音'
  if (status.phase === 'tool_running') return '正在执行你的请求'
  if (status.phase === 'speaking') return '学习伙伴正在说话'
  if (status.phase === 'recovering') return '实时语音正在重连'
  return settings.enabled ? `唤醒词“${settings.wakeWord}”已保存，监听尚未启动` : '唤醒词待机未启用'
}
