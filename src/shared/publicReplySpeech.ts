export type PublicReplySpeechSource = { sessionId: string; childId: string; agentTurnId: string }
export type PublicReplySpeechOpen = { status: 'eligible'; leaseId: string } | { status: 'unavailable' } | { status: 'unconfirmed' }
export type PublicReplyAudio = { leaseId: string; responseId: string; voice: string; model: string; pcmBase64: string;
  sampleRate: 24000; sampleCount: number; frameCount: number; generationCompleted: true; renderedCompleted: false }
export type PublicReplySettlement = 'started' | 'rendered' | 'interrupted' | 'unconfirmed'
