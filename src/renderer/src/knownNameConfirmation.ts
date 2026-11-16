import { pinyin } from 'pinyin-pro'

const compact = (value: string): string => value.toLowerCase().replace(/[\s，。！？、,.!?;；:："“”'‘’（）()【】\[\]-]/g, '')

function sameCompletePronunciation(value: string, expected: string): boolean {
  const heard = compact(value), target = compact(expected)
  if (!target || heard.length !== target.length) return false
  if (heard === target) return true
  if (!/^[\u3400-\u9fff]+$/.test(heard) || !/^[\u3400-\u9fff]+$/.test(target)) return false
  const phonemes = (text: string): string => pinyin(text, { toneType: 'none', type: 'array' }).join('|')
  return phonemes(heard) === phonemes(target)
}

/** Resolve only a complete affirmation of an already pending nickname. The
 * original transcript and cloud decision remain their original evidence. */
export function resolvesKnownNameConfirmation(transcript: string, pendingName: string, cloudCandidate: string): boolean {
  if (pendingName !== pendingName.trim() || pendingName.length < 2 || pendingName.length > 12
    || /[\s\r\n]/.test(pendingName) || compact(pendingName) !== pendingName.toLowerCase()
    || cloudCandidate.length < 2 || cloudCandidate.length > 12 || /[\r\n]/.test(cloudCandidate)
    || transcript.length > 80) return false
  return sameCompletePronunciation(cloudCandidate, pendingName)
    && sameCompletePronunciation(transcript, '对就叫' + pendingName)
}
