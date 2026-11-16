type VoiceCaptureListener = () => void

let activeOwner: string | null = null
const listeners = new Set<VoiceCaptureListener>()

function notify(): void {
  listeners.forEach((listener) => listener())
}

export function claimVoiceCapture(owner: string): boolean {
  if (activeOwner && activeOwner !== owner) return false
  if (activeOwner === owner) return true
  activeOwner = owner
  notify()
  return true
}

export function releaseVoiceCapture(owner: string): void {
  if (activeOwner !== owner) return
  activeOwner = null
  notify()
}

export function getVoiceCaptureOwner(): string | null {
  return activeOwner
}

export function subscribeVoiceCapture(listener: VoiceCaptureListener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function resetVoiceCaptureCoordinatorForTests(): void {
  if (!activeOwner) return
  activeOwner = null
  notify()
}
