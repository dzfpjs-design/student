import { useSyncExternalStore } from 'react'
import { AlertTriangle, BellRing, HeartHandshake, Mic, Sparkles } from 'lucide-react'
import { getCompanionVoiceState, subscribeCompanionVoice } from './companionVoice'

export type CompanionAvatarMode = 'listening' | 'thinking' | 'speaking' | 'waiting' | 'encouraging' | 'reminding' | 'error'

const copy: Record<CompanionAvatarMode, string> = {
  listening: '在认真听', thinking: '正在整理', speaking: '正在说话', waiting: '随时等你',
  encouraging: '陪你继续', reminding: '温和提醒', error: '暂时需要你帮忙'
}

export function CompanionAvatar({ mode = 'waiting', compact = false }: { mode?: CompanionAvatarMode; compact?: boolean }): React.JSX.Element {
  const voiceState = useSyncExternalStore(subscribeCompanionVoice, getCompanionVoiceState, getCompanionVoiceState)
  const effective = voiceState === 'speaking' ? 'speaking' : voiceState === 'preparing' ? 'thinking' : voiceState === 'error' ? 'error' : mode
  const Icon = effective === 'listening' ? Mic : effective === 'thinking' ? Sparkles : effective === 'reminding' ? BellRing : effective === 'error' ? AlertTriangle : HeartHandshake
  return <div className={`companion-avatar is-${effective}${compact ? ' is-compact' : ''}`} aria-label={`学习伙伴：${copy[effective]}`}>
    <div className="companion-orbit" aria-hidden="true" />
    <div className="companion-core" aria-hidden="true"><span /><span /><i /></div>
    <div className="companion-state"><Icon size={compact ? 13 : 16} /><span>{copy[effective]}</span></div>
  </div>
}
