export type MistakeWorkspaceVoiceCommand = 'capture' | 'paper' | 'voice' | 'cancel' | 'topics' | 'topic_explain'

/** Commands in the learning workspace that act on the local study surface. */
export function mistakeWorkspaceVoiceCommand(text: string, wakeWord?: string): MistakeWorkspaceVoiceCommand | null {
  let command = text.normalize('NFKC').replace(/[\s\p{P}]/gu, '')
  if (wakeWord && command.startsWith(wakeWord)) command = command.slice(wakeWord.length)
  command = command.replace(/^(?:请)?(?:帮我|给我)?/, '')
  if (/^(?:(?:打开|开始)错题录入|(?:录入|收录|记下|整理)(?:一下)?(?:这道|这题|一道|新的?)?错题|把这(?:道)?题(?:加|记|放)(?:到|进|入)错题本)$/.test(command)) return 'capture'
  if (/^(?:我)?(?:做完了|(?:检查|看看|拍一下)(?:我(?:写|重做)的|这道)?(?:草稿|纸上的答案|纸上作答)|(?:我在)?(?:纸上|草稿纸上)(?:重做|做完了))$/.test(command)) return 'paper'
  if (/^(?:我)?(?:用(?:语音|口述)(?:来)?(?:作答|讲解|回答|说一遍)|(?:说|讲)(?:一遍|一下)(?:这(?:道)?题|我的思路)(?:怎么做)?|口述作答)$/.test(command)) return 'voice'
  if (/^(?:取消(?:这次|本次)?(?:作答|复习|口述)?|退出口述|先不说了)$/.test(command)) return 'cancel'
  if (/^(?:打开|查看|看看|看一下)(?:一下)?(?:高频错题(?:题型)?|高频题型|错题专题)$/.test(command)) return 'topics'
  if (/^(?:(?:开始|播放)(?:错题)?专题讲解|(?:讲讲|讲解|讲一下)(?:这组错题|这组题|这个专题|(?:错题|高频)专题|高频错题))$/.test(command)) return 'topic_explain'
  return null
}

export type MistakeVoiceInput = { transcript: string; voiceInputId: string; capturedAt: number; onSafety?:()=>void }
export const MISTAKE_WORKSPACE_LISTEN = 'hsc-mistake-workspace-listen'
