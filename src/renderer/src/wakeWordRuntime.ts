import { pinyin } from 'pinyin-pro'

export type WakeWordMatch = { matched: boolean; command: string }
export type WakeWordSetupDecision =
  | { type: 'candidate'; candidate: string }
  | { type: 'confirm'; candidate: string }
  | { type: 'reject' }
  | { type: 'invalid' }

const rejectedWakeWordFragments = new Set([
  '这个', '那个', '这个名', '这个名字', '你的名字', '名字', '唤醒词', '叫什么', '叫你',
])

function compact(value: string): string {
  return value.toLowerCase().replace(/[\s，。！？、,.!?;；:："“”'‘’（）()【】\[\]-]/g, '')
}

function compactWithIndexes(value: string): { text: string; indexes: number[] } {
  let text = ''
  const indexes: number[] = []
  for (let index = 0; index < value.length; index += 1) {
    const normalized = compact(value[index])
    if (!normalized) continue
    text += normalized
    indexes.push(index)
  }
  return { text, indexes }
}

/** Prevent the setup prompt or a partial conversational phrase from becoming
 * a durable wake word when far-field ASR hears the companion's own speech. */
export function normalizeSpokenWakeWordCandidate(value: string): string | null {
  const candidate = value.trim()
    .replace(/^(以后)?(就)?叫你(叫)?/, '')
    .replace(/^(你的名字|唤醒词|名字)(就)?(叫|是)/, '')
    .replace(/[，。！？、,.!?]/g, '')
    .trim()
  if (candidate.length < 2 || candidate.length > 12 || /\s/.test(candidate)) return null
  if (rejectedWakeWordFragments.has(candidate)) return null
  if (/(没(?:有)?听清|请只说|例如|短名字|两到十二个字|先给我起|直接说)/.test(candidate)) return null
  return candidate
}

export function decideWakeWordSetup(value: string, pendingCandidate: string | null): WakeWordSetupDecision {
  const heard = compact(value)
  if (pendingCandidate) {
    if (/^(确认|对|是的|没错|就叫这个|就这个)$/.test(heard)) {
      return { type: 'confirm', candidate: pendingCandidate }
    }
    if (/^(不对|不是|换一个|重新说|重来|取消)$/.test(heard)) {
      return { type: 'reject' }
    }
  }
  const candidate = normalizeSpokenWakeWordCandidate(value)
  return candidate ? { type: 'candidate', candidate } : { type: 'invalid' }
}

export function matchWakeWord(transcript: string, wakeWord: string): WakeWordMatch {
  const target = compact(wakeWord)
  const heard = compactWithIndexes(transcript)
  let matchedAt = heard.text.indexOf(target)
  if (matchedAt < 0 && /^[\u3400-\u9fff]{2,6}$/.test(target)) {
    const targetPinyin = pinyin(target, { toneType: 'none', type: 'array' }).join('|')
    for (let index = 0; index <= heard.text.length - target.length; index += 1) {
      const candidate = heard.text.slice(index, index + target.length)
      if (!/^[\u3400-\u9fff]+$/.test(candidate)) continue
      if (pinyin(candidate, { toneType: 'none', type: 'array' }).join('|') === targetPinyin) {
        matchedAt = index
        break
      }
    }
  }
  if (!target || matchedAt < 0) return { matched: false, command: '' }
  const start = heard.indexes[matchedAt]
  const end = heard.indexes[matchedAt + target.length - 1] + 1
  const withoutWakeWord = `${transcript.slice(0, start)}${transcript.slice(end)}`
    .replace(/^[\s，。！？、,.!?;；:："“”'‘’（）()【】\[\]-]+/, '')
    .trim()
  return { matched: true, command: withoutWakeWord }
}

export function shouldReturnToWakeStandby(conversationActive: boolean, lastActivityAt: number, now: number, inactivitySeconds: number): boolean {
  return conversationActive && lastActivityAt > 0 && now - lastActivityAt >= inactivitySeconds * 1000
}
